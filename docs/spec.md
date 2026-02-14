# Spec

## Overview

**sauna_control** retrofits an existing sauna with wireless on/off control. An ESP32 polls a FastAPI server for desired state, drives relays to match, and posts telemetry. A web UI lets users toggle the sauna or add scheduled sessions. Schedules are cached on the device so it can turn the sauna on at the scheduled preheat time even when WiFi is down.

## Components

| Component | Role |
|-----------|------|
| **ESP32** | Polls desired state and schedule; drives relays (power toggle, start); reads power/heat inputs and DS18B20 temp; posts telemetry; caches schedule in NVS; uses NTP for offline time |
| **FastAPI server** | Stores desired state, telemetry, and schedule in SQLite; serves single-page UI; device and app endpoints |
| **Web UI** | Embedded in server at `/`; shows temp, power/heat status, toggle, schedule (add/edit/delete sessions); uses app token in `localStorage` |

## Auth

| Token | Used by | Purpose |
|-------|---------|---------|
| `SAUNA_DEVICE_TOKEN` | ESP32 (`secrets.h`) | `GET /v1/device/{id}/desired`, `GET .../schedule`, `POST .../telemetry` |
| `SAUNA_APP_TOKEN` | Browser (pasted in UI, stored in `localStorage`) | `GET/POST /v1/app/{id}/state`, `POST .../desired`, schedule CRUD |

Tokens must match between server `.env` and firmware `secrets.h` (device token).

## Endpoints

| Who | Method | Path | Auth |
|-----|--------|------|------|
| Device | GET | `/v1/device/{id}/desired` | Device token |
| Device | GET | `/v1/device/{id}/schedule` | Device token |
| Device | POST | `/v1/device/{id}/telemetry` | Device token |
| App | GET | `/v1/app/{id}/state` | App token |
| App | POST | `/v1/app/{id}/desired` | App token |
| App | GET | `/v1/app/{id}/schedule` | App token |
| App | POST | `/v1/app/{id}/schedule` | App token |
| App | PUT | `/v1/app/{id}/schedule/{id}` | App token |
| App | DELETE | `/v1/app/{id}/schedule/{id}` | App token |
| Any | GET | `/` | — (HTML UI) |

## Schedule semantics

- **Session**: `start_time_utc` (ISO), `preheat_min` (default 30), `duration_min` (0 = no auto-off). No duration field in UI; sauna controller or user turns off.
- **Device behavior**: Turn ON at `start_time - preheat_min`. No automatic turn OFF from schedule.
- **Offline**: Device caches schedule in NVS; uses NTP-synced time (or millis delta during outage) to run schedule when WiFi is down.
- **Reconciliation**: When online, server desired is authoritative; if a schedule session is active, device keeps ON even if server says OFF to avoid oscillation.

## Config

- **Backend**: `.env` (see `backend_ui/.env.example`). `SAUNA_DEVICE_TOKEN`, `SAUNA_APP_TOKEN` required.
- **Firmware**: `firmware/secrets.h` (copy from `secrets.h.example`). `WIFI_SSID`, `WIFI_PASS`, `API_HOST`, `DEVICE_ID`, `DEVICE_TOKEN`. File is gitignored.

## Branches

- **main**: Stable/prototype version.
- **On-Scheduling**: Schedule feature (calendar, preheat, offline cache, NTP). Use for new deploys.
