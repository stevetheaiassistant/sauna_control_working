import os
import sqlite3
from datetime import datetime, timezone
from typing import Optional, List

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel


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


app = FastAPI(title="Sauna Control", version="1.0.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)

init_db()


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
    duration_min: int = 0  # 0 = no auto-off (sauna controller or user turns off)
    preheat_min: int = 30
    enabled: int = 1
    note: Optional[str] = None


class ScheduleSessionUpdate(BaseModel):
    start_time_utc: Optional[str] = None
    duration_min: Optional[int] = None
    preheat_min: Optional[int] = None
    enabled: Optional[int] = None
    note: Optional[str] = None


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


@app.get("/v1/device/{device_id}/schedule")
def get_device_schedule(device_id: str, request: Request, since: Optional[str] = None):
    require_bearer(request, DEVICE_TOKEN)
    if device_id != DEVICE_ID:
        raise HTTPException(status_code=404, detail="Unknown device")

    conn = db()
    cur = conn.cursor()
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
            "duration_min": int(r["duration_min"]),
            "preheat_min": int(r["preheat_min"]),
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
            "duration_min": int(r["duration_min"]),
            "preheat_min": int(r["preheat_min"]),
            "enabled": int(r["enabled"]),
            "note": r["note"],
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
    """, (device_id, payload.start_time_utc, payload.duration_min, payload.preheat_min, payload.enabled, payload.note or "", now))
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
    if payload.duration_min is not None:
        updates.append("duration_min = ?")
        args.append(payload.duration_min)
    if payload.preheat_min is not None:
        updates.append("preheat_min = ?")
        args.append(payload.preheat_min)
    if payload.enabled is not None:
        updates.append("enabled = ?")
        args.append(payload.enabled)
    if payload.note is not None:
        updates.append("note = ?")
        args.append(payload.note)

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
    body {{ font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Arial, sans-serif;
      margin: 0; padding: 24px; background: #0b0f14; color: #e8eef6; }}
    .card {{ max-width: 560px; margin: 0 auto; background: #111826; border: 1px solid #1c2a3d;
      border-radius: 16px; padding: 18px; box-shadow: 0 8px 30px rgba(0,0,0,.35); }}
    .row {{ display:flex; justify-content:space-between; align-items:center; gap:12px; margin: 14px 0; }}
    .big {{ width: 100%; font-size: 20px; padding: 14px 16px; border-radius: 14px; border: 1px solid #2a3b54;
      background: #182235; color: #e8eef6; cursor: pointer; }}
    .big:active {{ transform: translateY(1px); }}
    .pill {{ display:inline-flex; align-items:center; gap:10px; padding: 10px 12px; border-radius: 999px;
      border: 1px solid #2a3b54; background: #0d1422; font-weight: 600; }}
    .dot {{ width: 14px; height: 14px; border-radius: 50%; background: #6b7280; box-shadow: 0 0 0 3px rgba(255,255,255,.04) inset; }}
    .dot.green {{ background: #22c55e; }} .dot.red {{ background: #ef4444; }}
    .muted {{ color: #9fb2cc; font-size: 13px; }}
    .temp {{ font-size: 44px; font-weight: 800; letter-spacing: -1px; }}
    .grid {{ display:grid; grid-template-columns: 1fr 1fr; gap: 12px; }}
    input, select {{ width: 100%; font-size: 14px; padding: 10px 12px; border-radius: 12px;
      border: 1px solid #2a3b54; background: #0d1422; color: #e8eef6; box-sizing: border-box; }}
    .btn {{ font-size: 14px; padding: 10px 12px; border-radius: 12px; border: 1px solid #2a3b54;
      background: #182235; color: #e8eef6; cursor: pointer; }}
    .schedule-list {{ list-style: none; padding: 0; margin: 12px 0; }}
    .schedule-list li {{ padding: 10px 12px; margin: 6px 0; background: #0d1422; border-radius: 12px;
      border: 1px solid #2a3b54; display: flex; justify-content: space-between; align-items: center; flex-wrap: wrap; gap: 8px; }}
    .schedule-list .actions {{ display: flex; gap: 6px; }}
    .section {{ margin-top: 20px; padding-top: 16px; border-top: 1px solid #1c2a3d; }}
    .section h3 {{ margin: 0 0 10px 0; font-size: 16px; }}
    .form-row {{ margin: 8px 0; }}
    .form-row label {{ display: block; margin-bottom: 4px; font-size: 12px; color: #9fb2cc; }}
  </style>
</head>
<body>
  <div class="card">
    <div class="row">
      <div>
        <div style="font-size:18px; font-weight:800;">Sauna</div>
        <div class="muted">Device: {DEVICE_ID}</div>
      </div>
      <div class="muted" id="lastUpdate">–</div>
    </div>

    <div class="row">
      <div>
        <div class="muted">Temperature</div>
        <div class="temp" id="temp">--.-°F</div>
      </div>
    </div>
    <div class="grid">
      <div class="pill"><span class="dot" id="powerDot"></span> Power On</div>
      <div class="pill"><span class="dot" id="heatDot"></span> Heat On</div>
    </div>
    <div class="row" style="margin-top:16px;">
      <button class="big" id="toggleBtn">Toggle Sauna</button>
    </div>
    <div class="row">
      <div class="muted">Desired: <span id="desiredText">–</span></div>
      <div class="muted">Applied: <span id="appliedText">–</span></div>
    </div>

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
      <div class="form-row">
        <label>Preheat (min)</label>
        <input type="number" id="schedPreheat" value="30" min="0" max="120"/>
      </div>
      <div class="form-row">
        <label>Note</label>
        <input type="text" id="schedNote" placeholder="Optional"/>
      </div>
      <div class="row">
        <button class="btn" id="schedAddBtn">Add session</button>
      </div>
    </div>

    <div style="margin-top:14px;">
      <div class="muted" style="margin-bottom:8px;">Auth</div>
      <div class="grid">
        <input id="token" placeholder="APP_TOKEN" type="password"/>
        <button class="btn" id="saveBtn">Save</button>
      </div>
      <div class="muted" style="margin-top:8px;">Tip: store token in your iPhone password manager, paste once, hit Save.</div>
    </div>
  </div>

<script>
const deviceId = "{DEVICE_ID}";
const stateUrl = `/v1/app/${{deviceId}}/state`;
const desiredUrl = `/v1/app/${{deviceId}}/desired`;
const scheduleUrl = `/v1/app/${{deviceId}}/schedule`;

function getToken() {{ return (document.getElementById("token").value || localStorage.getItem("APP_TOKEN") || "").trim(); }}

function loadTokenIntoField() {{ document.getElementById("token").value = localStorage.getItem("APP_TOKEN") || ""; }}

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

async function fetchState() {{
  const token = getToken();
  if (!token) return;
  const res = await fetch(stateUrl, {{ headers: {{ "Authorization": "Bearer " + token }} }});
  if (!res.ok) {{ document.getElementById("lastUpdate").textContent = "Auth error"; return; }}
  const data = await res.json();
  const telem = data.telemetry;
  const desired = data.desired;
  document.getElementById("desiredText").textContent = desired.sauna_on ? `ON (v${{desired.version}})` : `OFF (v${{desired.version}})`;
  if (telem) {{
    const tf = (telem.temp_f == null) ? "--.-" : telem.temp_f.toFixed(1);
    document.getElementById("temp").textContent = tf + "°F";
    setDot(document.getElementById("powerDot"), telem.power_in);
    setDot(document.getElementById("heatDot"), telem.heat_in);
    document.getElementById("appliedText").textContent = (telem.last_desired_version_applied == null) ? "-" : `v${{telem.last_desired_version_applied}}`;
    document.getElementById("lastUpdate").textContent = telem.updated_at ? new Date(telem.updated_at).toLocaleString() : "";
  }} else {{
    document.getElementById("temp").textContent = "--.-°F";
    setDot(document.getElementById("powerDot"), null);
    setDot(document.getElementById("heatDot"), null);
    document.getElementById("appliedText").textContent = "-";
    document.getElementById("lastUpdate").textContent = "No telemetry yet";
  }}
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
    li.innerHTML = `<span>${{formatLocal(s.start_time_utc)}} · preheat ${{s.preheat_min}}m ${{s.note ? "· " + s.note : ""}}</span>
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
    document.getElementById("schedPreheat").value = s.preheat_min;
    document.getElementById("schedNote").value = s.note || "";
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
  const preheat = parseInt(document.getElementById("schedPreheat").value, 10) || 30;
  const note = document.getElementById("schedNote").value.trim() || null;
  const editId = document.getElementById("schedAddBtn").dataset.editId;
  if (editId) {{
    const res = await fetch(`${{scheduleUrl}}/${{editId}}`, {{
      method: "PUT",
      headers: {{ "Authorization": "Bearer " + token, "Content-Type": "application/json" }},
      body: JSON.stringify({{ start_time_utc: utcIso, preheat_min: preheat, note }})
    }});
    if (!res.ok) {{ alert("Update failed"); return; }}
    delete document.getElementById("schedAddBtn").dataset.editId;
  }} else {{
    const res = await fetch(scheduleUrl, {{
      method: "POST",
      headers: {{ "Authorization": "Bearer " + token, "Content-Type": "application/json" }},
      body: JSON.stringify({{ start_time_utc: utcIso, duration_min: 0, preheat_min: preheat, enabled: 1, note }})
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
  const res = await fetch(stateUrl, {{ headers: {{ "Authorization": "Bearer " + token }} }});
  if (!res.ok) return;
  const data = await res.json();
  const next = !data.desired.sauna_on;
  await fetch(desiredUrl, {{ method: "POST", headers: {{ "Authorization": "Bearer " + token, "Content-Type": "application/json" }}, body: JSON.stringify({{ sauna_on: next }}) }});
  fetchState();
}}

loadTokenIntoField();
document.getElementById("saveBtn").addEventListener("click", () => {{ saveToken(); fetchState(); fetchSchedule(); }});
document.getElementById("toggleBtn").addEventListener("click", toggleDesired);
document.getElementById("schedAddBtn").addEventListener("click", addOrUpdateSession);

fetchState();
fetchSchedule();
setInterval(() => {{ fetchState(); fetchSchedule(); }}, 5000);
</script>
</body>
</html>
"""
