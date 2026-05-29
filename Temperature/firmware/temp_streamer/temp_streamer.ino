// temp_streamer.ino
// M5StickC PLUS2 + MLX90614 — stream object temperature at 1 Hz over USB serial.
//
// Wiring (M5StickC PLUS2 → MLX90614 breakout):
//   G0  -> MLX90614 SDA
//   G26 -> MLX90614 SCL
//   3V3 -> MLX90614 VIN   (NOT 5V — MLX90614 is 3.3 V only)
//   GND -> MLX90614 GND
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
//   [0xAA][0x57]              sync (different from the PPG sketch's 0xAA 0x55)
//   [seq u16]                 wraps at 65535
//   [t_us u32]                esp_timer_get_time() lower 32 bits at moment of read
//   [t_obj_c f32]             object temperature in degrees Celsius
//   [crc8]                    CRC8/Dallas over bytes [2..11]
//   [0x0A]                    '\n' framing aid

#include <M5StickCPlus2.h>
#include <Wire.h>
#include <Adafruit_MLX90614.h>
#include "esp_timer.h"

static const uint32_t SERIAL_BAUD = 115200;
static const int      I2C_SDA     = 0;
static const int      I2C_SCL     = 26;
static const uint32_t I2C_HZ      = 100000;     // MLX90614 SMBus tops out near 100 kHz
static const uint32_t EMIT_PERIOD_MS = 1000;    // 1 Hz

Adafruit_MLX90614 mlx = Adafruit_MLX90614();

static uint16_t seq = 0;
static uint32_t last_emit_ms = 0;
static float    last_temp_for_display = NAN;

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
  uint32_t t_us = (uint32_t)(esp_timer_get_time() & 0xFFFFFFFFu);
  uint8_t pkt[14];
  pkt[0] = 0xAA; pkt[1] = 0x57;
  memcpy(&pkt[2],  &seq,     2);
  memcpy(&pkt[4],  &t_us,    4);
  memcpy(&pkt[8],  &t_obj_c, 4);
  pkt[12] = crc8(&pkt[2], 10);
  pkt[13] = '\n';
  Serial.write(pkt, sizeof(pkt));
  seq++;
  last_temp_for_display = t_obj_c;
}

void setup() {
  auto cfg = M5.config();
  StickCP2.begin(cfg);
  StickCP2.Display.setRotation(3);
  StickCP2.Display.setTextSize(2);
  StickCP2.Display.fillScreen(BLACK);
  StickCP2.Display.setCursor(0, 0);
  StickCP2.Display.println("Temp streamer");

  Serial.begin(SERIAL_BAUD);
  while (!Serial && millis() < 2000) {}

  Wire.begin(I2C_SDA, I2C_SCL, I2C_HZ);
  if (!mlx.begin(0x5A, &Wire)) {
    StickCP2.Display.fillScreen(RED);
    StickCP2.Display.setCursor(0, 0);
    StickCP2.Display.println("MLX90614");
    StickCP2.Display.println("NOT FOUND");
    while (true) { delay(1000); }
  }

  StickCP2.Display.println("OK 1 Hz");
  last_emit_ms = millis();
}

void loop() {
  StickCP2.update();

  uint32_t now_ms = millis();
  if (now_ms - last_emit_ms >= EMIT_PERIOD_MS) {
    last_emit_ms = now_ms;
    float t_obj_c = mlx.readObjectTempC();
    if (!isnan(t_obj_c)) {
      emit_packet(t_obj_c);
    }
    StickCP2.Display.fillScreen(BLACK);
    StickCP2.Display.setCursor(0, 0);
    StickCP2.Display.printf("seq %u\n", seq);
    if (!isnan(last_temp_for_display)) {
      StickCP2.Display.printf("T %.2f C\n", last_temp_for_display);
    } else {
      StickCP2.Display.println("T --");
    }
  }
}
