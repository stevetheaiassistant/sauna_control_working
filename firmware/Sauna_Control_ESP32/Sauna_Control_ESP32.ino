#include <WiFi.h>
#include <WiFiClientSecure.h>
#include <HTTPClient.h>
#include <ArduinoJson.h>
#include <OneWire.h>
#include <DallasTemperature.h>
#include <Preferences.h>
#include <time.h>

// -------------------- User Config (secrets in secrets.h, not in repo) --------------------
#include "secrets.h"
static const char* WIFI_SSID = WIFI_SSID;
static const char* WIFI_PASS = WIFI_PASS;
static const char* API_HOST = API_HOST;
static const char* DEVICE_ID = DEVICE_ID;
static const char* DEVICE_TOKEN = DEVICE_TOKEN;

// Polling
static const uint32_t POLL_INTERVAL_MS = 3000;
static const uint32_t SCHEDULE_POLL_INTERVAL_MS = 60000;

// Retry behavior if desired != actual
static const uint32_t RETRY_COOLDOWN_MS = 10000;

// DS18B20
static const uint8_t ONE_WIRE_PIN = 26;

// GPIO Map
static const uint8_t POWER_TOGGLE_OUT_PIN = 13;
static const uint8_t START_OUT_PIN        = 12;
static const uint8_t POWER_IN_PIN         = 14;
static const uint8_t HEAT_IN_PIN          = 27;

static const uint32_t PULSE_MS = 200;
static const uint32_t START_DELAY_MS = 2000;

// Schedule cache: max sessions
static const int MAX_SCHEDULE_SESSIONS = 20;

// NTP
static const char* NTP_SERVER = "pool.ntp.org";
static const long GMT_OFFSET_SEC = 0;
static const int DAYLIGHT_OFFSET_SEC = 0;

// -------------------- Globals --------------------
Preferences prefs;

OneWire oneWire(ONE_WIRE_PIN);
DallasTemperature sensors(&oneWire);

uint32_t lastPollMs = 0;
uint32_t lastSchedulePollMs = 0;
int lastAppliedVersion = 0;
int pendingVersion = -1;
uint32_t lastAttemptMs = 0;

// Schedule cache (NVS)
int cachedScheduleVersion = -1;
bool scheduleParsed = false;
struct ScheduleSession {
  int64_t start_epoch;
  int duration_min;
  int preheat_min;
  bool enabled;
};
static const int MAX_SESSIONS = 20;
ScheduleSession scheduleSessions[MAX_SESSIONS];
int scheduleSessionCount = 0;

// Time sync for offline
bool timeSynced = false;
uint32_t timeSyncedAtMs = 0;
int64_t epochAtSync = 0;

// -------------------- Helpers --------------------
String httpsUrl(const String& path) {
  return String("https://") + API_HOST + path;
}

bool readPowerIn() {
  return digitalRead(POWER_IN_PIN) == HIGH;
}

bool readHeatIn() {
  return digitalRead(HEAT_IN_PIN) == HIGH;
}

void pulsePin(uint8_t pin, uint32_t ms) {
  digitalWrite(pin, HIGH);
  delay(ms);
  digitalWrite(pin, LOW);
}

float readTempF() {
  sensors.requestTemperatures();
  float c = sensors.getTempCByIndex(0);
  if (c == DEVICE_DISCONNECTED_C) return NAN;
  return c * 9.0f / 5.0f + 32.0f;
}

// Best-effort current epoch (UTC). Valid only after NTP sync; during outage uses stored sync + millis() delta.
int64_t nowEpochUtc() {
  if (!timeSynced) return 0;
  uint32_t nowMs = millis();
  uint32_t deltaMs = nowMs - timeSyncedAtMs;  // wraps after ~49 days
  return epochAtSync + (int64_t)(deltaMs / 1000);
}

void syncTimeFromNtp() {
  configTime(GMT_OFFSET_SEC, DAYLIGHT_OFFSET_SEC, NTP_SERVER);
  time_t now = time(nullptr);
  int retries = 0;
  while (now < 1000000000 && retries++ < 10) {
    delay(200);
    now = time(nullptr);
  }
  if (now >= 1000000000) {
    timeSynced = true;
    timeSyncedAtMs = millis();
    epochAtSync = (int64_t)now;
    prefs.putULong("syncMs", timeSyncedAtMs);
    prefs.putLong("epoch", epochAtSync);
    Serial.println("NTP sync OK");
  } else {
    Serial.println("NTP sync failed (time not set)");
  }
}

// Returns true if current time is in "on" window: [start - preheat, start] or [start - preheat, start + duration].
// duration_min == 0 means no auto-off: window is [preheatStart, start + 24h) so we only turn on, never off from schedule.
bool scheduleDerivedDesiredOn() {
  int64_t now = nowEpochUtc();
  if (now <= 0) return false;

  for (int i = 0; i < scheduleSessionCount; i++) {
    const ScheduleSession& s = scheduleSessions[i];
    if (!s.enabled) continue;
    int64_t preheatStart = s.start_epoch - (int64_t)s.preheat_min * 60;
    int64_t end = (s.duration_min > 0)
      ? (s.start_epoch + (int64_t)s.duration_min * 60)
      : (s.start_epoch + (int64_t)24 * 3600);  // 0 = no auto-off: keep "on" for 24h from start
    if (now >= preheatStart && now <= end)
      return true;
  }
  return false;
}

// Load schedule from NVS JSON string into scheduleSessions[].
void loadScheduleFromPrefs() {
  String json = prefs.getString("schedJson", "[]");
  scheduleSessionCount = 0;
  scheduleParsed = false;

  DynamicJsonDocument doc(2048);
  DeserializationError err = deserializeJson(doc, json);
  if (err || !doc.is<JsonArray>()) return;

  JsonArray arr = doc.as<JsonArray>();
  for (JsonVariant v : arr) {
    if (scheduleSessionCount >= MAX_SESSIONS) break;
    if (!v.is<JsonObject>()) continue;
    JsonObject o = v.as<JsonObject>();
    ScheduleSession& s = scheduleSessions[scheduleSessionCount++];
    s.start_epoch = o["start_time_epoch_utc"] | 0;
    s.duration_min = o["duration_min"] | 0;
    s.preheat_min = o["preheat_min"] | 30;
    s.enabled = (o["enabled"] | 1) != 0;
  }
  scheduleParsed = true;
}

void saveScheduleToPrefs(int version, const String& json) {
  prefs.putInt("schedVer", version);
  prefs.putString("schedJson", json);
  cachedScheduleVersion = version;
  loadScheduleFromPrefs();
}

void ensureWiFi() {
  static bool wifiStarted = false;
  static uint32_t wifiStartMs = 0;
  static bool printedConnected = false;
  static bool dnsChecked = false;

  if (WiFi.status() == WL_CONNECTED) {
    if (!printedConnected) {
      printedConnected = true;
      Serial.println("WiFi connected!");
      syncTimeFromNtp();
    }
    if (!dnsChecked) {
      dnsChecked = true;
      IPAddress resolved;
      WiFi.hostByName(API_HOST, resolved);
    }
    return;
  }

  printedConnected = false;
  dnsChecked = false;

  if (!wifiStarted) {
    WiFi.mode(WIFI_STA);
    WiFi.begin(WIFI_SSID, WIFI_PASS);
    wifiStarted = true;
    wifiStartMs = millis();
    return;
  }

  if (millis() - wifiStartMs > 20000) {
    Serial.println("WiFi timeout, retrying...");
    WiFi.disconnect(true);
    delay(1000);
    wifiStarted = false;
  }
}

bool httpGetJson(const String& url, const char* bearerToken, DynamicJsonDocument& outDoc) {
  WiFiClientSecure client;
  client.setInsecure();

  HTTPClient https;
  if (!https.begin(client, url)) return false;
  https.addHeader("Authorization", String("Bearer ") + bearerToken);

  int code = https.GET();
  if (code <= 0) {
    https.end();
    return false;
  }
  String body = https.getString();
  https.end();

  DeserializationError err = deserializeJson(outDoc, body);
  if (err) return false;
  return true;
}

bool httpPostJson(const String& url, const char* bearerToken, const JsonDocument& doc) {
  WiFiClientSecure client;
  client.setInsecure();
  HTTPClient https;
  if (!https.begin(client, url)) return false;
  https.addHeader("Authorization", String("Bearer ") + bearerToken);
  https.addHeader("Content-Type", "application/json");
  String payload;
  serializeJson(doc, payload);
  int code = https.POST(payload);
  https.end();
  return (code >= 200 && code < 300);
}

bool enforceDesired(bool desiredOn) {
  bool powerActual = readPowerIn();
  if (desiredOn == powerActual) return false;

  if (desiredOn && !powerActual) {
    pulsePin(POWER_TOGGLE_OUT_PIN, PULSE_MS);
    delay(START_DELAY_MS);
    if (readPowerIn())
      pulsePin(START_OUT_PIN, PULSE_MS);
    return true;
  }
  if (!desiredOn && powerActual) {
    pulsePin(POWER_TOGGLE_OUT_PIN, PULSE_MS);
    return true;
  }
  return false;
}

void postTelemetry(int desiredVersion, bool desiredOn, bool timeSyncedVal, int64_t epochUtc, int schedVer) {
  DynamicJsonDocument doc(512);

  float tempF = readTempF();
  bool powerIn = readPowerIn();
  bool heatIn  = readHeatIn();

  if (isnan(tempF)) doc["temp_f"] = nullptr;
  else doc["temp_f"] = tempF;

  doc["power_in"] = powerIn;
  doc["heat_in"]  = heatIn;

  if (WiFi.status() == WL_CONNECTED) {
    doc["rssi"] = (int)WiFi.RSSI();
    doc["ip"] = WiFi.localIP().toString();
  } else {
    doc["rssi"] = nullptr;
    doc["ip"] = nullptr;
  }

  doc["uptime_s"] = (int)(millis() / 1000);
  doc["time_synced"] = timeSyncedVal;
  if (epochUtc > 0) doc["epoch_utc"] = epochUtc;
  doc["schedule_version"] = schedVer;

  if (powerIn == desiredOn) doc["last_desired_version_applied"] = desiredVersion;
  else doc["last_desired_version_applied"] = lastAppliedVersion;

  String path = String("/v1/device/") + DEVICE_ID + "/telemetry";
  httpPostJson(httpsUrl(path), DEVICE_TOKEN, doc);
}

// Fetch schedule from server and update cache if version changed.
void syncScheduleIfNeeded() {
  DynamicJsonDocument doc(2048);
  String path = String("/v1/device/") + DEVICE_ID + "/schedule";
  if (!httpGetJson(httpsUrl(path), DEVICE_TOKEN, doc)) return;

  int serverVer = doc["schedule_version"] | 0;
  if (serverVer <= cachedScheduleVersion) return;

  String outJson;
  serializeJson(doc["sessions"], outJson);
  saveScheduleToPrefs(serverVer, outJson);
  Serial.printf("Schedule synced, version=%d\n", serverVer);
}

void setupPins() {
  pinMode(POWER_TOGGLE_OUT_PIN, OUTPUT);
  pinMode(START_OUT_PIN, OUTPUT);
  digitalWrite(POWER_TOGGLE_OUT_PIN, LOW);
  digitalWrite(START_OUT_PIN, LOW);
  pinMode(POWER_IN_PIN, INPUT_PULLDOWN);
  pinMode(HEAT_IN_PIN, INPUT_PULLDOWN);
}

void setup() {
  Serial.begin(115200);
  delay(500);

  setupPins();
  sensors.begin();

  prefs.begin("sauna", false);
  lastAppliedVersion = prefs.getInt("lastVer", 0);
  cachedScheduleVersion = prefs.getInt("schedVer", -1);
  loadScheduleFromPrefs();

  ensureWiFi();

  Serial.println("Boot complete.");
  Serial.print("Last applied desired version: ");
  Serial.println(lastAppliedVersion);
}

void loop() {
  ensureWiFi();

  uint32_t now = millis();
  bool wifiUp = (WiFi.status() == WL_CONNECTED);

  // Schedule sync when online (every 60s)
  if (wifiUp && (now - lastSchedulePollMs >= SCHEDULE_POLL_INTERVAL_MS)) {
    lastSchedulePollMs = now;
    syncScheduleIfNeeded();
  }

  if (!wifiUp) {
    // Offline: use schedule-derived desired only.
    bool desiredOn = scheduleDerivedDesiredOn();
    bool powerActual = readPowerIn();
    if (desiredOn != powerActual)
      enforceDesired(desiredOn);
    postTelemetry(lastAppliedVersion, desiredOn, timeSynced, nowEpochUtc(), cachedScheduleVersion);
    delay(200);
    return;
  }

  // Online: poll desired and reconcile.
  if (now - lastPollMs < POLL_INTERVAL_MS) {
    delay(20);
    return;
  }
  lastPollMs = now;

  DynamicJsonDocument desiredDoc(512);
  String desiredPath = String("/v1/device/") + DEVICE_ID + "/desired";
  if (!httpGetJson(httpsUrl(desiredPath), DEVICE_TOKEN, desiredDoc)) {
    Serial.println("GET desired failed.");
    return;
  }

  bool serverDesiredOn = desiredDoc["sauna_on"] | false;
  int desiredVersion = desiredDoc["version"] | 0;
  bool scheduleActive = scheduleDerivedDesiredOn();

  // Reconciliation: when online, server is source of truth; but if a schedule session is active, keep ON to avoid flipping off.
  bool desiredOn = serverDesiredOn || scheduleActive;

  bool powerActual = readPowerIn();

  if (desiredVersion <= lastAppliedVersion && !scheduleActive) {
    postTelemetry(desiredVersion, desiredOn, true, (int64_t)time(nullptr), cachedScheduleVersion);
    return;
  }

  if (desiredOn == powerActual) {
    if (!scheduleActive)
      lastAppliedVersion = desiredVersion;
    prefs.putInt("lastVer", lastAppliedVersion);
    pendingVersion = -1;
    postTelemetry(desiredVersion, desiredOn, true, (int64_t)time(nullptr), cachedScheduleVersion);
    return;
  }

  if (pendingVersion != desiredVersion) {
    pendingVersion = desiredVersion;
    lastAttemptMs = 0;
  }

  if (now - lastAttemptMs >= RETRY_COOLDOWN_MS) {
    enforceDesired(desiredOn);
    lastAttemptMs = now;
    bool newActual = readPowerIn();
    if (newActual == desiredOn) {
      if (!scheduleActive)
        lastAppliedVersion = desiredVersion;
      prefs.putInt("lastVer", lastAppliedVersion);
      pendingVersion = -1;
    }
  }

  postTelemetry(desiredVersion, desiredOn, true, (int64_t)time(nullptr), cachedScheduleVersion);
}
