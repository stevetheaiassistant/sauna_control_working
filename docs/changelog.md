# Changelog

## main (stable – Feb 2026)

- **Schedule**: Date and time only. No preheat, duration, or notes. Sessions auto-remove from UI 2+ minutes after start.
- **Poll intervals**: 3s desired, 5s telemetry, 30s schedule (stable for HTTPS).
- **Stability**: `heapOkForHttps()` guard, 4KB response limit, 15s boot warmup, `platform.local.txt` for loop stack size.
- **Deploy**: scp `backend_ui` to VPS + `systemctl restart sauna`. Production: `https://sauna1.wilsondesignllc.com`.
- **Backend**: `delete_past_schedule_sessions()` on schedule fetch; schedule version bumped when sessions removed.

## Previous (On-Scheduling)

- Schedule feature: calendar UI, device NVS cache, offline run, NTP sync.
- No duration: sauna controller or user turns off.
- Backend: `schedule_sessions`, `schedule_meta` tables; device/app schedule endpoints.
