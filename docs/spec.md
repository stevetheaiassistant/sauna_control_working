# Spec (short)

## What it does
- **ESP32** reads desired state from the API, drives relays to turn sauna on/off, and posts telemetry (temp, power/heat state).
- **API** (FastAPI) stores desired state and latest telemetry in SQLite; serves a single-page UI.
- **UI** (HTML in server) shows temperature, power/heat status, and a toggle; uses app token in `localStorage`.

## Auth
- **Device token** (`SAUNA_DEVICE_TOKEN`): used by the ESP32 for `GET /v1/device/{id}/desired` and `POST .../telemetry`. Must match the token in firmware (in `secrets.h`, not in repo).
- **App token** (`SAUNA_APP_TOKEN`): used by the browser for `GET/POST /v1/app/{id}/state` and `POST .../desired`. User pastes into UI and it’s stored in `localStorage`.

## Endpoints
| Who    | Method | Path                           | Auth        |
|--------|--------|--------------------------------|-------------|
| Device | GET    | `/v1/device/{id}/desired`      | Device token |
| Device | POST   | `/v1/device/{id}/telemetry`    | Device token |
| App    | GET    | `/v1/app/{id}/state`           | App token   |
| App    | POST   | `/v1/app/{id}/desired`         | App token   |
| Any    | GET    | `/`                            | — (HTML UI) |

## Config
- Backend: `.env` or systemd `EnvironmentFile`; see `backend_ui/.env.example`.
- Firmware: `firmware/secrets.h` (copy from `secrets.h.example`); file is gitignored.
