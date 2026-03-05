import os
import sqlite3
from datetime import datetime, timezone
from typing import Optional, List

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

BASE_DIR = os.path.dirname(os.path.abspath(__file__))


DB_PATH = os.environ.get("SAUNA_DB", "sauna.db")

DEVICE_TOKEN = os.environ.get("SAUNA_DEVICE_TOKEN", "CHANGE_ME_DEVICE_TOKEN")
APP_TOKEN = os.environ.get("SAUNA_APP_TOKEN", "CHANGE_ME_APP_TOKEN")
DEVICE_ID = os.environ.get("SAUNA_DEVICE_ID", "sauna-1")


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db() -> None:
    conn = db()
    cur = conn.cursor()

    cur.execute("""
    CREATE TABLE IF NOT EXISTS desired_state (
        device_id TEXT PRIMARY KEY,
        sauna_on INTEGER NOT NULL,
        version INTEGER NOT NULL,
        updated_at TEXT NOT NULL
    )
    """)

    cur.execute("""
    CREATE TABLE IF NOT EXISTS telemetry_latest (
        device_id TEXT PRIMARY KEY,
        temp_f REAL,
        power_in INTEGER,
        heat_in INTEGER,
        rssi INTEGER,
        ip TEXT,
        uptime_s INTEGER,
        last_desired_version_applied INTEGER,
        updated_at TEXT NOT NULL
    )
    """)

    # Schedule: one row per session
    cur.execute("""
    CREATE TABLE IF NOT EXISTS schedule_sessions (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        device_id TEXT NOT NULL,
        start_time_utc TEXT NOT NULL,
        duration_min INTEGER NOT NULL,
        preheat_min INTEGER NOT NULL DEFAULT 30,
        enabled INTEGER NOT NULL DEFAULT 1,
        note TEXT,
        updated_at TEXT NOT NULL
    )
    """)

    # Monotonic version per device for schedule cache invalidation
    cur.execute("""
    CREATE TABLE IF NOT EXISTS schedule_meta (
        device_id TEXT PRIMARY KEY,
        schedule_version INTEGER NOT NULL DEFAULT 0,
        updated_at TEXT NOT NULL
    )
    """)

    # Add telemetry columns for schedule/time (idempotent)
    for col, typ in [("time_synced", "INTEGER"), ("epoch_utc", "INTEGER"), ("schedule_version", "INTEGER")]:
        try:
            cur.execute(f"ALTER TABLE telemetry_latest ADD COLUMN {col} {typ}")
        except sqlite3.OperationalError:
            pass

    cur.execute("SELECT device_id FROM desired_state WHERE device_id = ?", (DEVICE_ID,))
    if cur.fetchone() is None:
        cur.execute("""
            INSERT INTO desired_state (device_id, sauna_on, version, updated_at)
            VALUES (?, ?, ?, ?)
        """, (DEVICE_ID, 0, 1, utc_now_iso()))

    conn.commit()
    conn.close()


def require_bearer(request: Request, expected_token: str) -> None:
    auth = request.headers.get("Authorization", "")
    if not auth.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Missing Bearer token")
    token = auth.split(" ", 1)[1].strip()
    if token != expected_token:
        raise HTTPException(status_code=403, detail="Invalid token")


def bump_schedule_version(cur: sqlite3.Cursor, device_id: str) -> int:
    cur.execute("SELECT schedule_version FROM schedule_meta WHERE device_id = ?", (device_id,))
    row = cur.fetchone()
    now = utc_now_iso()
    if row is None:
        cur.execute(
            "INSERT INTO schedule_meta (device_id, schedule_version, updated_at) VALUES (?, 1, ?)",
            (device_id, now),
        )
        return 1
    ver = int(row["schedule_version"]) + 1
    cur.execute(
        "UPDATE schedule_meta SET schedule_version = ?, updated_at = ? WHERE device_id = ?",
        (ver, now, device_id),
    )
    return ver


def iso_to_epoch(iso: str) -> int:
    s = iso.replace("Z", "+00:00")
    return int(datetime.fromisoformat(s).timestamp())


def delete_past_schedule_sessions(cur, device_id: str) -> bool:
    """Delete sessions whose start time was more than 2 min ago (executed). Returns True if any deleted."""
    cur.execute(
        """DELETE FROM schedule_sessions WHERE device_id = ?
           AND datetime(start_time_utc) < datetime('now', '-2 minutes')""",
        (device_id,),
    )
    return cur.rowcount > 0


app = FastAPI(title="Sauna Control", version="1.0.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)

init_db()

app.mount("/static", StaticFiles(directory=os.path.join(BASE_DIR, "static")), name="static")


# --- Models ---
class DesiredUpdate(BaseModel):
    sauna_on: bool


class TelemetryIn(BaseModel):
    temp_f: Optional[float] = None
    power_in: Optional[bool] = None
    heat_in: Optional[bool] = None
    rssi: Optional[int] = None
    ip: Optional[str] = None
    uptime_s: Optional[int] = None
    last_desired_version_applied: Optional[int] = None
    time_synced: Optional[bool] = None
    epoch_utc: Optional[int] = None
    schedule_version: Optional[int] = None


class ScheduleSessionCreate(BaseModel):
    start_time_utc: str  # ISO 8601
    enabled: int = 1


class ScheduleSessionUpdate(BaseModel):
    start_time_utc: Optional[str] = None
    enabled: Optional[int] = None


# --- Device endpoints ---
@app.get("/v1/device/{device_id}/desired")
def get_desired(device_id: str, request: Request):
    require_bearer(request, DEVICE_TOKEN)
    if device_id != DEVICE_ID:
        raise HTTPException(status_code=404, detail="Unknown device")

    conn = db()
    cur = conn.cursor()
    cur.execute("SELECT sauna_on, version, updated_at FROM desired_state WHERE device_id = ?", (device_id,))
    row = cur.fetchone()
    conn.close()
    if not row:
        raise HTTPException(status_code=404, detail="No desired state")

    return {
        "sauna_on": bool(row["sauna_on"]),
        "version": int(row["version"]),
        "updated_at": row["updated_at"],
    }


@app.post("/v1/device/{device_id}/desired")
def device_set_desired(device_id: str, payload: DesiredUpdate, request: Request):
    """Device can set desired (e.g. when user manually turns off sauna at unit)."""
    require_bearer(request, DEVICE_TOKEN)
    if device_id != DEVICE_ID:
        raise HTTPException(status_code=404, detail="Unknown device")

    conn = db()
    cur = conn.cursor()
    cur.execute("SELECT sauna_on, version FROM desired_state WHERE device_id = ?", (device_id,))
    row = cur.fetchone()
    if row is None:
        conn.close()
        raise HTTPException(status_code=404, detail="Missing desired state row")

    new_sauna_on = 1 if payload.sauna_on else 0
    old_sauna_on = int(row["sauna_on"])
    version = int(row["version"])
    if new_sauna_on != old_sauna_on:
        version += 1
        cur.execute(
            "UPDATE desired_state SET sauna_on = ?, version = ?, updated_at = ? WHERE device_id = ?",
            (new_sauna_on, version, utc_now_iso(), device_id),
        )
        conn.commit()
    conn.close()
    return {"ok": True, "version": version}


@app.get("/v1/device/{device_id}/schedule")
def get_device_schedule(device_id: str, request: Request, since: Optional[str] = None):
    require_bearer(request, DEVICE_TOKEN)
    if device_id != DEVICE_ID:
        raise HTTPException(status_code=404, detail="Unknown device")

    conn = db()
    cur = conn.cursor()
    if delete_past_schedule_sessions(cur, device_id):
        bump_schedule_version(cur, device_id)
        conn.commit()
    cur.execute("SELECT schedule_version, updated_at FROM schedule_meta WHERE device_id = ?", (device_id,))
    meta = cur.fetchone()
    version = int(meta["schedule_version"]) if meta else 0

    cur.execute(
        """SELECT id, start_time_utc, duration_min, preheat_min, enabled, note, updated_at
           FROM schedule_sessions WHERE device_id = ? AND enabled = 1
           ORDER BY start_time_utc""",
        (device_id,),
    )
    rows = cur.fetchall()
    conn.close()

    sessions = []
    for r in rows:
        sessions.append({
            "start_time_epoch_utc": iso_to_epoch(r["start_time_utc"]),
            "enabled": int(r["enabled"]),
        })

    return {"schedule_version": version, "sessions": sessions}


@app.post("/v1/device/{device_id}/telemetry")
def post_telemetry(device_id: str, payload: TelemetryIn, request: Request):
    require_bearer(request, DEVICE_TOKEN)
    if device_id != DEVICE_ID:
        raise HTTPException(status_code=404, detail="Unknown device")

    conn = db()
    cur = conn.cursor()
    cur.execute("""
        INSERT INTO telemetry_latest (
            device_id, temp_f, power_in, heat_in, rssi, ip, uptime_s,
            last_desired_version_applied, time_synced, epoch_utc, schedule_version, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(device_id) DO UPDATE SET
            temp_f=excluded.temp_f,
            power_in=excluded.power_in,
            heat_in=excluded.heat_in,
            rssi=excluded.rssi,
            ip=excluded.ip,
            uptime_s=excluded.uptime_s,
            last_desired_version_applied=excluded.last_desired_version_applied,
            time_synced=excluded.time_synced,
            epoch_utc=excluded.epoch_utc,
            schedule_version=excluded.schedule_version,
            updated_at=excluded.updated_at
    """, (
        device_id,
        payload.temp_f,
        None if payload.power_in is None else (1 if payload.power_in else 0),
        None if payload.heat_in is None else (1 if payload.heat_in else 0),
        payload.rssi,
        payload.ip,
        payload.uptime_s,
        payload.last_desired_version_applied,
        None if payload.time_synced is None else (1 if payload.time_synced else 0),
        payload.epoch_utc,
        payload.schedule_version,
        utc_now_iso(),
    ))

    conn.commit()
    conn.close()
    return {"ok": True}


@app.post("/v1/device/{device_id}/desired")
def device_set_desired(device_id: str, payload: DesiredUpdate, request: Request):
    """Allow device to set desired state (e.g. when schedule fires)."""
    require_bearer(request, DEVICE_TOKEN)
    if device_id != DEVICE_ID:
        raise HTTPException(status_code=404, detail="Unknown device")

    conn = db()
    cur = conn.cursor()
    cur.execute("SELECT sauna_on, version FROM desired_state WHERE device_id = ?", (device_id,))
    row = cur.fetchone()
    if row is None:
        conn.close()
        raise HTTPException(status_code=404, detail="Missing desired state row")

    new_sauna_on = 1 if payload.sauna_on else 0
    old_sauna_on = int(row["sauna_on"])
    version = int(row["version"])
    if new_sauna_on != old_sauna_on:
        version += 1
        cur.execute(
            "UPDATE desired_state SET sauna_on = ?, version = ?, updated_at = ? WHERE device_id = ?",
            (new_sauna_on, version, utc_now_iso(), device_id),
        )
        conn.commit()
    conn.close()
    return {"ok": True, "version": version}


# --- App endpoints ---
@app.get("/v1/app/{device_id}/state")
def app_state(device_id: str, request: Request):
    require_bearer(request, APP_TOKEN)
    if device_id != DEVICE_ID:
        raise HTTPException(status_code=404, detail="Unknown device")

    conn = db()
    cur = conn.cursor()
    cur.execute("SELECT sauna_on, version, updated_at FROM desired_state WHERE device_id = ?", (device_id,))
    desired = cur.fetchone()
    cur.execute("""
        SELECT temp_f, power_in, heat_in, rssi, ip, uptime_s, last_desired_version_applied,
               time_synced, epoch_utc, schedule_version, updated_at
        FROM telemetry_latest WHERE device_id = ?
    """, (device_id,))
    telem = cur.fetchone()
    conn.close()

    telemetry = None
    if telem is not None:
        telemetry = {
            "temp_f": telem["temp_f"],
            "power_in": None if telem["power_in"] is None else bool(telem["power_in"]),
            "heat_in": None if telem["heat_in"] is None else bool(telem["heat_in"]),
            "rssi": telem["rssi"],
            "ip": telem["ip"],
            "uptime_s": telem["uptime_s"],
            "last_desired_version_applied": telem["last_desired_version_applied"],
            "time_synced": None if telem["time_synced"] is None else bool(telem["time_synced"]),
            "epoch_utc": telem["epoch_utc"],
            "schedule_version": telem["schedule_version"],
            "updated_at": telem["updated_at"],
        }

    return {
        "device_id": device_id,
        "desired": {
            "sauna_on": bool(desired["sauna_on"]),
            "version": int(desired["version"]),
            "updated_at": desired["updated_at"],
        },
        "telemetry": telemetry,
    }


@app.post("/v1/app/{device_id}/desired")
def app_set_desired(device_id: str, payload: DesiredUpdate, request: Request):
    require_bearer(request, APP_TOKEN)
    if device_id != DEVICE_ID:
        raise HTTPException(status_code=404, detail="Unknown device")

    conn = db()
    cur = conn.cursor()
    cur.execute("SELECT sauna_on, version FROM desired_state WHERE device_id = ?", (device_id,))
    row = cur.fetchone()
    if row is None:
        conn.close()
        raise HTTPException(status_code=404, detail="Missing desired state row")

    new_sauna_on = 1 if payload.sauna_on else 0
    old_sauna_on = int(row["sauna_on"])
    version = int(row["version"])
    if new_sauna_on != old_sauna_on:
        version += 1
        cur.execute(
            "UPDATE desired_state SET sauna_on = ?, version = ?, updated_at = ? WHERE device_id = ?",
            (new_sauna_on, version, utc_now_iso(), device_id),
        )
        conn.commit()
    conn.close()
    return {"ok": True, "version": version}


# --- Schedule app endpoints ---
@app.get("/v1/app/{device_id}/schedule")
def app_get_schedule(device_id: str, request: Request):
    require_bearer(request, APP_TOKEN)
    if device_id != DEVICE_ID:
        raise HTTPException(status_code=404, detail="Unknown device")

    conn = db()
    cur = conn.cursor()
    if delete_past_schedule_sessions(cur, device_id):
        bump_schedule_version(cur, device_id)
        conn.commit()
    cur.execute(
        """SELECT id, start_time_utc, duration_min, preheat_min, enabled, note, updated_at
           FROM schedule_sessions WHERE device_id = ?
           ORDER BY start_time_utc""",
        (device_id,),
    )
    rows = cur.fetchall()
    conn.close()

    sessions = []
    for r in rows:
        sessions.append({
            "id": r["id"],
            "start_time_utc": r["start_time_utc"],
            "enabled": int(r["enabled"]),
            "updated_at": r["updated_at"],
        })
    return {"sessions": sessions}


@app.post("/v1/app/{device_id}/schedule")
def app_create_schedule(device_id: str, payload: ScheduleSessionCreate, request: Request):
    require_bearer(request, APP_TOKEN)
    if device_id != DEVICE_ID:
        raise HTTPException(status_code=404, detail="Unknown device")

    now = utc_now_iso()
    conn = db()
    cur = conn.cursor()
    bump_schedule_version(cur, device_id)
    cur.execute("""
        INSERT INTO schedule_sessions (device_id, start_time_utc, duration_min, preheat_min, enabled, note, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?)
    """, (device_id, payload.start_time_utc, 0, 0, payload.enabled, "", now))
    sid = cur.lastrowid
    conn.commit()
    conn.close()
    return {"ok": True, "id": sid}


@app.put("/v1/app/{device_id}/schedule/{session_id}")
def app_update_schedule(device_id: str, session_id: int, payload: ScheduleSessionUpdate, request: Request):
    require_bearer(request, APP_TOKEN)
    if device_id != DEVICE_ID:
        raise HTTPException(status_code=404, detail="Unknown device")

    conn = db()
    cur = conn.cursor()
    cur.execute("SELECT id FROM schedule_sessions WHERE device_id = ? AND id = ?", (device_id, session_id))
    if cur.fetchone() is None:
        conn.close()
        raise HTTPException(status_code=404, detail="Session not found")

    updates = []
    args = []
    if payload.start_time_utc is not None:
        updates.append("start_time_utc = ?")
        args.append(payload.start_time_utc)
    if payload.enabled is not None:
        updates.append("enabled = ?")
        args.append(payload.enabled)

    if updates:
        now = utc_now_iso()
        updates.append("updated_at = ?")
        args.append(now)
        bump_schedule_version(cur, device_id)
        args.append(session_id)
        cur.execute(
            "UPDATE schedule_sessions SET " + ", ".join(updates) + " WHERE id = ?",
            args,
        )
        conn.commit()
    conn.close()
    return {"ok": True}


@app.delete("/v1/app/{device_id}/schedule/{session_id}")
def app_delete_schedule(device_id: str, session_id: int, request: Request):
    require_bearer(request, APP_TOKEN)
    if device_id != DEVICE_ID:
        raise HTTPException(status_code=404, detail="Unknown device")

    conn = db()
    cur = conn.cursor()
    cur.execute("DELETE FROM schedule_sessions WHERE device_id = ? AND id = ?", (device_id, session_id))
    if cur.rowcount == 0:
        conn.close()
        raise HTTPException(status_code=404, detail="Session not found")
    bump_schedule_version(cur, device_id)
    conn.commit()
    conn.close()
    return {"ok": True}


# --- UI ---
@app.get("/", response_class=HTMLResponse)
def index():
    return HTMLResponse(content=UI_HTML)


UI_HTML = f"""
<!doctype html>
<html>
<head>
  <meta charset="utf-8"/>
  <meta name="viewport" content="width=device-width, initial-scale=1"/>
  <title>Sauna Control</title>
  <style>
    * {{ box-sizing: border-box; }}
    body {{ font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, "Helvetica Neue", Arial, sans-serif;
      margin: 0; padding: 0; background: #f5f5f5; color: #2d3748; line-height: 1.5; }}
    .header {{ width: 100%; height: 220px; background: url("/static/images/header.jpeg") center/cover no-repeat; }}
    .content {{ max-width: 900px; margin: -40px auto 40px; padding: 0 20px; display: grid; grid-template-columns: 1fr 1.8fr; gap: 24px; }}
    .card {{ background: #fff; border-radius: 12px; padding: 24px; box-shadow: 0 4px 20px rgba(0,0,0,.08); }}
    .sidebar {{ display: flex; flex-direction: column; align-items: center; text-align: center; }}
    .logo {{ width: 100px; height: 100px; border-radius: 50%; object-fit: cover; margin-bottom: 12px; border: 3px solid #fff; box-shadow: 0 4px 12px rgba(0,0,0,.15); }}
    .title {{ font-size: 22px; font-weight: 700; margin: 0 0 4px 0; color: #1a202c; }}
    .muted {{ color: #718096; font-size: 13px; margin-bottom: 16px; }}
    .temp {{ font-size: 36px; font-weight: 800; letter-spacing: -1px; color: #2d3748; margin: 8px 0; }}
    .pills {{ display: flex; gap: 10px; flex-wrap: wrap; justify-content: center; margin: 12px 0; }}
    .pill {{ display: inline-flex; align-items: center; gap: 8px; padding: 8px 14px; border-radius: 999px;
      border: 1px solid #e2e8f0; background: #f7fafc; font-weight: 500; font-size: 14px; }}
    .dot {{ width: 10px; height: 10px; border-radius: 50%; background: #a0aec0; }}
    .dot.green {{ background: #38a169; }} .dot.red {{ background: #e53e3e; }}
    .big {{ width: 100%; font-size: 16px; padding: 14px 20px; border-radius: 10px; border: 2px solid transparent;
      background: #38c4b7; color: #fff; cursor: pointer; font-weight: 600; margin-top: 8px; transition: opacity 0.2s; }}
    .big:active {{ transform: translateY(1px); }}
    .big.pending {{ cursor: not-allowed; opacity: 0.9; animation: pulse-btn 1.2s ease-in-out infinite; }}
    .big.pending:hover {{ background: #38c4b7; color: #fff; border-color: transparent; }}
    .big.disconnected {{ cursor: not-allowed; background: #a0aec0; color: #fff; }}
    .big.disconnected:hover {{ background: #a0aec0; color: #fff; border-color: transparent; }}
    @keyframes pulse-btn {{ 0%, 100% {{ opacity: 0.9; box-shadow: 0 0 0 0 rgba(56, 196, 183, 0.5); }} 50% {{ opacity: 1; box-shadow: 0 0 0 8px rgba(56, 196, 183, 0); }} }}
    @media (hover: hover) {{ .big:hover:not(.pending) {{ background: #fff; color: #38c4b7; border-color: #38c4b7; }} }}
    .row {{ display: flex; justify-content: space-between; align-items: center; gap: 12px; margin: 12px 0; flex-wrap: wrap; }}
    .section {{ margin-top: 24px; padding-top: 20px; border-top: 1px solid #e2e8f0; }}
    .section h3 {{ margin: 0 0 12px 0; font-size: 18px; font-weight: 700; color: #1a202c;
      padding-bottom: 8px; border-bottom: 3px solid #38c4b7; display: inline-block; }}
    .form-row {{ margin: 10px 0; }}
    .form-row label {{ display: block; margin-bottom: 4px; font-size: 13px; color: #718096; }}
    input, select {{ width: 100%; font-size: 14px; padding: 10px 12px; border-radius: 8px;
      border: 1px solid #e2e8f0; background: #fff; color: #2d3748; }}
    .btn {{ font-size: 14px; padding: 10px 16px; border-radius: 8px; border: 1px solid #e2e8f0;
      background: #fff; color: #2d3748; cursor: pointer; font-weight: 500; }}
    .btn-accent {{ background: #38c4b7; color: #fff; border: 2px solid transparent; }}
    @media (hover: hover) {{ .btn:hover {{ background: #f7fafc; border-color: #cbd5e0; }} .btn-accent:hover {{ background: #fff; color: #38c4b7; border-color: #38c4b7; }} }}
    .schedule-list {{ list-style: none; padding: 0; margin: 12px 0; }}
    .schedule-list li {{ padding: 12px 14px; margin: 8px 0; background: #f7fafc; border-radius: 8px;
      border: 1px solid #e2e8f0; display: flex; justify-content: space-between; align-items: center; flex-wrap: wrap; gap: 8px; }}
    .schedule-list .actions {{ display: flex; gap: 8px; }}
    .auth-grid {{ display: grid; grid-template-columns: 1fr auto; gap: 10px; align-items: end; }}
    @media (max-width: 500px) {{ .auth-grid {{ grid-template-columns: 1fr; }} }}
    @media (max-width: 700px) {{ .content {{ grid-template-columns: 1fr; margin-top: -20px; padding: 0 24px; }} .card {{ padding: 24px 48px !important; }} }}
  </style>
</head>
<body>
  <div class="header"></div>
  <div class="content">
    <div class="card sidebar">
      <img src="/static/images/logo.png" alt="Logo" class="logo"/>
      <h1 class="title">Sauna Control</h1>
      <div class="muted" id="lastUpdate">–</div>
      <div class="temp" id="temp">--.-°F</div>
      <div class="muted">Temperature</div>
      <div class="pills">
        <div class="pill"><span class="dot" id="powerDot"></span> Power</div>
        <div class="pill"><span class="dot" id="heatDot"></span> Heat</div>
      </div>
      <button class="big" id="toggleBtn">Toggle Sauna</button>
      <div class="row" style="margin-top:12px; justify-content:center;">
        <span class="muted">Desired: <span id="desiredText">–</span></span>
        <span class="muted">Applied: <span id="appliedText">–</span></span>
      </div>
    </div>
    <div class="card">
      <div class="section">
        <h3>Schedule</h3>
        <ul class="schedule-list" id="scheduleList"></ul>
        <div class="form-row">
          <label>Date</label>
          <input type="date" id="schedDate"/>
        </div>
        <div class="form-row">
          <label>Time (local)</label>
          <input type="time" id="schedTime"/>
        </div>
        <button class="btn btn-accent" id="schedAddBtn">Add session</button>
      </div>
      <div class="section" id="authSection">
        <h3>Auth</h3>
        <div class="auth-grid">
          <div class="form-row" style="margin:0;">
            <label>APP_TOKEN</label>
            <input id="token" placeholder="Paste token" type="password"/>
          </div>
          <button class="btn btn-accent" id="saveBtn">Save</button>
        </div>
        <div class="muted" style="margin-top:8px;">Store token in your password manager, paste once, hit Save.</div>
      </div>
    </div>
  </div>

<script>
const deviceId = "{DEVICE_ID}";
const stateUrl = `/v1/app/${{deviceId}}/state`;
const desiredUrl = `/v1/app/${{deviceId}}/desired`;
const scheduleUrl = `/v1/app/${{deviceId}}/schedule`;

function getToken() {{ return (document.getElementById("token").value || localStorage.getItem("APP_TOKEN") || "").trim(); }}

function loadTokenIntoField() {{ document.getElementById("token").value = localStorage.getItem("APP_TOKEN") || ""; }}

function hideAuthSection() {{ const el = document.getElementById("authSection"); if (el) el.style.display = "none"; }}
function showAuthSection() {{ const el = document.getElementById("authSection"); if (el) el.style.display = ""; }}

function saveToken() {{
  const t = document.getElementById("token").value.trim();
  localStorage.setItem("APP_TOKEN", t);
  return t;
}}

function setDot(el, val) {{
  el.classList.remove("green","red");
  if (val === true) el.classList.add("green");
  else if (val === false) el.classList.add("red");
}}

function formatLocal(isoUtc) {{
  if (!isoUtc) return "–";
  const d = new Date(isoUtc);
  return d.toLocaleString(undefined, {{ dateStyle: "short", timeStyle: "short" }});
}}

function localToUtcIso(dateStr, timeStr) {{
  if (!dateStr || !timeStr) return null;
  const d = new Date(dateStr + "T" + timeStr);
  if (isNaN(d.getTime())) return null;
  return d.toISOString();
}}

let pendingDesired = null;
let pendingSince = 0;
let successState = null;
const PENDING_TIMEOUT_MS = 60000;

function setToggleBtnState(text, opts) {{
  const {{ pulse = false, disabled = false, disconnected = false }} = opts || {{}};
  const btn = document.getElementById("toggleBtn");
  btn.textContent = text;
  btn.disabled = disabled;
  btn.classList.toggle("pending", pulse);
  btn.classList.toggle("disconnected", disconnected);
}}

function updateToggleButton(isDisconnected, appliedValue, telemAgeSec) {{
  const now = Date.now();
  if (successState) {{
    if (now >= successState.until) {{
      successState = null;
      setToggleBtnState("Toggle Sauna", {{ pulse: false, disabled: false }});
    }} else {{
      setToggleBtnState(successState.label, {{ pulse: false, disabled: true }});
    }}
    return;
  }}
  if (isDisconnected && pendingDesired === null) {{
    setToggleBtnState("Trying to Connect…", {{ pulse: false, disabled: true, disconnected: true }});
    return;
  }}
  if (pendingDesired !== null) {{
    if (now - pendingSince > PENDING_TIMEOUT_MS) {{
      pendingDesired = null;
      setToggleBtnState("Toggle Sauna", {{ pulse: false, disabled: false }});
      return;
    }}
    const telemFresh = telemAgeSec < 15;
    if (telemFresh && appliedValue !== null && appliedValue !== undefined && appliedValue === pendingDesired) {{
      const label = pendingDesired ? "Power On!" : "Power Off!";
      successState = {{ label, until: now + 2000 }};
      pendingDesired = null;
      setToggleBtnState(label, {{ pulse: false, disabled: true }});
      setTimeout(() => {{
        successState = null;
        setToggleBtnState("Toggle Sauna", {{ pulse: false, disabled: false }});
      }}, 2000);
      return;
    }}
    setToggleBtnState("Sending…", {{ pulse: true, disabled: true }});
    return;
  }}
  setToggleBtnState("Toggle Sauna", {{ pulse: false, disabled: false }});
}}

async function fetchState() {{
  const token = getToken();
  if (!token) return;
  const res = await fetch(stateUrl, {{ headers: {{ "Authorization": "Bearer " + token }} }});
  if (!res.ok) {{ document.getElementById("lastUpdate").textContent = "Auth error"; showAuthSection(); return; }}
  hideAuthSection();
  const data = await res.json();
  const telem = data.telemetry;
  const desired = data.desired;
  document.getElementById("desiredText").textContent = desired.sauna_on ? "ON" : "OFF";
  const telemAgeSec = telem && telem.updated_at ? (Date.now() - new Date(telem.updated_at).getTime()) / 1000 : Infinity;
  const isDisconnected = !telem || telemAgeSec > 15;
  const appliedValue = telem && telem.power_in !== null && telem.power_in !== undefined ? telem.power_in : null;
  if (telem) {{
    const tf = (telem.temp_f == null) ? "--.-" : telem.temp_f.toFixed(1);
    document.getElementById("temp").textContent = tf + "°F";
    setDot(document.getElementById("powerDot"), telem.power_in);
    setDot(document.getElementById("heatDot"), telem.heat_in);
    document.getElementById("appliedText").textContent = pendingDesired !== null ? "Pending…" : (isDisconnected ? "Disconnected" : (telem.power_in === true ? "ON" : telem.power_in === false ? "OFF" : "–"));
    document.getElementById("lastUpdate").textContent = telem.updated_at ? new Date(telem.updated_at).toLocaleString() : "";
  }} else {{
    document.getElementById("temp").textContent = "--.-°F";
    setDot(document.getElementById("powerDot"), null);
    setDot(document.getElementById("heatDot"), null);
    document.getElementById("appliedText").textContent = pendingDesired !== null ? "Pending…" : "Disconnected";
    document.getElementById("lastUpdate").textContent = "No telemetry yet";
  }}
  updateToggleButton(isDisconnected, appliedValue, telemAgeSec);
}}

let scheduleSessionsList = [];
async function fetchSchedule() {{
  const token = getToken();
  if (!token) return;
  const res = await fetch(scheduleUrl, {{ headers: {{ "Authorization": "Bearer " + token }} }});
  if (!res.ok) return;
  const data = await res.json();
  scheduleSessionsList = data.sessions || [];
  const list = document.getElementById("scheduleList");
  list.innerHTML = "";
  scheduleSessionsList.forEach(s => {{
    const li = document.createElement("li");
    li.innerHTML = `<span>${{formatLocal(s.start_time_utc)}}</span>
      <div class="actions">
        <button class="btn" data-edit-id="${{s.id}}">Edit</button>
        <button class="btn" data-delete="${{s.id}}">Delete</button>
      </div>`;
    list.appendChild(li);
  }});
  list.querySelectorAll("[data-delete]").forEach(btn => btn.addEventListener("click", () => deleteSession(parseInt(btn.dataset.delete))));
  list.querySelectorAll("[data-edit-id]").forEach(btn => btn.addEventListener("click", () => {{
    const s = scheduleSessionsList.find(x => x.id === parseInt(btn.dataset.editId));
    if (!s) return;
    document.getElementById("schedDate").value = s.start_time_utc.slice(0, 10);
    document.getElementById("schedTime").value = new Date(s.start_time_utc).toTimeString().slice(0, 5);
    document.getElementById("schedAddBtn").dataset.editId = s.id;
  }}));
}}

async function addOrUpdateSession() {{
  const token = getToken();
  if (!token) return;
  const dateStr = document.getElementById("schedDate").value;
  const timeStr = document.getElementById("schedTime").value;
  const utcIso = localToUtcIso(dateStr, timeStr);
  if (!utcIso) {{ alert("Set date and time"); return; }}
  const editId = document.getElementById("schedAddBtn").dataset.editId;
  if (editId) {{
    const res = await fetch(`${{scheduleUrl}}/${{editId}}`, {{
      method: "PUT",
      headers: {{ "Authorization": "Bearer " + token, "Content-Type": "application/json" }},
      body: JSON.stringify({{ start_time_utc: utcIso }})
    }});
    if (!res.ok) {{ alert("Update failed"); return; }}
    delete document.getElementById("schedAddBtn").dataset.editId;
  }} else {{
    const res = await fetch(scheduleUrl, {{
      method: "POST",
      headers: {{ "Authorization": "Bearer " + token, "Content-Type": "application/json" }},
      body: JSON.stringify({{ start_time_utc: utcIso, enabled: 1 }})
    }});
    if (!res.ok) {{ alert("Add failed"); return; }}
  }}
  fetchSchedule();
}}

async function deleteSession(id) {{
  const token = getToken();
  if (!token) return;
  const res = await fetch(`${{scheduleUrl}}/${{id}}`, {{ method: "DELETE", headers: {{ "Authorization": "Bearer " + token }} }});
  if (!res.ok) {{ alert("Delete failed"); return; }}
  fetchSchedule();
}}

async function toggleDesired() {{
  const token = getToken();
  if (!token) return;
  const btn = document.getElementById("toggleBtn");
  if (btn.disabled) return;
  setToggleBtnState("Sending…", {{ pulse: true, disabled: true }});
  const res = await fetch(stateUrl, {{ headers: {{ "Authorization": "Bearer " + token }} }});
  if (!res.ok) {{ setToggleBtnState("Toggle Sauna", {{ pulse: false, disabled: false }}); return; }}
  const data = await res.json();
  const next = !data.desired.sauna_on;
  const postRes = await fetch(desiredUrl, {{ method: "POST", headers: {{ "Authorization": "Bearer " + token, "Content-Type": "application/json" }}, body: JSON.stringify({{ sauna_on: next }}) }});
  if (!postRes.ok) {{ setToggleBtnState("Toggle Sauna", {{ pulse: false, disabled: false }}); return; }}
  pendingDesired = next;
  pendingSince = Date.now();
  fetchState();
}}

loadTokenIntoField();
document.getElementById("saveBtn").addEventListener("click", () => {{ saveToken(); fetchState(); fetchSchedule(); }});
document.getElementById("toggleBtn").addEventListener("click", toggleDesired);
document.getElementById("schedAddBtn").addEventListener("click", addOrUpdateSession);

fetchState();
fetchSchedule();
setInterval(fetchState, 5000);
setInterval(fetchSchedule, 3000);
</script>
</body>
</html>
"""
