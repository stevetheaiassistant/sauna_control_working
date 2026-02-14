# Documentation index

| Doc | Purpose |
|-----|---------|
| [spec.md](spec.md) | What the system does, auth, endpoints, schedule semantics, config |
| [architecture.md](architecture.md) | Data flow, DB tables, deploy layout, ESP32 behavior |
| [deploy-ubuntu.md](deploy-ubuntu.md) | Step-by-step VPS deploy (clone, venv, .env, systemd, firewall, IONOS) |
| [schedule-test-plan.md](schedule-test-plan.md) | Manual tests for schedule (create, offline preheat, reconciliation) |
| [git-push-from-cursor.md](git-push-from-cursor.md) | Enable `git push` from Cursor (SSH vs HTTPS) |
| [decisions.md](decisions.md) | Architecture decisions (secrets, no duration, offline, reconciliation) |
| [changelog.md](changelog.md) | What changed in main vs On-Scheduling |

---

## Agent context (read this first)

**Project**: sauna_control — ESP32 + FastAPI + web UI for wireless sauna on/off and scheduled sessions. Schedule survives WiFi outages via NVS cache and NTP.

**Branches**: `main` = prototype. `On-Scheduling` = schedule feature (current). Deploy from `On-Scheduling`.

**Credentials** (all gitignored; never commit):
- `ops/local-credentials` — VPS SSH (host, user, password), GitHub PAT, SAUNA_DEVICE_TOKEN, SAUNA_APP_TOKEN. Used for deploy.
- `backend_ui/.env` — SAUNA_DEVICE_TOKEN, SAUNA_APP_TOKEN (server env).
- `firmware/Sauna_Control_ESP32/secrets.h` — WiFi, API_HOST, DEVICE_TOKEN (device).

**VPS**: Ubuntu on IONOS. Host: `74.208.133.101`. User: `root`. SSH key auth configured. App at `/opt/sauna/backend_ui/`. systemd: `sauna.service`. UI: `http://74.208.133.101:8000/`.

**Deploy flow**:
1. Commit and push to `On-Scheduling`.
2. SSH to VPS and pull: `ssh root@74.208.133.101 'cd /opt/sauna && git pull origin On-Scheduling && systemctl restart sauna'`

**Common tasks**:
- Deploy: push, then run the SSH command above.
- Add feature: backend in `server.py`, firmware in `firmware/Sauna_Control_ESP32/Sauna_Control_ESP32.ino`.
- Debug: `ssh root@74.208.133.101 'journalctl -u sauna -n 50'`
