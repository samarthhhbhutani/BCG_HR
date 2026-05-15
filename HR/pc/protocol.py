"""Wire protocol for hr_streamer.ino. Keep in sync with the firmware."""
from __future__ import annotations
import struct
from dataclasses import dataclass

SYNC0 = 0xAA
SYNC1 = 0x55
PACKET_SIZE = 22  # sync(2) + seq(2) + t_us(4) + 6*i16 + crc(1) + '\n'(1)
PAYLOAD_SLICE = slice(2, 20)  # bytes hashed by CRC8

SAMPLE_RATE_HZ = 200
FS_ACCEL_G = 4.0
FS_GYRO_DPS = 1000.0
ACCEL_LSB_PER_G = 32768.0 / FS_ACCEL_G
GYRO_LSB_PER_DPS = 32768.0 / FS_GYRO_DPS

# struct format for the payload after the sync bytes (everything CRC covers)
_PAYLOAD_FMT = "<HI6h"
_PAYLOAD_STRUCT = struct.Struct(_PAYLOAD_FMT)


@dataclass(slots=True)
class Sample:
    seq: int
    t_us: int
    ax_g: float
    ay_g: float
    az_g: float
    gx_dps: float
    gy_dps: float
    gz_dps: float


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
    if len(pkt) < 21 or pkt[0] != SYNC0 or pkt[1] != SYNC1:
        return None
    payload = pkt[PAYLOAD_SLICE]
    if crc8(payload) != pkt[20]:
        return None
    seq, t_us, ax, ay, az, gx, gy, gz = _PAYLOAD_STRUCT.unpack(payload)
    return Sample(
        seq=seq,
        t_us=t_us,
        ax_g=ax / ACCEL_LSB_PER_G,
        ay_g=ay / ACCEL_LSB_PER_G,
        az_g=az / ACCEL_LSB_PER_G,
        gx_dps=gx / GYRO_LSB_PER_DPS,
        gy_dps=gy / GYRO_LSB_PER_DPS,
        gz_dps=gz / GYRO_LSB_PER_DPS,
    )
