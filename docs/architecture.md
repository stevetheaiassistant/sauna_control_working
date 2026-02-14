# Architecture

## Data flow

```
  [ESP32]  <--HTTPS-->  [FastAPI server]  <--HTTPS-->  [Browser]
   device                  SQLite                       single page
   - poll desired          - desired_state              - toggle
   - poll schedule         - schedule_sessions          - schedule CRUD
   - post telemetry        - schedule_meta              - view state
   - NTP + NVS cache       - telemetry_latest
   - relays (power/start)
   - DS18B20, GPIO inputs
```

## Database (SQLite)

| Table | Purpose |
|-------|---------|
| `desired_state` | Per-device desired on/off + version (for immediate toggles) |
| `telemetry_latest` | Latest telemetry per device (temp, power_in, heat_in, rssi, time_synced, epoch_utc, schedule_version) |
| `schedule_sessions` | Sessions: device_id, start_time_utc, duration_min, preheat_min, enabled, note |
| `schedule_meta` | Per-device schedule_version (monotonic, for cache invalidation) |

## Deploy

- **Location**: `/opt/sauna/` (clone full repo) or `/opt/sauna/backend_ui/` (app directory).
- **Service**: `sauna.service` (systemd), runs `uvicorn server:app --host 0.0.0.0 --port 8000`.
- **Secrets**: `EnvironmentFile=/opt/sauna/backend_ui/.env`.
- **Firewall**: UFW + **cloud provider firewall** (IONOS, AWS, etc.) must allow port 8000 (or 80/443 if using reverse proxy).

## Repo layout

```
sauna_control/
├── backend_ui/           # FastAPI (server.py), requirements.txt, .env.example
├── firmware/
│   └── Sauna_Control_ESP32/  # .ino, secrets.h.example (secrets.h gitignored)
├── docs/                 # spec, architecture, deploy, test plan, git-push
└── ops/                  # deploy notes, local-credentials (gitignored)
```

## Deploy flow

1. Commit and push to `On-Scheduling`.
2. SSH to VPS: `ssh root@74.208.133.101 'cd /opt/sauna && git pull origin On-Scheduling && systemctl restart sauna'`
3. Check: `ssh root@74.208.133.101 'systemctl status sauna'`

## ESP32 behavior summary

- **Online**: Poll desired every 3 s, schedule every 60 s. Server desired is source of truth; if schedule session active, keep ON (reconciliation).
- **Offline**: Use cached schedule + NTP-derived time. Turn ON at (start - preheat). No auto-off.
- **Time**: NTP on WiFi connect; store epoch_at_sync + millis for offline time (valid until reboot).
