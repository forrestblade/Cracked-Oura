# SESSION HANDOFF — 2026-07-21 evening (ring crash day)

> Read this FIRST. `ringlink/HANDOFF.md` is the deep protocol/history doc;
> this file is the state of the world as of ~18:11 CDT 2026-07-21.

## CURRENT STATE: ✅ ALL GREEN (verify before trusting)

- **Ring**: Ring 4, worn, live-connected to the persistent daemon. Battery ~100%.
- **Daemon** (`ringlink/ring_daemon.py`): holds a permanent BLE connection,
  20 s flush/drain loop, auto-ingest every 5 min. PID in `ringlink/.daemon.lock`.
- **Backend**: uvicorn on :8000, **started WITHOUT --reload** (see gotcha #1),
  running from `backend/venv`. Log: `backend_server.log` (repo root).
  After editing backend code you must MANUALLY kill + restart it:
  `nohup backend/venv/Scripts/python.exe -m uvicorn backend.src.api.main:app --host 127.0.0.1 --port 8000 >> backend_server.log 2>&1 &`
- **Frontend**: vite on :5173 + electron (dev). Right Now page = rolling 3 h
  windows, auto-refresh every 60 s.
- **DB**: `%APPDATA%/CrackedOura/oura_database.db`. Verify freshness:
  `SELECT MAX(timestamp) FROM heart_rate;` — should be < ~10 min old while worn.

Quick health check:
```bash
curl -s http://127.0.0.1:8000/api/ring/status   # want: indicator ok, live true
tail -20 ringlink/daemon.log
cat ringlink/ring_status.json
```

## WHAT HAPPENED TODAY (the short version)

1. **The ring's firmware crashed spontaneously at 15:14 CDT** while worn
   (96% battery). Uptime counter reset, on-ring history wiped, crash-looped
   ~45 min (only alive on charger). Recovered after charger dockings + rewear.
   HR 15:14→17:00 is permanently lost (never recorded).
2. The crash exposed a pile of real bugs, all fixed today (list below).
3. Architecture changed from poll-every-15-min to a **persistent connection
   daemon** — the model the ring firmware expects (the official app holds the
   link 24/7 and refreshes every 20 s; open_ring PROTOCOL.md §10).

## KEY DISCOVERIES (do not re-learn these the hard way)

- **A worn ring's radio sleeps in waves** — silent for many minutes, then
  advertises. Unconnected+idle it advertises every ~150 ms; on charger always.
  One catch = daemon holds forever, so waves only matter after link loss.
  **Dock the ring ~5 s = instant catch** (user-facing "force sync" gesture).
- **Data is NEVER lost during radio silence** — ring buffers to flash; every
  catch back-fills the DB and charts repaint retroactively.
- **The ring's internal clock PAUSES during crash/hibernate** → its 0x42 time
  anchors can be hours behind. Decoder now validates anchors against host
  receive time (`ANCHOR_HOST_TOLERANCE_MS`, decode_events.py) — this is what
  the official app does too.
- **Ring reboots restart ring_time from ~0** → early frames byte-identical to
  previous boot → naive dedupe swallowed reboots. Decoder now tracks **boot
  generations** (drain-session max-ring_time regression = reboot).
- **pc-ble-driver silently ignores conn-param update requests** → ring drops
  the held link. Fixed: `on_conn_param_update_request` accepts (oura_ring.py).
  Validated live tonight (1 accept, link held).
- **SMP collision on reconnect after ring reboot**: ring initiates its own
  security with a stale bond → our authenticate gets `INVALID_STATE`. Fixed:
  wait 3 s, subscribe directly (oura_ring.py `_subscribe_with_pairing_fallback`).
- **Abandoned pending connects jam the ring** (a connected ring stops
  advertising). Fixed: `sd_ble_gap_connect_cancel` on scan timeout.
- **Dongle H5 transport wedges** (`NRF_ERROR_SD_RPC_H5_TRANSPORT_STATE`) —
  known upstream bug; only a USB re-enumeration fixes it. Auto-reset ladder
  exists (see Outstanding #2). A scan that hears ZERO ambient devices = deaf
  dongle (routed to the same ladder).
- **Phone-faithful session order matters** (open_ring §6.1/§10, btsnoop-verified):
  auth → subscribe enable (16 01 02) → state_cmd (1c 01 bf) → caps dance →
  battery → data_flush (28 01 00) → GetEvent(cursor) → ack-fetch (max_events=0)
  → time-sync LAST. All implemented in oura_ring.py / ring_daemon.py.
- **Soft reset** `0e 01 ff` (open_ring §6.8): CLI `oura_ring.py soft-reset`, or
  touch `ringlink/soft_reset.request` while the daemon is connected. Tonight
  the ring dropped the link on it without a clean ack — semantics uncertain.

## GOTCHAS (environment)

1. **uvicorn --reload wedges and orphans workers serving STALE code** on :8000
   (bit us twice today — UI "not updating" was a stale backend). Backend now
   runs WITHOUT --reload. Hunt strays:
   `netstat -ano | grep :8000` + kill; also kill `multiprocessing.spawn` /
   rogue uv-python uvicorn processes.
2. **Git Bash mangles `schtasks /Run`** → use `MSYS_NO_PATHCONV=1`.
3. **`ls` on the lock DIRS prints nothing** (they're empty dirs): `.sync.lock`
   (one-shot), `.daemon.lock` (holds daemon PID).
4. Frontend `.npmrc` sets script-shell to Git Bash; npm scripts assume it.

## FILES CHANGED TODAY (all verified syntax/tsc-clean)

**Frontend** (Right Now 3 h rolling windows):
- `src/components/widgets/SmartTrendWidgetCanvas.tsx` — sub-day relative
  windows: exact windowStart/End (crosses midnight), 60 s rolling tick +
  refetch, loading only on first load.
- `src/lib/data-processing.ts` — datetime-precision requestedEnd; daily branch
  strips time part.
- `src/hooks/useMultiOuraQuery.ts` — refreshKey param.
- `src/components/RingStatus.tsx` — live/phase labels, Sync button notice text
  (shows backend message instead of silently swallowing errors).
- `src/lib/api.ts` — ringSyncNow surfaces error detail.

**Backend**:
- `src/api/ring.py` — daemon-aware status (`live` field, wedged detection via
  heartbeat staleness), Sync button returns guidance messages when daemon owns
  the ring, `-u` on spawn, LOCK_STALE_S=420/HEARTBEAT_STALE_S=240.

**ringlink**:
- `ring_daemon.py` — NEW: persistent connection daemon (see above).
- `sync_dashboard.sh` — now a WATCHDOG that (re)starts the daemon (Task
  Scheduler `RingLocalSync` every 15 min). Log rotation for daemon.log.
- `sync_ring.py` — one-shot fallback (skips if daemon alive): retry cycles
  with driver reopen, incremental cursor (`ring_cursor.txt`, 1 h overlap,
  empty→full-drain fallback), dongle wedge ladder, status heartbeats.
- `oura_ring.py` — scan-timeout retries, connect-cancel, SMP-collision heal,
  conn-param accept, engage_data_plane/data_flush/ack-fetch, tz-correct
  sync_time (half-hours byte; 0xF6 for CDT = phone-verified value),
  `soft-reset` CLI cmd, deaf-dongle detection in scan errors.
- `decode_events.py` — boot-generation-aware dedupe/segmentation + host-clock
  anchor validation (rejects stale ring-clock anchors).
- `dongle_usb_reset.ps1` + `install_dongle_reset_task.sh` — USB reset ladder.
- `configure_dashboard.py` — Right Now = 3 h windows (live config also patched
  via API; user edits autosave — don't blindly re-run).

**Dashboards**: saved config in `%APPDATA%/CrackedOura/oura_dashboard.json`
via backend `/api/dashboard`. Right Now widgets: `{type:relative, value:3,
unit:hours, anchor:today}`.

## OUTSTANDING WORK

1. **Windows packaged app (double-click .exe)** — ✅ BUILDS as of the
   2026-07-21 late-evening audit session. All landmines fixed:
   - `build:backend` now picks `venv/Scripts/python.exe` or `venv/bin/python`.
   - PyInstaller 6.21 installed in `backend/venv`; `build.spec` modernized
     for PyInstaller 6 (legacy kwargs removed). Frozen backend verified to
     boot end-to-end (fails only at :8000 bind when dev backend is running).
   - `ring.py` honors `RINGLINK_DIR` env override for frozen builds.
   - electron-builder win/nsis target configured; installer produced at
     `frontend/dist/Cracked Oura Setup 0.1.0.exe`.
   - Machine gotcha: electron-builder's winCodeSign cache extraction fails
     on Windows without Developer Mode (darwin symlinks). Fixed once by
     manually extracting the 7z and renaming to
     `%LOCALAPPDATA%/electron-builder/Cache/winCodeSign/winCodeSign-2.6.0`.
   - NOT yet done: actually installing/running the packaged app end-to-end
     (it will fight the dev backend for :8000 — test with dev stack stopped).
2. **RingDongleReset scheduled task is broken**: currently registered as
   SYSTEM → non-elevated `schtasks /Run` gets Access denied. The installer
   (`install_dongle_reset_task.sh`) was FIXED (registers as user + /RL
   HIGHEST) but **has NOT been re-run** — needs one UAC prompt.
3. **Soft-reset semantics unverified** (no clean 0x0F ack observed).
4. **Sleep data**: user naps/sleeps wearing the ring → sleep summaries
   (0x49/0x4c/0x4f), SpO2 (0x6f), HRV (0x5d) should appear — exporter only
   fills heartrate/temperature/battery CSVs today. Extend exporter →
   `sleepmodel.csv`/`dailysleep.csv`/`dailyspo2.csv` so score widgets light up.
   **Investigated 2026-07-21 late evening: BLOCKED on data.** The decoded
   history contains ZERO 0x49/0x4c/0x4f/0x6f/0x5d frames (the firmware crash
   wiped on-ring history; no overnight wear since). The openring decoders
   for these tags are auto-extractor guesses (unnamed uint16s at offsets) —
   mapping them to Oura CSV columns without real frames to validate against
   would be invention. Revisit AFTER the first full night of wear: check
   `decoded_events.jsonl` for those tags, compare values against plausible
   sleep durations/stages, then extend `decode_events.py cmd_export`.
5. **Daemon steady-state DHR**: open_ring §6.7 documents a daytime-HR burst
   mode (re-trigger every 15 s) — not implemented; current HR cadence is the
   ring's own background sampling.

## CHEAT SHEET

```bash
# daemon
cat ringlink/.daemon.lock                     # PID
tail -f ringlink/daemon.log
kill <pid> && rm ringlink/.daemon.lock && ringlink/sync_dashboard.sh  # restart

# force instant ring catch: dock the ring ~5 s (or wait for its adv wave)

# manual one-shot sync (only if daemon stopped)
cd ringlink && ./venv310/Scripts/python.exe -u sync_ring.py

# decode/ingest pipeline manually
cd ringlink && ./venv310/Scripts/python.exe -u decode_events.py ingest

# backend restart (NO --reload!)
netstat -ano | grep :8000   # kill listener first
nohup backend/venv/Scripts/python.exe -m uvicorn backend.src.api.main:app \
  --host 127.0.0.1 --port 8000 >> backend_server.log 2>&1 &

# ring soft reset while daemon connected
touch ringlink/soft_reset.request
```
