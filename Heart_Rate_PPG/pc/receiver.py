"""Read framed PPG packets from USB serial → ring buffer + Parquet rolling files.

Two output streams per session:
  - ppg_NNNN.parquet   raw IR/Red @ sample rate (rolled every PARQUET_ROLL_SECONDS)
  - hr_30s.parquet     one row per 30 s (mean HR over the window)

Recording is gated by arm()/disarm(). Ring buffer is always fed (so the live UI
keeps working); Parquet writes only happen between arm() and disarm().
"""
from __future__ import annotations
import argparse
import json
import queue
import signal
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pyarrow as pa
import pyarrow.parquet as pq
import serial
from serial.tools import list_ports

import protocol
from ringbuffer import RingBuffer

PARQUET_ROLL_SECONDS = 300
FS_WINDOW_SAMPLES = 400  # rolling window for measured-fs estimate (~2 s at 200 Hz)


def autodetect_port() -> str | None:
    for p in list_ports.comports():
        name = (p.device or "").lower()
        if "usbserial" in name or "usbmodem" in name or "wchusbserial" in name:
            return p.device
    return None


class Receiver:
    def __init__(self, port: str, ringbuf: RingBuffer, data_dir: Path, session_meta: dict):
        self.port = port
        self.ring = ringbuf
        self.data_dir = data_dir
        self.session_meta = session_meta
        self._stop = threading.Event()
        self._writer_q: queue.Queue = queue.Queue(maxsize=20000)
        self._stats = {
            "received": 0,
            "dropped": 0,
            "bad_crc": 0,
            "resyncs": 0,
            "started_at": None,
            "recorded": 0,
            "session_dir": None,
            "recording": False,
            "measured_fs_hz": 0.0,
        }
        self._last_seq: int | None = None
        # Rolling window of recent t_us values for measured-fs estimation.
        self._fs_t_us: list[int] = []
        self._recording = threading.Event()
        self._current_session_dir: Path | None = None
        self._writer_thread: threading.Thread | None = None

    def stop(self) -> None:
        self._stop.set()

    def stats(self) -> dict:
        return dict(self._stats)

    def is_recording(self) -> bool:
        return self._recording.is_set()

    def session_dir(self) -> Path | None:
        return self._current_session_dir

    def arm(self) -> Path:
        if self._recording.is_set():
            assert self._current_session_dir is not None
            return self._current_session_dir
        session_start = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        session_dir = self.data_dir / f"session_{session_start}"
        session_dir.mkdir(parents=True, exist_ok=True)
        meta = {
            **self.session_meta,
            "started_utc": session_start,
            "sample_rate_hz": protocol.SAMPLE_RATE_HZ,
            "port": self.port,
        }
        (session_dir / "session.json").write_text(json.dumps(meta, indent=2))
        self._current_session_dir = session_dir
        self._stats["session_dir"] = str(session_dir)
        self._stats["recorded"] = 0
        self._writer_q.put(("ARM", session_dir))
        self._recording.set()
        self._stats["recording"] = True
        return session_dir

    def disarm(self) -> None:
        if not self._recording.is_set():
            return
        self._recording.clear()
        self._stats["recording"] = False
        self._writer_q.put(("DISARM", None))

    def log_hr_average(self, t_unix: float, mean_hr_bpm: float, n_beats: int,
                       finger_present_frac: float) -> None:
        """Append one row to hr_30s.parquet for the current session."""
        if not self._recording.is_set():
            return
        self._writer_q.put(("HR30", (t_unix, mean_hr_bpm, n_beats, finger_present_frac)))

    def run(self) -> None:
        self._writer_thread = threading.Thread(
            target=self._writer_loop, name="parquet-writer", daemon=True
        )
        self._writer_thread.start()
        try:
            self._read_loop()
        finally:
            self._writer_q.put(("QUIT", None))
            self._writer_thread.join(timeout=5)

    def _read_loop(self) -> None:
        with serial.Serial(self.port, 115200, timeout=1) as s:
            self._stats["started_at"] = datetime.now(timezone.utc).isoformat()
            buf = bytearray()
            while not self._stop.is_set():
                chunk = s.read(4096)
                if not chunk:
                    continue
                buf.extend(chunk)
                self._consume(buf)

    def _consume(self, buf: bytearray) -> None:
        while True:
            i = buf.find(bytes((protocol.SYNC0, protocol.SYNC1)))
            if i < 0:
                if len(buf) > 1:
                    del buf[: len(buf) - 1]
                return
            if i > 0:
                self._stats["resyncs"] += 1
                del buf[:i]
            if len(buf) < protocol.PACKET_SIZE:
                return
            pkt = bytes(buf[: protocol.PACKET_SIZE])
            sample = protocol.parse_packet(pkt)
            if sample is None:
                self._stats["bad_crc"] += 1
                del buf[:1]
                continue
            del buf[: protocol.PACKET_SIZE]
            self._on_sample(sample)

    def _on_sample(self, s: "protocol.Sample") -> None:
        if self._last_seq is not None:
            gap = (s.seq - self._last_seq) & 0xFFFF
            if gap > 1:
                self._stats["dropped"] += gap - 1
        self._last_seq = s.seq
        self._stats["received"] += 1

        # Measured fs from device timestamps. Robust to USB buffering since
        # t_us is stamped on the firmware side at moment-of-read. Median over
        # the rolling window of differences kills outliers from FIFO bursts.
        self._fs_t_us.append(s.t_us)
        if len(self._fs_t_us) > FS_WINDOW_SAMPLES:
            self._fs_t_us = self._fs_t_us[-FS_WINDOW_SAMPLES:]
        if len(self._fs_t_us) >= 32:
            arr = np.asarray(self._fs_t_us, dtype=np.int64)
            # Handle u32 wrap by taking unsigned diffs
            diffs = (np.diff(arr) & 0xFFFFFFFF).astype(np.float64)
            med = float(np.median(diffs))
            if med > 0:
                self._stats["measured_fs_hz"] = 1e6 / med

        row = np.array([s.t_us, s.ir, s.red], dtype=np.float64)
        self.ring.append(row)

        if self._recording.is_set():
            try:
                self._writer_q.put_nowait(("SAMPLE", (s.seq, row)))
            except queue.Full:
                pass

    def _writer_loop(self) -> None:
        session_dir: Path | None = None
        seqs, t_us, irs, reds = [], [], [], []
        hr30_t, hr30_hr, hr30_n, hr30_pres = [], [], [], []
        roll_started = time.monotonic()
        roll_index = 0

        def flush_ppg() -> None:
            nonlocal roll_index
            if not seqs or session_dir is None:
                return
            table = pa.table({
                "seq": pa.array(seqs, type=pa.uint32()),
                "t_us": pa.array(t_us, type=pa.uint64()),
                "ir": pa.array(irs, type=pa.uint32()),
                "red": pa.array(reds, type=pa.uint32()),
            })
            out = session_dir / f"ppg_{roll_index:04d}.parquet"
            pq.write_table(table, out, compression="zstd")
            roll_index += 1
            seqs.clear(); t_us.clear(); irs.clear(); reds.clear()

        def flush_hr30() -> None:
            if not hr30_t or session_dir is None:
                return
            table = pa.table({
                "t_unix": pa.array(hr30_t, type=pa.float64()),
                "mean_hr_bpm": pa.array(hr30_hr, type=pa.float32()),
                "n_beats": pa.array(hr30_n, type=pa.uint32()),
                "finger_present_frac": pa.array(hr30_pres, type=pa.float32()),
            })
            out = session_dir / "hr_30s.parquet"
            # Append by re-writing: HR30 rows are slow (1 per 30 s) so cheap.
            pq.write_table(table, out, compression="zstd")

        while True:
            item = self._writer_q.get()
            kind, payload = item
            if kind == "QUIT":
                flush_ppg()
                flush_hr30()
                return
            if kind == "ARM":
                flush_ppg(); flush_hr30()
                session_dir = payload
                roll_started = time.monotonic()
                roll_index = 0
                hr30_t.clear(); hr30_hr.clear(); hr30_n.clear(); hr30_pres.clear()
                continue
            if kind == "DISARM":
                flush_ppg(); flush_hr30()
                session_dir = None
                continue
            if kind == "SAMPLE":
                if session_dir is None:
                    continue
                seq, row = payload
                seqs.append(seq)
                t_us.append(int(row[0]))
                irs.append(int(row[1]))
                reds.append(int(row[2]))
                self._stats["recorded"] = self._stats.get("recorded", 0) + 1
                if time.monotonic() - roll_started >= PARQUET_ROLL_SECONDS:
                    flush_ppg()
                    roll_started = time.monotonic()
                continue
            if kind == "HR30":
                if session_dir is None:
                    continue
                t, hr, n, pres = payload
                hr30_t.append(float(t))
                hr30_hr.append(float(hr))
                hr30_n.append(int(n))
                hr30_pres.append(float(pres))
                flush_hr30()  # write-through; one row every 30 s is fine
                continue


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", help="serial port; auto-detected if omitted")
    ap.add_argument("--data-dir", default=str(Path(__file__).resolve().parent.parent / "data"))
    ap.add_argument("--subject", default="self")
    ap.add_argument("--species", default="human")
    ap.add_argument("--mount", default="finger")
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
        session_meta={
            "subject": args.subject,
            "species": args.species,
            "mount": args.mount,
            "note": args.note,
        },
    )

    def handle_sigint(*_):
        rx.disarm(); rx.stop()
    signal.signal(signal.SIGINT, handle_sigint)

    print(f"Reading from {port}. Ctrl+C to stop. (CLI mode records the whole session.)")
    rx.arm()
    last_print = time.monotonic()
    t = threading.Thread(target=rx.run, daemon=True)
    t.start()
    while t.is_alive():
        time.sleep(0.5)
        if time.monotonic() - last_print >= 1.0:
            last_print = time.monotonic()
            st = rx.stats()
            print(
                f"\rrx={st['received']} rec={st['recorded']} drop={st['dropped']} "
                f"bad_crc={st['bad_crc']} resync={st['resyncs']}",
                end="", flush=True,
            )
    print()
    return 0


if __name__ == "__main__":
    sys.exit(main())
