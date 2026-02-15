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
static const char* api_host      = SECRET_API_HOST;  // e.g. "sauna.wilsondesignllc.com" (HTTPS) or "IP:8000" (HTTP)
static const char* device_id     = SECRET_DEVICE_ID;
static const char* device_token  = SECRET_DEVICE_TOKEN;

// Polling (balance responsiveness vs HTTPS stability)
static const uint32_t POLL_INTERVAL_MS = 3000;
static const uint32_t TELEMETRY_INTERVAL_MS = 5000;
static const uint32_t SCHEDULE_POLL_INTERVAL_MS = 30000;

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

// Manual turn-off: if user turns off at sauna, don't retry; POST desired=OFF so UI reflects it
#define MANUAL_OFF_DEBOUNCE_MS 15000
static bool manualTurnOffDetected = false;
static uint32_t powerOffSinceMs = 0;
static bool desiredOnWhenPowerWentOff = false;
static bool manualOffPosted = false;  // only POST desired=OFF once
static bool lastKnownDesiredOn = false;  // from server GET or schedule (for manual turn-off detection)

// Schedule cache (NVS)
int cachedScheduleVersion = -1;
bool scheduleParsed = false;
struct ScheduleSession {
  int64_t start_epoch;
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

// Boot warmup: no HTTP for 15s after boot (WiFi/SSL stack needs time to stabilize)
static uint32_t bootMs = 0;
#define WARMUP_MS 15000

// Minimum gap between any two HTTP requests (HTTPS needs recovery time)
#define HTTP_COOLDOWN_MS 1500
static uint32_t lastHttpMs = 0;

// Max HTTP response size (prevents heap exhaustion from HTML error pages)
#define MAX_HTTP_BODY 4096

// -------------------- Helpers --------------------
// Use HTTPS when API_HOST has no port (e.g. domain); use HTTP when API_HOST is "IP:8000"
static bool useHttps() {
  return (strstr(api_host, ":8000") == nullptr);
}

String apiUrl(const String& path) {
  return String(useHttps() ? "https://" : "http://") + api_host + path;
}

// Check for manual turn-off: power was ON with desired ON, now OFF for 15s -> don't retry, POST desired=OFF.
void checkManualTurnOff(uint32_t now, bool powerActual, bool desiredOn) {
  if (powerActual) {
    powerOffSinceMs = 0;
    return;
  }
  if (powerOffSinceMs == 0) {
    powerOffSinceMs = now;
    desiredOnWhenPowerWentOff = desiredOn;
  }
  if (desiredOnWhenPowerWentOff && (now - powerOffSinceMs) >= MANUAL_OFF_DEBOUNCE_MS) {
    manualTurnOffDetected = true;
    desiredOnWhenPowerWentOff = false;
    powerOffSinceMs = 0;
    lastKnownDesiredOn = false;
    if (!manualOffPosted && WiFi.status() == WL_CONNECTED) {
      if (postDesiredState(false)) manualOffPosted = true;
    }
  }
}

// Clear manual-turn-off state when schedule window ends (next session can turn on again).
void maybeClearManualTurnOff() {
  if (!scheduleDerivedDesiredOnRaw()) {
    manualTurnOffDetected = false;
    manualOffPosted = false;
  }
}

bool readPowerIn() {
  return digitalRead(POWER_IN_PIN) == HIGH;
}

bool readHeatIn() {
  return digitalRead(HEAT_IN_PIN) == HIGH;
}

void pulsePin(uint8_t pin, uint32_t ms) {
  digitalWrite(pin, HIGH);
  for (uint32_t t = millis(); millis() - t < ms; ) { yield(); delay(20); }
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
  while (now < 1000000000 && retries++ < 25) {   // up to 5 seconds (was 8)
    delay(200);
    yield();
    now = time(nullptr);
  }
  if (now >= 1000000000) {
    timeSynced = true;
    timeSyncedAtMs = millis();
    epochAtSync = (int64_t)now;
    Serial.println(" OK");
    yield();
    // Defer NVS writes to avoid crash during sync - do after a settle delay
    prefs.putULong("syncMs", timeSyncedAtMs);
    prefs.putLong("epoch", epochAtSync);
  } else {
    Serial.println(" failed (will retry)");
  }
}

// Raw schedule check (no manual-turn-off override).
static bool scheduleDerivedDesiredOnRaw() {
  int64_t now = nowEpochUtc();
  if (now <= 0) return false;
  for (int i = 0; i < scheduleSessionCount; i++) {
    const ScheduleSession& s = scheduleSessions[i];
    if (!s.enabled) continue;
    int64_t end = s.start_epoch + (int64_t)24 * 3600;
    if (now >= s.start_epoch && now <= end) return true;
  }
  return false;
}

// Returns true if current time is in "on" window. Returns false when manualTurnOffDetected.
bool scheduleDerivedDesiredOn() {
  if (manualTurnOffDetected) return false;
  return scheduleDerivedDesiredOnRaw();
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
    s.enabled = (o["enabled"] | 1) != 0;
  }
  scheduleParsed = true;
}

void saveScheduleToPrefs(int version, const String& json) {
  yield();  // feed watchdog before blocking NVS write
  prefs.putInt("schedVer", version);
  prefs.putString("schedJson", json);
  yield();  // recover after NVS write
  cachedScheduleVersion = version;
  loadScheduleFromPrefs();
}

void ensureWiFi() {
  static bool wifiStarted = false;
  static uint32_t wifiStartMs = 0;
  static bool printedConnected = false;
  static uint32_t wifiConnectedAtMs = 0;  // when we first saw connected (for deferring NTP)
  static uint32_t lastNtpRetryMs = 0;
  static uint32_t wifiDownSince = 0;  // debounce: only reset printedConnected after 5s down

  if (WiFi.status() == WL_CONNECTED) {
    wifiDownSince = 0;
    if (!printedConnected) {
      printedConnected = true;
      wifiConnectedAtMs = millis();  // defer NTP - don't run immediately
      // Don't print here - print only when we actually run NTP (avoids spam if crash-looping)
    }
    // Run NTP 3s after first connect (let WiFi stack stabilize) or retry every 90s if failed
    uint32_t sinceConnect = millis() - wifiConnectedAtMs;
    bool firstRun = (lastNtpRetryMs == 0);
    bool retryDue = (!firstRun && (millis() - lastNtpRetryMs > 90000));
    if (!timeSynced && sinceConnect >= 3000 && (firstRun || retryDue)) {
      lastNtpRetryMs = millis();
      if (firstRun) Serial.println("WiFi connected, syncing NTP...");
      else Serial.println("Retrying NTP...");
      syncTimeFromNtp();
    }
    return;
  }

  // Only reset printedConnected after WiFi has been down for 5s (debounce flapping)
  if (wifiDownSince == 0) wifiDownSince = millis();
  if (millis() - wifiDownSince > 5000) printedConnected = false;

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

// Wait for HTTP cooldown (HTTPS needs recovery time between requests)
static void waitHttpCooldown() {
  if (!useHttps()) return;
  while (millis() - lastHttpMs < HTTP_COOLDOWN_MS) {
    delay(100);
    yield();
  }
}

// Skip HTTP if heap critically low (prevents crash during SSL)
static bool heapOkForHttps() {
  if (!useHttps()) return true;
  return ESP.getFreeHeap() > 20000;
}

// GET JSON into shared jsonBuf. Caller must use jsonBuf before next HTTP call.
// For HTTPS: limits response size to avoid heap exhaustion from HTML error pages.
bool httpGetJson(const String& url, const char* bearerToken) {
  int code;
  String body;

  if (useHttps()) {
    if (!heapOkForHttps()) { delay(500); yield(); return false; }
    waitHttpCooldown();
    WiFiClientSecure client;
    client.setInsecure();  // accept Let's Encrypt / CA-signed certs
    HTTPClient http;
    http.setTimeout(8000);  // HTTPS handshake can be slow
    if (!http.begin(client, url)) return false;
    http.addHeader("Authorization", String("Bearer ") + bearerToken);
    code = http.GET();
    yield();
    if (code <= 0) { http.end(); lastHttpMs = millis(); delay(500); yield(); return false; }
    // Limit response size to prevent heap exhaustion from large HTML/error pages
    int len = http.getSize();
    if (len > 0 && len > MAX_HTTP_BODY) { http.end(); lastHttpMs = millis(); return false; }
    // Read with limit (getString() allocates full response; stream read avoids heap exhaustion)
    body.reserve(MAX_HTTP_BODY);
    body = "";
    Stream& s = http.getStream();
    char buf[128];
    size_t total = 0;
    while (total < (size_t)MAX_HTTP_BODY && s.available()) {
      size_t toRead = (sizeof(buf) < (size_t)MAX_HTTP_BODY - total) ? sizeof(buf) : (size_t)MAX_HTTP_BODY - total;
      size_t n = s.readBytes(buf, toRead);
      if (n == 0) break;
      total += n;
      body.concat(buf, n);
      yield();
    }
    http.end();
    lastHttpMs = millis();
    delay(500);  // let WiFi/SSL stack settle
    yield();
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

// POST jsonBuf contents as JSON. Logs HTTP code on failure for debug.
bool httpPostJson(const String& url, const char* bearerToken) {
  String payload;
  serializeJson(jsonBuf, payload);
  int code;

  if (useHttps()) {
    if (!heapOkForHttps()) { delay(500); yield(); return false; }
    waitHttpCooldown();
    WiFiClientSecure client;
    client.setInsecure();
    HTTPClient http;
    http.setTimeout(8000);  // HTTPS handshake can be slow
    if (!http.begin(client, url)) { Serial.println("POST begin failed"); return false; }
    http.addHeader("Authorization", String("Bearer ") + bearerToken);
    http.addHeader("Content-Type", "application/json");
    code = http.POST(payload);
    http.end();
    lastHttpMs = millis();
    delay(500);  // let WiFi/SSL stack settle
    yield();
  } else {
    WiFiClient client;
    HTTPClient http;
    http.setTimeout(5000);
    if (!http.begin(client, url)) { Serial.println("POST begin failed"); return false; }
    http.addHeader("Authorization", String("Bearer ") + bearerToken);
    http.addHeader("Content-Type", "application/json");
    code = http.POST(payload);
    http.end();
  }
  yield();
  if (code < 200 || code >= 300) {
    Serial.printf("POST failed: HTTP %d\n", code);
    if (code < 0 && useHttps()) { lastHttpMs = millis(); delay(1000); }
  }
  return (code >= 200 && code < 300);
}

// Tell server to set desired state (schedule fires or manual turn-off at sauna).
bool postDesiredState(bool saunaOn) {
  jsonBuf.clear();
  jsonBuf["sauna_on"] = saunaOn;
  String path = String("/v1/device/") + device_id + "/desired";
  bool ok = httpPostJson(apiUrl(path), device_token);
  Serial.println(ok ? (saunaOn ? "POST desired ON" : "POST desired OFF (manual turn-off)") : "POST desired failed");
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

  if (!jsonBuf["sessions"].is<JsonArray>()) return;  // skip malformed response
  String outJson;
  serializeJson(jsonBuf["sessions"], outJson);
  yield();

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
  bootMs = millis();

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

  // --- Warmup: no HTTP for 10s after boot (prevents crash on first HTTPS) ---
  if (now - bootMs < WARMUP_MS) {
    delay(100);
    yield();
    return;
  }

  bool powerActual = readPowerIn();
  if (!wifiUp) lastKnownDesiredOn = scheduleDerivedDesiredOnRaw();
  checkManualTurnOff(now, powerActual, lastKnownDesiredOn);
  maybeClearManualTurnOff();

  // --- Offline mode: schedule-only control ---
  if (!wifiUp) {
    bool desiredOn = scheduleDerivedDesiredOn();
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
    delay(500);  // let stack settle after NVS write (was 200)
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
      lastKnownDesiredOn = true;
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
  lastKnownDesiredOn = desiredOn;
  int desiredVersion = jsonBuf["version"] | 0;

  powerActual = readPowerIn();

  // Don't enforce if user manually turned off at sauna (we'll POST desired=OFF from checkManualTurnOff)
  if (manualTurnOffDetected) {
    delay(50);
    return;
  }

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
    yield();
    enforceDesired(desiredOn);
    lastAttemptMs = now;
    enforceRetries++;
    delay(useHttps() ? 800 : 500);  // settle after relay
    if (readPowerIn() == desiredOn) {
      lastAppliedVersion = desiredVersion;
      prefs.putInt("lastVer", lastAppliedVersion);
      pendingVersion = -1;
      enforceRetries = 0;
    }
  }
}
