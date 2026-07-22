<div align="center">
  <img src="frontend/public/icon.png" alt="Logo" width="128">
  <h1>Cracked Oura × RingLink</h1>
  <p><b>Read your Oura Ring 4 directly over Bluetooth LE — no account, no cloud, no subscription — and see everything in a local dashboard.</b></p>
</div>

---

This is **not** a plain fork of Cracked-Oura. It is a new project that combines
**three open-source codebases plus substantial original work** into one fully
local pipeline:

```
Oura Ring 4 ──BLE──▶ nRF52840 dongle ──▶ ringlink/ (Python)          ──▶ SQLite ──▶ Electron dashboard
                     (Nordic connectivity   drain → decode → export       %APPDATA%/       (frontend/ + backend/)
                      firmware, serial)     → ingest                      CrackedOura
```

Your health data never touches Oura's cloud. You don't need an Oura account,
an internet connection, or a subscription — just the ring you own and a ~$10
Nordic dongle.

## What this project is built from

| Component | Origin | What it contributes |
|---|---|---|
| `frontend/`, `backend/` | **[EIrno/Cracked-Oura](https://github.com/EIrno/Cracked-Oura)** (fork base) | Electron/React dashboard, FastAPI backend, SQLite ingestion (`OuraParser`), widget system, chat UI |
| `ringlink/openring/` | **[LogosIsLife/open_ring](https://github.com/LogosIsLife/open_ring)** (GPL-3.0, vendored) | Ring 4 inner-TLV framing, 50+ record decoders, event enums, the ~720-line PROTOCOL.md this pipeline is built against |
| Protocol reference | **[Th0rgal/open_oura](https://github.com/Th0rgal/open_oura)** | First public documentation of the local BLE conversation (GATT UUIDs, AES-128/ECB auth handshake + known-answer vector, frame format, event-drain loop) |
| Everything else in `ringlink/` + backend/frontend extensions | **Original work in this repo** | See below |

Full credits and licensing details: **[ATTRIBUTION.md](ATTRIBUTION.md)** and
`ringlink/openring/NOTICE.md`.

### Original work in this repo

- **`ringlink/oura_ring.py`** — a from-scratch Python BLE client for the
  Ring 4, driving an **nRF52840 USB dongle** as a BLE central via
  `pc-ble-driver-py` (the native Windows BLE stack is not used at all).
  Handles scan, pairing/bonding, key install, AES-ECB app auth, clock sync,
  and the cursor-advance history drain.
- **`ringlink/decode_events.py`** — decode → export → ingest pipeline: raw
  frames → typed records with resolved UTC timestamps → Oura-cloud-export
  format CSVs → the upstream `OuraParser` ingests them unchanged into SQLite.
- **`ringlink/ring_analysis.py`** — turns the ring's raw sleep-night frames
  into full Oura-style summaries, computed 100% locally every night:
  - **sleep sessions** from the ring's own bedtime window (`0x76`) + 30 s
    sleep-period frames (`0x6a`: HR, breath, motion, sleep state),
  - a **4-stage hypnogram** (deep/light/REM/awake — heuristic staging over
    the ring's classifier, motion, and breath variability; validated
    physiologically plausible on real nights),
  - **HRV (RMSSD)** from the beat-to-beat IBI stream (`0x60`) merged with the
    ring's own 5-min HRV pairs (`0x5d`), lowest/average HR, breath rate,
    sleep skin temperature (`0x75`),
  - **Sleep + Readiness scores with all contributors** (structure follows
    the on-phone ecore engine documented by open_oura; curve points are our
    own honest approximations, refined against rolling baselines in
    `baselines.json`),
  - **daily activity** (calories, MET, activity classes) from the ring's
    per-minute MET samples (`0x50`).
  Personal tuning lives in `ringlink/profile.json` (age, weight, sleep need;
  gitignored).
- **`ringlink/ring_daemon.py`** — a **persistent connection daemon**, the
  architecture the ring firmware expects (the official app holds the link
  24/7): connect once, flush/drain every 20 s, auto-ingest every 5 min,
  hourly time-sync, reconnect backoff, and a dongle USB-reset ladder for
  wedged transports. Status heartbeats go to `ring_status.json`.
- **`ringlink/sync_ring.py`** + **`install_task.sh`** — one-shot sync
  fallback (skips itself while the daemon is alive). The Windows Scheduled
  Task (`RingLocalSync`, every 15 min via `sync_dashboard.sh`) acts as a
  **watchdog** that restarts the daemon if it died.
- **Ring status in the dashboard** — new backend endpoints
  (`GET /api/ring/status`, `POST /api/ring/sync`) and a header widget
  (`RingStatus.tsx`): colored dot, battery %, last-sync age, "Sync now".
  Indicator states: **green** = live link / recently synced · **pulsing
  yellow** = actively transferring · **steady blue** = waiting for the
  ring's radio (it naps between advertising waves while worn — dock the
  ring ~5 s for an instant catch; buffered data back-fills) · **amber** =
  last sync is getting old · **red** = pipeline down (daemon dead / dongle
  wedged).
- **Manual tags & workouts + local resilience** — Settings → Log lets you
  tag sleep/lifestyle events (caffeine, alcohol, stress, naps…) and log
  workouts; calories are estimated from your recorded heart rate over the
  workout window (Keytel formula, profile-aware). Resilience levels appear
  automatically once ≥3 nights of baselines accumulate.
- **AI analyst on Claude subscription OAuth** — replaced the upstream
  Ollama/LangChain agent with a direct Anthropic tool-use loop using the
  PKCE subscription OAuth flow (`backend/src/claude_auth.py`) — no API key.
- **Removed upstream's cloud automation** — the Playwright-driven Oura-cloud
  login/export scraper is gone (`automation.py`, `/api/automation/*`, the
  scheduled cloud-sync worker, and the login/OTP settings UI). The ring is
  the data source; the only cloud-adjacent path left is the optional manual
  ZIP import for historical exports.
- **Empirical Gen-4 findings** (not documented anywhere else we found):
  - The ring accepts a new auth key (`24 10 <key>`) **only on the very first
    connection after a factory reset**; any earlier connection — even a
    read-only battery probe — consumes the window (`25 01 01` refused).
  - Awake heart rate arrives as `0x80` green-IBI quality records (7× 11-bit
    IBI), not only the `0x5d`/`0x60` rest-mode records.
  - Plus dashboard fixes: timezone-correct date windows, °F/local-naive
    export convention matching Oura's own export, 12-hour times, widget
    label/gauge fixes, and three provisioned dashboards
    (`ringlink/configure_dashboard.py`).

---

## Hardware requirements

- **Oura Ring 4** (yours, physically on hand — pairing requires a factory
  reset via the charger ritual).
- **Nordic nRF52840 USB dongle (PCA10059)**, flashed with Nordic's
  `connectivity_4.1.4_usb_with_s132_5.1.0` firmware (ships inside the
  `pc-ble-driver-py` wheel; `ringlink/flash_dongle.sh` flashes it after you
  press the dongle's RESET button to enter the DFU bootloader).
- Windows + Git Bash (current dev environment; the Python pipeline itself is
  portable in principle).

> A plain USB Bluetooth adapter is **not** enough for this code path — the
> client speaks the Nordic connectivity serial protocol, not the OS BLE stack.

## Quick start

```bash
# 0. One-time: flash the dongle (press its RESET button first → red LED pulse)
cd ringlink && ./flash_dongle.sh

# 1. One-time: pair with a freshly factory-reset ring.
#    CRITICAL: pairing must be the FIRST connection after the reset completes.
./venv310/Scripts/python.exe oura_ring.py pair     # writes oura_key.hex — back it up, never commit it

# 2. Start the persistent daemon (or install the watchdog task that keeps it alive)
./sync_dashboard.sh                                 # starts ring_daemon.py (PID lock, idempotent)
./install_task.sh                                   # registers Scheduled Task "RingLocalSync" (15-min watchdog)

# 3. Run the dashboard (vite + electron; electron spawns the FastAPI backend on :8000)
cd .. && ./start_dashboard.sh
```

Data lands in `%APPDATA%/CrackedOura/oura_database.db`. The dashboard's ring
indicator shows sync freshness and battery, with a "Sync now" button.

### Useful ringlink commands

```bash
./venv310/Scripts/python.exe oura_ring.py scan      # BLE scan (Oura highlighted)
./venv310/Scripts/python.exe oura_ring.py info      # firmware / serial / battery
./venv310/Scripts/python.exe oura_ring.py events    # drain history → events.jsonl
./venv310/Scripts/python.exe decode_events.py ingest  # decode + export + import into the DB
./venv310/Scripts/python.exe sync_ring.py           # one-shot sync (only if the daemon is stopped)
tail -f daemon.log                                  # watch the live daemon
cat .daemon.lock                                    # daemon PID
touch soft_reset.request                            # clean ring reboot via the connected daemon
```

`ringlink/HANDOFF.md` is the authoritative engineering log — protocol notes,
failure modes, and every gotcha hit along the way.
`SESSION-HANDOFF-2026-07-21.md` captures the current operational state
(daemon architecture, environment gotchas, outstanding work).

---

## Repo layout

```
frontend/    Electron + React + TypeScript + Tailwind (upstream base + fixes)
backend/     FastAPI + SQLite (+ /api/ring/*, Claude OAuth analyst)
ringlink/    The BLE pipeline: client, vendored decoders (GPL-3.0), sync, task installer
```

## Licensing & disclaimer

- `ringlink/openring/` is vendored from LogosIsLife/open_ring under
  **GPL-3.0**; that code and the `ringlink/` derivatives of it remain
  GPL-3.0 (`ringlink/openring/NOTICE.md`).
- The dashboard portions derive from EIrno/Cracked-Oura — see
  [ATTRIBUTION.md](ATTRIBUTION.md).
- `oura_key.hex`, `ring_bond.json`, and the databases contain personal data /
  secrets. They are gitignored — keep them that way.

> This project is not affiliated with, associated with, or endorsed by
> Oura Health Oy. It reads a ring **you own** using hardware **you own**, for
> personal data access. Use at your own risk.
