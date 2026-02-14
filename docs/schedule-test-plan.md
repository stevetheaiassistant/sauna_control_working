# Schedule feature – manual test plan

## Prerequisites
- Backend running with schedule tables migrated (start server once so `schedule_sessions` and `schedule_meta` exist).
- ESP32 flashed with firmware, WiFi connected, NTP synced (check Serial for "NTP sync OK").
- Web UI: paste APP_TOKEN and Save.

---

## Test 1: Create session and confirm device receives it

1. In the web UI, open the **Schedule** section.
2. Create a session **45 minutes from now**:
   - **Date**: today (or tomorrow if it's late).
   - **Time (local)**: current local time + 45 minutes (e.g. if now is 14:00, set 14:45).
3. Click **Add session**. The session should appear in the list.
4. Wait up to **30 seconds** (device polls schedule every 30 s). On the ESP32 Serial monitor you should see: `Schedule synced, version=1` (or higher).
5. Optional: Inspect telemetry (e.g. via API or UI). After sync, telemetry should include `schedule_version` equal to the server's schedule version.

**Pass**: Session appears in UI; device logs "Schedule synced"; telemetry shows updated `schedule_version`.

---

## Test 2: Offline – device turns on without WiFi

1. Ensure one session exists with **start time** about **5 minutes from now**.
2. Confirm device has already synced the schedule (Serial: "Schedule synced").
3. **Disconnect device WiFi** (router off, or disable WiFi on ESP32 for testing).
4. Wait until **start time**. Within a few minutes after that moment, the device should turn the sauna **ON** (relay sequence: power toggle, then start if needed).
5. Observe Serial (if still connected via USB): device should be in "offline" path and applying schedule-derived desired state.

**Pass**: With WiFi off, at start time the sauna turns on.

---

## Test 3: WiFi back – system stable, no oscillation

1. After Test 2, sauna should be ON and device offline.
2. **Reconnect WiFi** (router on or re-enable WiFi). Device connects and resumes polling `/desired` and posting telemetry.
3. **Do not** touch the manual "Toggle Sauna" in the UI. Leave server desired state as-is (e.g. OFF).
4. Observe for **2–5 minutes**:
   - Sauna should **stay ON** (schedule session still active; reconciliation keeps ON).
   - Device should not repeatedly toggle (no oscillation).
   - Telemetry should show `time_synced: true`, `epoch_utc`, and `schedule_version`.
5. The schedule does not auto-off. You or the sauna controller turn it off. If the server desired state is OFF and no schedule window is active, the device will follow the server when online.

**Pass**: After reconnecting WiFi, sauna remains ON; no relay chattering.

---

## Test 4: Session auto-removal

1. Create a session with start time **2–3 minutes in the past** (or wait for an existing session to pass).
2. Refresh the schedule list in the UI (or wait for the 3 s auto-refresh).
3. Sessions whose start time was more than 2 minutes ago should disappear from the list.

**Pass**: Past sessions no longer appear in the UI.

---

## Optional quick checks

- **Edit session**: Change time of an existing session; confirm list updates and device syncs new version.
- **Delete session**: Delete a session; confirm it disappears and device schedule version increments.
- **Multiple sessions**: Add two sessions at different times; confirm device turns on at each start time (with WiFi off for a stricter test).
