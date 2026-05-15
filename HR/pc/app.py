"""Live UI: starts receiver + HR worker, draws 4 panes.

Run:
  .venv/bin/python app.py [--port /dev/cu.usbmodemXXXX] [--subject self]

Panes:
  1. Raw selected accel axis (last WINDOW_S seconds)
  2. Band-pass filtered SCG
  3. Envelope + detected beats
  4. HR trend (ACF, envelope, fused)

Recording: live preview is always on. Press SPACE (or click the Record button) to
start writing Parquet; press again to stop. Each start creates a new session_<UTC>/.

Status row: HR readout (color = confidence), axis, SNR, per-method HRs, beat count,
and recording status (REC ●).
"""
from __future__ import annotations
import argparse
import sys
import threading
import time
from pathlib import Path

import numpy as np
import pyqtgraph as pg
from PyQt6 import QtCore, QtGui, QtWidgets

import protocol
from hr_worker import HRWorker, WINDOW_S
from receiver import Receiver, autodetect_port
from ringbuffer import RingBuffer, COL_AX, COL_AY, COL_AZ


HR_HISTORY_S = 300


class HRWindow(QtWidgets.QMainWindow):
    def __init__(self, ring: RingBuffer, worker: HRWorker, receiver: Receiver):
        super().__init__()
        self.ring = ring
        self.worker = worker
        self.receiver = receiver
        self.setWindowTitle("HR — M5StickC PLUS2 SCG")
        self.resize(1200, 900)

        central = QtWidgets.QWidget()
        self.setCentralWidget(central)
        layout = QtWidgets.QVBoxLayout(central)

        pg.setConfigOptions(antialias=True, useOpenGL=False)
        gw = pg.GraphicsLayoutWidget()
        layout.addWidget(gw, stretch=1)

        self.plot_raw = gw.addPlot(row=0, col=0, title="Raw accel (selected axis)")
        self.plot_filt = gw.addPlot(row=1, col=0, title="Band-pass 5–25 Hz")
        self.plot_env = gw.addPlot(row=2, col=0, title="Envelope + beats")
        self.plot_hr = gw.addPlot(row=3, col=0, title="HR trend (bpm)")
        for p in (self.plot_raw, self.plot_filt, self.plot_env):
            p.showGrid(x=True, y=True, alpha=0.2)
            p.setLabel("bottom", "time (s)")
        self.plot_hr.showGrid(x=True, y=True, alpha=0.2)
        self.plot_hr.setLabel("bottom", "session time (s)")
        self.plot_hr.setLabel("left", "HR (bpm)")
        self.plot_hr.setYRange(40, 180)

        self.curve_raw = self.plot_raw.plot(pen=pg.mkPen(width=1))
        self.curve_filt = self.plot_filt.plot(pen=pg.mkPen("#1f77b4", width=1))
        self.curve_env = self.plot_env.plot(pen=pg.mkPen("#ff7f0e", width=1))
        self.scatter_beats = pg.ScatterPlotItem(size=8, brush=pg.mkBrush("#d62728"))
        self.plot_env.addItem(self.scatter_beats)
        self.curve_hr_fused = self.plot_hr.plot(pen=pg.mkPen("#2ca02c", width=2), name="fused")
        self.curve_hr_acf = self.plot_hr.plot(pen=pg.mkPen("#1f77b4", width=1, style=QtCore.Qt.PenStyle.DashLine), name="acf")
        self.curve_hr_env = self.plot_hr.plot(pen=pg.mkPen("#ff7f0e", width=1, style=QtCore.Qt.PenStyle.DashLine), name="env")
        self.plot_hr.addLegend(offset=(10, 10))

        status = QtWidgets.QHBoxLayout()
        layout.addLayout(status)
        self.lbl_hr = QtWidgets.QLabel("--")
        self.lbl_hr.setStyleSheet("font-size: 36px; font-weight: bold;")
        status.addWidget(self.lbl_hr)
        self.lbl_meta = QtWidgets.QLabel("waiting for data…")
        status.addWidget(self.lbl_meta, stretch=1)

        rec_row = QtWidgets.QHBoxLayout()
        layout.addLayout(rec_row)
        self.btn_rec = QtWidgets.QPushButton("● Start Recording  (Space)")
        self.btn_rec.setCheckable(True)
        self.btn_rec.setStyleSheet(self._btn_style(False))
        self.btn_rec.clicked.connect(self._toggle_recording)
        rec_row.addWidget(self.btn_rec)
        self.lbl_rec = QtWidgets.QLabel("LIVE PREVIEW — not recording")
        self.lbl_rec.setStyleSheet("font-size: 14px; color: #888;")
        rec_row.addWidget(self.lbl_rec, stretch=1)

        # Spacebar shortcut
        self._space_shortcut = QtGui.QShortcut(QtGui.QKeySequence(QtCore.Qt.Key.Key_Space), self)
        self._space_shortcut.activated.connect(self._toggle_recording)

        self._t_session_start = time.time()
        self._t_record_start: float | None = None
        self._hr_history: list[tuple[float, float, float, float]] = []  # (t, fused, acf, env)

        self.timer = QtCore.QTimer(self)
        self.timer.timeout.connect(self._tick)
        self.timer.start(100)  # 10 Hz UI refresh

    @staticmethod
    def _btn_style(recording: bool) -> str:
        base = "font-size: 16px; padding: 8px 16px; font-weight: bold;"
        if recording:
            return base + " background-color: #d62728; color: white;"
        return base + " background-color: #2ca02c; color: white;"

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
        n = int(WINDOW_S * protocol.SAMPLE_RATE_HZ)
        data = self.ring.latest(n)
        if data.shape[0] == 0:
            return
        update = self.worker.latest

        if update is not None:
            col_idx = {"ax": COL_AX, "ay": COL_AY, "az": COL_AZ}[update.axis]
            raw = data[:, col_idx]
            t_axis = np.arange(len(raw)) / protocol.SAMPLE_RATE_HZ
            self.curve_raw.setData(t_axis, raw)

            filt_t = np.arange(len(update.filtered)) / protocol.SAMPLE_RATE_HZ
            self.curve_filt.setData(filt_t, update.filtered)
            self.curve_env.setData(filt_t, update.envelope)

            if len(update.peak_indices) > 0:
                px = update.peak_indices / protocol.SAMPLE_RATE_HZ
                py = update.envelope[update.peak_indices]
                self.scatter_beats.setData(px, py)
            else:
                self.scatter_beats.setData([], [])

            t_rel = update.t_unix - self._t_session_start
            self._hr_history.append((t_rel, update.hr_bpm, update.hr_acf_bpm, update.hr_env_bpm))
            cutoff = t_rel - HR_HISTORY_S
            self._hr_history = [h for h in self._hr_history if h[0] >= cutoff]
            if self._hr_history:
                arr = np.array(self._hr_history)
                self.curve_hr_fused.setData(arr[:, 0], arr[:, 1])
                self.curve_hr_acf.setData(arr[:, 0], arr[:, 2])
                self.curve_hr_env.setData(arr[:, 0], arr[:, 3])

            if np.isnan(update.hr_bpm):
                self.lbl_hr.setText("--")
                self.lbl_hr.setStyleSheet("font-size: 36px; font-weight: bold; color: gray;")
            else:
                self.lbl_hr.setText(f"{update.hr_bpm:5.1f} bpm")
                color = "#2ca02c" if update.confident else "#ff7f0e"
                self.lbl_hr.setStyleSheet(f"font-size: 36px; font-weight: bold; color: {color};")
            conf = "high" if update.confident else "low"
            acf = f"{update.hr_acf_bpm:.1f}" if not np.isnan(update.hr_acf_bpm) else "—"
            env = f"{update.hr_env_bpm:.1f}" if not np.isnan(update.hr_env_bpm) else "—"
            self.lbl_meta.setText(
                f"axis={update.axis}  SNR={update.snr_db:5.1f} dB  "
                f"acf={acf}  env={env}  beats={update.n_beats}  conf={conf}"
            )
        else:
            t_axis = np.arange(data.shape[0]) / protocol.SAMPLE_RATE_HZ
            self.curve_raw.setData(t_axis, data[:, COL_AZ])

        # Refresh recording status line + always-visible receiver stats
        st = self.receiver.stats()
        rx_info = (
            f"rx={st['received']} drop={st['dropped']} "
            f"bad_crc={st['bad_crc']} resync={st['resyncs']}"
        )
        if self.receiver.is_recording() and self._t_record_start is not None:
            elapsed = time.time() - self._t_record_start
            session_name = Path(st.get("session_dir") or "").name
            self.lbl_rec.setText(
                f"● REC  {elapsed:5.1f}s  recorded={st.get('recorded', 0)}  → {session_name}    [{rx_info}]"
            )
            self.lbl_rec.setStyleSheet("font-size: 14px; color: #d62728; font-weight: bold;")
        else:
            self.lbl_rec.setText(f"LIVE PREVIEW — not recording    [{rx_info}]")
            self.lbl_rec.setStyleSheet("font-size: 14px; color: #888;")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", help="serial port; auto-detected if omitted")
    ap.add_argument("--data-dir", default=str(Path.home() / "Desktop/HR/data"))
    ap.add_argument("--subject", default="self")
    ap.add_argument("--mount", default="sternum-finger-press")
    ap.add_argument("--note", default="")
    args = ap.parse_args()

    port = args.port or autodetect_port()
    if not port:
        print("No serial port found. Pass --port /dev/cu.usbmodemXXXX", file=sys.stderr)
        return 2

    ring = RingBuffer(capacity=protocol.SAMPLE_RATE_HZ * 60)

    rx = Receiver(
        port=port,
        ringbuf=ring,
        data_dir=Path(args.data_dir),
        session_meta={"subject": args.subject, "mount": args.mount, "note": args.note},
    )
    rx_thread = threading.Thread(target=rx.run, name="receiver", daemon=True)
    rx_thread.start()

    worker = HRWorker(ring=ring)
    worker_thread = threading.Thread(target=worker.run, name="hr-worker", daemon=True)
    worker_thread.start()

    app = QtWidgets.QApplication(sys.argv)
    win = HRWindow(ring=ring, worker=worker, receiver=rx)
    win.show()
    rc = app.exec()
    rx.disarm()
    rx.stop()
    worker.stop()
    return rc


if __name__ == "__main__":
    sys.exit(main())
