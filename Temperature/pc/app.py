"""Live UI for the MLX90614 temperature pipeline.

Run:
  .venv/bin/python app.py [--port /dev/cu.usbmodemXXXX]

Layout:
  Top status row:
    Big T readout (°C)
    Subject + receiver stats
  Plot pane:
    Object temperature over the last HISTORY_S seconds.
  Recording controls:
    Press SPACE or click Record to begin writing temp_1hz.parquet.

A new data/session_<UTC>/ directory is created with:
  - temp_1hz.parquet      one row per emit (~1 Hz)
  - session.json          metadata (subject + emit rate + start time)
"""
from __future__ import annotations
import argparse
import sys
import threading
import time
from collections import deque
from pathlib import Path

import numpy as np
import pyqtgraph as pg
from PyQt6 import QtCore, QtGui, QtWidgets

import protocol
from receiver import Receiver, autodetect_port
from ringbuffer import RingBuffer, COL_TEMP


HISTORY_S = 600        # 10 min visible trend
COLD_HOT_C = (10.0, 50.0)  # plot y-range — generous for body, room, finger


class TempWindow(QtWidgets.QMainWindow):
    def __init__(self, ring: RingBuffer, receiver: Receiver, subject: str):
        super().__init__()
        self.ring = ring
        self.receiver = receiver
        self.subject = subject
        self.setWindowTitle(f"Temperature — MLX90614 ({subject})")
        self.resize(1000, 600)

        central = QtWidgets.QWidget()
        self.setCentralWidget(central)
        layout = QtWidgets.QVBoxLayout(central)

        # Top status row.
        top = QtWidgets.QHBoxLayout()
        layout.addLayout(top)
        self.lbl_temp = QtWidgets.QLabel("--")
        self.lbl_temp.setStyleSheet("font-size: 56px; font-weight: bold;")
        top.addWidget(self.lbl_temp)
        self.lbl_meta = QtWidgets.QLabel("waiting for data…")
        self.lbl_meta.setStyleSheet("font-size: 13px;")
        top.addWidget(self.lbl_meta, stretch=1)

        pg.setConfigOptions(antialias=True, useOpenGL=False)
        gw = pg.GraphicsLayoutWidget()
        layout.addWidget(gw, stretch=1)
        self.plot = gw.addPlot(title="Object temperature (°C)")
        self.plot.showGrid(x=True, y=True, alpha=0.2)
        self.plot.setLabel("bottom", "session time (s)")
        self.plot.setLabel("left", "T (°C)")
        self.plot.setYRange(*COLD_HOT_C)
        self.curve = self.plot.plot(
            pen=pg.mkPen("#d62728", width=2), symbol="o", symbolSize=4,
            symbolBrush=pg.mkBrush("#d62728"),
        )

        # Recording controls.
        rec_row = QtWidgets.QHBoxLayout()
        layout.addLayout(rec_row)
        self.btn_rec = QtWidgets.QPushButton("● Start Recording  (Space)")
        self.btn_rec.setCheckable(True)
        self.btn_rec.setStyleSheet(self._btn_style(False))
        self.btn_rec.clicked.connect(self._toggle_recording)
        rec_row.addWidget(self.btn_rec)
        self.lbl_rec = QtWidgets.QLabel("LIVE PREVIEW — not recording")
        self.lbl_rec.setStyleSheet("font-size: 13px; color: #888;")
        rec_row.addWidget(self.lbl_rec, stretch=1)

        self._space_shortcut = QtGui.QShortcut(QtGui.QKeySequence(QtCore.Qt.Key.Key_Space), self)
        self._space_shortcut.activated.connect(self._toggle_recording)

        self._t_session_start = time.time()
        self._t_record_start: float | None = None
        # (t_rel, t_obj_c) history for the plot — drawn from the ring buffer
        # every tick, so the plot shows samples that arrived while recording
        # was off too.
        self._last_total = 0

        self.timer = QtCore.QTimer(self)
        self.timer.timeout.connect(self._tick)
        self.timer.start(250)  # 4 Hz UI refresh — plenty for 1 Hz data

    @staticmethod
    def _btn_style(recording: bool) -> str:
        base = "font-size: 15px; padding: 8px 16px; font-weight: bold;"
        return base + (
            " background-color: #d62728; color: white;"
            if recording else " background-color: #2ca02c; color: white;"
        )

    def _toggle_recording(self) -> None:
        if self.receiver.is_recording():
            self.receiver.disarm()
            self._t_record_start = None
            self.btn_rec.setChecked(False)
            self.btn_rec.setText("● Start Recording  (Space)")
            self.btn_rec.setStyleSheet(self._btn_style(False))
        else:
            session_dir = self.receiver.arm()
            self._t_record_start = time.time()
            self.btn_rec.setChecked(True)
            self.btn_rec.setText("■ Stop Recording  (Space)")
            self.btn_rec.setStyleSheet(self._btn_style(True))
            self.lbl_rec.setText(f"REC → {session_dir.name}")

    def _tick(self) -> None:
        # Pull up to HISTORY_S worth of samples (1 Hz, so 600 rows max).
        n = HISTORY_S  # one row per second
        data = self.ring.latest(n)
        now = time.time()

        if data.shape[0] > 0:
            # x axis: seconds since session start. The ring buffer's t_us is
            # the firmware timestamp; for the plot we use position-in-history
            # since temp arrives at ~1 Hz and we don't need µs precision.
            n_rows = data.shape[0]
            x = np.arange(n_rows, dtype=np.float64) - n_rows + 1
            y = data[:, COL_TEMP]
            self.curve.setData(x, y)

            t_now = float(y[-1])
            color = self._t_color(t_now)
            self.lbl_temp.setText(f"{t_now:5.2f} °C")
            self.lbl_temp.setStyleSheet(
                f"font-size: 56px; font-weight: bold; color: {color};"
            )
        else:
            self.lbl_temp.setText("--")
            self.lbl_temp.setStyleSheet(
                "font-size: 56px; font-weight: bold; color: gray;"
            )

        # Recording status line + receiver stats.
        st = self.receiver.stats()
        rx_info = (
            f"rx={st['received']} drop={st['dropped']} "
            f"bad_crc={st['bad_crc']} resync={st['resyncs']}"
        )
        self.lbl_meta.setText(f"subject={self.subject}    [{rx_info}]")

        if self.receiver.is_recording() and self._t_record_start is not None:
            elapsed = now - self._t_record_start
            session_name = Path(st.get("session_dir") or "").name
            self.lbl_rec.setText(
                f"● REC  {elapsed:5.1f}s  recorded={st.get('recorded', 0)}  "
                f"→ {session_name}"
            )
            self.lbl_rec.setStyleSheet("font-size: 13px; color: #d62728; font-weight: bold;")
        else:
            self.lbl_rec.setText("LIVE PREVIEW — not recording")
            self.lbl_rec.setStyleSheet("font-size: 13px; color: #888;")

    @staticmethod
    def _t_color(t_c: float) -> str:
        # Cold blue → neutral grey → warm orange → hot red, using rough body-temp bands.
        if t_c < 30.0:
            return "#1f77b4"
        if t_c < 35.5:
            return "#888888"
        if t_c < 37.5:
            return "#2ca02c"
        if t_c < 38.5:
            return "#ff7f0e"
        return "#d62728"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", help="serial port; auto-detected if omitted")
    ap.add_argument("--data-dir",
                    default=str(Path(__file__).resolve().parent.parent / "data"))
    ap.add_argument("--subject", default="self")
    ap.add_argument("--note", default="")
    args = ap.parse_args()

    port = args.port or autodetect_port()
    if not port:
        print("No serial port found. Pass --port /dev/cu.usbmodemXXXX", file=sys.stderr)
        return 2

    ring = RingBuffer(capacity=int(protocol.EMIT_RATE_HZ * 3600))  # 1 hour

    rx = Receiver(
        port=port,
        ringbuf=ring,
        data_dir=Path(args.data_dir),
        session_meta={
            "sensor": "MLX90614",
            "subject": args.subject,
            "note": args.note,
        },
    )
    rx_thread = threading.Thread(target=rx.run, name="receiver", daemon=True)
    rx_thread.start()

    app = QtWidgets.QApplication(sys.argv)
    win = TempWindow(ring=ring, receiver=rx, subject=args.subject)
    win.show()
    rc = app.exec()
    rx.disarm()
    rx.stop()
    return rc


if __name__ == "__main__":
    sys.exit(main())
