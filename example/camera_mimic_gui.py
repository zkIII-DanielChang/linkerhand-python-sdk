#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""摄像头手势模仿的图形界面（PyQt5），映射逻辑复用 camera_mimic.py。

用法:
    python example/camera_mimic_gui.py

左侧是摄像头画面与手部骨架，右侧依次是实时关节值、灵巧手连接、录制与回放、
静态手势库、运行参数、整手标定和日志。
不点「连接灵巧手」时只做识别预览，不会碰 CAN 设备；录制、拍照和标定同样可用。
"""
import json
import os
import sys
import time

import cv2
import mediapipe as mp
import numpy as np
from mediapipe.tasks import python as mp_python
from mediapipe.tasks.python import vision
from PyQt5 import QtCore, QtGui, QtWidgets

sys.path.append(os.path.dirname(os.path.abspath(__file__)))
import camera_mimic as mimic

JOINT_LABELS = ["拇指弯曲", "拇指横摆", "食指", "中指", "无名指", "小指"]


class MimicWorker(QtCore.QThread):
    frame_ready = QtCore.pyqtSignal(object, list, dict, float, str)
    log = QtCore.pyqtSignal(str)
    calibration_done = QtCore.pyqtSignal()

    def __init__(self, params):
        super().__init__()
        self.params = params
        self.running = True
        self.paused = False
        self.hand = None
        self.connect_spec = None
        self.disconnect_requested = False
        self.release_requested = False
        self.smoothed = None
        self.last_sent = None
        self.last_speed = None
        self.recording = False
        self.record_start = None
        self.recorded_frames = []
        self.player = None
        self.gesture_capture = None
        self.gesture_play = None
        self.calib_capture = None
        self.calib_meas = {}

    def capture_calibration(self, slot, meas):
        """记录一个标定姿态；两个都记完就写文件并立即生效。"""
        self.calib_meas[slot] = dict(meas)
        if not (self.calib_meas.get("open") and self.calib_meas.get("fist")):
            self.log.emit(f"已记录{'张开' if slot == 'open' else '握拳'}姿态")
            return
        open_meas, closed_meas = self.calib_meas.pop("open"), self.calib_meas.pop("fist")
        mimic.save_calibration(mimic.CALIBRATION_PATH, self.params["hand_joint"],
                               self.params["hand_type"], open_meas, closed_meas)
        self.params["calibration"] = mimic.load_calibration(
            mimic.CALIBRATION_PATH, self.params["hand_joint"], self.params["hand_type"])
        self.log.emit("已保存标定 resource/calibration.json\n"
                      + mimic.calibration_summary(open_meas, closed_meas))
        self.calibration_done.emit()

    def start_recording(self):
        self.recorded_frames = []
        self.record_start = time.monotonic()
        self.recording = True

    def snapshot_recording(self):
        """停止录制：取出帧快照并清空，由界面线程负责落盘。"""
        self.recording = False
        frames = list(self.recorded_frames)
        self.recorded_frames = []
        return frames

    def _handle_gesture_requests(self, pose, frame):
        if self.gesture_capture is not None:
            name, self.gesture_capture = self.gesture_capture, None
            if pose is None:
                self.log.emit("画面里没有手，未保存手势")
            else:
                path = mimic.save_gesture(mimic.GESTURES_DIR, pose, self.params["hand_joint"],
                                          frame, name or None)
                self.log.emit(f"已保存手势: {path}")
        if self.gesture_play:
            name, self.gesture_play = self.gesture_play, None
            path = os.path.join(mimic.GESTURES_DIR, f"{name}.json")
            try:
                target = mimic.load_gesture(path, self.params["hand_joint"])
            except (OSError, ValueError) as exc:
                self.log.emit(f"手势读取失败: {exc}")
                return
            if self.hand:
                mimic.move_to(self.hand, mimic.current_pose(self.hand, mimic.OPEN_POSE), target)
                self.last_sent = target
                self.smoothed = [float(v) for v in target]
                self.log.emit(f"已执行手势 {name}: {target}")
            else:
                self.log.emit(f"未连接灵巧手，手势 {name} 只打印: {target}")

    def request_connect(self, spec):
        self.connect_spec = spec

    def _handle_hand_requests(self):
        if self.connect_spec:
            spec, self.connect_spec = self.connect_spec, None
            try:
                from LinkerHand.linker_hand_api import LinkerHandApi
                self.hand = LinkerHandApi(**spec)
                self.last_speed = self.params["speed"]
                self.hand.set_speed(speed=[self.last_speed] * 6)
                self.log.emit(f"已连接 {spec['hand_type']} {spec['hand_joint']} ({spec['can']})")
            except Exception as exc:
                self.hand = None
                self.log.emit(f"连接灵巧手失败: {exc}")
        if self.disconnect_requested:
            self.disconnect_requested = False
            self.hand = None
            self.last_sent = None
            self.log.emit("已断开灵巧手")
        if self.release_requested:
            self.release_requested = False
            if self.hand:
                mimic.release_hand(self.hand, self.last_sent)
                self.log.emit("已平滑回到张开位")
        if self.hand and self.last_speed != self.params["speed"]:
            self.last_speed = self.params["speed"]
            self.hand.set_speed(speed=[self.last_speed] * 6)
            self.log.emit(f"速度设为 {self.last_speed}")

    def _pose_from(self, landmarks):
        meas = mimic.measure_joints(landmarks)
        pose = mimic.curls_to_pose(
            mimic.curls_from_measure(meas, self.params["calibration"]))
        return pose, meas

    def run(self):
        mimic.ensure_model()
        with open(mimic.MODEL_PATH, "rb") as model_file:
            model_bytes = model_file.read()
        landmarker = vision.HandLandmarker.create_from_options(vision.HandLandmarkerOptions(
            base_options=mp_python.BaseOptions(model_asset_buffer=model_bytes),
            running_mode=vision.RunningMode.VIDEO,
            num_hands=1,
        ))
        cap = mimic.open_camera(self.params["camera"])
        if not cap.isOpened():
            self.log.emit(f"无法打开摄像头 {self.params['camera']}")
            landmarker.close()
            return

        last_timestamp = 0
        fps, fps_start, fps_count = 0.0, time.monotonic(), 0
        try:
            while self.running:
                self._handle_hand_requests()
                now = time.monotonic()
                ok, frame = cap.read()
                if not ok:
                    self.log.emit("摄像头读取失败")
                    break
                frame = cv2.flip(frame, 1) if self.params["mirror"] else frame

                pose, raw = None, {}
                status = "未检测到手（保持上一姿态）"
                if self.player:
                    played = self.player.next_pose()
                    if played is None:
                        if self.player.done:
                            self.player = None
                            status = "回放结束"
                            self.log.emit("回放结束")
                    else:
                        pose = [int(v) for v in played]
                        status = "回放中"
                        if self.hand:
                            self.hand.finger_move(pose=pose)
                            self.last_sent = pose
                elif self.paused:
                    status = "已暂停"
                else:
                    rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
                    timestamp = max(int(now * 1000), last_timestamp + 1)
                    last_timestamp = timestamp
                    result = landmarker.detect_for_video(
                        mp.Image(image_format=mp.ImageFormat.SRGB, data=rgb), timestamp)
                    if result.hand_landmarks:
                        landmarks = result.hand_landmarks[0]
                        target, raw = self._pose_from(landmarks)
                        self.smoothed = ([float(v) for v in target] if self.smoothed is None
                                         else mimic.smooth(self.smoothed, target,
                                                           self.params["alpha"]))
                        pose = [int(round(v)) for v in self.smoothed]
                        if self.hand and (self.last_sent is None
                                          or max(abs(a - b) for a, b in zip(pose, self.last_sent))
                                          >= self.params["deadband"]):
                            self.hand.finger_move(pose=pose)
                            self.last_sent = pose
                        status = "已连接灵巧手" if self.hand else "预览中（未连接）"
                        mimic.draw_landmarks(frame, landmarks)
                        if self.calib_capture:
                            slot, self.calib_capture = self.calib_capture, None
                            self.capture_calibration(slot, raw)

                if self.recording and pose is not None:
                    self.recorded_frames.append([int((now - self.record_start) * 1000), pose])
                self._handle_gesture_requests(pose, frame)

                fps_count += 1
                if now - fps_start >= 1.0:
                    fps = fps_count / (now - fps_start)
                    fps_start, fps_count = now, 0

                image = QtGui.QImage(cv2.cvtColor(frame, cv2.COLOR_BGR2RGB).tobytes(),
                                     frame.shape[1], frame.shape[0], 3 * frame.shape[1],
                                     QtGui.QImage.Format_RGB888).copy()
                self.frame_ready.emit(image, pose or [], raw, fps, status)
                time.sleep(0.001)
        finally:
            cap.release()
            landmarker.close()


def make_spin(minimum, maximum, value, decimals=0, step=1.0, suffix=""):
    spin = (QtWidgets.QDoubleSpinBox() if decimals else QtWidgets.QSpinBox())
    if decimals:
        spin.setDecimals(decimals)
        spin.setRange(minimum, maximum)
        spin.setSingleStep(step)
    else:
        spin.setRange(int(minimum), int(maximum))
    if suffix:
        spin.setSuffix(suffix)
    spin.setValue(value)
    return spin


class MainWindow(QtWidgets.QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("LinkerHand 摄像头手势模仿")
        self.params = {
            "camera": 0,
            "mirror": True,
            "hand_joint": "O6",
            "hand_type": "right",
            "rec_name": "",
            "alpha": mimic.SMOOTH_ALPHA,
            "deadband": mimic.DEADBAND,
            "speed": mimic.SEND_SPEED,
            "calibration": mimic.load_calibration(mimic.CALIBRATION_PATH, "O6", "right")
                           or mimic.default_calibration(),
        }
        self.spin = {}
        self.sliders = []
        self.value_labels = []
        self.latest_raw = {}

        self.worker = MimicWorker(self.params)
        self.worker.frame_ready.connect(self.on_frame)
        self.worker.log.connect(self.on_log)
        self.worker.calibration_done.connect(self.update_cal_source)

        self.build_ui()
        self.update_cal_source()
        self.worker.start()

    def build_ui(self):
        central = QtWidgets.QWidget()
        layout = QtWidgets.QHBoxLayout(central)

        self.video = QtWidgets.QLabel("正在打开摄像头...")
        self.video.setMinimumSize(640, 480)
        self.video.setAlignment(QtCore.Qt.AlignCenter)
        self.video.setStyleSheet("background:#202020;color:#cccccc;")
        layout.addWidget(self.video, 3)
        layout.addWidget(self.build_panel(), 2)
        self.setCentralWidget(central)

    def build_panel(self):
        panel = QtWidgets.QScrollArea()
        panel.setWidgetResizable(True)
        body = QtWidgets.QWidget()
        outer = QtWidgets.QVBoxLayout(body)

        self.status_label = QtWidgets.QLabel("启动中")
        self.status_label.setStyleSheet("font-weight:bold;")
        outer.addWidget(self.status_label)

        group = QtWidgets.QGroupBox("实时下发值")
        grid = QtWidgets.QGridLayout(group)
        for i, label in enumerate(JOINT_LABELS):
            grid.addWidget(QtWidgets.QLabel(label), i, 0)
            slider = QtWidgets.QSlider(QtCore.Qt.Horizontal)
            slider.setRange(0, 255)
            slider.setEnabled(False)
            value = QtWidgets.QLabel("--")
            value.setMinimumWidth(34)
            grid.addWidget(slider, i, 1)
            grid.addWidget(value, i, 2)
            self.sliders.append(slider)
            self.value_labels.append(value)
        outer.addWidget(group)

        self.raw_label = QtWidgets.QLabel("原始角度: --")
        self.raw_label.setWordWrap(True)
        outer.addWidget(self.raw_label)

        device = QtWidgets.QGroupBox("灵巧手")
        form = QtWidgets.QFormLayout(device)
        self.hand_type = QtWidgets.QComboBox()
        self.hand_type.addItems(["right", "left"])
        self.hand_joint = QtWidgets.QComboBox()
        self.hand_joint.addItems(list(mimic.SUPPORTED_JOINTS))
        self.can_edit = QtWidgets.QLineEdit("PCAN_USBBUS1")
        form.addRow("型号", self.hand_joint)
        form.addRow("左右手", self.hand_type)
        form.addRow("CAN", self.can_edit)
        buttons = QtWidgets.QHBoxLayout()
        self.connect_button = QtWidgets.QPushButton("连接灵巧手")
        self.disconnect_button = QtWidgets.QPushButton("断开")
        self.release_button = QtWidgets.QPushButton("张开回位")
        self.pause_button = QtWidgets.QPushButton("暂停")
        self.pause_button.setCheckable(True)
        buttons.addWidget(self.connect_button)
        buttons.addWidget(self.disconnect_button)
        buttons.addWidget(self.release_button)
        buttons.addWidget(self.pause_button)
        form.addRow(buttons)
        outer.addWidget(device)

        record = QtWidgets.QGroupBox("录制与回放")
        rl = QtWidgets.QFormLayout(record)
        row = QtWidgets.QHBoxLayout()
        self.rec_files = QtWidgets.QComboBox()
        self.rec_files.setMinimumWidth(150)
        refresh_rec = QtWidgets.QPushButton("刷新")
        row.addWidget(self.rec_files, 1)
        row.addWidget(refresh_rec)
        rl.addRow("录制文件", row)
        self.rec_name = QtWidgets.QLineEdit()
        self.rec_name.setPlaceholderText("留空自动按时间命名")
        rl.addRow("新录制名", self.rec_name)
        row = QtWidgets.QHBoxLayout()
        self.record_button = QtWidgets.QPushButton("开始录制")
        self.play_button = QtWidgets.QPushButton("回放")
        row.addWidget(self.record_button)
        row.addWidget(self.play_button)
        rl.addRow(row)
        row = QtWidgets.QHBoxLayout()
        self.play_speed = make_spin(0.25, 4.0, 1.0, decimals=2, step=0.25)
        self.play_loop = QtWidgets.QCheckBox("循环")
        row.addWidget(QtWidgets.QLabel("倍速"))
        row.addWidget(self.play_speed)
        row.addWidget(self.play_loop)
        rl.addRow("回放设置", row)
        outer.addWidget(record)

        gestures = QtWidgets.QGroupBox("静态手势")
        gl = QtWidgets.QFormLayout(gestures)
        self.gesture_name = QtWidgets.QLineEdit()
        self.gesture_name.setPlaceholderText("留空自动按时间命名")
        gl.addRow("保存名", self.gesture_name)
        self.capture_button = QtWidgets.QPushButton("拍照保存当前手势")
        gl.addRow(self.capture_button)
        row = QtWidgets.QHBoxLayout()
        self.gesture_files = QtWidgets.QComboBox()
        self.gesture_files.setMinimumWidth(150)
        refresh_g = QtWidgets.QPushButton("刷新")
        row.addWidget(self.gesture_files, 1)
        row.addWidget(refresh_g)
        gl.addRow("手势库", row)
        self.apply_button = QtWidgets.QPushButton("执行一次")
        gl.addRow(self.apply_button)
        outer.addWidget(gestures)

        tuning = QtWidgets.QGroupBox("运行参数")
        tform = QtWidgets.QFormLayout(tuning)
        self.add_spin(tform, "摄像头编号", "camera", 0, 5)
        self.add_spin(tform, "电机速度", "speed", 10, 255)
        self.add_spin(tform, "平滑系数", "alpha", 0.1, 1.0, decimals=2, step=0.05)
        self.add_spin(tform, "死区", "deadband", 0, 20)
        self.mirror_check = QtWidgets.QCheckBox("镜像画面")
        self.mirror_check.setChecked(True)
        tform.addRow(self.mirror_check)
        outer.addWidget(tuning)

        calibrate = QtWidgets.QGroupBox("整手标定")
        clayout = QtWidgets.QVBoxLayout(calibrate)
        clayout.addWidget(QtWidgets.QLabel(
            "1) 五指完全张开、拇指尽量外展，点「记录张开」\n"
            "2) 攥拳并把拇指压在中指第一指节，点「记录握拳」\n"
            "两个姿态分别映射成全部 255 和全部 0"))
        row = QtWidgets.QHBoxLayout()
        self.cal_open = QtWidgets.QPushButton("记录张开")
        self.cal_fist = QtWidgets.QPushButton("记录握拳")
        row.addWidget(self.cal_open)
        row.addWidget(self.cal_fist)
        clayout.addLayout(row)
        self.cal_label = QtWidgets.QLabel()
        self.cal_label.setWordWrap(True)
        clayout.addWidget(self.cal_label)
        self.use_default_cal = QtWidgets.QCheckBox("忽略标定文件，用默认阈值")
        clayout.addWidget(self.use_default_cal)
        outer.addWidget(calibrate)

        self.log_view = QtWidgets.QPlainTextEdit()
        self.log_view.setReadOnly(True)
        self.log_view.setMaximumHeight(140)
        outer.addWidget(QtWidgets.QLabel("日志"))
        outer.addWidget(self.log_view)
        outer.addStretch(1)

        panel.setWidget(body)

        self.connect_button.clicked.connect(self.on_connect)
        self.disconnect_button.clicked.connect(self.on_disconnect)
        self.release_button.clicked.connect(self.on_release)
        self.pause_button.toggled.connect(self.on_pause)
        self.mirror_check.toggled.connect(lambda v: self.params.update(mirror=v))
        self.cal_open.clicked.connect(lambda: self.request_calibration("open"))
        self.cal_fist.clicked.connect(lambda: self.request_calibration("fist"))
        self.use_default_cal.toggled.connect(self.on_toggle_default_cal)
        self.hand_type.currentTextChanged.connect(self.on_hand_type_change)
        self.hand_joint.currentTextChanged.connect(self.on_hand_joint_change)
        self.hand_joint.currentTextChanged.connect(self.on_hand_joint_change)
        self.rec_name.textChanged.connect(lambda v: self.params.update(rec_name=v.strip()))
        self.record_button.clicked.connect(self.on_toggle_record)
        self.play_button.clicked.connect(self.on_play)
        self.capture_button.clicked.connect(self.on_capture)
        self.apply_button.clicked.connect(self.on_apply_gesture)
        refresh_rec.clicked.connect(self.refresh_files)
        refresh_g.clicked.connect(self.refresh_files)
        self.refresh_files()
        return panel

    def refresh_files(self):
        self.rec_files.clear()
        self.rec_files.addItems(mimic.list_names(mimic.RECORDINGS_DIR))
        self.gesture_files.clear()
        self.gesture_files.addItems(mimic.list_names(mimic.GESTURES_DIR))

    def on_toggle_record(self):
        if self.worker.recording:
            frames = self.worker.snapshot_recording()
            self.record_button.setText("开始录制")
            if not frames:
                self.on_log("录制期间没有检测到手，未保存")
                return
            path = mimic.save_recording(mimic.RECORDINGS_DIR, frames,
                                        self.params["hand_joint"],
                                        self.params.get("rec_name") or None)
            self.on_log(f"已保存录制 {len(frames)} 帧: {path}")
            self.refresh_files()
        else:
            self.worker.start_recording()
            self.record_button.setText("停止录制")

    def on_play(self):
        name = self.rec_files.currentText()
        if not name:
            self.on_log("没有可回放的录制文件，先按「开始录制」录一段")
            return
        try:
            frames = mimic.load_recording(
                os.path.join(mimic.RECORDINGS_DIR, f"{name}.json"), self.params["hand_joint"])
        except (OSError, ValueError) as exc:
            self.on_log(f"录制读取失败: {exc}")
            return
        self.worker.player = mimic.Player(frames, self.play_speed.value(),
                                         self.play_loop.isChecked())
        self.on_log(f"开始回放 {name}（{len(frames)} 帧）")

    def on_capture(self):
        self.worker.gesture_capture = self.gesture_name.text().strip()

    def on_apply_gesture(self):
        name = self.gesture_files.currentText()
        if not name:
            self.on_log("手势库是空的，先「拍照保存当前手势」")
            return
        self.worker.gesture_play = name

    def make_spin_widget(self, key, minimum, maximum, decimals=0, step=1.0):
        spin = make_spin(minimum, maximum, self.params[key], decimals=decimals, step=step)
        spin.valueChanged.connect(lambda v, k=key: self.params.update({k: v}))
        self.spin[key] = spin
        return spin

    def add_spin(self, form, label, key, minimum, maximum, decimals=0, step=1.0):
        form.addRow(label, self.make_spin_widget(key, minimum, maximum, decimals, step))

    def on_connect(self):
        self.worker.request_connect({
            "hand_type": self.hand_type.currentText(),
            "hand_joint": self.hand_joint.currentText(),
            "can": self.can_edit.text().strip(),
        })

    def on_disconnect(self):
        self.worker.request_disconnect()

    def on_release(self):
        self.worker.release_requested = True

    def on_pause(self, checked):
        self.worker.paused = checked
        self.pause_button.setText("继续" if checked else "暂停")

    def reload_calibration(self):
        if self.use_default_cal.isChecked():
            self.params["calibration"] = mimic.default_calibration()
        else:
            self.params["calibration"] = (
                mimic.load_calibration(mimic.CALIBRATION_PATH, self.params["hand_joint"],
                                       self.params["hand_type"])
                or mimic.default_calibration())
        self.update_cal_source()

    def on_hand_type_change(self, value):
        self.params["hand_type"] = value
        self.reload_calibration()

    def on_hand_joint_change(self, value):
        self.params["hand_joint"] = value
        self.reload_calibration()

    def on_toggle_default_cal(self, checked):
        self.reload_calibration()

    def request_calibration(self, slot):
        if not self.latest_raw:
            self.on_log("画面里没有手，无法记录")
            return
        self.worker.calib_capture = slot

    def update_cal_source(self):
        if self.use_default_cal.isChecked():
            self.cal_label.setText("当前使用默认阈值")
        elif os.path.isfile(mimic.CALIBRATION_PATH):
            with open(mimic.CALIBRATION_PATH, encoding="utf-8") as f:
                data = json.load(f)
            self.cal_label.setText(
                f"已标定 {data.get('hand_joint')}/{data.get('hand_type')} "
                f"{data.get('created', '')}")
        else:
            self.cal_label.setText("未标定（使用默认阈值）")

    def on_frame(self, image, pose, raw, fps, status):
        self.video.setPixmap(QtGui.QPixmap.fromImage(image))
        if raw:
            self.latest_raw = raw
        self.status_label.setText(
            f"{status}   FPS {fps:.0f}   "
            f"{'已暂停' if self.worker.paused else '运行中'}")
        if pose:
            for slider, value_label, value in zip(self.sliders, self.value_labels, pose):
                slider.setValue(value)
                value_label.setText(str(value))
        if raw:
            self.raw_label.setText("各关节读数 " + "  ".join(
                f"{key.split('_')[-1]}:{raw[key]:.0f}" for key in mimic.CALIB_KEYS))

    def on_log(self, message):
        self.log_view.appendPlainText(message)

    def closeEvent(self, event):
        self.worker.running = False
        self.worker.wait(3000)
        event.accept()


def main():
    app = QtWidgets.QApplication(sys.argv)
    window = MainWindow()
    window.resize(1180, 620)
    window.show()
    sys.exit(app.exec_())


if __name__ == "__main__":
    main()
