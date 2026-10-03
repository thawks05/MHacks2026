/*
 * pod.ino — GY-6500 (MPU-6500) vibration pod -> ESP-NOW -> host
 * Board: "ESP32 Dev Module" (arduino-esp32 core 2.x or 3.x)
 *
 * Reads accel X/Y/Z from the sensor FIFO at 1 kHz (sensor-clocked, so evenly
 * spaced), packs 32 raw int16 samples per packet, sends to the host.
 * USB serial (115200) prints a 1 Hz status line for debugging.
 */
#include <Arduino.h>
#include <Wire.h>
#include <WiFi.h>
#include <esp_wifi.h>
#include <esp_now.h>

// ======================= user config =======================
#define POD_ID          1
#define ESPNOW_CHANNEL  1          // must match host.ino

// Paste the MAC the host prints at boot. All 0xFF = broadcast (works, but no ACK/retries).
uint8_t HOST_MAC[6] = {0xB0, 0xCB, 0xD8, 0xCF, 0xFC, 0x40};

#define I2C_SDA   21
#define I2C_SCL   22
#define I2C_HZ    400000
// Strap pins driven by GPIO instead of tying to GND/3V3. Set to -1 if hard-wired or unconnected.
#define PIN_AD0   18               // driven LOW  -> I2C address 0x68
#define PIN_NCS   19               // driven HIGH -> stay in I2C mode (LOW = SPI)

static uint8_t mpuAddr = 0x68;     // auto-detected in setup (0x68 or 0x69)

#define ACCEL_RANGE_G  4           // 2, 4, 8 or 16
#define FS_HZ          1000        // set by the register config below

// ============ shared packet (keep IDENTICAL in host.ino) ============
#define PKT_MAGIC        0xA5
#define PKT_VERSION      1
#define SAMPLES_PER_PKT  32
#define FLAG_DATA_GAP    0x01      // FIFO overflow / read error before this packet

typedef struct __attribute__((packed)) {
  uint8_t  magic;
  uint8_t  version;
  uint8_t  pod_id;
  uint8_t  n_samples;
  uint8_t  range_g;
  uint8_t  flags;
  uint16_t fs_hz;
  uint32_t seq;                    // packet counter
  uint32_t first_idx;              // sample index of xyz[0] since pod boot
  int16_t  xyz[SAMPLES_PER_PKT][3];// raw counts, native (little-endian) order
} AccelPacket;                     // 16 + 192 = 208 bytes
static_assert(sizeof(AccelPacket) <= 250, "packet too big for ESP-NOW");

// ======================= MPU-6500 registers =======================
#define REG_SMPLRT_DIV    0x19
#define REG_CONFIG        0x1A
#define REG_ACCEL_CONFIG  0x1C
#define REG_ACCEL_CONFIG2 0x1D
#define REG_FIFO_EN       0x23
#define REG_INT_ENABLE    0x38
#define REG_INT_STATUS    0x3A
#define REG_USER_CTRL     0x6A
#define REG_PWR_MGMT_1    0x6B
#define REG_PWR_MGMT_2    0x6C
#define REG_FIFO_COUNTH   0x72
#define REG_FIFO_R_W      0x74
#define REG_WHO_AM_I      0x75

#define MAX_FRAMES_PER_READ 20     // 20 * 6 = 120 bytes, under the 128-byte Wire buffer

// ======================= state =======================
static AccelPacket pkt;
static uint8_t  fill = 0;
static uint32_t seq = 0, sampleIdx = 0;
static uint8_t  pendingFlags = 0;
static uint32_t sentOk = 0, sendErr = 0, overflows = 0, i2cErr = 0;
static int16_t  lastXYZ[3] = {0, 0, 0};

// ======================= I2C helpers =======================
static bool writeReg(uint8_t reg, uint8_t val) {
  Wire.beginTransmission(mpuAddr);
  Wire.write(reg);
  Wire.write(val);
  return Wire.endTransmission() == 0;
}

static bool readRegs(uint8_t reg, uint8_t *buf, uint8_t len) {
  Wire.beginTransmission(mpuAddr);
  Wire.write(reg);
  if (Wire.endTransmission(false) != 0) return false;            // repeated start
  if (Wire.requestFrom(mpuAddr, len) != len) return false;
  for (uint8_t i = 0; i < len; i++) buf[i] = Wire.read();
  return true;
}

static void i2cScan() {
  Serial.println("I2C scan:");
  for (uint8_t a = 1; a < 127; a++) {
    Wire.beginTransmission(a);
    if (Wire.endTransmission() == 0) Serial.printf("  found device at 0x%02X\n", a);
  }
}

static uint8_t rangeBits(int g) {
  switch (g) {
    case 2:  return 0x00;
    case 4:  return 0x08;
    case 8:  return 0x10;
    default: return 0x18;          // 16 g
  }
}

// ======================= MPU setup =======================
static void fifoReset() {
  writeReg(REG_FIFO_EN, 0x00);
  writeReg(REG_USER_CTRL, 0x04);   // FIFO_RST
  delay(1);
  writeReg(REG_USER_CTRL, 0x40);   // FIFO_EN
  writeReg(REG_FIFO_EN, 0x08);     // accel X/Y/Z -> FIFO (6 bytes per sample)
}

static bool mpuInit() {
  if (!writeReg(REG_PWR_MGMT_1, 0x80)) return false;  // device reset
  delay(100);
  writeReg(REG_PWR_MGMT_1, 0x01);  // wake, auto-select clock
  delay(10);

  uint8_t who = 0;
  if (!readRegs(REG_WHO_AM_I, &who, 1)) return false;
  Serial.printf("WHO_AM_I = 0x%02X  (MPU-6500 = 0x70; 0x71/0x73 = MPU-9250/9255 clone, also OK)\n", who);

  writeReg(REG_PWR_MGMT_2, 0x07);  // accel on, gyro off
  writeReg(REG_CONFIG, 0x41);      // FIFO_MODE=1 (stop when full), DLPF_CFG=1 -> 1 kHz internal rate
  writeReg(REG_SMPLRT_DIV, 0x00);  // 1 kHz / (1 + 0) = 1 kHz into FIFO
  writeReg(REG_ACCEL_CONFIG, rangeBits(ACCEL_RANGE_G));
  writeReg(REG_ACCEL_CONFIG2, 0x00); // accel DLPF: 460 Hz bandwidth @ 1 kHz (0x01 = 184 Hz if you see aliasing)
  writeReg(REG_INT_ENABLE, 0x10);  // record FIFO overflow in INT_STATUS
  fifoReset();
  return true;
}

// ======================= ESP-NOW =======================
static void espNowInit() {
  WiFi.mode(WIFI_STA);
  WiFi.disconnect();
  esp_wifi_set_ps(WIFI_PS_NONE);
  esp_wifi_set_channel(ESPNOW_CHANNEL, WIFI_SECOND_CHAN_NONE);

  if (esp_now_init() != ESP_OK) {
    Serial.println("esp_now_init FAILED");
    while (true) delay(1000);
  }

  esp_now_peer_info_t peer = {};
  memcpy(peer.peer_addr, HOST_MAC, 6);
  peer.channel = ESPNOW_CHANNEL;
  peer.ifidx   = WIFI_IF_STA;
  peer.encrypt = false;
  if (esp_now_add_peer(&peer) != ESP_OK) Serial.println("esp_now_add_peer FAILED");

  uint8_t mac[6];
  esp_wifi_get_mac(WIFI_IF_STA, mac);
  Serial.printf("Pod %d MAC %02X:%02X:%02X:%02X:%02X:%02X -> host %02X:%02X:%02X:%02X:%02X:%02X, ch %d\n",
                POD_ID, mac[0], mac[1], mac[2], mac[3], mac[4], mac[5],
                HOST_MAC[0], HOST_MAC[1], HOST_MAC[2], HOST_MAC[3], HOST_MAC[4], HOST_MAC[5],
                ESPNOW_CHANNEL);
}

static void sendPacket() {
  pkt.magic     = PKT_MAGIC;
  pkt.version   = PKT_VERSION;
  pkt.pod_id    = POD_ID;
  pkt.n_samples = fill;
  pkt.range_g   = ACCEL_RANGE_G;
  pkt.flags     = pendingFlags;
  pkt.fs_hz     = FS_HZ;
  pkt.seq       = seq++;
  pkt.first_idx = sampleIdx - fill;

  if (esp_now_send(HOST_MAC, (const uint8_t *)&pkt, sizeof(pkt)) == ESP_OK) sentOk++;
  else sendErr++;

  pendingFlags = 0;
  fill = 0;
}

static void markGap() {
  fifoReset();
  fill = 0;                        // drop the partial packet; host sees the flag + idx jump
  pendingFlags |= FLAG_DATA_GAP;
}

// ======================= Arduino =======================
void setup() {
  Serial.begin(115200);
  delay(300);
  Serial.println("\n=== pod boot ===");

  // Drive the strap pins BEFORE talking to the sensor
  if (PIN_NCS >= 0) { pinMode(PIN_NCS, OUTPUT); digitalWrite(PIN_NCS, HIGH); }
  if (PIN_AD0 >= 0) { pinMode(PIN_AD0, OUTPUT); digitalWrite(PIN_AD0, LOW);  }
  delay(50);

  Wire.begin(I2C_SDA, I2C_SCL, I2C_HZ);

  // Find the sensor at 0x68 or 0x69
  const uint8_t candidates[2] = {0x68, 0x69};
  bool found = false;
  for (uint8_t a : candidates) {
    Wire.beginTransmission(a);
    if (Wire.endTransmission() == 0) { mpuAddr = a; found = true; break; }
  }
  if (found) Serial.printf("MPU found at 0x%02X\n", mpuAddr);

  if (!found || !mpuInit()) {
    Serial.println("MPU not responding at 0x68/0x69. Check SDA/SCL/power; power-cycle if NCS was low at boot.");
    i2cScan();
    while (true) delay(1000);
  }
  espNowInit();
}

void loop() {
  // 1) overflow check (reading INT_STATUS clears it)
  uint8_t st;
  if (!readRegs(REG_INT_STATUS, &st, 1)) { i2cErr++; delay(1); return; }
  if (st & 0x10) { overflows++; markGap(); return; }

  // 2) how many whole samples are waiting
  uint8_t c[2];
  if (!readRegs(REG_FIFO_COUNTH, c, 2)) { i2cErr++; delay(1); return; }
  uint16_t frames = ((((uint16_t)(c[0] & 0x1F)) << 8) | c[1]) / 6;

  // 3) drain FIFO into packets
  uint8_t buf[MAX_FRAMES_PER_READ * 6];
  while (frames > 0) {
    uint8_t n = (frames > MAX_FRAMES_PER_READ) ? MAX_FRAMES_PER_READ : (uint8_t)frames;
    if (n > SAMPLES_PER_PKT - fill) n = SAMPLES_PER_PKT - fill;

    if (!readRegs(REG_FIFO_R_W, buf, n * 6)) { i2cErr++; markGap(); return; }  // lost alignment

    for (uint8_t i = 0; i < n; i++) {
      const uint8_t *p = &buf[i * 6];
      pkt.xyz[fill][0] = (int16_t)((p[0] << 8) | p[1]);   // big-endian -> int16
      pkt.xyz[fill][1] = (int16_t)((p[2] << 8) | p[3]);
      pkt.xyz[fill][2] = (int16_t)((p[4] << 8) | p[5]);
      fill++;
      sampleIdx++;
    }
    memcpy(lastXYZ, pkt.xyz[fill - 1], sizeof(lastXYZ));
    frames -= n;

    if (fill == SAMPLES_PER_PKT) sendPacket();
  }

  // 4) 1 Hz debug status on USB serial
  static uint32_t lastStat = 0, lastIdx = 0;
  uint32_t now = millis();
  if (now - lastStat >= 1000) {
    const float cpg = 32768.0f / ACCEL_RANGE_G;           // counts per g
    Serial.printf("rate=%lu Hz  sent=%lu  sendErr=%lu  overflow=%lu  i2cErr=%lu  last[g]= %.3f %.3f %.3f\n",
                  (unsigned long)(sampleIdx - lastIdx), (unsigned long)sentOk, (unsigned long)sendErr,
                  (unsigned long)overflows, (unsigned long)i2cErr,
                  lastXYZ[0] / cpg, lastXYZ[1] / cpg, lastXYZ[2] / cpg);
    lastIdx = sampleIdx;
    lastStat = now;
  }

  delay(2);  // FIFO gains ~12 bytes per 2 ms; it holds ~85 ms, so plenty of margin
}
