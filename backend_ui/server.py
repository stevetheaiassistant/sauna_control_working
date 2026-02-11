import os
import sqlite3
import time
from datetime import datetime, timezone
from typing import Optional, Dict, Any

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel


DB_PATH = os.environ.get("SAUNA_DB", "sauna.db")

# Set these in your VPS env
DEVICE_TOKEN = os.environ.get("SAUNA_DEVICE_TOKEN", "CHANGE_ME_DEVICE_TOKEN")
APP_TOKEN = os.environ.get("SAUNA_APP_TOKEN", "CHANGE_ME_APP_TOKEN")

# One device for now (keep it simple). You can extend to many later.
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

    # Seed desired state if not present
    cur.execute("SELECT device_id FROM desired_state WHERE device_id = ?", (DEVICE_ID,))
    row = cur.fetchone()
    if row is None:
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


app = FastAPI(title="Sauna Control", version="1.0.0")

# Optional: if you serve the HTML from same domain, you don't need CORS.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)

init_db()


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


@app.get("/", response_class=HTMLResponse)
def index():
    # Single-page UI that uses the /v1/app endpoints below.
    return HTMLResponse(content=UI_HTML)


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

    return {
        "sauna_on": bool(row["sauna_on"]),
        "version": int(row["version"]),
        "updated_at": row["updated_at"],
    }


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
            last_desired_version_applied, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(device_id) DO UPDATE SET
            temp_f=excluded.temp_f,
            power_in=excluded.power_in,
            heat_in=excluded.heat_in,
            rssi=excluded.rssi,
            ip=excluded.ip,
            uptime_s=excluded.uptime_s,
            last_desired_version_applied=excluded.last_desired_version_applied,
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
        utc_now_iso()
    ))

    conn.commit()
    conn.close()
    return {"ok": True}


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
        SELECT temp_f, power_in, heat_in, rssi, ip, uptime_s, last_desired_version_applied, updated_at
        FROM telemetry_latest WHERE device_id = ?
    """, (device_id,))
    telem = cur.fetchone()

    conn.close()

    # telemetry might not exist yet
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
            "updated_at": telem["updated_at"],
        }

    return {
        "device_id": device_id,
        "desired": {
            "sauna_on": bool(desired["sauna_on"]),
            "version": int(desired["version"]),
            "updated_at": desired["updated_at"],
        },
        "telemetry": telemetry
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
        raise HTTPException(status_code=404, detail="Missing desired state row")

    new_sauna_on = 1 if payload.sauna_on else 0
    old_sauna_on = int(row["sauna_on"])
    version = int(row["version"])

    # Only bump version if the desired value actually changes
    if new_sauna_on != old_sauna_on:
        version += 1
        cur.execute("""
            UPDATE desired_state
            SET sauna_on = ?, version = ?, updated_at = ?
            WHERE device_id = ?
        """, (new_sauna_on, version, utc_now_iso(), device_id))
        conn.commit()

    conn.close()
    return {"ok": True, "version": version}


UI_HTML = f"""
<!doctype html>
<html>
<head>
  <meta charset="utf-8"/>
  <meta name="viewport" content="width=device-width, initial-scale=1"/>
  <title>Sauna Control</title>
  <style>
    body {{
      font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Arial, sans-serif;
      margin: 0; padding: 24px;
      background: #0b0f14; color: #e8eef6;
    }}
    .card {{
      max-width: 560px;
      margin: 0 auto;
      background: #111826;
      border: 1px solid #1c2a3d;
      border-radius: 16px;
      padding: 18px;
      box-shadow: 0 8px 30px rgba(0,0,0,.35);
    }}
    .row {{ display:flex; justify-content:space-between; align-items:center; gap:12px; margin: 14px 0; }}
    .big {{
      width: 100%;
      font-size: 20px;
      padding: 14px 16px;
      border-radius: 14px;
      border: 1px solid #2a3b54;
      background: #182235;
      color: #e8eef6;
      cursor: pointer;
    }}
    .big:active {{ transform: translateY(1px); }}
    .pill {{
      display:inline-flex; align-items:center; gap:10px;
      padding: 10px 12px;
      border-radius: 999px;
      border: 1px solid #2a3b54;
      background: #0d1422;
      font-weight: 600;
    }}
    .dot {{
      width: 14px; height: 14px; border-radius: 50%;
      background: #6b7280;
      box-shadow: 0 0 0 3px rgba(255,255,255,.04) inset;
    }}
    .dot.green {{ background: #22c55e; }}
    .dot.red {{ background: #ef4444; }}
    .muted {{ color: #9fb2cc; font-size: 13px; }}
    .temp {{ font-size: 44px; font-weight: 800; letter-spacing: -1px; }}
    .grid {{ display:grid; grid-template-columns: 1fr 1fr; gap: 12px; }}
    input {{
      width: 100%;
      font-size: 14px;
      padding: 10px 12px;
      border-radius: 12px;
      border: 1px solid #2a3b54;
      background: #0d1422;
      color: #e8eef6;
      box-sizing: border-box;
    }}
    .btn {{
      font-size: 14px;
      padding: 10px 12px;
      border-radius: 12px;
      border: 1px solid #2a3b54;
      background: #182235;
      color: #e8eef6;
      cursor: pointer;
    }}
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

    <div style="margin-top:14px;">
      <div class="muted" style="margin-bottom:8px;">Auth</div>
      <div class="grid">
        <input id="token" placeholder="APP_TOKEN" type="password"/>
        <button class="btn" id="saveBtn">Save</button>
      </div>
      <div class="muted" style="margin-top:8px;">
        Tip: store token in your iPhone password manager, paste once, hit Save.
      </div>
    </div>
  </div>

<script>
const deviceId = "{DEVICE_ID}";
const stateUrl = `/v1/app/${{deviceId}}/state`;
const desiredUrl = `/v1/app/${{deviceId}}/desired`;

function loadToken() {{
  const t = localStorage.getItem("APP_TOKEN") || "";
  document.getElementById("token").value = t;
  return t;
}}

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

async function fetchState() {{
  const token = loadToken();
  if (!token) return;

  const res = await fetch(stateUrl, {{
    headers: {{ "Authorization": "Bearer " + token }}
  }});

  if (!res.ok) {{
    document.getElementById("lastUpdate").textContent = "Auth error";
    return;
  }}

  const data = await res.json();

  const telem = data.telemetry;
  const desired = data.desired;

  document.getElementById("desiredText").textContent =
    desired.sauna_on ? `ON (v${{desired.version}})` : `OFF (v${{desired.version}})`;

  if (telem) {{
    const tf = (telem.temp_f == null) ? "--.-" : telem.temp_f.toFixed(1);
    document.getElementById("temp").textContent = tf + "°F";

    setDot(document.getElementById("powerDot"), telem.power_in);
    setDot(document.getElementById("heatDot"), telem.heat_in);

    document.getElementById("appliedText").textContent =
      (telem.last_desired_version_applied == null) ? "-" : `v${{telem.last_desired_version_applied}}`;

    document.getElementById("lastUpdate").textContent = "Updated " + (telem.updated_at || "");
  }} else {{
    document.getElementById("temp").textContent = "--.-°F";
    setDot(document.getElementById("powerDot"), null);
    setDot(document.getElementById("heatDot"), null);
    document.getElementById("appliedText").textContent = "-";
    document.getElementById("lastUpdate").textContent = "No telemetry yet";
  }}
}}

async function toggleDesired() {{
  const token = loadToken();
  if (!token) return;

  // Read current state first, then flip desired
  const res = await fetch(stateUrl, {{
    headers: {{ "Authorization": "Bearer " + token }}
  }});
  if (!res.ok) return;

  const data = await res.json();
  const next = !data.desired.sauna_on;

  await fetch(desiredUrl, {{
    method: "POST",
    headers: {{
      "Authorization": "Bearer " + token,
      "Content-Type": "application/json"
    }},
    body: JSON.stringify({{ sauna_on: next }})
  }});

  await fetchState();
}}

document.getElementById("saveBtn").addEventListener("click", () => {{
  saveToken();
  fetchState();
}});

document.getElementById("toggleBtn").addEventListener("click", toggleDesired);

// Poll UI every 1s (this is UI polling, not device polling)
loadToken();
fetchState();
setInterval(fetchState, 1000);
</script>
</body>
</html>
"""
