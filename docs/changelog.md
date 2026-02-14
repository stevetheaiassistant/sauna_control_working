# Changelog

## On-Scheduling branch (current)

- **Schedule feature**: Calendar UI (date, time, preheat, note); device caches schedule in NVS; runs schedule offline; NTP sync.
- **No duration**: Removed duration field; sauna controller or user turns off.
- **Backend**: `schedule_sessions`, `schedule_meta` tables; device/app schedule endpoints; telemetry `time_synced`, `epoch_utc`, `schedule_version`.
- **Deploy**: Ubuntu VPS guide with systemd, cloud firewall (IONOS), PAT/SSH for clone.
- **Docs**: spec, architecture, deploy-ubuntu, schedule-test-plan, git-push-from-cursor.

## main (prototype)

- FastAPI backend, SQLite, device desired/telemetry, app state/desired.
- Embedded web UI (toggle, temp, power/heat status).
- ESP32 firmware: poll desired, enforce via relays, post telemetry, Preferences for last applied version.
- GPIO pinout, secrets in `secrets.h` / `.env`, README.
