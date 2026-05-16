// hr_streamer.ino
// M5StickC PLUS2 — stream MPU6886 IMU at 200 Hz over USB serial.
// Binary framed packets so the PC can detect drops via seq # and corruption via CRC8.
//
// Packet layout (little-endian, 22 bytes total):
//   [0xAA][0x55]              sync
//   [seq u16]                 wraps at 65535
//   [t_us u32]                esp_timer_get_time() lower 32 bits
//   [ax i16][ay i16][az i16]  raw accel counts
//   [gx i16][gy i16][gz i16]  raw gyro counts
//   [crc8]                    CRC8/Dallas over bytes [2..19]
//
// Convert on PC: accel_g = raw * (FS_ACCEL_G / 32768.0)
//                gyro_dps = raw * (FS_GYRO_DPS / 32768.0)
// FS_ACCEL_G defaults to 4 (M5Unified default), FS_GYRO_DPS to 1000.

#include <M5StickCPlus2.h>
#include "esp_timer.h"

static const uint32_t SAMPLE_PERIOD_US = 5000;  // 200 Hz
static const uint32_t SERIAL_BAUD = 115200;

static volatile bool tick = false;
static esp_timer_handle_t sample_timer;

static uint16_t seq = 0;
static uint32_t last_health_ms = 0;
static uint32_t samples_since_health = 0;

static void IRAM_ATTR on_sample_timer(void*) {
  tick = true;
}

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

static inline int16_t to_i16(float v, float scale) {
  float s = v * scale;
  if (s >  32767.0f) s =  32767.0f;
  if (s < -32768.0f) s = -32768.0f;
  return (int16_t)s;
}

void setup() {
  auto cfg = M5.config();
  StickCP2.begin(cfg);
  StickCP2.Display.setRotation(3);
  StickCP2.Display.setTextSize(2);
  StickCP2.Display.fillScreen(BLACK);
  StickCP2.Display.setCursor(0, 0);
  StickCP2.Display.println("HR streamer");
  StickCP2.Display.println("200 Hz");

  StickCP2.Imu.begin();

  Serial.begin(SERIAL_BAUD);
  while (!Serial && millis() < 2000) {}

  const esp_timer_create_args_t targs = {
    .callback = &on_sample_timer,
    .arg = nullptr,
    .dispatch_method = ESP_TIMER_TASK,
    .name = "imu_tick",
    .skip_unhandled_events = true,
  };
  esp_timer_create(&targs, &sample_timer);
  esp_timer_start_periodic(sample_timer, SAMPLE_PERIOD_US);

  last_health_ms = millis();
}

void loop() {
  StickCP2.update();

  if (tick) {
    tick = false;

    float ax_g, ay_g, az_g, gx_dps, gy_dps, gz_dps;
    StickCP2.Imu.getAccel(&ax_g, &ay_g, &az_g);
    StickCP2.Imu.getGyro(&gx_dps, &gy_dps, &gz_dps);

    // Pack to int16 using full-scale assumptions; the PC reverses with the same scale.
    const float FS_ACCEL_G = 4.0f;
    const float FS_GYRO_DPS = 1000.0f;
    int16_t ax = to_i16(ax_g, 32768.0f / FS_ACCEL_G);
    int16_t ay = to_i16(ay_g, 32768.0f / FS_ACCEL_G);
    int16_t az = to_i16(az_g, 32768.0f / FS_ACCEL_G);
    int16_t gx = to_i16(gx_dps, 32768.0f / FS_GYRO_DPS);
    int16_t gy = to_i16(gy_dps, 32768.0f / FS_GYRO_DPS);
    int16_t gz = to_i16(gz_dps, 32768.0f / FS_GYRO_DPS);

    uint32_t t_us = (uint32_t)(esp_timer_get_time() & 0xFFFFFFFFu);

    uint8_t pkt[22];
    pkt[0] = 0xAA; pkt[1] = 0x55;
    memcpy(&pkt[2],  &seq,   2);
    memcpy(&pkt[4],  &t_us,  4);
    memcpy(&pkt[8],  &ax, 2);
    memcpy(&pkt[10], &ay, 2);
    memcpy(&pkt[12], &az, 2);
    memcpy(&pkt[14], &gx, 2);
    memcpy(&pkt[16], &gy, 2);
    memcpy(&pkt[18], &gz, 2);
    pkt[20] = crc8(&pkt[2], 18);
    pkt[21] = '\n';  // makes the stream easier to eyeball; PC ignores it

    Serial.write(pkt, sizeof(pkt));

    seq++;
    samples_since_health++;
  }

  uint32_t now_ms = millis();
  if (now_ms - last_health_ms >= 1000) {
    last_health_ms = now_ms;
    StickCP2.Display.fillScreen(BLACK);
    StickCP2.Display.setCursor(0, 0);
    StickCP2.Display.printf("seq %u\n", seq);
    StickCP2.Display.printf("%lu /s\n", (unsigned long)samples_since_health);
    samples_since_health = 0;
  }
}
