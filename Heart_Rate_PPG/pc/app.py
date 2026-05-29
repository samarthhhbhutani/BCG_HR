"""Live UI for the PPG HR pipeline.

Run:
  .venv/bin/python app.py [--port /dev/cu.usbmodemXXXX] [--species human]

Panes:
  1. Raw IR (last WINDOW_S seconds)
  2. Band-passed IR + detected beat markers
  3. Instant HR (per estimator update, ~1 Hz)
  4. 30 s mean HR (one point per 30 s window)

Top status:
  Big HR readout (color = confidence)
  Finger-on / finger-off indicator (green dot / grey dot)
  Subject species, IR DC, AC amplitude, beats-in-window, conf

Recording: live preview is always on. Press SPACE or click Record to start
writing Parquet. Each session creates data/session_<UTC>/ with:
  - ppg_NNNN.parquet      raw IR/Red samples
  - hr_30s.parquet        one row per 30 s window (mean HR + finger-present %)
  - session.json          metadata (subject, species, mount, start time)
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
import subject_config
from hr_worker import HRWorker, WINDOW_S
from multi_worker import MultiDetectorWorker, MultiUpdate
from receiver import Receiver, autodetect_port
from ringbuffer import RingBuffer, COL_IR


HR_HISTORY_S = 600        # how much of the trend to plot
HR_WINDOW_S = 30.0        # averaging window for the 30 s HR

# Stable colors for the multi-detector overlay.
DETECTOR_COLORS = {
    "terma":    "#d62728",  # red
    "elgendi":  "#1f77b4",  # blue
    "bishop":   "#2ca02c",  # green
    "charlton": "#ff7f0e",  # orange
}


class HRWindow(QtWidgets.QMainWindow):
    def __init__(self, ring: RingBuffer, worker: HRWorker, receiver: Receiver,
                 species: str, multi_worker: MultiDetectorWorker | None = None):
        super().__init__()
        self.ring = ring
        self.worker = worker
        self.receiver = receiver
        self.species = species
        self.multi_worker = multi_worker
        title_suffix = f" + {','.join(multi_worker.detector_names)}" if multi_worker else ""
        self.setWindowTitle(f"PPG HR — MAX30102 ({species}){title_suffix}")
        self.resize(1200, 900 if multi_worker is None else 1080)

        central = QtWidgets.QWidget()
        self.setCentralWidget(central)
        layout = QtWidgets.QVBoxLayout(central)

        # Top status row
        top = QtWidgets.QHBoxLayout()
        layout.addLayout(top)
        self.lbl_hr = QtWidgets.QLabel("--")
        self.lbl_hr.setStyleSheet("font-size: 48px; font-weight: bold;")
        top.addWidget(self.lbl_hr)
        self.lbl_finger = QtWidgets.QLabel("● FINGER")
        self.lbl_finger.setStyleSheet(self._finger_style(False))
        top.addWidget(self.lbl_finger)
        self.lbl_meta = QtWidgets.QLabel("waiting for data…")
        self.lbl_meta.setStyleSheet("font-size: 13px;")
        top.addWidget(self.lbl_meta, stretch=1)

        pg.setConfigOptions(antialias=True, useOpenGL=False)
        gw = pg.GraphicsLayoutWidget()
        layout.addWidget(gw, stretch=1)

        self.plot_raw = gw.addPlot(row=0, col=0, title="Raw IR (MAX30102)")
        self.plot_filt = gw.addPlot(row=1, col=0, title="Band-passed IR + beats")
        self.plot_hr_inst = gw.addPlot(row=2, col=0, title="Instant HR (bpm)")
        self.plot_hr_30 = gw.addPlot(row=3, col=0, title="30 s mean HR (bpm)")
        for p in (self.plot_raw, self.plot_filt):
            p.showGrid(x=True, y=True, alpha=0.2)
            p.setLabel("bottom", "time (s)")
        for p in (self.plot_hr_inst, self.plot_hr_30):
            p.showGrid(x=True, y=True, alpha=0.2)
            p.setLabel("bottom", "session time (s)")
            p.setLabel("left", "HR (bpm)")
            p.setYRange(40, 180)

        self.curve_raw = self.plot_raw.plot(pen=pg.mkPen("#888", width=1))
        self.curve_filt = self.plot_filt.plot(pen=pg.mkPen("#1f77b4", width=1))
        self.scatter_beats = pg.ScatterPlotItem(size=8, brush=pg.mkBrush("#d62728"))
        self.plot_filt.addItem(self.scatter_beats)
        self.curve_hr_inst = self.plot_hr_inst.plot(
            pen=pg.mkPen("#2ca02c", width=2), symbol="o", symbolSize=4,
            symbolBrush=pg.mkBrush("#2ca02c"),
        )
        self.curve_hr_30 = self.plot_hr_30.plot(
            pen=pg.mkPen("#ff7f0e", width=2), symbol="o", symbolSize=8,
            symbolBrush=pg.mkBrush("#ff7f0e"),
        )

        # Multi-detector overlay pane (optional).
        self.plot_multi: pg.PlotItem | None = None
        self.curve_multi_signal = None
        self.scatter_multi: dict[str, pg.ScatterPlotItem] = {}
        self.lbl_multi_hr = None
        if self.multi_worker is not None:
            self.plot_multi = gw.addPlot(row=4, col=0, title="Multi-detector overlay")
            self.plot_multi.showGrid(x=True, y=True, alpha=0.2)
            self.plot_multi.setLabel("bottom", "time (s)")
            # Show one cleaned signal underneath (use TERMA's if running, else first).
            ref_name = "terma" if "terma" in self.multi_worker.detector_names \
                else self.multi_worker.detector_names[0]
            self._multi_ref = ref_name
            self.curve_multi_signal = self.plot_multi.plot(
                pen=pg.mkPen("#888", width=1)
            )
            for n in self.multi_worker.detector_names:
                color = DETECTOR_COLORS.get(n, "#cccccc")
                sp = pg.ScatterPlotItem(size=8, brush=pg.mkBrush(color), pen=None)
                self.plot_multi.addItem(sp)
                self.scatter_multi[n] = sp
            self.lbl_multi_hr = QtWidgets.QLabel("multi: …")
            self.lbl_multi_hr.setStyleSheet(
                "font-size: 13px; padding: 4px;"
                " font-family: 'SF Mono','Menlo','Courier New',monospace;"
            )
            top.addWidget(self.lbl_multi_hr)

        # Recording controls
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
        # Instant HR history: (t_rel, hr) — only when finger present + estimable.
        self._hr_inst_history: deque[tuple[float, float]] = deque()
        # 30 s averages: (t_rel, mean_hr) — populated by _maybe_emit_30s.
        self._hr_30_history: list[tuple[float, float]] = []
        # Rolling buffer for 30 s averaging: (t_unix, hr_bpm, finger_present_bool)
        self._hr30_buffer: deque[tuple[float, float, bool]] = deque()
        self._last_30s_emit: float = time.time()

        self.timer = QtCore.QTimer(self)
        self.timer.timeout.connect(self._tick)
        self.timer.start(100)  # 10 Hz UI refresh

    def _update_multi_pane(self) -> None:
        if self.multi_worker is None or self.plot_multi is None:
            return
        upd = self.multi_worker.latest
        if upd is None or not upd.snapshots:
            return
        ref = upd.snapshots.get(self._multi_ref) or next(iter(upd.snapshots.values()))
        t_axis = np.arange(len(ref.cleaned)) / upd.fs
        self.curve_multi_signal.setData(t_axis, ref.cleaned)
        for name, snap in upd.snapshots.items():
            sp = self.scatter_multi.get(name)
            if sp is None:
                continue
            if len(snap.peaks) == 0:
                sp.setData([], [])
                continue
            # Each detector's peaks are indices into ITS OWN cleaned signal —
            # cleaners differ in cutoff, so the y-amplitudes will differ. Plot
            # the markers at the reference-cleaner's value at the same x for
            # visual alignment; the color tells you which detector found it.
            x = snap.peaks.astype(np.float64) / upd.fs
            # Cap into the shared reference array.
            ref_idx = np.clip(snap.peaks, 0, len(ref.cleaned) - 1)
            y = ref.cleaned[ref_idx]
            sp.setData(x, y)
        if self.lbl_multi_hr is not None:
            parts = []
            for name in self.multi_worker.detector_names:
                snap = upd.snapshots.get(name)
                if snap is None or np.isnan(snap.hr_bpm):
                    parts.append(f"{name}=—")
                else:
                    parts.append(f"{name}={snap.hr_bpm:.1f}")
            self.lbl_multi_hr.setText("  ".join(parts))

    @staticmethod
    def _btn_style(recording: bool) -> str:
        base = "font-size: 15px; padding: 8px 16px; font-weight: bold;"
        return base + (
            " background-color: #d62728; color: white;"
            if recording
            else " background-color: #2ca02c; color: white;"
        )

    @staticmethod
    def _finger_style(present: bool) -> str:
        base = "font-size: 18px; padding: 4px 10px; font-weight: bold; border-radius: 4px;"
        if present:
            return base + " background-color: #2ca02c; color: white;"
        return base + " background-color: #888; color: #ddd;"

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

    def _maybe_emit_30s(self, now: float) -> None:
        """Every HR_WINDOW_S, compute the mean of finger-present HR samples in
        the trailing window and (a) push to the 30 s plot, (b) log to receiver."""
        if now - self._last_30s_emit < HR_WINDOW_S:
            return
        cutoff = now - HR_WINDOW_S
        while self._hr30_buffer and self._hr30_buffer[0][0] < cutoff:
            self._hr30_buffer.popleft()
        if not self._hr30_buffer:
            self._last_30s_emit = now
            return
        present = [hr for (_t, hr, p) in self._hr30_buffer if p and not np.isnan(hr)]
        finger_frac = sum(1 for (_t, _hr, p) in self._hr30_buffer if p) / len(self._hr30_buffer)
        if not present:
            self._last_30s_emit = now
            return
        mean_hr = float(np.mean(present))
        n_beats = len(present)
        t_rel = now - self._t_session_start
        self._hr_30_history.append((t_rel, mean_hr))
        # Trim to HR_HISTORY_S
        while self._hr_30_history and (t_rel - self._hr_30_history[0][0]) > HR_HISTORY_S:
            self._hr_30_history.pop(0)
        self.receiver.log_hr_average(now, mean_hr, n_beats, finger_frac)
        self._last_30s_emit = now

    def _tick(self) -> None:
        n = int(WINDOW_S * protocol.SAMPLE_RATE_HZ)
        data = self.ring.latest(n)
        update = self.worker.latest
        now = time.time()

        if data.shape[0] > 0:
            t_axis = np.arange(data.shape[0]) / protocol.SAMPLE_RATE_HZ
            self.curve_raw.setData(t_axis, data[:, COL_IR])

        if update is not None:
            t_filt = np.arange(len(update.filtered)) / protocol.SAMPLE_RATE_HZ
            # Display sign-flipped band-pass so peaks point up (matches scatter).
            display_filt = -update.filtered
            self.curve_filt.setData(t_filt, display_filt)
            if len(update.peak_indices) > 0:
                px = update.peak_indices / protocol.SAMPLE_RATE_HZ
                py = display_filt[update.peak_indices]
                self.scatter_beats.setData(px, py)
            else:
                self.scatter_beats.setData([], [])

            # Buffer for the 30 s average.
            self._hr30_buffer.append((update.t_unix, update.hr_bpm, update.finger_present))

            # Instant HR plot (only when present + finite).
            t_rel = update.t_unix - self._t_session_start
            if update.finger_present and not np.isnan(update.hr_bpm):
                self._hr_inst_history.append((t_rel, update.hr_bpm))
            cutoff = t_rel - HR_HISTORY_S
            while self._hr_inst_history and self._hr_inst_history[0][0] < cutoff:
                self._hr_inst_history.popleft()
            if self._hr_inst_history:
                arr = np.array(self._hr_inst_history)
                self.curve_hr_inst.setData(arr[:, 0], arr[:, 1])

            if self._hr_30_history:
                arr30 = np.array(self._hr_30_history)
                self.curve_hr_30.setData(arr30[:, 0], arr30[:, 1])

            self.lbl_finger.setText("● FINGER ON" if update.finger_present else "○ NO FINGER")
            self.lbl_finger.setStyleSheet(self._finger_style(update.finger_present))

            if not update.finger_present:
                self.lbl_hr.setText("--")
                self.lbl_hr.setStyleSheet("font-size: 48px; font-weight: bold; color: gray;")
            elif np.isnan(update.hr_bpm):
                self.lbl_hr.setText("…")
                self.lbl_hr.setStyleSheet("font-size: 48px; font-weight: bold; color: orange;")
            else:
                self.lbl_hr.setText(f"{update.hr_bpm:5.1f} bpm")
                color = "#2ca02c" if update.confident else "#ff7f0e"
                self.lbl_hr.setStyleSheet(f"font-size: 48px; font-weight: bold; color: {color};")

            conf = "high" if update.confident else "low"
            self.lbl_meta.setText(
                f"species={self.species}  IR_DC={update.ir_dc:.0f}  "
                f"AC={update.ac_amplitude:.0f}  beats={update.n_beats}  conf={conf}"
            )

        self._update_multi_pane()

        self._maybe_emit_30s(now)

        # Recording status line + receiver stats.
        st = self.receiver.stats()
        fs_meas = st.get("measured_fs_hz", 0.0)
        fs_warn = ""
        if fs_meas > 0:
            ratio = fs_meas / protocol.SAMPLE_RATE_HZ
            if ratio < 0.9 or ratio > 1.1:
                fs_warn = " ⚠"
        rx_info = (
            f"rx={st['received']} drop={st['dropped']} "
            f"bad_crc={st['bad_crc']} resync={st['resyncs']} "
            f"fs={fs_meas:.1f}Hz{fs_warn}"
        )
        if self.receiver.is_recording() and self._t_record_start is not None:
            elapsed = now - self._t_record_start
            session_name = Path(st.get("session_dir") or "").name
            self.lbl_rec.setText(
                f"● REC  {elapsed:5.1f}s  recorded={st.get('recorded', 0)}  "
                f"→ {session_name}    [{rx_info}]"
            )
            self.lbl_rec.setStyleSheet("font-size: 13px; color: #d62728; font-weight: bold;")
        else:
            self.lbl_rec.setText(f"LIVE PREVIEW — not recording    [{rx_info}]")
            self.lbl_rec.setStyleSheet("font-size: 13px; color: #888;")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", help="serial port; auto-detected if omitted")
    ap.add_argument("--data-dir",
                    default=str(Path(__file__).resolve().parent.parent / "data"))
    ap.add_argument("--subject", default="self")
    ap.add_argument("--species", default="human",
                    help="human / dog / cat, or path to a JSON SubjectConfig")
    ap.add_argument("--mount", default="finger")
    ap.add_argument("--note", default="")
    ap.add_argument("--multi-detector", action="store_true",
                    help="run all 4 detectors live and overlay them (extra pane)")
    ap.add_argument("--detector",
                    help="comma-separated subset of terma,elgendi,bishop,charlton "
                         "for the multi-detector pane (default: all 4)")
    args = ap.parse_args()

    cfg = subject_config.load(args.species)

    port = args.port or autodetect_port()
    if not port:
        print("No serial port found. Pass --port /dev/cu.usbmodemXXXX", file=sys.stderr)
        return 2

    ring = RingBuffer(capacity=protocol.SAMPLE_RATE_HZ * 60)

    rx = Receiver(
        port=port,
        ringbuf=ring,
        data_dir=Path(args.data_dir),
        session_meta={
            "subject": args.subject,
            "species": cfg.name,
            "mount": args.mount,
            "note": args.note,
            "subject_config": cfg.to_dict(),
        },
    )
    rx_thread = threading.Thread(target=rx.run, name="receiver", daemon=True)
    rx_thread.start()

    worker = HRWorker(ring=ring, cfg=cfg)
    worker_thread = threading.Thread(target=worker.run, name="hr-worker", daemon=True)
    worker_thread.start()

    multi: MultiDetectorWorker | None = None
    multi_thread: threading.Thread | None = None
    if args.multi_detector or args.detector:
        from pipelines import ALL_NAMES
        if args.detector:
            names = [s.strip().lower() for s in args.detector.split(",") if s.strip()]
            for n in names:
                if n not in ALL_NAMES:
                    print(f"unknown detector {n!r}; valid: {','.join(ALL_NAMES)}",
                          file=sys.stderr)
                    return 2
        else:
            names = list(ALL_NAMES)
        multi = MultiDetectorWorker(ring=ring, detector_names=names)
        multi_thread = threading.Thread(target=multi.run, name="multi-worker", daemon=True)
        multi_thread.start()

    app = QtWidgets.QApplication(sys.argv)
    win = HRWindow(ring=ring, worker=worker, receiver=rx, species=cfg.name,
                   multi_worker=multi)
    win.show()
    rc = app.exec()
    rx.disarm()
    rx.stop()
    worker.stop()
    if multi is not None:
        multi.stop()
    return rc


if __name__ == "__main__":
    sys.exit(main())
