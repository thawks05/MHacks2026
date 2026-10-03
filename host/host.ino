/*
 * host.ino — ESP-NOW receiver -> USB serial
 * Board: "ESP32 Dev Module" (arduino-esp32 core 2.x or 3.x)
 *
 * Output (921600 baud):
 *   CSV mode:     pod,idx,ax,ay,az        one line per sample
 *   lines starting with '#' are status/info — parsers should skip them
 *   Plotter mode: ax:..,ay:..,az:..       for the Arduino IDE Serial Plotter
 */
#include <Arduino.h>
#include <WiFi.h>
#include <esp_wifi.h>
#include <esp_now.h>

// ======================= user config =======================
#define ESPNOW_CHANNEL   1         // must match pod.ino
#define SERIAL_BAUD      921600

#define OUT_MODE_CSV     0
#define OUT_MODE_PLOTTER 1
#define OUT_MODE         OUT_MODE_CSV

#define OUTPUT_IN_G      0         // 0 = raw int16 counts, 1 = g (4 decimals)
#define PRINT_DECIMATE   1         // print every Nth sample (1 = all). ~20 for eyeballing/plotter
#define STATUS_MS        2000      // '#' status line period (CSV mode only)
#define MAX_PODS         8

// ============ shared packet (keep IDENTICAL in pod.ino) ============
#define PKT_MAGIC        0xA5
#define PKT_VERSION      1
#define SAMPLES_PER_PKT  32
#define FLAG_DATA_GAP    0x01

typedef struct __attribute__((packed)) {
  uint8_t  magic;
  uint8_t  version;
  uint8_t  pod_id;
  uint8_t  n_samples;
  uint8_t  range_g;
  uint8_t  flags;
  uint16_t fs_hz;
  uint32_t seq;
  uint32_t first_idx;
  int16_t  xyz[SAMPLES_PER_PKT][3];
} AccelPacket;
static_assert(sizeof(AccelPacket) <= 250, "packet too big for ESP-NOW");

// ======================= state =======================
struct PodStats {
  bool     seen;
  uint32_t nextSeq, pkts, lost, gaps, samples, samplesAtLastStat;
};
static PodStats pods[MAX_PODS];

static QueueHandle_t rxQueue;
static volatile uint32_t rxBad = 0, rxQueueFull = 0;

// ======================= receive callback =======================
// Runs in the WiFi task: validate + copy only, no printing here.
#if ESP_ARDUINO_VERSION_MAJOR >= 3
static void onRecv(const esp_now_recv_info_t *info, const uint8_t *data, int len) {
#else
static void onRecv(const uint8_t *mac, const uint8_t *data, int len) {
#endif
  if (len != (int)sizeof(AccelPacket) || data[0] != PKT_MAGIC || data[1] != PKT_VERSION) {
    rxBad++;
    return;
  }
  if (xQueueSend(rxQueue, data, 0) != pdTRUE) rxQueueFull++;
}

// ======================= output =======================
static void printPacket(const AccelPacket &p) {
  char line[80];
  const float cpg = 32768.0f / p.range_g;   // counts per g (8192 at ±4 g)

  for (uint8_t i = 0; i < p.n_samples; i++) {
    uint32_t idx = p.first_idx + i;
    if (idx % PRINT_DECIMATE) continue;
    int x = p.xyz[i][0], y = p.xyz[i][1], z = p.xyz[i][2];
    int n;

#if OUT_MODE == OUT_MODE_PLOTTER
  #if OUTPUT_IN_G
    n = snprintf(line, sizeof(line), "ax:%.4f,ay:%.4f,az:%.4f\n", x / cpg, y / cpg, z / cpg);
  #else
    n = snprintf(line, sizeof(line), "ax:%d,ay:%d,az:%d\n", x, y, z);
  #endif
#else
  #if OUTPUT_IN_G
    n = snprintf(line, sizeof(line), "%u,%lu,%.4f,%.4f,%.4f\n",
                 p.pod_id, (unsigned long)idx, x / cpg, y / cpg, z / cpg);
  #else
    n = snprintf(line, sizeof(line), "%u,%lu,%d,%d,%d\n",
                 p.pod_id, (unsigned long)idx, x, y, z);
  #endif
#endif
    Serial.write((const uint8_t *)line, n);
  }
}

static void printStatus(uint32_t elapsedMs) {
#if OUT_MODE == OUT_MODE_CSV
  for (int i = 0; i < MAX_PODS; i++) {
    PodStats &s = pods[i];
    if (!s.seen) continue;
    uint32_t rate = (uint32_t)((uint64_t)(s.samples - s.samplesAtLastStat) * 1000 / elapsedMs);
    Serial.printf("# pod%d rate=%lu Hz pkts=%lu lost=%lu gaps=%lu\n", i,
                  (unsigned long)rate, (unsigned long)s.pkts, (unsigned long)s.lost, (unsigned long)s.gaps);
    s.samplesAtLastStat = s.samples;
  }
  Serial.printf("# bad=%lu qfull=%lu\n", (unsigned long)rxBad, (unsigned long)rxQueueFull);
#endif
}

// ======================= Arduino =======================
void setup() {
  Serial.setTxBufferSize(4096);
  Serial.begin(SERIAL_BAUD);
  delay(300);

  rxQueue = xQueueCreate(32, sizeof(AccelPacket));

  WiFi.mode(WIFI_STA);
  WiFi.disconnect();
  esp_wifi_set_ps(WIFI_PS_NONE);
  esp_wifi_set_channel(ESPNOW_CHANNEL, WIFI_SECOND_CHAN_NONE);

  if (esp_now_init() != ESP_OK) {
    Serial.println("# esp_now_init FAILED");
    while (true) delay(1000);
  }
  esp_now_register_recv_cb(onRecv);

  uint8_t mac[6];
  esp_wifi_get_mac(WIFI_IF_STA, mac);
#if OUT_MODE == OUT_MODE_CSV
  Serial.printf("# host MAC %02X:%02X:%02X:%02X:%02X:%02X  ch %d\n",
                mac[0], mac[1], mac[2], mac[3], mac[4], mac[5], ESPNOW_CHANNEL);
  Serial.printf("# paste into pod.ino: uint8_t HOST_MAC[6] = {0x%02X, 0x%02X, 0x%02X, 0x%02X, 0x%02X, 0x%02X};\n",
                mac[0], mac[1], mac[2], mac[3], mac[4], mac[5]);
  Serial.println(OUTPUT_IN_G ? "# pod,idx,ax_g,ay_g,az_g" : "# pod,idx,ax,ay,az");
#endif
}

void loop() {
  AccelPacket p;
  while (xQueueReceive(rxQueue, &p, 0) == pdTRUE) {
    if (p.pod_id >= MAX_PODS) { rxBad++; continue; }
    PodStats &s = pods[p.pod_id];

    if (s.seen && p.seq != s.nextSeq) {
      if (p.seq > s.nextSeq) {
        s.lost += p.seq - s.nextSeq;
      } else {
#if OUT_MODE == OUT_MODE_CSV
        Serial.printf("# pod%u restarted\n", p.pod_id);
#endif
      }
    }
    if (p.flags & FLAG_DATA_GAP) s.gaps++;

    s.seen = true;
    s.nextSeq = p.seq + 1;
    s.pkts++;
    s.samples += p.n_samples;

    printPacket(p);
  }

  static uint32_t lastStat = 0;
  uint32_t now = millis();
  if (now - lastStat >= STATUS_MS) {
    printStatus(now - lastStat);
    lastStat = now;
  }
  delay(1);
}
