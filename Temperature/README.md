# Temperature

Standalone temperature pipeline for M5StickC PLUS2 + MLX90614.

No HR logic. Single-pixel object temperature, recorded at ~1 Hz.

## Layout

```
Temperature/
  firmware/temp_streamer/temp_streamer.ino    flash to M5StickC PLUS2
  pc/
    protocol.py        wire format
    ringbuffer.py      shared sample buffer
    receiver.py        serial → ring buffer → parquet
    app.py             PyQt6 live UI (big T readout + line plot)
    requirements.txt
  data/                                        session_<UTC>/ output goes here
```

## Hardware

- M5StickC PLUS2 (ESP32-PICO-V3-02)
- MLX90614 single-pixel IR thermometer (I²C address 0x5A)
- Wiring (M5StickC PLUS2 → MLX90614):
  - `G0`  → SDA
  - `G26` → SCL
  - `3V3` → VIN  *(NOT 5V — MLX90614 is 3.3 V only)*
  - GND   → GND

The MLX90614 sits on the same I²C pins the PPG project uses, but at a
different address, so the two sensors *could* coexist on one bus if you ever
want both. This pipeline runs the temp sensor on its own.

## Firmware

Open `firmware/temp_streamer/temp_streamer.ino` in the Arduino IDE.

Required library: `Adafruit MLX90614 Library` (the IDE Library Manager will
also pull `Adafruit BusIO`).

Board: `M5StickC-PLUS2`. Flash, then unplug-replug USB.

The display shows:
```
Temp streamer
OK 1 Hz
seq <number>
T <reading> C
```

## PC pipeline

```sh
cd Temperature/pc
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

python app.py                  # auto-detects port
python app.py --port /dev/cu.usbserial-XXXXXXXX
python app.py --subject self --note "post-run"
```

Press **Space** (or click *Start Recording*) to begin a session. A new
`Temperature/data/session_<UTC>/` directory is created with:
- `temp_1hz.parquet`  — `seq`, `t_us`, `t_obj_c` (one row per emit)
- `session.json`      — sensor + subject + note + start time + emit rate

## Headless mode

If you only want to record without the UI:

```sh
python pc/receiver.py --subject self
```

It auto-records the whole session; Ctrl+C stops it.

## Wire format

14-byte little-endian packets:

```
[0xAA][0x57]              sync (different from PPG's 0xAA 0x55)
[seq u16]                 wraps at 65535
[t_us u32]                esp_timer_get_time() at moment of read
[t_obj_c f32]             object temperature, °C
[crc8]                    CRC8/Dallas over bytes [2..11]
[0x0A]                    '\n' framing aid
```

Emitted at ~1 Hz. The CRC matches the firmware's `crc8()` exactly.
