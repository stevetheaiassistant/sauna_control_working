# Architecture (short)

## Components
```
  [ESP32]  <--HTTPS-->  [API server]  <--HTTPS-->  [Browser UI]
   device                  FastAPI                    single page
   polls desired           SQLite                     toggle + telemetry
   posts telemetry         desired_state
   drives relays           telemetry_latest
```

## Deploy (VPS)
- **Service name**: e.g. `sauna.service`.
- **Location**: e.g. app in `/opt/sauna/` (or your choice); run with `uvicorn server:app --host 0.0.0.0 --port 8000`.
- **Secrets**: set `SAUNA_DEVICE_TOKEN`, `SAUNA_APP_TOKEN`, optionally `SAUNA_DEVICE_ID` and `SAUNA_DB` via:
  - systemd `Environment=` or `EnvironmentFile=/opt/sauna/.env`, or
  - a `.env` file in the app directory (and point systemd to it if you use `EnvironmentFile`).
- **DB**: SQLite file path from `SAUNA_DB` (default `sauna.db`); ensure the process has write permission to that path.

## Repo layout
- `firmware/` — ESP32 Arduino sketch; secrets in `secrets.h` (see `secrets.h.example`).
- `backend_ui/` — FastAPI app (`server.py`), `requirements.txt`, `.env.example`.
- `docs/` — spec, architecture, decisions, changelog.
- `ops/` — deploy notes.
