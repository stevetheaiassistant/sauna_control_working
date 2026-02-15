# sauna_control
Web app and hardware to retrofit to existing sauna for wireless power on/off control.

**Branches:** `main` = stable (toggle + telemetry + schedule). Deploy from `main`.

**How it works:** The ESP32 polls the API for “desired” on/off, drives relays to match, and posts telemetry (temp, power/heat). The web UI lets you toggle the sauna or add **scheduled sessions** (date, time). When online, the server is the source of truth; when WiFi is down, the device uses its cached schedule so it can still turn the sauna on at the scheduled time.

## Prerequisites
- **Backend:** Python 3.9+
- **Firmware:** Arduino IDE or PlatformIO with ESP32 board support
- **Hardware:** ESP32 dev board, 2-channel relay (or optocoupler), DS18B20 temperature sensor, wiring to sauna control panel for power/heat feedback (or discrete inputs)

## Project structure
```
sauna_control/
├── backend_ui/                    # FastAPI server + embedded UI (server.py, .env.example, requirements.txt)
├── firmware/Sauna_Control_ESP32/  # ESP32 sketch (.ino) + secrets.h.example
├── docs/                          # spec, architecture, deploy, test plan, git-push
└── ops/                           # deploy notes, local-credentials (gitignored)
```

## Backend (API + UI)

```bash
cd backend_ui
python3 -m venv venv
source venv/bin/activate   # Windows: venv\Scripts\activate
pip install -r requirements.txt
# Set secrets: copy .env.example to .env and fill in SAUNA_DEVICE_TOKEN, SAUNA_APP_TOKEN
uvicorn server:app --reload
```

Open http://127.0.0.1:8000 for the UI. **Production:** [https://sauna.wilsondesignllc.com](https://sauna.wilsondesignllc.com). To run on an Ubuntu VPS (systemd, HTTPS with Caddy): [docs/deploy-ubuntu.md](docs/deploy-ubuntu.md).

## Firmware (ESP32)
Open the sketch from `firmware/Sauna_Control_ESP32/` in Arduino IDE or PlatformIO. Copy `firmware/Sauna_Control_ESP32/secrets.h.example` to `firmware/Sauna_Control_ESP32/secrets.h` and set WiFi, API host, device id, and device token (same value as `SAUNA_DEVICE_TOKEN` on the server). Do not commit `secrets.h`.

### ESP32 GPIO pinout

| GPIO | Direction | Description |
|------|-----------|-------------|
| 12   | Output    | **Relay 2 (Start)** — active HIGH; pulse to press sauna “Start” after power-on. |
| 13   | Output    | **Relay 1 (Power toggle)** — active HIGH; pulse to turn sauna power on/off. |
| 14   | Input     | **Power state** — reads actual sauna power (HIGH = on). Uses internal pull-down. |
| 26   | One-Wire  | **DS18B20 temperature** — data line for Dallas 1-Wire temp sensor. |
| 27   | Input     | **Heat state** — reads actual heating (HIGH = heating). Uses internal pull-down. |

Outputs drive relays (e.g. optocoupler/relay boards); pulse length is 200 ms. Power-on sequence: pulse power toggle → wait 2 s → if power is on, pulse start.

## Schedule
- In the web UI **Schedule** section, add sessions with **date** and **time** (local).
- The device turns the sauna **on** at the scheduled time. You or the sauna’s built-in controller turn it off.
- Sessions disappear from the UI 2+ minutes after their start time (executed).
- Schedules are stored on the server and synced to the ESP32 every 30 s. The device caches them in NVS and uses NTP for time so it can run scheduled on even when WiFi is down.
- Manual test steps: [docs/schedule-test-plan.md](docs/schedule-test-plan.md).

## Troubleshooting
| Symptom | Likely cause |
|--------|----------------|
| ESP32 gets **403** on desired or telemetry | `DEVICE_TOKEN` in `secrets.h` does not match `SAUNA_DEVICE_TOKEN` on the server. |
| UI shows **“Auth error”** or no state | Wrong or missing app token; paste `SAUNA_APP_TOKEN` into the Auth field and Save. |
| **No telemetry** / “No telemetry yet” | Device not reaching server (WiFi, wrong API host, or 403). Check Serial monitor for HTTP codes and DNS. |
| **Temp stays --.-°F** | DS18B20 not connected, wrong GPIO (26), or bad 1-Wire wiring (data + 3.3 V + GND, 4.7 kΩ pull-up often used). |
| **Desired never applies** | Power/heat input wiring or logic; device only acts when desired ≠ actual and retries every 10 s. |
| **Schedule not syncing** | Device polls schedule every 30 s when online. Check Serial for “Schedule synced”; ensure NTP sync (“NTP sync OK”) so time is correct. |

| **ESP32 crashes / freezes** | **Power**: Use a dedicated 5V 2A+ supply instead of USB from a computer—USB can brown out under WiFi+HTTPS+relay load. **Software**: Polling too fast causes HTTPS overload; stable config: 3s desired, 5s telemetry, 30s schedule. Add `platform.local.txt` for loop stack size (see [deploy-ubuntu](docs/deploy-ubuntu.md)). Ensure strong WiFi. |

More detail: [docs/spec.md](docs/spec.md), [docs/architecture.md](docs/architecture.md).

## Documentation

Full doc index: [docs/README.md](docs/README.md). Key docs: [spec](docs/spec.md), [architecture](docs/architecture.md), [deploy-ubuntu](docs/deploy-ubuntu.md), [schedule-test-plan](docs/schedule-test-plan.md), [git-push-from-cursor](docs/git-push-from-cursor.md).
