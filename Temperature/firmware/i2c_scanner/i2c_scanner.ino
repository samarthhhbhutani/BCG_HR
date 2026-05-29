// i2c_scanner.ino — diagnostic for the MLX90614 (or any I2C device).
//
// Scans every 7-bit I2C address on the same bus the temp_streamer uses
// (G0=SDA, G26=SCL) and prints whatever responds. Use this when
// "MLX90614 NOT FOUND" appears on the M5 screen.
//
// Wiring (M5StickC PLUS2 → MLX90614):
//   G0  → SDA
//   G26 → SCL
//   3V3 → VIN   (NOT 5V — MLX90614 is 3.3 V only)
//   GND → GND
//
// Workflow:
//   1. Flash this sketch (Arduino IDE → Upload).
//   2. Unplug, replug USB.
//   3. Read the M5 screen. It'll show one of:
//        "0x5A"          → MLX90614 found at default address — re-flash
//                          temp_streamer.ino, the original failure was a
//                          boot-time race, unplug/replug after flashing.
//        "0xXX"          → device at non-default address; tell me the value
//                          and I'll patch the firmware.
//        "NO DEVICES"    → bus is dead — wiring or hardware problem
//                          (see Temperature/README troubleshooting).
//   4. Optional: open Arduino Serial Monitor at 115200 baud for the same
//      info plus a "Scanning..." header.

#include <M5StickCPlus2.h>
#include <Wire.h>

static const int      I2C_SDA = 0;
static const int      I2C_SCL = 26;
static const uint32_t I2C_HZ  = 10000;   // 10 kHz — slow enough for ESP32
                                         // internal pull-ups to maybe carry the bus.
                                         // Default is 100 kHz which needs proper
                                         // external 4.7 kΩ pull-ups.

void setup() {
  auto cfg = M5.config();
  StickCP2.begin(cfg);
  StickCP2.Display.setRotation(3);
  StickCP2.Display.setTextSize(2);
  StickCP2.Display.fillScreen(BLACK);
  StickCP2.Display.setCursor(0, 0);
  StickCP2.Display.println("I2C scan");

  Serial.begin(115200);
  while (!Serial && millis() < 2000) {}

  // Enable ESP32 internal pull-ups before Wire.begin() takes over the pins.
  // These are weak (~45 kΩ) so they only work at low bus speeds with short
  // wires, but it's free to try and may rescue an MLX breakout that lacks
  // its own pull-up resistors.
  pinMode(I2C_SDA, INPUT_PULLUP);
  pinMode(I2C_SCL, INPUT_PULLUP);

  Wire.begin(I2C_SDA, I2C_SCL, I2C_HZ);
  delay(200);

  Serial.println("Scanning I2C bus (G0=SDA, G26=SCL, 10 kHz, internal pull-ups)...");
  int found = 0;
  for (uint8_t addr = 1; addr < 127; addr++) {
    Wire.beginTransmission(addr);
    uint8_t err = Wire.endTransmission();
    if (err == 0) {
      Serial.printf("Device at 0x%02X\n", addr);
      StickCP2.Display.printf("0x%02X\n", addr);
      found++;
    }
  }
  if (found == 0) {
    Serial.println("No I2C devices found.");
    StickCP2.Display.fillScreen(RED);
    StickCP2.Display.setCursor(0, 0);
    StickCP2.Display.setTextSize(2);
    StickCP2.Display.println("NO DEVICES");
  } else {
    Serial.printf("Done. %d device(s).\n", found);
  }
}

void loop() {
  // Done — keep the screen visible.
  delay(1000);
}
