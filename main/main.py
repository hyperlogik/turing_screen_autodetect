import sys
import queue
import threading
import time
from io import BytesIO
from datetime import datetime

import numpy as np
from PyQt6.QtCore import Qt, QThread, pyqtSignal, QObject, QTimer
from PyQt6.QtGui import QPixmap, QTextCursor
from PyQt6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout,
    QHBoxLayout, QComboBox, QLabel, QPushButton, QTextEdit
)
from PIL import Image, ImageDraw, ImageFont as PILFont
import serial
import serial.tools.list_ports

# ── Turing library / Raw Fallback ─────────────────────────────────────────────
USING_RAW_FALLBACK = False

try:
    from library.lcd.lcd_comm import Orientation
    from library.lcd.lcd_comm_rev_a import LcdCommRevA
    from library.lcd.lcd_comm_rev_b import LcdCommRevB
    from library.lcd.lcd_comm_rev_c import LcdCommRevC
    from library.lcd.lcd_comm_rev_d import LcdCommRevD
    LIBRARY_OK = True
    print("Using official turing-smart-screen-python library.")
except ImportError as e:
    LIBRARY_OK = True
    USING_RAW_FALLBACK = True
    print(f"Library not found ({e}), using internal RAW BYPASS.")

    class Orientation:
        PORTRAIT = 0
        REVERSE_PORTRAIT = 1
        LANDSCAPE = 2
        REVERSE_LANDSCAPE = 3

    CMD_HELLO       = 69
    CMD_SCREEN_ON   = 109
    CMD_BRIGHTNESS  = 110
    CMD_ORIENTATION = 121
    CMD_DISPLAY_BMP = 197
    _STOP = object()

    def _wire_time(num_bytes: int, baud: int) -> float:
        return (num_bytes * 10 / baud) * 1.25

    def _image_to_rgb565_le(image: Image.Image) -> bytes:
        if image.mode not in ("RGB", "RGBA"):
            image = image.convert("RGB")
        rgb = np.asarray(image).reshape(image.size[1] * image.size[0], -1)
        r = rgb[:, 0].astype(np.uint16)
        g = rgb[:, 1].astype(np.uint16)
        b = rgb[:, 2].astype(np.uint16)
        rgb565 = ((r >> 3) << 11) | ((g >> 2) << 5) | (b >> 3)
        return rgb565.astype("<u2").tobytes()

    class RawLcdCommBase:
        def __init__(self, com_port, display_width, display_height, baud, update_queue):
            self.com_port = com_port
            self.width = display_width
            self.height = display_height
            self.baud = baud
            self._q = update_queue
            self._open_serial()
            self._writer = threading.Thread(
                target=self._writer_loop, daemon=True, name="SerialWriter"
            )
            self._writer.start()

        def _open_serial(self):
            self.ser = serial.Serial(
                self.com_port, baudrate=self.baud,
                timeout=1, write_timeout=None, rtscts=True,
            )
            self.ser.flushInput()
            self.ser.flushOutput()

        def _writer_loop(self):
            while True:
                item = self._q.get()
                if item is _STOP:
                    self._q.task_done()
                    break
                payload, sleep_after = item
                try:
                    if self.ser and self.ser.is_open:
                        self.ser.write(payload)
                        self.ser.flush()
                        if sleep_after > 0:
                            time.sleep(sleep_after)
                except Exception as e:
                    print(f"[SerialWriter] error: {e}")
                finally:
                    self._q.task_done()

        def _enqueue(self, payload, sleep_after=0.0):
            self._q.put((payload, sleep_after))

        def _enqueue_sync(self, payload, sleep_after=0.0):
            self._enqueue(payload, sleep_after)
            self._q.join()

        def Reset(self): pass  # deliberately no-op — never re-probe in raw mode

        def InitializeComm(self): pass

        def closeSerial(self):
            self._q.put(_STOP)
            self._writer.join(timeout=5)
            if self.ser and self.ser.is_open:
                try: self.ser.flush()
                except Exception: pass
                self.ser.close()

    class LcdCommRevA(RawLcdCommBase):
        def _cmd(self, cmd, x=0, y=0, ex=0, ey=0):
            b = bytearray(6)
            b[0] = x >> 2
            b[1] = ((x & 3) << 6) + (y >> 4)
            b[2] = ((y & 15) << 4) + (ex >> 6)
            b[3] = ((ex & 63) << 2) + (ey >> 8)
            b[4] = ey & 255
            b[5] = cmd
            return bytes(b)

        def InitializeComm(self):
            hello = bytes([CMD_HELLO] * 6)
            self._enqueue_sync(hello + self._cmd(CMD_SCREEN_ON), sleep_after=0.5)

        def SetBrightness(self, level):
            level = max(0, min(100, level))
            abs_level = int(255 - (level / 100) * 255)
            self._enqueue_sync(self._cmd(CMD_BRIGHTNESS, x=abs_level), sleep_after=0.05)

        def SetOrientation(self, orientation):
            b = bytearray(16)
            b[5] = CMD_ORIENTATION
            b[6] = orientation + 100
            b[7] = self.width >> 8
            b[8] = self.width & 255
            b[9] = self.height >> 8
            b[10] = self.height & 255
            self._enqueue_sync(bytes(b), sleep_after=0.35)

        def DisplayPILImage(self, img, log_fn=None):
            img = img.convert('RGB').resize((self.width, self.height))
            header = self._cmd(CMD_DISPLAY_BMP, 0, 0, self.width - 1, self.height - 1)
            buf = _image_to_rgb565_le(img)
            wire = _wire_time(len(buf), self.baud)
            if log_fn:
                log_fn(f"Frame: {len(buf):,} bytes | ~{wire:.1f}s wire time at {self.baud:,} baud", "info")
            self._enqueue_sync(header, sleep_after=0.02)
            self._enqueue_sync(buf, sleep_after=wire + 0.5)

    class LcdCommRevB(RawLcdCommBase):
        def SetBrightness(self, level): pass
        def SetOrientation(self, orientation): pass
        def DisplayPILImage(self, img, log_fn=None): pass

    LcdCommRevC = LcdCommRevB
    LcdCommRevD = LcdCommRevB


# ── Revision table ────────────────────────────────────────────────────────────

REVISIONS = [
    ("A", 'Rev A — Turing 3.5"', 320, 480, LcdCommRevA),
    ("B", 'Rev B — XuanFang',    320, 480, LcdCommRevB),
    ("C", 'Rev C — Turing 5"',   800, 480, LcdCommRevC),
    ("D", 'Rev D — UsbPCMonitor',320, 480, LcdCommRevD),
]

# ── CRITICAL: only test combos for revisions your hardware actually is.
# The official library calls sys.exit(0) if the wrong class tries to open an
# already-in-use port, so we must never instantiate a class for a revision
# we don't have. Change to None to disable the filter (multi-device setups only).
ACTIVE_REVISIONS = ["A"]   # confirmed: SubRevision.TURING_3_5 on COM5

ORIENTATIONS = [
    (Orientation.PORTRAIT,          "Portrait",          False),
    (Orientation.LANDSCAPE,         "Landscape",         True),
    (Orientation.REVERSE_PORTRAIT,  "Reverse Portrait",  False),
    (Orientation.REVERSE_LANDSCAPE, "Reverse Landscape", True),
]

THEMES = [
    ("#0d0d1a", "#00ffe0", "#111128", "Neon Teal"),
    ("#1a0000", "#ff5555", "#2a0808", "Crimson"),
    ("#001a0d", "#39ff14", "#0a2010", "Matrix"),
    ("#0a0a1f", "#c77dff", "#18183a", "Ultraviolet"),
    ("#1a1200", "#ffcf00", "#2a1e00", "Solar"),
]

ALL_COMBOS = [
    (r, o, t)
    for r in REVISIONS
    for o in ORIENTATIONS
    for t in THEMES
    if ACTIVE_REVISIONS is None or r[0] in ACTIVE_REVISIONS
]
TOTAL = len(ALL_COMBOS)


# ── PIL image ─────────────────────────────────────────────────────────────────

def make_image(w, h, bg, accent, panel, idx, rev_label, ori_name, theme_name, port_name):
    img = Image.new("RGB", (w, h), bg)
    draw = ImageDraw.Draw(img)
    draw.rectangle([0, 0, w, max(h // 7, 22)], fill=panel)

    try:    font_large = PILFont.truetype("arialbd.ttf", max(24, h // 9))
    except: font_large = PILFont.load_default()
    try:    font_small = PILFont.truetype("arial.ttf", max(12, h // 22))
    except: font_small = PILFont.load_default()

    draw.text((8, 4),           f"TURZX  {port_name}",  font=font_small, fill=accent)
    draw.text((w - 50, 4),      f"{idx + 1}/{TOTAL}",    font=font_small, fill=accent)
    draw.text((w // 4, h // 3), "Hello, World!",         font=font_large, fill=accent)
    draw.line([(w//8, h//2), (w - w//8, h//2)],          fill=accent, width=2)

    iy = h // 2 + 15
    for line in [f"Rev: {rev_label[:15]}", f"Orient: {ori_name}", f"Res: {w}x{h}"]:
        draw.text((w // 8, iy), line, font=font_small, fill="#ffffff")
        iy += 20

    draw.rectangle([2, 2, w - 3, h - 3], outline=accent, width=2)
    return img


def pil_to_qpixmap(img):
    buf = BytesIO()
    img.save(buf, "PNG")
    buf.seek(0)
    px = QPixmap()
    px.loadFromData(buf.read())
    return px


# ── Send worker ───────────────────────────────────────────────────────────────

class SendWorker(QObject):
    sig_log  = pyqtSignal(str, str)
    sig_done = pyqtSignal(bool)

    def __init__(self, idx, lcd_state, lcd_lock, com_port, baud):
        super().__init__()
        self._idx       = idx
        self._lcd_state = lcd_state
        self._lcd_lock  = lcd_lock
        self.com_port   = com_port
        self.baud       = baud

    def _log(self, msg, level="info"):
        self.sig_log.emit(msg, level)

    def run(self):
        idx = self._idx
        rev, ori, theme = ALL_COMBOS[idx]
        rev_key, rev_label, nat_w, nat_h, rev_cls = rev
        ori_obj, ori_name, is_landscape = ori
        disp_w = nat_h if is_landscape else nat_w
        disp_h = nat_w if is_landscape else nat_h

        self._log(f"--- Combo {idx+1}/{TOTAL}: Rev {rev_key} | {ori_name} | {theme[3]} ---", "header")

        with self._lcd_lock:
            lcd      = self._lcd_state.get("lcd")
            last_rev = self._lcd_state.get("last_rev")

            # Close only when the revision truly changes
            if lcd is not None and last_rev != rev_key:
                self._log(f"Revision changed ({last_rev}→{rev_key}), reinitialising...", "info")
                try: lcd.closeSerial()
                except Exception: pass
                lcd = None
                self._lcd_state.update({"lcd": None, "last_rev": None})
                time.sleep(0.5)  # let the OS release the COM handle

            if lcd is None:
                try:
                    self._log(f"Opening {self.com_port} at {self.baud:,} baud...", "info")
                    q = None  # pass None so library writes synchronously; a queue needs an external drain thread the app never starts

                    if USING_RAW_FALLBACK:
                        lcd = rev_cls(
                            com_port=self.com_port,
                            display_width=nat_w,
                            display_height=nat_h,
                            baud=self.baud,
                            update_queue=q,
                        )
                    else:
                        # Official library: __init__ calls openSerial() internally.
                        # No 'baud' kwarg — library always opens at 115200.
                        lcd = rev_cls(
                            com_port=self.com_port,
                            display_width=nat_w,
                            display_height=nat_h,
                            update_queue=q,
                        )
                        # Bump baud after init if user selected something higher
                        if hasattr(lcd, 'lcd_serial') and lcd.lcd_serial and lcd.lcd_serial.is_open:
                            lcd.lcd_serial.baudrate = self.baud

                    # InitializeComm() sends HELLO once and identifies sub-revision.
                    # DO NOT call Reset() here — Reset() closes the port, waits 5 s,
                    # then reopens it, which re-runs the A→B→C→D detection sequence
                    # and causes "Device not recognised" warnings + PermissionError.
                    lcd.InitializeComm()
                    lcd.SetBrightness(50)

                    self._lcd_state.update({"lcd": lcd, "last_rev": rev_key})
                    self._log("Hardware ready.", "ok")

                except Exception as e:
                    self._log(f"Connect failed: {e}", "err")
                    self.sig_done.emit(False)
                    return
            else:
                # Same revision: reuse the open connection — no re-probe, no re-init
                self._log(f"Reusing existing Rev {rev_key} connection.", "info")

        try:
            lcd.SetOrientation(orientation=ori_obj)
            img = make_image(
                disp_w, disp_h, theme[0], theme[1], theme[2],
                idx, rev_label, ori_name, theme[3], self.com_port
            )
            self._log("Sending image...", "info")
            if USING_RAW_FALLBACK:
                lcd.DisplayPILImage(img, log_fn=self._log)
            else:
                lcd.DisplayPILImage(img)
            self._log("Success!", "ok")
            self.sig_done.emit(True)

        except Exception as e:
            self._log(f"Send failed: {e}", "err")
            with self._lcd_lock:
                lcd = self._lcd_state.get("lcd")
                if lcd:
                    self._log("Closing connection after error.", "warn")
                    try: lcd.closeSerial()
                    except Exception: pass
                    self._lcd_state.update({"lcd": None, "last_rev": None})
            self.sig_done.emit(False)


# ── Main window ───────────────────────────────────────────────────────────────

class MainWindow(QMainWindow):
    # Dedicated signal so cycle_timer is always touched on the main thread.
    # Without this, _on_send_done (invoked on the worker QThread via sig_done)
    # would call cycle_timer.start() cross-thread → QBasicTimer::stop warning.
    sig_schedule_next = pyqtSignal()

    def __init__(self):
        super().__init__()
        self.setWindowTitle("TURZX Auto-Cycle Tester")
        self.resize(860, 580)

        self._idx       = 0
        self._sending   = False
        self._lcd_state = {"lcd": None, "last_rev": None}
        self._lcd_lock  = threading.Lock()
        self.auto_cycle = False

        self.cycle_timer = QTimer(self)
        self.cycle_timer.setSingleShot(True)
        self.cycle_timer.timeout.connect(self._auto_next_and_send)
        self.sig_schedule_next.connect(self._on_schedule_next_main)

        self._resize_timer = QTimer(self)
        self._resize_timer.setSingleShot(True)
        self._resize_timer.setInterval(150)
        self._resize_timer.timeout.connect(lambda: self._load_combo(self._idx))

        self._thread = None
        self._worker = None

        self._build_ui()
        self.populate_ports()
        self._load_combo(0)

        self._log(f"Active revision filter: {ACTIVE_REVISIONS}  —  {TOTAL} combos total.", "info")
        if USING_RAW_FALLBACK:
            self._log("RAW BYPASS active — official library not found.", "warn")

    def _on_schedule_next_main(self):
        """Always runs on the main thread — safe to call QTimer methods here."""
        if self.auto_cycle:
            self.cycle_timer.start(500)

    def _build_ui(self):
        central = QWidget()
        self.setCentralWidget(central)
        root = QVBoxLayout(central)

        ctrl = QHBoxLayout()

        self.port_combo  = QComboBox()
        self.btn_refresh = QPushButton("↻")
        self.btn_refresh.setFixedWidth(32)
        self.btn_refresh.clicked.connect(self.populate_ports)

        self.baud_combo = QComboBox()
        for baud in [115200, 230400, 460800, 921600, 1000000, 2000000]:
            self.baud_combo.addItem(f"{baud:,}", userData=baud)
        self.baud_combo.setCurrentIndex(1)  # default 230400
        self.baud_combo.currentIndexChanged.connect(self._on_baud_changed)

        self.btn_prev = QPushButton("◀")
        self.btn_prev.setFixedWidth(40)
        self.btn_prev.clicked.connect(self._prev)

        self.btn_auto = QPushButton("▶ Auto")
        self.btn_auto.setCheckable(True)
        self.btn_auto.clicked.connect(self._toggle_auto)

        self.btn_next = QPushButton("▶")
        self.btn_next.setFixedWidth(40)
        self.btn_next.clicked.connect(self._next)

        self.btn_send = QPushButton("⬆ Send")
        self.btn_send.setStyleSheet("background-color:#2e7d32;color:white;font-weight:bold;")
        self.btn_send.clicked.connect(self._send)

        ctrl.addWidget(QLabel("Port:"));  ctrl.addWidget(self.port_combo)
        ctrl.addWidget(self.btn_refresh); ctrl.addSpacing(6)
        ctrl.addWidget(QLabel("Baud:")); ctrl.addWidget(self.baud_combo)
        ctrl.addSpacing(6)
        ctrl.addWidget(self.btn_prev); ctrl.addWidget(self.btn_auto); ctrl.addWidget(self.btn_next)
        ctrl.addStretch(); ctrl.addWidget(self.btn_send)
        root.addLayout(ctrl)

        self.lbl_info = QLabel()
        self.lbl_info.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.lbl_info.setStyleSheet("font-weight:bold;font-size:14px;padding:4px;")
        root.addWidget(self.lbl_info)

        split = QHBoxLayout()

        self.lbl_preview = QLabel("Preview")
        self.lbl_preview.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.lbl_preview.setMinimumSize(320, 240)
        self.lbl_preview.setStyleSheet("background:#f0f0f0;border:1px solid #ccc;")
        split.addWidget(self.lbl_preview, stretch=1)

        self.txt_log = QTextEdit()
        self.txt_log.setReadOnly(True)
        self.txt_log.setStyleSheet("font-family:Consolas,monospace;font-size:12px;background:#fff;")
        split.addWidget(self.txt_log, stretch=1)
        root.addLayout(split)

        self.lbl_status = QLabel(self._status_hint())
        self.lbl_status.setStyleSheet("color:#666;font-size:11px;padding:2px 4px;")
        root.addWidget(self.lbl_status)

    def _status_hint(self):
        baud = self.baud_combo.currentData() or 230400
        if USING_RAW_FALLBACK:
            t = _wire_time(320 * 480 * 2, baud)
            return (f"RAW mode | {baud:,} baud | 320×480 frame ≈ {t:.1f}s  "
                    f"— ⚠ Rev A brightness kept ≤ 50%")
        return f"Official library active | {baud:,} baud"

    def _on_baud_changed(self):
        with self._lcd_lock:
            lcd = self._lcd_state.get("lcd")
            if lcd:
                try: lcd.closeSerial()
                except Exception: pass
            self._lcd_state.update({"lcd": None, "last_rev": None})
        self.lbl_status.setText(self._status_hint())
        self._log(f"Baud → {self.baud_combo.currentData():,}. Will reconnect on next send.", "warn")

    def populate_ports(self):
        cur = self.port_combo.currentText()
        self.port_combo.clear()
        for p in sorted(serial.tools.list_ports.comports()):
            self.port_combo.addItem(p.device)
        if cur:
            self.port_combo.setCurrentText(cur)

    def _log(self, msg, level="info"):
        colours = {"header":"blue","ok":"green","err":"red","warn":"darkorange","info":"black"}
        c  = colours.get(level, "black")
        ts = datetime.now().strftime("%H:%M:%S")
        self.txt_log.append(f'<span style="color:{c};">[{ts}] {msg}</span>')
        self.txt_log.moveCursor(QTextCursor.MoveOperation.End)

    def _load_combo(self, idx):
        self._idx = idx
        rev, ori, theme = ALL_COMBOS[idx]
        is_land = ori[2]
        w = rev[3] if is_land else rev[2]
        h = rev[2] if is_land else rev[3]
        self.lbl_info.setText(f"Combo {idx+1}/{TOTAL}: Rev {rev[0]} | {ori[1]} | {theme[3]}")
        self.btn_prev.setEnabled(idx > 0 and not self._sending)
        self.btn_next.setEnabled(idx < TOTAL - 1 and not self._sending)
        img = make_image(w, h, theme[0], theme[1], theme[2],
                         idx, rev[1], ori[1], theme[3], self.port_combo.currentText())
        px = pil_to_qpixmap(img)
        self.lbl_preview.setPixmap(
            px.scaled(self.lbl_preview.width() - 10, self.lbl_preview.height() - 10,
                      Qt.AspectRatioMode.KeepAspectRatio,
                      Qt.TransformationMode.SmoothTransformation)
        )

    def _toggle_auto(self, checked):
        if checked and not self.port_combo.currentText():
            self._log("Select a COM port first.", "err")
            self.btn_auto.setChecked(False)
            return
        self.auto_cycle = checked
        if self.auto_cycle:
            self.btn_auto.setText("⏸ Pause")
            self.btn_auto.setStyleSheet("background-color:#f57c00;color:white;font-weight:bold;")
            self._log("Auto-cycle started.", "info")
            if not self._sending:
                self._send()
        else:
            self.btn_auto.setText("▶ Auto")
            self.btn_auto.setStyleSheet("")
            self.cycle_timer.stop()
            self._log("Auto-cycle paused.", "warn")

    def _auto_next_and_send(self):
        if not self.auto_cycle:
            return
        if self._idx < TOTAL - 1:
            self._next()
        else:
            self._log("End of combos — looping.", "ok")
            self._load_combo(0)
        self._send()

    def _prev(self):
        if self._idx > 0:
            self._load_combo(self._idx - 1)

    def _next(self):
        if self._idx < TOTAL - 1:
            self._load_combo(self._idx + 1)

    def _send(self):
        if self._sending:
            return
        port = self.port_combo.currentText()
        if not port:
            self._log("Select a COM port first.", "err")
            return
        self._sending = True
        self.btn_send.setEnabled(False)
        self.btn_prev.setEnabled(False)
        self.btn_next.setEnabled(False)

        self._worker = SendWorker(
            self._idx, self._lcd_state, self._lcd_lock,
            port, self.baud_combo.currentData()
        )
        self._thread = QThread()
        self._worker.moveToThread(self._thread)
        self._thread.started.connect(self._worker.run)
        self._worker.sig_log.connect(self._log)
        self._worker.sig_done.connect(self._on_send_done)
        self._thread.start()

    def _on_send_done(self, success):
        self._thread.quit()
        self._thread.wait()
        self._thread = None
        self._worker = None
        self._sending = False
        self.btn_send.setEnabled(True)
        self.btn_prev.setEnabled(self._idx > 0)
        self.btn_next.setEnabled(self._idx < TOTAL - 1)
        # Do NOT touch cycle_timer directly here — this slot may be delivered on
        # the worker thread. Emit the signal instead; the main-thread slot will
        # call cycle_timer.start() safely.
        if self.auto_cycle:
            self.sig_schedule_next.emit()

    def resizeEvent(self, e):
        super().resizeEvent(e)
        self._resize_timer.start()

    def closeEvent(self, e):
        self.auto_cycle = False
        self.cycle_timer.stop()
        with self._lcd_lock:
            lcd = self._lcd_state.get("lcd")
            if lcd:
                try: lcd.closeSerial()
                except Exception: pass
        if self._thread and self._thread.isRunning():
            self._thread.quit()
            self._thread.wait(2000)
        super().closeEvent(e)


if __name__ == "__main__":
    app = QApplication(sys.argv)
    win = MainWindow()
    win.show()
    sys.exit(app.exec())

