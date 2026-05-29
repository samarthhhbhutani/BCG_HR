// ppg_streamer.ino
// M5StickC PLUS2 + MAX30102 — stream raw PPG (IR + Red) at 200 Hz over USB serial.
//
// Wiring (M5StickC PLUS2 → MAX30102 breakout):
//   G0  -> MAX30102 SDA
//   G26 -> MAX30102 SCL
//   5V  -> MAX30102 VIN
//   GND -> MAX30102 GND
//
// Library required: SparkFun MAX3010x Pulse and Proximity Sensor Library
//   Arduino IDE → Library Manager → search "SparkFun MAX3010x" → install.
//
// Sampling design:
//   The sensor itself is the rate source. We configure it for 200 Hz output
//   (internal 400 Hz / sampleAverage 2) and the firmware just drains the FIFO,
//   emitting one packet per real reading. No esp_timer — that approach
//   produced phantom 200 Hz packets while the FIFO actually delivered ~58 Hz,
//   and timestamps were bursty.
//
// Packet layout (little-endian, 18 bytes total):
//   [0xAA][0x55]              sync
//   [seq u16]                 wraps at 65535
//   [t_us u32]                esp_timer_get_time() lower 32 bits at moment of read
//   [ir  u32]                 raw IR count
//   [red u32]                 raw Red count
//   [crc8]                    CRC8/Dallas over bytes [2..15]
//   [0x0A]                    '\n' framing aid

#include <M5StickCPlus2.h>
#include <Wire.h>
#include "MAX30105.h"   // SparkFun lib (works for MAX30102 too)
#include "esp_timer.h"

static const uint32_t SERIAL_BAUD = 115200;
static const int      I2C_SDA     = 0;
static const int      I2C_SCL     = 26;
static const uint32_t I2C_HZ      = 400000;

MAX30105 ppg;

static uint16_t seq = 0;
static uint32_t last_health_ms = 0;
static uint32_t samples_since_health = 0;
static uint32_t last_ir_for_display = 0;

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

static void config_ppg() {
  // setup(powerLevel, sampleAverage, ledMode, sampleRate, pulseWidth, adcRange)
  //   sampleRate    400  Hz internal
  //   sampleAverage 2    →  effective output = 400/2 = 200 Hz, matches PC pipeline.
  //   ledMode       2    Red + IR (no green on MAX30102)
  //   pulseWidth    411  µs → 18-bit ADC resolution
  //   adcRange      4096 nA full scale
  //   powerLevel    0x1F ~6.4 mA per LED, mid-range; raise for thicker fingers.
  ppg.setup(0x1F, 2, 2, 400, 411, 4096);
  ppg.setPulseAmplitudeRed(0x1F);
  ppg.setPulseAmplitudeIR(0x1F);
}

static void emit_packet(uint32_t ir, uint32_t red) {
  uint32_t t_us = (uint32_t)(esp_timer_get_time() & 0xFFFFFFFFu);
  uint8_t pkt[18];
  pkt[0] = 0xAA; pkt[1] = 0x55;
  memcpy(&pkt[2],  &seq,  2);
  memcpy(&pkt[4],  &t_us, 4);
  memcpy(&pkt[8],  &ir,   4);
  memcpy(&pkt[12], &red,  4);
  pkt[16] = crc8(&pkt[2], 14);
  pkt[17] = '\n';
  Serial.write(pkt, sizeof(pkt));
  seq++;
  samples_since_health++;
  last_ir_for_display = ir;
}

void setup() {
  auto cfg = M5.config();
  StickCP2.begin(cfg);
  StickCP2.Display.setRotation(3);
  StickCP2.Display.setTextSize(2);
  StickCP2.Display.fillScreen(BLACK);
  StickCP2.Display.setCursor(0, 0);
  StickCP2.Display.println("PPG streamer");

  Serial.begin(SERIAL_BAUD);
  while (!Serial && millis() < 2000) {}

  Wire.begin(I2C_SDA, I2C_SCL, I2C_HZ);
  if (!ppg.begin(Wire, I2C_SPEED_FAST)) {
    StickCP2.Display.fillScreen(RED);
    StickCP2.Display.setCursor(0, 0);
    StickCP2.Display.println("MAX30102");
    StickCP2.Display.println("NOT FOUND");
    while (true) { delay(1000); }
  }
  config_ppg();

  StickCP2.Display.println("OK 200 Hz");
  last_health_ms = millis();
}

void loop() {
  StickCP2.update();

  // Drain whatever the sensor has queued, ONE packet per FIFO entry. No fake
  // timer ticks — the sensor's own rate dictates the output rate.
  while (ppg.available()) {
    uint32_t ir  = ppg.getFIFOIR();
    uint32_t red = ppg.getFIFORed();
    ppg.nextSample();
    emit_packet(ir, red);
  }

  // Pull new readings into the local FIFO. check() reads however many entries
  // are available since the last call; the while() above will then emit them.
  ppg.check();

  uint32_t now_ms = millis();
  if (now_ms - last_health_ms >= 1000) {
    last_health_ms = now_ms;
    StickCP2.Display.fillScreen(BLACK);
    StickCP2.Display.setCursor(0, 0);
    StickCP2.Display.printf("seq %u\n", seq);
    StickCP2.Display.printf("%lu /s\n", (unsigned long)samples_since_health);
    StickCP2.Display.printf("IR %lu\n", (unsigned long)last_ir_for_display);
    samples_since_health = 0;
  }
}
