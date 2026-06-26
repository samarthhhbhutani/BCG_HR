// i2c_scanner_xiao.ino — diagnostic for any I²C device on the XIAO nRF52840.
//
// Scans every 7-bit I²C address on D4 (SDA) / D5 (SCL) and prints whatever
// responds. Use this when "[mlx90614] NOT FOUND at 0x5A" appears, or when any
// new I²C breakout doesn't show up where expected.
//
// Wiring (XIAO → breakout under test):
//   3V3 → VIN
//   GND → GND
//   D4  → SDA
//   D5  → SCL
//
// Workflow:
//   1. Flash this sketch (Arduino IDE → Upload).
//   2. Open Serial Monitor at 115200.
//   3. Single-tap reset to re-run the scan.
//   4. Read the result:
//        "0x5A"          → MLX90614 found at default address — re-flash
//                          temp_streamer_xiao.ino, the original failure was
//                          likely a boot-time race or transient.
//        "0x68" or "0x69" → BMI330 — wrong sketch loaded?
//        "0xXX"          → device at non-default address; tell me the value.
//        "NO DEVICES"    → bus is dead — wiring or pull-up problem.

#include <Wire.h>

void setup() {
  Serial.begin(115200);
  while (!Serial && millis() < 3000) {}

  // Internal weak pull-ups (~13 kΩ) — rescue boards lacking external pull-ups.
  // Real designs should add 4.7 kΩ external. Safe to leave on regardless.
  pinMode(4, INPUT_PULLUP);   // D4 = SDA
  pinMode(5, INPUT_PULLUP);   // D5 = SCL

  Wire.begin();
  Wire.setClock(100000);      // 100 kHz — slow and forgiving
  delay(200);

  Serial.println("Scanning I2C bus (D4=SDA, D5=SCL, 100 kHz, internal pull-ups)...");
  int found = 0;
  for (uint8_t addr = 1; addr < 127; addr++) {
    Wire.beginTransmission(addr);
    uint8_t err = Wire.endTransmission();
    if (err == 0) {
      Serial.print("  Device at 0x");
      if (addr < 0x10) Serial.print("0");
      Serial.println(addr, HEX);
      found++;
    }
  }
  if (found == 0) {
    Serial.println("No I2C devices found.");
    Serial.println("Likely causes:");
    Serial.println("  - VIN connected to 5V (not 3V3) — sensor brownout");
    Serial.println("  - SDA/SCL swapped");
    Serial.println("  - GND not connected");
    Serial.println("  - Missing pull-up resistors (need 4.7 kΩ on SDA & SCL to 3V3)");
    Serial.println("  - Damaged sensor");
  } else {
    Serial.print("Done. ");
    Serial.print(found);
    Serial.println(" device(s) found.");
  }
}

void loop() {
  delay(1000);
}
