"""Read framed IMU packets from USB serial → ring buffer + Parquet rolling files.

Recording is gated by an arm/disarm API. Ring buffer is always fed (so the live UI
keeps working); Parquet writes only happen between arm() and disarm(). Each arm()
creates a new session_<UTC>/ directory.
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
        self._writer_q: queue.Queue = queue.Queue(maxsize=10000)
        self._stats = {
            "received": 0,
            "dropped": 0,
            "bad_crc": 0,
            "resyncs": 0,
            "started_at": None,
            "recorded": 0,
            "session_dir": None,
            "recording": False,
        }
        self._last_seq: int | None = None
        # Recording control: writer thread waits for items tagged with a session_dir.
        # arm() sends a "start" sentinel with a freshly-created dir; disarm() sends None.
        self._recording = threading.Event()
        self._current_session_dir: Path | None = None
        self._writer_thread: threading.Thread | None = None

    def stop(self) -> None:
        self._stop.set()

    def stats(self) -> dict:
        return dict(self._stats)

    def is_recording(self) -> bool:
        return self._recording.is_set()

    def arm(self) -> Path:
        """Begin a new recording session. Returns the session directory path."""
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
            "fs_accel_g": protocol.FS_ACCEL_G,
            "fs_gyro_dps": protocol.FS_GYRO_DPS,
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
        """Stop the current recording and flush its final Parquet chunk."""
        if not self._recording.is_set():
            return
        self._recording.clear()
        self._stats["recording"] = False
        self._writer_q.put(("DISARM", None))

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
        with serial.Serial(self.port, 921600, timeout=1) as s:
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

        row = np.array(
            [s.t_us, s.ax_g, s.ay_g, s.az_g, s.gx_dps, s.gy_dps, s.gz_dps],
            dtype=np.float64,
        )
        self.ring.append(row)

        if self._recording.is_set():
            try:
                self._writer_q.put_nowait(("SAMPLE", (s.seq, row)))
            except queue.Full:
                pass

    def _writer_loop(self) -> None:
        session_dir: Path | None = None
        chunk_seq, chunk_t_us, chunk_ax, chunk_ay, chunk_az, chunk_gx, chunk_gy, chunk_gz = (
            [], [], [], [], [], [], [], [],
        )
        roll_started = time.monotonic()
        roll_index = 0

        def flush() -> None:
            nonlocal roll_index
            if not chunk_seq or session_dir is None:
                return
            table = pa.table({
                "seq": pa.array(chunk_seq, type=pa.uint32()),
                "t_us": pa.array(chunk_t_us, type=pa.uint64()),
                "ax_g": pa.array(chunk_ax, type=pa.float32()),
                "ay_g": pa.array(chunk_ay, type=pa.float32()),
                "az_g": pa.array(chunk_az, type=pa.float32()),
                "gx_dps": pa.array(chunk_gx, type=pa.float32()),
                "gy_dps": pa.array(chunk_gy, type=pa.float32()),
                "gz_dps": pa.array(chunk_gz, type=pa.float32()),
            })
            out = session_dir / f"imu_{roll_index:04d}.parquet"
            pq.write_table(table, out, compression="zstd")
            roll_index += 1
            chunk_seq.clear(); chunk_t_us.clear()
            chunk_ax.clear(); chunk_ay.clear(); chunk_az.clear()
            chunk_gx.clear(); chunk_gy.clear(); chunk_gz.clear()

        while True:
            item = self._writer_q.get()
            kind, payload = item
            if kind == "QUIT":
                flush()
                return
            if kind == "ARM":
                flush()  # flush any leftovers from previous session (paranoia)
                session_dir = payload
                roll_started = time.monotonic()
                roll_index = 0
                continue
            if kind == "DISARM":
                flush()
                session_dir = None
                continue
            if kind == "SAMPLE":
                if session_dir is None:
                    continue  # late sample after disarm; drop
                seq, row = payload
                chunk_seq.append(seq)
                chunk_t_us.append(int(row[0]))
                chunk_ax.append(float(row[1]))
                chunk_ay.append(float(row[2]))
                chunk_az.append(float(row[3]))
                chunk_gx.append(float(row[4]))
                chunk_gy.append(float(row[5]))
                chunk_gz.append(float(row[6]))
                self._stats["recorded"] = self._stats.get("recorded", 0) + 1
                if time.monotonic() - roll_started >= PARQUET_ROLL_SECONDS:
                    flush()
                    roll_started = time.monotonic()


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

    def handle_sigint(*_):
        rx.disarm()
        rx.stop()
    signal.signal(signal.SIGINT, handle_sigint)

    print(f"Reading from {port}. Ctrl+C to stop. (CLI is preview-only; use app.py to record.)")
    rx.arm()  # CLI mode records the whole session
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
                end="",
                flush=True,
            )
    print()
    return 0


if __name__ == "__main__":
    sys.exit(main())
