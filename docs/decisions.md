# Architecture decisions

## Secrets

- **Backend**: `.env` file, gitignored. `SAUNA_DEVICE_TOKEN`, `SAUNA_APP_TOKEN` required.
- **Firmware**: `secrets.h` (copy from `secrets.h.example`), gitignored. Never commit real credentials.
- **Ops**: `ops/local-credentials` (gitignored) — VPS SSH, GitHub PAT, tokens. Used for deploy; never commit.

## Schedule: no duration

- User or sauna's built-in controller turns the sauna off. No automatic turn-off from schedule.
- `duration_min` stored as 0; device treats 0 as "no auto-off" (keeps ON for 24h from start as a practical upper bound).

## Offline schedule

- Device caches schedule in NVS. When WiFi is down, uses NTP-synced time (or millis delta) to run schedule.
- Time is valid only within same boot; after reboot, NTP must sync again before schedule works offline.

## Reconciliation

- When online: server desired is authoritative. If a schedule session is active, device keeps ON (avoids flipping off when reconnecting).
- When offline: schedule-derived desired only.

## Single device

- MVP targets `sauna-1`. DB and API use `device_id` throughout for future multi-device support.

## Vanilla JS

- No frameworks. UI is embedded HTML in `server.py`. Schedule and state poll every 5 s.
