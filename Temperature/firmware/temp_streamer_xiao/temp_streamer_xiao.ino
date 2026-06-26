// temp_streamer_xiao.ino
// XIAO nRF52840 (Sense or regular) + MLX90614 — stream object temperature at 1 Hz
// over native USB CDC.
//
// Replaces the M5StickC-era temp_streamer.ino. Wire format is byte-identical
// (sync 0xAA 0x57, 14-byte packet, CRC8), so Temperature/pc/protocol.py and
// Temperature/pc/receiver.py work unchanged.
//
// Wiring (XIAO → MLX90614 breakout):
//   3V3 → VIN   (NOT 5V — MLX90614 is 3.3 V only)
//   GND → GND
//   D4  → SDA
//   D5  → SCL
//
// Library required: Adafruit MLX90614 Library
//   Arduino IDE → Library Manager → search "Adafruit MLX90614" → install.
//   Adafruit BusIO is pulled in automatically as a dependency.
//
// Sampling: MLX90614's I2C read is slow (~10 ms) and the temperature changes
// slowly, so we emit one packet per second. Object temperature only — that's
// the IR-measured surface temperature pointed at the lens. Ambient (die
// temperature) is intentionally omitted to keep the packet small.
//
// Packet layout (little-endian, 14 bytes total):
//   [0xAA][0x57]              sync (different from the PPG/BCG sketches)
//   [seq u16]                 wraps at 65535
//   [t_us u32]                micros() lower 32 bits at moment of read
//   [t_obj_c f32]             object temperature in degrees Celsius
//   [crc8]                    CRC8/Dallas over bytes [2..11]
//   [0x0A]                    '\n' framing aid (parser ignores)

#include <Wire.h>
#include <Adafruit_MLX90614.h>

static const uint32_t SERIAL_BAUD     = 115200;       // virtual on USB CDC
static const uint32_t I2C_HZ          = 100000;       // MLX90614 SMBus tops near 100 kHz
static const uint32_t EMIT_PERIOD_MS  = 1000;         // 1 Hz emission

Adafruit_MLX90614 mlx = Adafruit_MLX90614();

static uint16_t seq            = 0;
static uint32_t last_emit_ms   = 0;
static bool     mlx_present    = false;

static uint8_t crc8(const uint8_t* p, size_t n) {
  uint8_t c = 0;
  for (size_t i = 0; i < n; i++) {
    c ^= p[i];
    for (int b = 0; b < 8; b++) {
      c = (c & 0x80) ? (uint8_t)((c << 1) ^ 0x07) : (uint8_t)(c << 1);
    }
  }
  return c;
}

static void emit_packet(float t_obj_c) {
  uint32_t t_us = micros();
  uint8_t pkt[14];
  pkt[0] = 0xAA; pkt[1] = 0x57;
  memcpy(&pkt[2],  &seq,     2);
  memcpy(&pkt[4],  &t_us,    4);
  memcpy(&pkt[8],  &t_obj_c, 4);
  pkt[12] = crc8(&pkt[2], 10);
  pkt[13] = '\n';
  Serial.write(pkt, sizeof(pkt));
  seq++;
}

void setup() {
  Serial.begin(SERIAL_BAUD);
  while (!Serial && millis() < 3000) {}    // wait briefly for USB CDC

  // Enable internal pull-ups on D4/D5 BEFORE Wire.begin() takes the pins.
  // These are weak (~13 kΩ on nRF52) — only enough to rescue a breakout with
  // missing/marginal pull-ups at low bus speeds. Safe to leave on.
  pinMode(4, INPUT_PULLUP);   // D4 = SDA
  pinMode(5, INPUT_PULLUP);   // D5 = SCL

  Wire.begin();
  Wire.setClock(I2C_HZ);
  delay(50);

  mlx_present = mlx.begin(0x5A, &Wire);
  if (!mlx_present) {
    Serial.println("[mlx90614] NOT FOUND at 0x5A — check wiring / pull-ups");
  } else {
    Serial.println("[mlx90614] OK — streaming at 1 Hz");
  }

  last_emit_ms = millis();
}

void loop() {
  uint32_t now_ms = millis();
  if (now_ms - last_emit_ms < EMIT_PERIOD_MS) return;
  last_emit_ms = now_ms;

  if (!mlx_present) {
    // Don't spam emits; just retry the bus once a second in case the sensor
    // re-enumerates (some breakouts boot slowly on cold power-up).
    mlx_present = mlx.begin(0x5A, &Wire);
    return;
  }

  float t_obj_c = mlx.readObjectTempC();
  if (!isnan(t_obj_c)) {
    emit_packet(t_obj_c);
  }
}
