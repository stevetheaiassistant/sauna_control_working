# Documentation index

| Doc | Purpose |
|-----|---------|
| [spec.md](spec.md) | What the system does, auth, endpoints, schedule semantics, config |
| [architecture.md](architecture.md) | Data flow, DB tables, deploy layout, ESP32 behavior |
| [deploy-ubuntu.md](deploy-ubuntu.md) | Step-by-step VPS deploy (clone, venv, .env, systemd, firewall, IONOS) |
| [schedule-test-plan.md](schedule-test-plan.md) | Manual tests for schedule (create, offline run, reconciliation, auto-remove) |
| [git-push-from-cursor.md](git-push-from-cursor.md) | Enable `git push` from Cursor (SSH vs HTTPS) |
| [decisions.md](decisions.md) | Architecture decisions (secrets, date/time only, offline, reconciliation) |
| [changelog.md](changelog.md) | Version history and stable config |

---

## Agent context (read this first)

**Project**: sauna_control — ESP32 + FastAPI + web UI for wireless sauna on/off and scheduled sessions. Schedule survives WiFi outages via NVS cache and NTP.

**Branches**: `main` = stable (toggle + telemetry + schedule). Deploy from `main`.

**Credentials** (all gitignored; never commit):
- `ops/local-credentials` — VPS SSH (host, user, password), GitHub PAT, SAUNA_DEVICE_TOKEN, SAUNA_APP_TOKEN. Used for deploy.
- `backend_ui/.env` — SAUNA_DEVICE_TOKEN, SAUNA_APP_TOKEN (server env).
- `firmware/Sauna_Control_ESP32/secrets.h` — WiFi, API_HOST, DEVICE_TOKEN (device).

**VPS**: Ubuntu on IONOS. Host: `74.208.194.144`. User: `root`. App at `/opt/sauna/backend_ui/`. systemd: `sauna.service`. UI: `https://sauna.wilsondesignllc.com/`.

**Deploy flow**:
1. `scp -r backend_ui root@74.208.194.144:/opt/sauna/`
2. `ssh root@74.208.194.144 "systemctl restart sauna && systemctl status sauna"`

**Common tasks**:
- Deploy: scp backend_ui, then restart sauna service.
- Add feature: backend in `server.py`, firmware in `firmware/Sauna_Control_ESP32/Sauna_Control_ESP32.ino`.
- Debug: `ssh root@74.208.194.144 'journalctl -u sauna -n 50'`
