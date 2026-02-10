#include <WiFi.h>
#include <WiFiClientSecure.h>
#include <HTTPClient.h>
#include <ArduinoJson.h>
#include <OneWire.h>
#include <DallasTemperature.h>
#include <Preferences.h>
#include <math.h>

// -------------------- User Config --------------------
static const char* WIFI_SSID = "NSA Surveillance Van";
static const char* WIFI_PASS = "i love fiber17!";

// Your VPS domain (recommended) or IP (NO https:// here)
static const char* API_HOST = "sauna.wilsondesignllc.com";   // e.g. "sauna.yourdomain.com"

// Must match your server.py settings
static const char* DEVICE_ID    = "sauna-1";
static const char* DEVICE_TOKEN = "bFeEmYJ-2nrHoBNWqtS309jbsS0_32TCe559y0MtPT8";

// Polling
static const uint32_t POLL_INTERVAL_MS = 3000;

// Retry behavior if desired != actual (power_in doesn't match yet)
static const uint32_t RETRY_COOLDOWN_MS = 10000;

// DS18B20
static const uint8_t ONE_WIRE_PIN = 26;

// GPIO Map (as provided)
static const uint8_t POWER_TOGGLE_OUT_PIN = 13;  // Relay 1 (active HIGH)
static const uint8_t START_OUT_PIN        = 12;  // Relay 2 (active HIGH)
static const uint8_t POWER_IN_PIN         = 14;  // Input: actual power state
static const uint8_t HEAT_IN_PIN          = 27;  // Input: actual heat state

// Pulse timings (per your spec)
static const uint32_t PULSE_MS = 200;
static const uint32_t START_DELAY_MS = 2000;

// -------------------- Globals --------------------
Preferences prefs;

OneWire oneWire(ONE_WIRE_PIN);
DallasTemperature sensors(&oneWire);

uint32_t lastPollMs = 0;
int lastAppliedVersion = 0;

// Track the currently "pending" desired version we are trying to enforce
int pendingVersion = -1;
uint32_t lastAttemptMs = 0;

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

// UPDATED ensureWiFi(): prevents calling WiFi.begin() while already connecting.
// Retries cleanly after a timeout.
void ensureWiFi() {
  static bool wifiStarted = false;
  static uint32_t wifiStartMs = 0;
  static bool printedConnected = false;
  static bool dnsChecked = false;

  if (WiFi.status() == WL_CONNECTED) {
    if (!printedConnected) {
      printedConnected = true;
      Serial.println("WiFi connected!");
      Serial.print("IP: "); Serial.println(WiFi.localIP());
      Serial.print("DNS: "); Serial.println(WiFi.dnsIP());
      Serial.print("Gateway: "); Serial.println(WiFi.gatewayIP());
      Serial.print("RSSI: "); Serial.println(WiFi.RSSI());
    }

    // Do DNS resolution once after connect
    if (!dnsChecked) {
      dnsChecked = true;
      IPAddress resolved;
      bool ok = WiFi.hostByName(API_HOST, resolved);
      Serial.print("DNS resolve "); Serial.print(API_HOST);
      Serial.print(" => ");
      if (ok) Serial.println(resolved);
      else Serial.println("FAILED");
    }
    return;
  }

  printedConnected = false;
  dnsChecked = false;

  if (!wifiStarted) {
    Serial.print("Starting WiFi connection to ");
    Serial.println(WIFI_SSID);

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
  client.setInsecure(); // ok for bring-up

  HTTPClient https;
  if (!https.begin(client, url)) {
    Serial.println("https.begin() failed");
    return false;
  }

  https.addHeader("Authorization", String("Bearer ") + bearerToken);

  int code = https.GET();
  Serial.print("GET ");
  Serial.print(url);
  Serial.print(" -> HTTP ");
  Serial.println(code);

  if (code <= 0) {
    Serial.print("HTTPClient error: ");
    Serial.println(https.errorToString(code));
    https.end();
    return false;
  }

  String body = https.getString();
  https.end();

  Serial.print("Body: ");
  Serial.println(body);

  DeserializationError err = deserializeJson(outDoc, body);
  if (err) {
    Serial.print("JSON parse error: ");
    Serial.println(err.c_str());
    return false;
  }

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

// Perform actions to make actual match desired.
// Returns true if we attempted an action.
bool enforceDesired(bool desiredOn) {
  bool powerActual = readPowerIn();

  // If desired matches actual: no action needed.
  if (desiredOn == powerActual) return false;

  if (desiredOn && !powerActual) {
    // Turn ON sequence:
    // 1) press power toggle
    // 2) wait 2s
    // 3) re-check power_in; if ON, press start
    pulsePin(POWER_TOGGLE_OUT_PIN, PULSE_MS);
    delay(START_DELAY_MS);

    if (readPowerIn()) {
      pulsePin(START_OUT_PIN, PULSE_MS);
    }
    return true;
  }

  if (!desiredOn && powerActual) {
    // Turn OFF sequence: press power toggle once
    pulsePin(POWER_TOGGLE_OUT_PIN, PULSE_MS);
    return true;
  }

  return false;
}

void postTelemetry(int desiredVersion, bool desiredOn) {
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

  if (powerIn == desiredOn) doc["last_desired_version_applied"] = desiredVersion;
  else doc["last_desired_version_applied"] = lastAppliedVersion;

  String path = String("/v1/device/") + DEVICE_ID + "/telemetry";
  httpPostJson(httpsUrl(path), DEVICE_TOKEN, doc);
}

void setupPins() {
  pinMode(POWER_TOGGLE_OUT_PIN, OUTPUT);
  pinMode(START_OUT_PIN, OUTPUT);
  digitalWrite(POWER_TOGGLE_OUT_PIN, LOW);
  digitalWrite(START_OUT_PIN, LOW);

  // If your inputs are floating, consider INPUT_PULLUP/PULLDOWN.
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

  ensureWiFi();

  Serial.println("Boot complete.");
  Serial.print("Last applied desired version: ");
  Serial.println(lastAppliedVersion);
}

void loop() {
  ensureWiFi();

  // If WiFi isn't connected yet, don't hammer the server.
  if (WiFi.status() != WL_CONNECTED) {
    delay(200);
    return;
  }

  uint32_t now = millis();
  if (now - lastPollMs < POLL_INTERVAL_MS) {
    delay(20);
    return;
  }
  lastPollMs = now;

  // 1) Get desired
  DynamicJsonDocument desiredDoc(512);
  String desiredPath = String("/v1/device/") + DEVICE_ID + "/desired";
  bool ok = httpGetJson(httpsUrl(desiredPath), DEVICE_TOKEN, desiredDoc);
  if (!ok) {
    Serial.println("GET desired failed.");
    return;
  }

  bool desiredOn = desiredDoc["sauna_on"] | false;
  int desiredVersion = desiredDoc["version"] | 0;

  bool powerActual = readPowerIn();

  // If already applied (or older), just post telemetry and exit.
  if (desiredVersion <= lastAppliedVersion) {
    postTelemetry(desiredVersion, desiredOn);
    return;
  }

  // If desired matches actual, mark applied immediately.
  if (desiredOn == powerActual) {
    lastAppliedVersion = desiredVersion;
    prefs.putInt("lastVer", lastAppliedVersion);
    pendingVersion = -1;
    postTelemetry(desiredVersion, desiredOn);
    Serial.printf("Applied (no action needed). Version=%d\n", desiredVersion);
    return;
  }

  // We need to act. Throttle attempts to avoid repeated presses every poll.
  if (pendingVersion != desiredVersion) {
    pendingVersion = desiredVersion;
    lastAttemptMs = 0;
  }

  if (now - lastAttemptMs >= RETRY_COOLDOWN_MS) {
    Serial.printf("Enforcing desired. Version=%d desiredOn=%d powerActual=%d\n",
                  desiredVersion, desiredOn ? 1 : 0, powerActual ? 1 : 0);

    enforceDesired(desiredOn);
    lastAttemptMs = now;

    bool newActual = readPowerIn();
    if (newActual == desiredOn) {
      lastAppliedVersion = desiredVersion;
      prefs.putInt("lastVer", lastAppliedVersion);
      pendingVersion = -1;
      Serial.printf("Applied after action. Version=%d\n", desiredVersion);
    } else {
      Serial.println("Not yet matching desired; will retry later if still needed.");
    }
  }

  // 3) Post telemetry
  postTelemetry(desiredVersion, desiredOn);
}