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
static const char* wifi_ssid     = SECRET_WIFI_SSID;
static const char* wifi_pass     = SECRET_WIFI_PASS;
static const char* api_host      = SECRET_API_HOST;
static const char* device_id     = SECRET_DEVICE_ID;
static const char* device_token  = SECRET_DEVICE_TOKEN;

// Polling
static const uint32_t POLL_INTERVAL_MS = 5000;
static const uint32_t TELEMETRY_INTERVAL_MS = 10000;
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
uint32_t lastTelemetryMs = 0;
uint32_t lastSchedulePollMs = 0;
int lastAppliedVersion = 0;
int pendingVersion = -1;
uint32_t lastAttemptMs = 0;
int enforceRetries = 0;
static const int MAX_ENFORCE_RETRIES = 3;
bool scheduleDesiredPosted = false;  // avoid spamming POST desired

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

// Shared JSON buffer -- allocated once, reused everywhere to avoid heap fragmentation.
DynamicJsonDocument jsonBuf(2048);

// Time sync for offline
bool timeSynced = false;
uint32_t timeSyncedAtMs = 0;
int64_t epochAtSync = 0;

// -------------------- Helpers --------------------
// Use HTTPS when API_HOST has no port (e.g. domain); use HTTP when API_HOST is "IP:8000"
static bool useHttps() {
  return (strstr(api_host, ":8000") == nullptr);
}

String apiUrl(const String& path) {
  return String(useHttps() ? "https://" : "http://") + api_host + path;
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
  Serial.print("NTP waiting...");
  time_t now = time(nullptr);
  int retries = 0;
  while (now < 1000000000 && retries++ < 40) {   // up to 8 seconds
    delay(200);
    now = time(nullptr);
  }
  if (now >= 1000000000) {
    timeSynced = true;
    timeSyncedAtMs = millis();
    epochAtSync = (int64_t)now;
    prefs.putULong("syncMs", timeSyncedAtMs);
    prefs.putLong("epoch", epochAtSync);
    Serial.println(" OK");
  } else {
    Serial.println(" failed (will retry)");
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
      : (s.start_epoch + (int64_t)24 * 3600);
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

  jsonBuf.clear();
  DeserializationError err = deserializeJson(jsonBuf, json);
  if (err || !jsonBuf.is<JsonArray>()) return;

  JsonArray arr = jsonBuf.as<JsonArray>();
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
  static uint32_t lastNtpRetryMs = 0;

  if (WiFi.status() == WL_CONNECTED) {
    if (!printedConnected) {
      printedConnected = true;
      Serial.println("WiFi connected!");
      syncTimeFromNtp();
    }
    // Retry NTP every 15s if still not synced
    if (!timeSynced && (millis() - lastNtpRetryMs > 15000)) {
      lastNtpRetryMs = millis();
      Serial.println("Retrying NTP...");
      syncTimeFromNtp();
    }
    return;
  }

  printedConnected = false;

  if (!wifiStarted) {
    WiFi.mode(WIFI_STA);
    WiFi.begin(wifi_ssid, wifi_pass);
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

// GET JSON into shared jsonBuf. Caller must use jsonBuf before next HTTP call.
bool httpGetJson(const String& url, const char* bearerToken) {
  int code;
  String body;

  if (useHttps()) {
    WiFiClientSecure client;
    client.setInsecure();  // accept Let's Encrypt / CA-signed certs
    HTTPClient http;
    http.setTimeout(5000);
    if (!http.begin(client, url)) return false;
    http.addHeader("Authorization", String("Bearer ") + bearerToken);
    code = http.GET();
    yield();
    if (code <= 0) { http.end(); return false; }
    body = http.getString();
    http.end();
  } else {
    WiFiClient client;
    HTTPClient http;
    http.setTimeout(5000);
    if (!http.begin(client, url)) return false;
    http.addHeader("Authorization", String("Bearer ") + bearerToken);
    code = http.GET();
    yield();
    if (code <= 0) { http.end(); return false; }
    body = http.getString();
    http.end();
  }
  yield();

  jsonBuf.clear();
  DeserializationError err = deserializeJson(jsonBuf, body);
  return !err;
}

// POST jsonBuf contents as JSON.
bool httpPostJson(const String& url, const char* bearerToken) {
  String payload;
  serializeJson(jsonBuf, payload);
  int code;

  if (useHttps()) {
    WiFiClientSecure client;
    client.setInsecure();
    HTTPClient http;
    http.setTimeout(5000);
    if (!http.begin(client, url)) return false;
    http.addHeader("Authorization", String("Bearer ") + bearerToken);
    http.addHeader("Content-Type", "application/json");
    code = http.POST(payload);
    http.end();
  } else {
    WiFiClient client;
    HTTPClient http;
    http.setTimeout(5000);
    if (!http.begin(client, url)) return false;
    http.addHeader("Authorization", String("Bearer ") + bearerToken);
    http.addHeader("Content-Type", "application/json");
    code = http.POST(payload);
    http.end();
  }
  yield();
  return (code >= 200 && code < 300);
}

// Tell server to set desired state (used when schedule fires).
bool postDesiredState(bool saunaOn) {
  jsonBuf.clear();
  jsonBuf["sauna_on"] = saunaOn;
  String path = String("/v1/device/") + device_id + "/desired";
  bool ok = httpPostJson(apiUrl(path), device_token);
  Serial.println(ok ? "Schedule -> desired ON" : "Schedule POST failed");
  return ok;
}

bool enforceDesired(bool desiredOn) {
  bool powerActual = readPowerIn();
  if (desiredOn == powerActual) return false;

  if (desiredOn && !powerActual) {
    pulsePin(POWER_TOGGLE_OUT_PIN, PULSE_MS);
    // Non-blocking wait: yield to avoid watchdog reset
    for (uint32_t t = millis(); millis() - t < START_DELAY_MS; ) { yield(); delay(50); }
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
  jsonBuf.clear();

  float tempF = readTempF();
  bool powerIn = readPowerIn();
  bool heatIn  = readHeatIn();

  if (isnan(tempF)) jsonBuf["temp_f"] = nullptr;
  else jsonBuf["temp_f"] = tempF;

  jsonBuf["power_in"] = powerIn;
  jsonBuf["heat_in"]  = heatIn;

  if (WiFi.status() == WL_CONNECTED) {
    jsonBuf["rssi"] = (int)WiFi.RSSI();
    jsonBuf["ip"] = WiFi.localIP().toString();
  } else {
    jsonBuf["rssi"] = nullptr;
    jsonBuf["ip"] = nullptr;
  }

  jsonBuf["uptime_s"] = (int)(millis() / 1000);
  jsonBuf["time_synced"] = timeSyncedVal;
  if (epochUtc > 0) jsonBuf["epoch_utc"] = epochUtc;
  jsonBuf["schedule_version"] = schedVer;

  if (powerIn == desiredOn) jsonBuf["last_desired_version_applied"] = desiredVersion;
  else jsonBuf["last_desired_version_applied"] = lastAppliedVersion;

  String path = String("/v1/device/") + device_id + "/telemetry";
  httpPostJson(apiUrl(path), device_token);
}

// Fetch schedule from server and update cache if version changed.
void syncScheduleIfNeeded() {
  String path = String("/v1/device/") + device_id + "/schedule";
  if (!httpGetJson(apiUrl(path), device_token)) return;
  yield();

  int serverVer = jsonBuf["schedule_version"] | 0;
  if (serverVer <= cachedScheduleVersion) return;

  String outJson;
  serializeJson(jsonBuf["sessions"], outJson);
  saveScheduleToPrefs(serverVer, outJson);
  yield();
  Serial.println("Schedule synced");
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
  Serial.printf("Free heap: %d bytes\n", ESP.getFreeHeap());
  Serial.printf("Last applied version: %d\n", lastAppliedVersion);
}

void loop() {
  ensureWiFi();
  yield();

  uint32_t now = millis();
  bool wifiUp = (WiFi.status() == WL_CONNECTED);

  // --- Offline mode: schedule-only control ---
  if (!wifiUp) {
    bool desiredOn = scheduleDerivedDesiredOn();
    bool powerActual = readPowerIn();
    if (desiredOn != powerActual && enforceRetries < MAX_ENFORCE_RETRIES) {
      enforceDesired(desiredOn);
      enforceRetries++;
    }
    delay(5000);
    return;
  }

  // --- Schedule sync (every 60s, separate cycle) ---
  if (now - lastSchedulePollMs >= SCHEDULE_POLL_INTERVAL_MS) {
    lastSchedulePollMs = now;
    syncScheduleIfNeeded();
    delay(200);  // let stack settle after NVS write
    yield();
    return;
  }

  // --- Telemetry (every 10s, separate cycle) ---
  if (now - lastTelemetryMs >= TELEMETRY_INTERVAL_MS) {
    lastTelemetryMs = now;
    postTelemetry(lastAppliedVersion, false, timeSynced, nowEpochUtc(), cachedScheduleVersion);
    return;  // don't do anything else this cycle
  }

  // --- Poll desired state (every 5s) ---
  if (now - lastPollMs < POLL_INTERVAL_MS) {
    delay(50);
    return;
  }
  lastPollMs = now;

  // Check if schedule should fire (POST desired=ON once)
  bool scheduleActive = scheduleDerivedDesiredOn();
  if (scheduleActive && !scheduleDesiredPosted) {
    if (postDesiredState(true)) {
      scheduleDesiredPosted = true;
      Serial.println("Schedule fired -> desired ON");
    }
    return;  // don't do anything else this cycle
  }
  if (!scheduleActive) scheduleDesiredPosted = false;

  // GET desired state from server (result goes into shared jsonBuf)
  String desiredPath = String("/v1/device/") + device_id + "/desired";
  if (!httpGetJson(apiUrl(desiredPath), device_token)) {
    Serial.println("GET desired failed.");
    return;
  }

  bool desiredOn = jsonBuf["sauna_on"] | false;
  int desiredVersion = jsonBuf["version"] | 0;

  bool powerActual = readPowerIn();

  // No change needed
  if (desiredVersion <= lastAppliedVersion) return;

  // Already in correct state
  if (desiredOn == powerActual) {
    lastAppliedVersion = desiredVersion;
    prefs.putInt("lastVer", lastAppliedVersion);
    pendingVersion = -1;
    enforceRetries = 0;
    return;
  }

  // Stop retrying after MAX_ENFORCE_RETRIES
  if (enforceRetries >= MAX_ENFORCE_RETRIES) return;

  if (pendingVersion != desiredVersion) {
    pendingVersion = desiredVersion;
    lastAttemptMs = 0;
    enforceRetries = 0;
  }

  if (now - lastAttemptMs >= RETRY_COOLDOWN_MS) {
    Serial.printf("[ENFORCE] attempt %d/%d\n", enforceRetries + 1, MAX_ENFORCE_RETRIES);
    enforceDesired(desiredOn);
    lastAttemptMs = now;
    enforceRetries++;
    if (readPowerIn() == desiredOn) {
      lastAppliedVersion = desiredVersion;
      prefs.putInt("lastVer", lastAppliedVersion);
      pendingVersion = -1;
      enforceRetries = 0;
    }
  }
}
