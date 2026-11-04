#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""摄像头手势实时模仿到 LinkerHand（O6/L6）。

示例:
    python example/camera_mimic.py --hand_type right --hand_joint O6 --can PCAN_USBBUS1
    python example/camera_mimic.py --dry-run      # 不连接灵巧手，只预览识别效果
    python example/camera_mimic.py --calibrate    # 整手标定：两个姿态分别对应全 255 和全 0

窗口快捷键: r 录制开关, p 回放最近一次, g 保存当前静态手势, 空格 暂停, q/ESC 退出（回到张开位）。
标定模式下用 a 记录「完全张开」、b 记录「攥拳并把拇指压在中指第一指节」。
标定/录制/静态手势分别存在 resource/calibration.json、resource/recordings/、resource/gestures/。
"""
import argparse
import glob
import json
import math
import os
import sys
import time
import urllib.request

import cv2
import mediapipe as mp
import numpy as np
from mediapipe.tasks import python as mp_python
from mediapipe.tasks.python import vision

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.append(ROOT)

MODEL_PATH = os.path.join(ROOT, "resource", "models", "hand_landmarker.task")
MODEL_URL = (
    "https://storage.googleapis.com/mediapipe-models/hand_landmarker/"
    "hand_landmarker/float16/1/hand_landmarker.task"
)
WINDOW_NAME = "LinkerHand camera mimic"
RECORDINGS_DIR = os.path.join(ROOT, "resource", "recordings")
GESTURES_DIR = os.path.join(ROOT, "resource", "gestures")
CALIBRATION_PATH = os.path.join(ROOT, "resource", "calibration.json")

SUPPORTED_JOINTS = ("O6", "L6")
POSE_OPEN, POSE_CLOSED = 255, 0
OPEN_POSE = [POSE_OPEN] * 6
# 出厂默认量程: 2026-11-04 用真手标定的结果（完全张开 / 攥拳并把拇指压在中指第一指节），
# 即每个关节的 (张开端读数, 握拳端读数)。再标定会写进 calibration.json 覆盖这组值。
DEFAULT_CALIBRATION = {
    "thumb_pitch": (21.39, 51.07),
    "thumb_yaw": (34.25, 14.79),
    "index": (14.24, 199.27),
    "middle": (13.81, 258.17),
    "ring": (10.77, 241.64),
    "pinky": (15.93, 227.59),
}
CALIB_KEYS = tuple(DEFAULT_CALIBRATION)
SMOOTH_ALPHA = 0.5
DEADBAND = 3
SEND_SPEED = 250

FINGER_LANDMARKS = {
    "index": (5, 6, 7, 8),
    "middle": (9, 10, 11, 12),
    "ring": (13, 14, 15, 16),
    "pinky": (17, 18, 19, 20),
}
HAND_CONNECTIONS = [
    (0, 1), (1, 2), (2, 3), (3, 4),
    (0, 5), (5, 6), (6, 7), (7, 8),
    (5, 9), (9, 10), (10, 11), (11, 12),
    (9, 13), (13, 14), (14, 15), (15, 16),
    (13, 17), (17, 18), (18, 19), (19, 20),
    (0, 17),
]


def _imread_unicode(path):
    # cv2.imread 打不开含中文的路径，先按字节读再解码
    return cv2.imdecode(np.fromfile(path, dtype=np.uint8), cv2.IMREAD_COLOR)


def check_images(directory):
    """对目录内的手势照片跑一遍映射，用于无摄像头、无硬件时回归验证。"""
    ensure_model()
    paths = sorted(
        glob.glob(os.path.join(directory, "*.jpg"))
        + glob.glob(os.path.join(directory, "*.jpeg"))
        + glob.glob(os.path.join(directory, "*.png"))
    )
    if not paths:
        print(f"{directory} 下没有找到 jpg/jpeg/png 图片")
        sys.exit(1)

    with open(MODEL_PATH, "rb") as model_file:
        model_bytes = model_file.read()
    landmarker = vision.HandLandmarker.create_from_options(vision.HandLandmarkerOptions(
        base_options=mp_python.BaseOptions(model_asset_buffer=model_bytes),
        running_mode=vision.RunningMode.IMAGE,
        num_hands=1,
    ))
    try:
        for path in paths:
            bgr = _imread_unicode(path)
            if bgr is None:
                print(f"{os.path.basename(path)}: 读取失败")
                continue
            rgb = cv2.cvtColor(bgr, cv2.COLOR_BGR2RGB)
            result = landmarker.detect(mp.Image(image_format=mp.ImageFormat.SRGB, data=rgb))
            if not result.hand_landmarks:
                print(f"{os.path.basename(path)}: 未检测到手")
                continue
            print(f"{os.path.basename(path)}: pose="
                  f"{landmarks_to_pose(result.hand_landmarks[0], default_calibration())}")
    finally:
        landmarker.close()


def ensure_model():
    if os.path.exists(MODEL_PATH):
        return
    os.makedirs(os.path.dirname(MODEL_PATH), exist_ok=True)
    print("首次运行，正在下载手部关键点模型（约 8MB）...")
    try:
        urllib.request.urlretrieve(MODEL_URL, MODEL_PATH)
    except Exception as exc:
        print(f"模型下载失败: {exc}")
        print(f"请手动下载 {MODEL_URL} 并保存到 {MODEL_PATH}")
        sys.exit(1)


def _xyz(landmark):
    return landmark.x, landmark.y, landmark.z


def _sub(a, b):
    return a[0] - b[0], a[1] - b[1], a[2] - b[2]


def _dot(a, b):
    return a[0] * b[0] + a[1] * b[1] + a[2] * b[2]


def _cross(a, b):
    return (a[1] * b[2] - a[2] * b[1],
            a[2] * b[0] - a[0] * b[2],
            a[0] * b[1] - a[1] * b[0])


def _unit(v):
    length = _dot(v, v) ** 0.5 or 1e-9
    return v[0] / length, v[1] / length, v[2] / length


def _clamp(value):
    return max(0.0, min(1.0, value))


def _bend_angle(a, b, c):
    ba = _sub(a, b)
    bc = _sub(c, b)
    cosine = _dot(ba, bc) / ((_dot(ba, ba) * _dot(bc, bc)) ** 0.5 or 1e-9)
    return math.degrees(math.acos(max(-1.0, min(1.0, cosine))))


def _palm_frame(landmarks):
    """由手腕、食指根、中指根、小指根建立掌平面坐标系 (法向/长轴/横轴)。"""
    wrist, index_mcp, middle_mcp, pinky_mcp = (
        _xyz(landmarks[i]) for i in (0, 5, 9, 17))
    normal = _unit(_cross(_sub(index_mcp, wrist), _sub(pinky_mcp, wrist)))
    long_axis = _sub(middle_mcp, wrist)
    long_axis = _unit(_sub(long_axis, tuple(_dot(long_axis, normal) * c for c in normal)))
    side_axis = _unit(_cross(normal, long_axis))
    return normal, long_axis, side_axis


def _flexion(landmarks, a, b, c):
    """b 关节的屈曲角: 伸直 0, 向内弯 90。"""
    return 180.0 - _bend_angle(
        _xyz(landmarks[a]), _xyz(landmarks[b]), _xyz(landmarks[c]))


def curl_from_angle(value, open_value, closed_value):
    """张开端读数映射到 0，握拳端读数映射到 1；两端谁大谁小都行。"""
    span = open_value - closed_value
    if abs(span) < 1e-6:
        return 0.0
    return _clamp((open_value - value) / span)


def finger_flexion(landmarks, chain):
    """MCP/PIP/DIP 三个屈曲角之和: 伸直≈0, 完全握拳≈240。"""
    mcp, pip, dip, tip = chain
    return (_flexion(landmarks, 0, mcp, pip)
            + _flexion(landmarks, mcp, pip, dip)
            + _flexion(landmarks, pip, dip, tip))


def thumb_angles(landmarks):
    """拇指两关节的原始量: (MCP+IP 屈曲角之和, 近节在掌平面内的摆角)。

    弯曲用拇指自己两个关节的屈曲角之和：单看 IP 角人手最多只降 50~60°，
    映射不到关节的弯曲端点。摆角只看近节(1→2)，不受指尖弯曲影响。
    两者都只依赖拇指和手掌关键点，不会跟着其余四指动。
    """
    _, long_axis, side_axis = _palm_frame(landmarks)
    proximal = _sub(_xyz(landmarks[2]), _xyz(landmarks[1]))
    yaw_angle = abs(math.degrees(math.atan2(
        _dot(proximal, side_axis), _dot(proximal, long_axis))))
    pitch_flexion = _flexion(landmarks, 1, 2, 3) + _flexion(landmarks, 2, 3, 4)
    return pitch_flexion, yaw_angle


def measure_joints(landmarks):
    """六个关节各自的原始读数，标定和实时映射都用它。"""
    pitch_flexion, yaw_angle = thumb_angles(landmarks)
    meas = {"thumb_pitch": pitch_flexion, "thumb_yaw": yaw_angle}
    for name in ("index", "middle", "ring", "pinky"):
        meas[name] = finger_flexion(landmarks, FINGER_LANDMARKS[name])
    return meas


def default_calibration():
    return {key: tuple(value) for key, value in DEFAULT_CALIBRATION.items()}


def curls_from_measure(meas, calibration):
    return [curl_from_angle(meas[key], *calibration[key]) for key in CALIB_KEYS]


def curls_to_pose(curls):
    """按 CALIB_KEYS 顺序输出 6 个关节值: 拇指弯曲, 拇指横摆, 食指, 中指, 无名指, 小指。"""
    return [round(POSE_OPEN + (POSE_CLOSED - POSE_OPEN) * curl) for curl in curls]


def landmarks_to_pose(landmarks, calibration):
    return curls_to_pose(curls_from_measure(measure_joints(landmarks), calibration))


def smooth(prev, target, alpha):
    return [p + (t - p) * alpha for p, t in zip(prev, target)]


def open_camera(index):
    if sys.platform == "win32":
        cap = cv2.VideoCapture(index, cv2.CAP_DSHOW)
        if cap.isOpened():
            return cap
        cap.release()
    return cv2.VideoCapture(index)


def draw_landmarks(frame, landmarks):
    h, w = frame.shape[:2]
    points = [(int(lm.x * w), int(lm.y * h)) for lm in landmarks]
    for a, b in HAND_CONNECTIONS:
        cv2.line(frame, points[a], points[b], (0, 255, 0), 2)
    for point in points:
        cv2.circle(frame, point, 3, (0, 0, 255), -1)


def draw_hud(frame, status, pose, fps, meas=None, calibrating=False, source=""):
    lines = [f"{status}  FPS {fps:.0f}"]
    if source:
        lines.append(source)
    if meas:
        lines.append("meas " + "  ".join(f"{k.split('_')[-1]}:{meas[k]:.0f}" for k in CALIB_KEYS))
    lines.append("thumb flex/yaw, index, middle, ring, pinky:")
    if pose:
        lines.append("  ".join(f"{value:3d}" for value in pose))
    for i, text in enumerate(lines):
        cv2.putText(frame, text, (10, 25 + i * 22),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.6, (0, 255, 255), 1, cv2.LINE_AA)
    hint = ("a: record open   b: record fist" if calibrating
            else "r: rec   p: play   g: snapshot   space: pause   q: quit")
    cv2.putText(frame, hint, (10, frame.shape[0] - 12),
                cv2.FONT_HERSHEY_SIMPLEX, 0.5, (200, 200, 200), 1, cv2.LINE_AA)


def release_hand(hand, start_pose, steps=12, delay=0.05):
    move_to(hand, start_pose, OPEN_POSE, steps, delay)


def move_to(hand, start_pose, target_pose, steps=12, delay=0.05):
    start = start_pose or target_pose
    for i in range(1, steps + 1):
        t = i / steps
        pose = [int(round(s + (g - s) * t)) for s, g in zip(start, target_pose)]
        hand.finger_move(pose=pose)
        time.sleep(delay)


def current_pose(hand, fallback):
    if hand is None:
        return fallback
    try:
        state = [int(v) for v in hand.get_state()]
    except Exception:
        return fallback
    return state if len(state) == len(fallback) else fallback


def _stamp_name(prefix):
    return f"{prefix}_{time.strftime('%Y%m%d_%H%M%S')}"


def _imwrite_unicode(path, image):
    # cv2.imwrite 打不开含中文的路径，先编码再按字节写
    ok, buffer = cv2.imencode(os.path.splitext(path)[1] or ".jpg", image)
    if ok:
        buffer.tofile(path)
    return bool(ok)


def _check_joint(data, hand_joint, path):
    if data.get("hand_joint") != hand_joint:
        raise ValueError(f"{os.path.basename(path)} 存的是 {data.get('hand_joint')}，当前是 {hand_joint}")
    return data


def save_calibration(path, hand_joint, hand_type, open_meas, closed_meas):
    """把两个标定姿态的读数写盘，之后每次启动自动生效。"""
    data = {"hand_joint": hand_joint, "hand_type": hand_type,
            "created": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "open": {k: round(float(open_meas[k]), 2) for k in CALIB_KEYS},
            "closed": {k: round(float(closed_meas[k]), 2) for k in CALIB_KEYS}}
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    return data


def load_calibration(path, hand_joint, hand_type):
    """返回 {关节: (张开读数, 握拳读数)}；文件缺失或型号/左右手不符返回 None。"""
    if not os.path.isfile(path):
        return None
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    if data.get("hand_joint") != hand_joint or data.get("hand_type") != hand_type:
        print(f"标定文件是 {data.get('hand_joint')}/{data.get('hand_type')}，"
              f"当前 {hand_joint}/{hand_type}，改用默认阈值")
        return None
    try:
        return {k: (float(data["open"][k]), float(data["closed"][k])) for k in CALIB_KEYS}
    except KeyError as exc:
        print(f"标定文件缺少字段 {exc}，改用默认阈值")
        return None


def calibration_summary(open_meas, closed_meas):
    lines = []
    for key in CALIB_KEYS:
        span = abs(open_meas[key] - closed_meas[key])
        flag = "" if span >= 1 else "   <- 这个量没动，检查两个姿态"
        lines.append(f"  {key:11s} 张开 {open_meas[key]:7.1f}   握拳 {closed_meas[key]:7.1f}{flag}")
    return "\n".join(lines)


def save_recording(directory, frames, hand_joint, name=None):
    os.makedirs(directory, exist_ok=True)
    path = os.path.join(directory, f"{name or _stamp_name('rec')}.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump({"hand_joint": hand_joint,
                   "created": time.strftime("%Y-%m-%dT%H:%M:%S"),
                   "frames": frames}, f, ensure_ascii=False)
    return path


def load_recording(path, hand_joint):
    with open(path, encoding="utf-8") as f:
        data = _check_joint(json.load(f), hand_joint, path)
    return data["frames"]


def save_gesture(directory, pose, hand_joint, frame=None, name=None):
    """保存一个静态手势: 姿态 JSON + 同名参考照片。"""
    os.makedirs(directory, exist_ok=True)
    stem = name or _stamp_name("g")
    path = os.path.join(directory, f"{stem}.json")
    data = {"hand_joint": hand_joint,
            "created": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "pose": [int(v) for v in pose]}
    if frame is not None and _imwrite_unicode(os.path.join(directory, f"{stem}.jpg"), frame):
        data["photo"] = f"{stem}.jpg"
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    return path


def load_gesture(path, hand_joint):
    with open(path, encoding="utf-8") as f:
        data = _check_joint(json.load(f), hand_joint, path)
    return [int(v) for v in data["pose"]]


def list_names(directory):
    if not os.path.isdir(directory):
        return []
    return sorted(os.path.splitext(os.path.basename(p))[0]
                  for p in glob.glob(os.path.join(directory, "*.json")))


def resolve_file(directory, name, hand_joint):
    path = name if os.path.isfile(name) else os.path.join(directory, f"{name}.json")
    if not os.path.isfile(path):
        print(f"找不到 {path}\n可用: {', '.join(list_names(directory)) or '（空）'}")
        sys.exit(1)
    return path


class Player:
    """按录制时的时间戳逐帧推进，主循环每帧调用一次 next_pose()。"""

    def __init__(self, frames, speed=1.0, loop=False):
        self.frames = frames
        self.speed = max(0.1, float(speed))
        self.loop = loop
        self.start = time.monotonic()
        self.index = 0
        self.done = not frames

    def next_pose(self):
        """返回这一拍该下发的姿态；没有新帧就返回 None，播完置 done。"""
        if self.done:
            return None
        elapsed = (time.monotonic() - self.start) * 1000 * self.speed
        pose = None
        while self.index < len(self.frames) and self.frames[self.index][0] <= elapsed:
            pose = self.frames[self.index][1]
            self.index += 1
        if self.index >= len(self.frames) and elapsed >= self.frames[-1][0]:
            if self.loop:
                self.start = time.monotonic()
                self.index = 0
            else:
                self.done = True
        return pose


def parse_args():
    parser = argparse.ArgumentParser(description="摄像头手势实时模仿到 LinkerHand（O6/L6）")
    parser.add_argument("--hand_type", choices=["left", "right"], default="right")
    parser.add_argument("--hand_joint", default="O6", help="灵巧手型号，目前支持 O6/L6")
    parser.add_argument("--can", default="PCAN_USBBUS1",
                        help="Ubuntu 一般为 can0，Windows 一般为 PCAN_USBBUS1")
    parser.add_argument("--camera", type=int, default=0, help="摄像头编号")
    parser.add_argument("--no-mirror", action="store_true", help="不镜像摄像头画面")
    parser.add_argument("--dry-run", action="store_true", help="不连接灵巧手，只预览识别结果")
    parser.add_argument("--calibrate", action="store_true",
                        help="整手标定：做两个姿态（完全张开 / 攥拳压拇指），写入 resource/calibration.json")
    parser.add_argument("--images", metavar="DIR",
                        help="对目录内的手势照片跑映射并打印姿态，不打开摄像头")
    parser.add_argument("--play", metavar="NAME",
                        help="回放录制文件（不开摄像头），给名字或完整路径")
    parser.add_argument("--gesture", metavar="NAME",
                        help="执行一次命名手势（不开摄像头）")
    parser.add_argument("--speed", type=float, default=1.0, help="回放倍速，默认 1.0")
    parser.add_argument("--loop", action="store_true", help="循环回放")
    parser.add_argument("--name", default=None, help="保存录制或手势时用的名字，留空自动按时间命名")
    parser.add_argument("--list", action="store_true", help="列出已保存的录制与手势")
    return parser.parse_args()


def run_offline(args, hand):
    """--play / --gesture 的离线路径：不开摄像头，直接让手执行。"""
    try:
        if args.gesture:
            path = resolve_file(GESTURES_DIR, args.gesture, args.hand_joint)
            pose = load_gesture(path, args.hand_joint)
        else:
            path = resolve_file(RECORDINGS_DIR, args.play, args.hand_joint)
            frames = load_recording(path, args.hand_joint)
    except (OSError, ValueError) as exc:
        print(f"读取失败: {exc}")
        sys.exit(1)

    if args.gesture:
        if hand:
            move_to(hand, current_pose(hand, OPEN_POSE), pose)
            print(f"已执行手势 {os.path.basename(path)}: {pose}")
        else:
            print(f"[dry-run] 手势 {os.path.basename(path)}: {pose}")
        return

    loop = args.loop and hand is not None
    if args.loop and hand is None:
        print("[dry-run] 循环已忽略，只打印一遍")
    player = Player(frames, args.speed, loop)
    sent = 0
    while not player.done:
        pose = player.next_pose()
        if pose is None:
            time.sleep(0.005)
            continue
        if hand:
            hand.finger_move(pose=list(pose))
        else:
            print(pose)
        sent += 1
        time.sleep(0.005)
    print(f"回放结束: {os.path.basename(path)}，共 {len(frames)} 帧，下发 {sent} 次")


def stop_recording(recording, args):
    if not recording["frames"]:
        print("录制期间没有检测到手，未保存")
        return
    path = save_recording(RECORDINGS_DIR, recording["frames"], args.hand_joint, args.name)
    print(f"已保存录制 {len(recording['frames'])} 帧: {path}")


def start_playback(args):
    names = list_names(RECORDINGS_DIR)
    if not names:
        print("还没有录制文件（resource/recordings）")
        return None
    path = os.path.join(RECORDINGS_DIR, names[-1] + ".json")
    try:
        frames = load_recording(path, args.hand_joint)
    except ValueError as exc:
        print(exc)
        return None
    print(f"回放 {names[-1]}（{len(frames)} 帧）")
    return Player(frames, args.speed, args.loop)


def finish_calibration(args, open_meas, closed_meas):
    """两个标定姿态都记好了：写文件并打印每个关节的量程。"""
    save_calibration(CALIBRATION_PATH, args.hand_joint, args.hand_type, open_meas, closed_meas)
    print(f"已保存标定: {CALIBRATION_PATH}")
    print(calibration_summary(open_meas, closed_meas))
    print("下次启动自动生效；张开端=255，握拳端=0")


def main():
    args = parse_args()
    args.hand_joint = args.hand_joint.upper()
    if args.images:
        check_images(args.images)
        return
    if args.list:
        print("录制:", ", ".join(list_names(RECORDINGS_DIR)) or "（空）")
        print("手势:", ", ".join(list_names(GESTURES_DIR)) or "（空）")
        return
    if args.hand_joint not in SUPPORTED_JOINTS:
        print(f"当前示例仅支持 {SUPPORTED_JOINTS}，收到 {args.hand_joint}")
        sys.exit(1)
    ensure_model()

    hand = None
    if not (args.dry_run or args.calibrate):
        from LinkerHand.linker_hand_api import LinkerHandApi
        hand = LinkerHandApi(hand_type=args.hand_type, hand_joint=args.hand_joint, can=args.can)
        hand.set_speed(speed=[SEND_SPEED] * 6)

    if args.play or args.gesture:
        run_offline(args, hand)
        return

    cap = open_camera(args.camera)
    if not cap.isOpened():
        print(f"无法打开摄像头 {args.camera}")
        sys.exit(1)
    cap.set(cv2.CAP_PROP_FRAME_WIDTH, 640)
    cap.set(cv2.CAP_PROP_FRAME_HEIGHT, 480)

    with open(MODEL_PATH, "rb") as model_file:
        model_bytes = model_file.read()
    # 传 buffer 而非路径：MediaPipe C++ 层打不开含中文的路径
    options = vision.HandLandmarkerOptions(
        base_options=mp_python.BaseOptions(model_asset_buffer=model_bytes),
        running_mode=vision.RunningMode.VIDEO,
        num_hands=1,
        min_hand_detection_confidence=0.5,
        min_hand_presence_confidence=0.5,
        min_tracking_confidence=0.5,
    )
    landmarker = vision.HandLandmarker.create_from_options(options)

    smoothed = None
    last_sent = None
    last_timestamp = 0
    paused = False
    calib_meas = {"open": None, "fist": None}
    recording = None
    player = None
    fps, fps_start = 0.0, time.monotonic()
    fps_count = 0

    file_calibration = load_calibration(CALIBRATION_PATH, args.hand_joint, args.hand_type)
    calibration = file_calibration or default_calibration()
    source = ("标定: calibration.json" if file_calibration else "标定: 默认常量")
    print(f"当前 {args.hand_type} {args.hand_joint}，{source}")

    if args.calibrate:
        print("整手标定（不连接灵巧手）:")
        print("  1) 五指完全张开、拇指尽量外展，按 a 记录")
        print("  2) 攥拳并把拇指压在中指第一指节上，按 b 记录")
    else:
        print("快捷键: r 开始/结束录制, p 回放最近一次录制, g 保存当前静态手势, 空格 暂停, q 退出")

    try:
        while True:
            now = time.monotonic()
            ok, frame = cap.read()
            if not ok:
                print("摄像头读取失败，退出")
                break
            if not args.no_mirror:
                frame = cv2.flip(frame, 1)

            status = "NO HAND (holding pose)"
            pose = None
            meas = None
            if player:
                played = player.next_pose()
                if played is None:
                    if player.done:
                        player = None
                        status = "PLAYBACK DONE"
                else:
                    pose = [int(v) for v in played]
                    status = "PLAYBACK"
                    if hand:
                        hand.finger_move(pose=pose)
                        last_sent = pose
            else:
                rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
                timestamp = max(int(now * 1000), last_timestamp + 1)
                last_timestamp = timestamp
                result = landmarker.detect_for_video(
                    mp.Image(image_format=mp.ImageFormat.SRGB, data=rgb), timestamp)
                if result.hand_landmarks:
                    landmarks = result.hand_landmarks[0]
                    meas = measure_joints(landmarks)
                    target = curls_to_pose(curls_from_measure(meas, calibration))
                    smoothed = [float(v) for v in target] if smoothed is None \
                        else smooth(smoothed, target, SMOOTH_ALPHA)
                    pose = [int(round(v)) for v in smoothed]
                    if hand and not paused and (
                            last_sent is None
                            or max(abs(a - b) for a, b in zip(pose, last_sent)) >= DEADBAND):
                        hand.finger_move(pose=pose)
                        last_sent = pose
                    status = "PAUSED" if paused else "TRACKING"
                    draw_landmarks(frame, landmarks)

            if recording is not None and pose is not None:
                recording["frames"].append([int((now - recording["start"]) * 1000), pose])

            fps_count += 1
            if now - fps_start >= 1.0:
                fps = fps_count / (now - fps_start)
                fps_start, fps_count = now, 0
            draw_hud(frame, status, pose, fps, meas, args.calibrate, source)
            cv2.imshow(WINDOW_NAME, frame)

            key = cv2.waitKey(1) & 0xFF
            if key in (ord("q"), 27):
                break
            if args.calibrate:
                if key in (ord("a"), ord("b")) and meas:
                    slot = "open" if key == ord("a") else "fist"
                    calib_meas[slot] = dict(meas)
                    print(f"已记录 {('完全张开' if slot == 'open' else '攥拳压拇指')}: "
                          + " ".join(f"{k}={meas[k]:.1f}" for k in CALIB_KEYS))
                    if calib_meas["open"] and calib_meas["fist"]:
                        finish_calibration(args, calib_meas["open"], calib_meas["fist"])
                        break
                if cv2.getWindowProperty(WINDOW_NAME, cv2.WND_PROP_VISIBLE) < 1:
                    break
                continue

            if key == ord(" "):
                paused = not paused
            elif key == ord("r"):
                if recording is None:
                    recording = {"start": now, "frames": []}
                    print("开始录制（再按 r 结束）")
                else:
                    stop_recording(recording, args)
                    recording = None
            elif key == ord("p"):
                player = start_playback(args)
            elif key == ord("g"):
                if pose is None:
                    print("画面里没有手，未保存")
                else:
                    print("已保存手势:", save_gesture(GESTURES_DIR, pose, args.hand_joint,
                                                     frame, args.name))
            if cv2.getWindowProperty(WINDOW_NAME, cv2.WND_PROP_VISIBLE) < 1:
                break
    except KeyboardInterrupt:
        pass
    finally:
        if recording and recording["frames"]:
            stop_recording(recording, args)
        if hand:
            release_hand(hand, last_sent)
            print("已退出，灵巧手回到张开位")
        landmarker.close()
        cap.release()
        cv2.destroyAllWindows()


if __name__ == "__main__":
    main()
