#include <M5StickCPlus2.h>

#define SAMPLE_RATE 200
#define WINDOW_SIZE 800  // 4 seconds

float samples[WINDOW_SIZE];
int sampleIdx = 0;
unsigned long lastSampleTime = 0;
float displayBPM = 0;

// DC removal
float dcFilter(float input) {
  static float w = 0;
  float result = input - w;
  w = w + 0.005 * result;
  return result;
}

// Smooth signal
float smooth(float input) {
  static float buf[16] = {0};
  static int idx = 0;
  buf[idx] = input;
  idx = (idx + 1) % 16;
  float sum = 0;
  for (int i = 0; i < 16; i++) sum += buf[i];
  return sum / 16.0;
}

float getBPM(float* data, int len) {
  float mean = 0;
  for (int i = 0; i < len; i++) mean += data[i];
  mean /= len;

  float var = 0;
  for (int i = 0; i < len; i++)
    var += (data[i] - mean) * (data[i] - mean);
  float stddev = sqrt(var / len);

  Serial.printf("StdDev: %.6f\n", stddev);

  if (stddev < 0.00005) {
    Serial.println("Signal too flat — press harder");
    return 0;
  }

  float threshold = mean + 0.3 * stddev;

  // 170 samples minimum = ~70 BPM max
  // Prevents double counting cardiac signal
  int minDist = 170;

  int peaks[20];
  int peakCount = 0;

  for (int i = 1; i < len - 1 && peakCount < 20; i++) {
    if (data[i] > threshold &&
        data[i] > data[i-1] &&
        data[i] > data[i+1]) {
      if (peakCount == 0 || (i - peaks[peakCount-1]) >= minDist) {
        peaks[peakCount++] = i;
      }
    }
  }

  Serial.printf("Valid peaks (min 170 sample gap): %d\n", peakCount);

  if (peakCount < 3) {
    Serial.println("Not enough peaks");
    return 0;
  }

  // Average interval between peaks
  float totalInterval = 0;
  int count = 0;
  for (int i = 1; i < peakCount; i++) {
    int interval = peaks[i] - peaks[i-1];
    Serial.printf("  Peak interval %d: %d samples = %.1f BPM\n",
                  i, interval, (200.0*60.0)/interval);
    // Valid: 40-80 BPM = 150-300 samples
    if (interval >= 150 && interval <= 300) {
      totalInterval += interval;
      count++;
    }
  }

  if (count == 0) {
    Serial.println("No valid intervals in 40-80 BPM range");
    return 0;
  }

  float avgInterval = totalInterval / count;
  float bpm = (SAMPLE_RATE * 60.0) / avgInterval;
  Serial.printf("Final BPM: %.1f\n", bpm);
  return bpm;
}

void setup() {
  auto cfg = M5.config();
  StickCP2.begin(cfg);
  StickCP2.Display.setRotation(3);
  StickCP2.Display.setTextSize(2);
  StickCP2.Imu.begin();
  Serial.begin(115200);
  Serial.println("PawSense BCG Ready");
}

void loop() {
  StickCP2.update();
  unsigned long now = micros();

  if (now - lastSampleTime >= 5000) {
    lastSampleTime = now;

    float ax, ay, az;
    StickCP2.Imu.getAccel(&ax, &ay, &az);

    float mag = sqrt(ax*ax + ay*ay + az*az);
    float filtered = smooth(dcFilter(mag));
    samples[sampleIdx++] = filtered;

    if (sampleIdx >= WINDOW_SIZE) {
      sampleIdx = 0;

      // Calculate stddev first to check if on body
      float mean = 0;
      for (int i = 0; i < WINDOW_SIZE; i++) mean += samples[i];
      mean /= WINDOW_SIZE;
      float var = 0;
      for (int i = 0; i < WINDOW_SIZE; i++)
        var += (samples[i] - mean) * (samples[i] - mean);
      float stddev = sqrt(var / WINDOW_SIZE);

      Serial.println("\n===== Computing BPM =====");

      // Only compute if signal strong enough
      // meaning device is pressed against body
      if (stddev > 0.0008) {
        displayBPM = getBPM(samples, WINDOW_SIZE);
        Serial.printf(">>> RESULT: %.1f BPM <<<\n", displayBPM);
      } else {
        displayBPM = 0;
        Serial.println("Not on body — signal too weak");
        Serial.printf("StdDev was: %.6f\n", stddev);
      }
    }
  }

  // Update display every 500ms
  static unsigned long lastDisp = 0;
  if (millis() - lastDisp > 500) {
    lastDisp = millis();
    StickCP2.Display.fillScreen(BLACK);
    StickCP2.Display.setCursor(0, 5);

    if (displayBPM > 0) {
      StickCP2.Display.setTextColor(WHITE);
      StickCP2.Display.setTextSize(2);
      StickCP2.Display.println("PAWSENSE");
      StickCP2.Display.println("HEART RATE");
      StickCP2.Display.println("");
      StickCP2.Display.setTextColor(GREEN);
      StickCP2.Display.setTextSize(4);
      StickCP2.Display.printf("%.0f\n", displayBPM);
      StickCP2.Display.setTextSize(2);
      StickCP2.Display.setTextColor(WHITE);
      StickCP2.Display.println("BPM");
    } else {
      StickCP2.Display.setTextColor(WHITE);
      StickCP2.Display.println("PAWSENSE");
      StickCP2.Display.println("");
      StickCP2.Display.setTextColor(YELLOW);
      StickCP2.Display.println("PRESS FIRM");
      StickCP2.Display.println("ON CHEST");
      StickCP2.Display.println("BARE SKIN");
    }
  }
}