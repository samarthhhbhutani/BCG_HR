// hr_streamer_bmi330.ino
// XIAO nRF52840 + 7SEMI BMI330 Nano — 6-axis IMU streamer over native USB CDC.
//
// Replaces the M5StickC PLUS2 hr_streamer.ino. Wire format is byte-identical
// so protocol.py / receiver.py / hr_worker.py / app.py work unchanged.
//
// Wiring (XIAO → BMI330 Nano):
//   3V3 → VIN, GND → GND, D4 → SDA, D5 → SCL.
//
// Sample-rate strategy: BMI330 internal ODR = 200 Hz. We poll STATUS at 1 kHz
// and emit a packet only when both drdy_acc and drdy_gyr are set. Chip is the
// rate source — no phantom samples (lesson learned from the PPG firmware).
//
// Wire packet (little-endian, 22 bytes):
//   [0xAA][0x55] sync
//   [seq u16][t_us u32]
//   [ax i16][ay i16][az i16]   FS = ±4 g
//   [gx i16][gy i16][gz i16]   FS = ±1000 dps
//   [crc8 over bytes 2..19]
//   [0x0A]                     framing aid (parser ignores)

#include <Wire.h>

// ===== BMI330 register map (verify against BST-BMI330-DS000) =====
// BMI3xx registers are word-addressed; reads/writes are in 2-byte units.
static constexpr uint8_t REG_CHIP_ID         = 0x00;  // expect 0x47 (BMI330)
static constexpr uint8_t REG_STATUS          = 0x02;  // bit 7 = drdy_acc, bit 6 = drdy_gyr
static constexpr uint8_t REG_ACC_DATA_X      = 0x03;  // 12 bytes total: 6 accel + 6 gyro
static constexpr uint8_t REG_ACC_CONF        = 0x20;  // 16-bit config word
static constexpr uint8_t REG_GYR_CONF        = 0x21;
static constexpr uint8_t REG_CMD             = 0x7E;  // write 0xDEAF for soft-reset

static constexpr uint8_t EXPECTED_CHIP_ID    = 0x47;  // BMI330; if firmware prints
                                                       // a different value, update this.

// ACC_CONF / GYR_CONF 16-bit layout:
//   bits  3:0  ODR        (0x09 = 200 Hz)
//   bits  6:4  range      (acc 0x01=±4g; gyr 0x03=±1000dps)
//   bit   7    bw         (0 = ODR/2 acc_avg, 1 = ODR/4 LP)
//   bits 10:8  avg        (0 = no averaging)
//   bits 14:12 mode       (7 = high-performance)
//
// 200 Hz, ±4 g, ODR/2 BW, no avg, hp:  mode<<12 | range<<4 | odr = 0x7019
// 200 Hz, ±1000 dps, ODR/2 BW, no avg, hp:                       = 0x7039
static constexpr uint16_t ACC_CONF_VALUE     = 0x7019;
static constexpr uint16_t GYR_CONF_VALUE     = 0x7039;

// Chip's internal ODR encoding for BMI330 differs from the BMI323 table; with
// ACC_CONF_VALUE=0x7019 the chip ran at ~1024 Hz on first-light testing, which
// at 1 kHz poll yielded 512 Hz observed (PC pipeline assumes 200 Hz → HR off
// by 2.56×). Until we pin down the exact BMI330 ODR table, gate the loop at
// 5 ms so we emit exactly 200 Hz regardless of how fast the chip refills.
// Decimation aliasing isn't a concern here — BCG energy is < 25 Hz, well
// below the 100 Hz post-decimation Nyquist.
static const uint32_t POLL_PERIOD_US = 5000;  // 200 Hz emission rate

static uint8_t  bmi_addr = 0x68;
static uint16_t seq = 0;
static uint32_t last_poll_us = 0;
static uint32_t last_health_ms = 0;
static uint32_t samples_since_health = 0;

// ----- I²C helpers -----
static bool i2c_write_u16(uint8_t reg, uint16_t v) {
  Wire.beginTransmission(bmi_addr);
  Wire.write(reg);
  Wire.write((uint8_t)(v & 0xFF));
  Wire.write((uint8_t)((v >> 8) & 0xFF));
  return Wire.endTransmission() == 0;
}

// BMI3xx I²C reads return 2 dummy bytes before the real data. This is
// documented in Bosch's SensorAPI as dev->dummy_byte = 2 for I²C mode.
// Without this, every read returns 0x00 in the first byte (the dummy).
static constexpr size_t BMI3_DUMMY_BYTES = 2;

static bool i2c_read(uint8_t reg, uint8_t* dst, size_t n) {
  Wire.beginTransmission(bmi_addr);
  Wire.write(reg);
  if (Wire.endTransmission(false) != 0) return false;  // repeated start
  size_t total = n + BMI3_DUMMY_BYTES;
  size_t got = Wire.requestFrom((int)bmi_addr, (int)total);
  if (got != total) return false;
  for (size_t i = 0; i < BMI3_DUMMY_BYTES; i++) Wire.read();  // discard
  for (size_t i = 0; i < n; i++) dst[i] = Wire.read();
  return true;
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

static bool detect_address() {
  static const uint8_t candidates[] = {0x68, 0x69};
  for (size_t i = 0; i < sizeof(candidates); i++) {
    Wire.beginTransmission(candidates[i]);
    if (Wire.endTransmission() == 0) {
      bmi_addr = candidates[i];
      return true;
    }
  }
  return false;
}

static bool bmi330_init() {
  // BMI3xx boots in SPI mode; any I²C transaction switches it to I²C.
  uint8_t dummy = 0;
  i2c_read(REG_CHIP_ID, &dummy, 1);
  delay(2);

  i2c_write_u16(REG_CMD, 0xDEAF);  // soft reset
  delay(50);

  uint8_t id = 0;
  if (!i2c_read(REG_CHIP_ID, &id, 1)) {
    Serial.println("[bmi330] CHIP_ID read failed");
    return false;
  }
  Serial.print("[bmi330] CHIP_ID = 0x"); Serial.println(id, HEX);
  if (id != EXPECTED_CHIP_ID) {
    Serial.print("[bmi330] WARNING: expected 0x");
    Serial.println(EXPECTED_CHIP_ID, HEX);
  }

  if (!i2c_write_u16(REG_ACC_CONF, ACC_CONF_VALUE)) return false;
  if (!i2c_write_u16(REG_GYR_CONF, GYR_CONF_VALUE)) return false;
  delay(10);
  return true;
}

void setup() {
  Serial.begin(115200);                       // virtual baud on native USB CDC
  while (!Serial && millis() < 3000) {}

  Wire.begin();
  Wire.setClock(400000);                      // I²C Fast Mode
  delay(50);                                  // BMI330 power-up settle

  if (!detect_address()) {
    Serial.println("[bmi330] no I²C device at 0x68 or 0x69 — check wiring");
  } else {
    Serial.print("[bmi330] found at 0x"); Serial.println(bmi_addr, HEX);
  }

  if (!bmi330_init()) {
    Serial.println("[bmi330] init failed");
  } else {
    Serial.println("[bmi330] streaming...");
  }

  last_health_ms = millis();
  last_poll_us = micros();
}

void loop() {
  uint32_t now_us = micros();
  if ((int32_t)(now_us - last_poll_us) < (int32_t)POLL_PERIOD_US) return;

  // Deadline-based scheduling: advance by exactly POLL_PERIOD_US so per-loop
  // work (~860 µs of I²C reads + USB writes) doesn't push the next sample
  // late. Without this, observed fs was 170 Hz instead of 200 Hz. If we ever
  // fall more than 2 periods behind (USB stall, etc.), snap forward.
  last_poll_us += POLL_PERIOD_US;
  if ((int32_t)(now_us - last_poll_us) > (int32_t)(2 * POLL_PERIOD_US)) {
    last_poll_us = now_us;
  }

  uint8_t status = 0;
  if (!i2c_read(REG_STATUS, &status, 1)) return;
  if (!((status & 0x80) && (status & 0x40))) return;  // need drdy_acc AND drdy_gyr

  uint8_t buf[12];
  if (!i2c_read(REG_ACC_DATA_X, buf, 12)) return;

  uint32_t t_us = micros();

  // BMI330 emits each axis as little-endian int16 — already matches our wire
  // format, so the 12 data bytes drop straight into pkt[8..19] verbatim.
  uint8_t pkt[22];
  pkt[0] = 0xAA; pkt[1] = 0x55;
  memcpy(&pkt[2], &seq,  2);
  memcpy(&pkt[4], &t_us, 4);
  memcpy(&pkt[8],  buf,  12);
  pkt[20] = crc8(&pkt[2], 18);
  pkt[21] = '\n';
  Serial.write(pkt, 22);

  seq++;
  samples_since_health++;

  uint32_t now_ms = millis();
  if (now_ms - last_health_ms >= 1000) {
    last_health_ms = now_ms;
    samples_since_health = 0;
  }
}
