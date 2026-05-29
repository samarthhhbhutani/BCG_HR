"""Wire protocol for temp_streamer.ino. Keep in sync with the firmware."""
from __future__ import annotations
import struct
from dataclasses import dataclass

SYNC0 = 0xAA
SYNC1 = 0x57            # different from the PPG firmware (which uses 0x55)
PACKET_SIZE = 14        # sync(2) + seq(2) + t_us(4) + t_obj(4) + crc(1) + '\n'(1)
PAYLOAD_SLICE = slice(2, 12)  # bytes hashed by CRC8

EMIT_RATE_HZ = 1.0      # nominal emit rate

# struct format for the payload after the sync bytes (everything CRC covers)
_PAYLOAD_FMT = "<HIf"   # u16 seq, u32 t_us, f32 t_obj_c
_PAYLOAD_STRUCT = struct.Struct(_PAYLOAD_FMT)


@dataclass(slots=True)
class Sample:
    seq: int
    t_us: int
    t_obj_c: float


def crc8(data: bytes) -> int:
    """CRC8/Dallas — matches the firmware implementation."""
    c = 0
    for b in data:
        c ^= b
        for _ in range(8):
            c = ((c << 1) ^ 0x07) & 0xFF if (c & 0x80) else (c << 1) & 0xFF
    return c


def parse_packet(pkt: bytes) -> Sample | None:
    """Validate sync + CRC and return a Sample. Returns None on bad frame."""
    if len(pkt) < PACKET_SIZE - 1 or pkt[0] != SYNC0 or pkt[1] != SYNC1:
        return None
    payload = pkt[PAYLOAD_SLICE]
    if crc8(payload) != pkt[12]:
        return None
    seq, t_us, t_obj = _PAYLOAD_STRUCT.unpack(payload)
    return Sample(seq=seq, t_us=t_us, t_obj_c=float(t_obj))
