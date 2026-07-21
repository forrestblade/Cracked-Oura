# Handoff — Local Oura Ring 4 data access over BLE

> **2026-07-21 EVENING: read `../SESSION-HANDOFF-2026-07-21.md` FIRST.**
> Ring firmware crash incident + architecture change to a persistent
> connection daemon (`ring_daemon.py`). Several sections below describe the
> old poll-based sync and are superseded.

> **THIS COPY (Cracked-Oura/ringlink/HANDOFF.md) IS NOW AUTHORITATIVE.**
> `ring-local/` is legacy; everything was consolidated into this repo 2026-07-21.

## Current status (2026-07-21 late): ✅ MONOREPO + UI INDICATOR + REAL WEAR DATA
- **Monorepo:** all ring code lives in `Cracked-Oura/ringlink/` — BLE client
  (`oura_ring.py`), vendored open_ring decoders (`openring/`), pipeline
  (`decode_events.py`), orchestrator (`sync_ring.py`, own py3.10 venv at
  `ringlink/venv310` — pc-ble-driver-py 0.17.0 + cryptography + pyserial,
  installed via `uv pip install --python`). Key/bond/events data copied here.
- **Backend:** `backend/src/api/ring.py` — `GET /api/ring/status` (reads
  `ringlink/ring_status.json` + lock; derives indicator ok/syncing/stale/error),
  `POST /api/ring/sync` (spawns sync_ring.py detached, 409 if lock held).
  Registered in `main.py`.
- **Frontend:** `frontend/src/components/RingStatus.tsx` mounted in App.tsx
  headerActions — polls status every 10 s, colored dot + battery % + last-sync
  age + "Sync now" button. api.ts: `getRingStatus` / `ringSyncNow`.
- **Sync flow (single BLE connection):** connect→auth→battery→sync_time→drain
  →decode→export→ingest, writing ring_status.json at each phase. Scheduled
  task **RingLocalSync** (every 15 min) → `ringlink/sync_task.vbs` →
  `sync_dashboard.sh` → `venv310 sync_ring.py`. Log: `ringlink/sync.log`.
- **REAL DATA CONFIRMED (first wear):** finger detected 18:54 UTC, HR mode
  cycles on; 62× 0x80 GREEN_IBI_QUALITY records → **50 heart_rate rows (66–100
  bpm, avg 78)**; 33 temperature rows; 8 battery rows. Ring now emits 0x42
  time-sync anchors (sync_time each connection) → exact timestamps.
- **HR decoding note (Gen 4):** awake HR arrives as **0x80** (7× 11-bit IBI ms
  + quality; keep qual_a≤1 && qual_b==0 per PROTOCOL.md §5.1), NOT just
  0x5d/0x60 (those appear in rest/sleep modes). decode_events.py handles all 3.
- **Ops gotchas learned:** uvicorn --reload can wedge leaving ORPHANED WORKER
  processes (`multiprocessing.spawn` cmdline, no 'Cracked-Oura'/'uvicorn'
  string) still bound to :8000 serving stale code — hunt with
  `C:\tmp\list_py.ps1` / `list_uvicorn.ps1`, kill, then restart. A rogue
  global-python uvicorn (uv-managed 3.11) had also been serving old code.
- **UI polish round (2026-07-21 evening, all owner-requested):**
  - Fixed tz bug in `SmartTrendWidgetCanvas` (`new Date('yyyy-MM-dd')` = UTC
    midnight → date windows ended YESTERDAY in negative-UTC zones — this was
    why "no data" showed). Also added `selected_day` range case + sub-day
    `relative` hours/minutes windows (fetch yesterday..today, client-side
    time filter `timeFilteredData`).
  - Exporter now emits **°F** (`skin_temp`) and **local-naive timestamps**
    (`iso_local`) — matches Oura's own export convention; UTC rows were
    displayed 5 h off. DB time-series tables were wiped + re-ingested once
    after each convention change (mixed conventions = duplicates).
  - 12-hour AM/PM times everywhere (TrendChartCanvas ticks + tooltips,
    TableWidget) and US date order in tooltips.
  - Label fix: score/metric widgets showed raw `dataKey` (e.g.
    `readiness.score`) — now `widget.title`. ScoreGaugeCanvas no longer
    duplicates the title inside the doughnut (overlapped the ring in short
    cards); card header owns the label, gauge shows the number only.
  - Dashboards: **Daily Overview** (today-only via `selected_day`),
    **Right Now** (last-hour HR/temp/battery via relative-hours), **Long-term**
    (90d). Pushed by `ringlink/configure_dashboard.py` (re-run overwrites
    layout — the renderer autosaves user edits, so only re-run when asked).
- **AI analyst rewritten to Claude OAuth (2026-07-21 night):** replaced the
  LangChain+Ollama SQL agent with a direct Anthropic Messages API tool-use
  loop using the pi/Claude Code subscription OAuth flow (PKCE, no API key).
  - `backend/src/claude_auth.py` — authorize on claude.ai (client id
    `9d1c250a-…`), paste-code exchange at console.anthropic.com, tokens in
    `%APPDATA%/CrackedOura/claude_oauth.json`, auto-refresh.
  - `backend/src/llm.py` — DataAnalyst: Bearer + `anthropic-beta:
    oauth-2025-04-20`; system prompt MUST start with the Claude Code identity
    block (OAuth requirement); one read-only `sql_query` tool (SELECT-only,
    `mode=ro`, 200-row cap); same `{response, thoughts}` contract; DB path
    fixed to APPDATA (old code pointed at a nonexistent backend/ path).
    Model via config key `claude_model` — default **claude-opus-4-8** (verified
    against the account's GET /v1/models list; also available: claude-sonnet-5,
    claude-opus-4-7, …). On a model-404 the analyst auto-resolves the newest
    opus/sonnet from /v1/models, retries once, and persists the fix to config.
    Owner OAuth-connected 2026-07-21; verified live: real SQL tool calls +
    correct °F/local-time answers (66 HR readings, avg 79.4 bpm).
  - `backend/src/api/claude.py` — /api/claude/auth/{status,start,finish,logout}.
  - Frontend: `ClaudeConnect` section in SettingsPanel (Connect → browser →
    paste code → Finish; disconnect); api.ts helpers. Chat UI unchanged.
  - Verified over HTTP: status/start OK; chat without auth returns a friendly
    "connect Claude" message. **Owner still needs to do the one-time connect.**
- **Chat renders Markdown (2026-07-21 final):** dependency-free renderer at
  `frontend/src/components/Markdown.tsx` (headings, bold/italic, inline code,
  fenced blocks, bullet/numbered lists, links; React nodes only — no
  dangerouslySetInnerHTML). Used for assistant messages in ChatPanel +
  ChatPage; user messages stay plain text.
- **Next:** wear overnight → sleep summaries (0x49/0x4c/0x4f), SpO2 (0x6f),
  HRV (0x5d) → extend exporter to sleepmodel.csv/dailysleep.csv/dailyspo2.csv
  rows so scores/contributor widgets light up.

## Goal
Read the owner's own Oura Ring 4 metrics **locally on this PC over Bluetooth LE**,
without a cloud subscription. This is a personal-device interoperability / data-access
task: talk to a ring the owner physically has, using the owner's own hardware.

Chosen toolkit: **`open_oura`** (Th0rgal/open_oura) — a local client that reads the ring
directly over BLE and stores results in SQLite. No account, no cloud upload.

## Current status (2026-07-21 night): ✅ FULL PIPELINE — ring → decode → Cracked-Oura DB
`decode_events.py` decodes `events.jsonl` and exports/imports into **Cracked-Oura**
(`../Cracked-Oura`, the Electron dashboard — un-parked, now the display layer):
- `./nrf310/Scripts/python.exe decode_events.py decode` → `decoded_events.jsonl`
  (typed records + resolved UTC; dedupes repeated drains — `events` appends from cursor 0)
- `... export` → `export/oura-export/*.csv` (semicolon CSVs in Oura cloud-export
  format: `heartrate.csv`, `temperature.csv`, `ringbatterylevel.csv`, header-only
  `dailysleep.csv` as the zip-detection marker) + `export/oura_export_ring.zip`
- `... ingest` → runs Cracked-Oura's own `OuraParser` (its venv) on the zip →
  `%APPDATA%/CrackedOura/oura_database.db`. **Verified 2026-07-21: 6 temperature
  rows + 1 battery row landed and are queryable.** (HR=0 — ring not yet worn.)

Decoders vendored from LogosIsLife/open_ring (GPL-3.0) into `ring-local/openring/`
(`framing.py` inner-TLV parser, `decoders.py` 50-type dispatch, `enums.py`).
Time resolution per open_ring PROTOCOL.md §7: single anchor, 100 ms/tick, `0x42`
sets anchor, `0x41` with ring_time regression starts a new boot segment. With no
`0x42` in the capture (fresh reset) it falls back to anchoring each segment's last
record at host receive time (`utc_source: "host_fallback"`, ±seconds accuracy —
validated: boot decoded to 13:39 local, matching the actual reset time).

### Cracked-Oura ingestion contract (learned from its source)
- Importer: `backend/src/ingestion/manager.py` `OuraParser.parse_zip` — extracts,
  then `os.walk`s for a dir containing **`dailysleep.csv` or `dailyactivity.csv`**
  (hence the header-only marker file). CSVs are **semicolon**-separated.
- Column contracts: `heartrate.csv` = `timestamp;bpm;source`; `temperature.csv` =
  `timestamp;skin_temp`; `ringbatterylevel.csv` = `timestamp;level;charging;in_charger`.
  Upserts key on `timestamp` (dup timestamps collapse).
- Its venv (`backend/venv`, py3.11, pandas+sqlalchemy) works; DB path from
  `backend/src/paths.py` → `%APPDATA%/CrackedOura/oura_database.db`.

## Earlier status (2026-07-21 eve): ✅ WORKING END-TO-END — paired, authenticated, events syncing
`./oura.sh info` / `battery` / `events` all work. Ring serial `<REDACTED-SERIAL>`,
fw 2.8.9, battery 100%. First `events` drain: **195 raw frames** in `events.jsonl`
(tags: 0x41 ring-start ×1, 0x43 debug ×116, 0x46 temp ×6, 0x5b BLE-conn ×28, 0x61
debug-data ×44 — ring was just reset, so mostly boot/debug; wear it to get real data).

### THE TWO GEN-4 FIXES THAT MADE AUTH WORK (after key install was already fine)
1. **ATT Write Command, not Write Request** — `adapter.write_cmd` (no response).
   The official app writes everything `response=False` (LogosIsLife/open_ring
   `driver/transport.py`). With Write Requests the Gen 4 ring answers a few frames,
   then stops ACKing entirely (writes time out, auth proof swallowed, connection mute).
2. **Phone-faithful preamble before the nonce** (open_ring PROTOCOL.md §6.1):
   `08 03 00 00 00` (fw probe) → `2f 02 01 00` → `2f 02 01 01` (capability pages)
   → `2f 01 2b` (nonce) → `2f 11 2d <proof>` → `2f 02 2e 00` (success).

### Ring 4 onboarding rule (our own empirical finding, 2 successes / 2 refusals)
`24 10 <key>` is accepted **only on the FIRST connection after a factory reset
completes**. Any earlier connection (even read-only) consumes the window → `25 01 01`
refused until re-reset. A key installed DURING the reset ritual is wiped at completion.

### Key repo for Gen 4: LogosIsLife/open_ring (clean-room Python, Ring 4-specific)
Complete protocol spec (`PROTOCOL.md`, ~720 lines): all opcodes, inner TLV record
format, time-resolution algorithm (100 ms/tick), connect-time sequences, failure
modes. Cloned at `C:/tmp/pi-github-repos/LogosIsLife/open_ring`. Their decoders
(`driver/decoders.py`) are the reference for the next step (event decoding — they map
50 inner record types). ringverse `oura/BLE.md` documents the event-tag catalog.

## Earlier status (2026-07-21 pm): dongle works as central; ring factory reset needed
The dongle was flashed with the s132 v5 connectivity firmware (now COM5, PID `0xC00A`)
and the Python client talks to the ring over it. Discovered along the way:
- The 13:10 `pair` reported success but **the key did not persist on the ring** — a
  later unauthenticated battery probe (`0c00` → `0d06...`) proved **no auth key installed**.
- With no key installed, re-running key install (`24 10 <key>`) now returns `25 01 01`
  (refused). **Empirical rule (2 successes, 2 refusals): Ring 4 accepts `24 10` only on
  the FIRST connection after a factory reset completes.** Any earlier connection (even a
  read-only battery probe) consumes the onboarding window → `250101` until re-reset.
  A key installed DURING the reset ritual gets wiped when the reset completes.
  → Protocol: reset ring (keep on charger) → `./oura.sh pair` as the first and only
  connection. `250101` is not documented in ringverse/open_oura/open_ring; the rule
  above is our own finding.
- Prior-art check done: ringverse `oura/BLE.md` (Ring 4 packet spec; matches ours),
  LogosIsLife/open_ring (Ring 4 Python; extracts the key from a rooted phone instead of
  onboarding — no help for key install), open_oura Ring 5 notes ("connect from a fresh
  scan, keep the ring on its charger").
- `pair` now verifies key persistence by reconnecting and re-authenticating before
  declaring success.
- Old key backed up at `oura_key.hex.stale-20260721` (not installed on ring; worthless
  except as a record). Stale `ring_bond.json`/`ring_addr.txt` deleted.

### Client hardening done today (oura_ring.py)
- Port auto-detect falls back to pyserial (VID 0x1915, prefers PID 0xC00A) — the
  native `BLEDriver.enum_serial_ports()` returns nothing on this box.
- `connect()` retries the whole scan+connect cycle (link establishment to the ring
  fails sporadically with `conn_failed_to_be_established` / `connection_timeout`).
- Ring discovery matches the **128-bit Oura service UUID in adv data** and a saved MAC
  (`ring_addr.txt`), not just the name — the ring often advertises without a name and
  rotates private resolvable addresses.
- Explicit conn params with **6 s supervision timeout** (default dropped the weak link
  within ~1 s; RSSI −50…−90 observed).
- **Bond persistence**: after Just Works pairing the peer LTK/ediv/rand are saved to
  `ring_bond.json`; reconnects re-encrypt (`sd_ble_gap_encrypt`) instead of re-pairing.
  A stale bond (no `conn_sec_update`) is auto-deleted + clean reconnect + fresh pairing.
- New `probe` command: unauthenticated battery — tells whether a key is installed
  (`0d..` = no key; `2f022f01` = key installed / auth required).
- Dead-link guards everywhere (`_check_link`) so fallback paths fail into the retry
  loop instead of throwing `TypeError: uint16_t`.

## Previous status: BLOCKED on BLE adapter hardware (resolved)
The PC's built-in Bluetooth works at the driver level but **detects zero BLE devices in
any direction** (ring, phone, everything invisible). Radio is healthy
(`CM_PROB_NONE`, `bleak` initializes with no error) — the symptom points to **no antenna
attached** on the desktop board. Owner has **no antenna and no spare standard USB BT
adapter**, and has decided to use the **Nordic nRF52840 USB dongle** instead.

Because of that decision, the active plan is to turn the Nordic dongle into a usable BLE
**central** and drive it from Python, then re-implement the ring's documented BLE
conversation on top of it.

---

## Hardware inventory
- **Oura Ring 4 (Gen 4).** Already **factory-reset** via the hardware charger method;
  it is in pairing/advertising mode (blue light). A factory-reset ring accepts a new
  16-byte auth key with no prior key needed.
- **Nordic nRF52840 USB Dongle (PCA10059).** Currently flashed with **nRF Sniffer for
  Bluetooth LE** firmware. Enumerates as `USB\VID_1915&PID_522A`, exposed as
  **USB Serial Device (COM3)**. As a sniffer it is passive/capture-only and **cannot act
  as a central** — it must be reflashed.
- **Built-in PC Bluetooth:** present + healthy but effectively no range (see above).

---

## What is already done
1. **`open_oura` builds and runs.** Repo at `~/projects/open_oura`.
   - MSVC toolchain is **not** installed (no Visual Studio). Build was switched to the
     **GNU toolchain**: `rustup` `stable-x86_64-pc-windows-gnu` + MinGW gcc 16.1 at
     `<winget WinLibs mingw64/bin>`.
   - Build command used:
     `PATH=<mingw bin>:$PATH cargo +stable-x86_64-pc-windows-gnu build --release`
   - Output binary: `~/projects/open_oura/target/release/oura.exe`
     (runs standalone; no mingw DLLs needed at runtime).
2. **Confirmed the built-in adapter cannot see the ring** (or anything) — via `oura scan`
   and an independent `bleak` scan (`ring-local/blescan.py`), both returned zero devices.
3. **Confirmed the dongle is a sniffer** (nRF Sniffer firmware) and identified the chip as
   **nRF52840** (native USB CDC + Nordic VID = the PCA10059 dongle, which needs **s140**).
4. **Python tooling for the dongle path is staged:**
   - `nrfutil` (legacy DFU) installed in `ring-local/nrfenv` (Python 3.11) — invoke via
     `ring-local/nrfenv/Scripts/nrfutil.exe` (console script; `python -m nrfutil` does not work).
   - **`pc-ble-driver-py 0.17.0` installed and importing** in a **Python 3.10** venv at
     `ring-local/nrf310` (created with `uv venv --python 3.10`). This is the version that
     supports SoftDevice API v5 (connectivity 4.1.4). On Python **3.11** pip caps at
     `0.11.4` (s130/s132 only — unusable for nRF52840), which is why 3.10 was used.

---

## The immediate blocker to solve next
**CORRECTED (2026-07-21):** the earlier assumption that the dongle needs the s140 build
was **wrong**. `pc-ble-driver-py 0.17.0` wraps **only SD API v2 and v5** (its `lib/`
contains `nrf_ble_driver_sd_api_v2` + `_v5` bindings only) — the s140 6.1.1 connectivity
firmware is **SD API v6 and unusable** with the Python driver. Nordic's official DevZone
answer (Q&A #81224) confirms: for the nRF52840 dongle + pc-ble-driver-py, flash the
**`connectivity_4.1.4_usb_with_s132_5.1.0`** build — the `usb_` variant is compiled for
the nRF52840 (only chip with USB), running the s132 SoftDevice. That DFU package ships
**inside the pip wheel** and is staged at:
```
ring-local/firmware/connectivity_4.1.4_usb_with_s132_5.1.0_dfu_pkg.zip   <- FLASH THIS
ring-local/firmware/s140_v6_NOT_FOR_PYDRIVER_...zip                      <- reference only
```

### Flashing steps (everything staged; needs one physical button press)
1. Put the dongle in **Open DFU bootloader**: press the dongle's **RESET (SW1)** button.
   USB re-enumerates (PID changes to `0x521F`, "Open DFU Bootloader", red LED pulsing).
   *(Requires a physical button press by the owner — cannot be automated.)*
2. Run **`ring-local/flash_dongle.sh`** — it auto-detects the bootloader COM port
   (VID 1915 / PID 521F) and runs `nrfutil dfu usb-serial` with the s132 v5 package.
3. After flash, the dongle re-enumerates as connectivity firmware (PID `0xC00A`) and is
   drivable by `pc-ble-driver-py` (`ring-local/nrf310`).

---

## After the dongle is a working central
`open_oura`/`btleplug` use the **native Windows BLE stack only** and **cannot** use a
serial dongle. So the ring conversation was re-implemented in Python on top of
`pc-ble-driver-py`.

### DONE — the port is written and smoke-tested: `ring-local/oura_ring.py`
Run via `ring-local/oura.sh` (wraps `nrf310/Scripts/python.exe oura_ring.py`):
- `ports` / `scan` — serial-port and BLE discovery
- `pair` — factory-reset ring: installs a random 16-byte key (`24 10 …` → `25 01 00`),
  saves it to `ring-local/oura_key.hex`, verifies auth, syncs ring clock. Refuses to
  overwrite an existing key file.
- `info` / `battery` — firmware `0803…`/`09`, serial `18 03 08 00 10`/`19`, battery `0c`/`0d`
- `events` — drains the history stream (`10 09 <cursor u32> ff ffffffff` → frames ≥`0x41`,
  summary `0x11`, cursor-advance loop like open_oura's `drain_events`) → raw JSONL in
  `ring-local/events.jsonl` (decoding to metrics is the next step)
- `raw <hex>` — authenticated raw command for protocol poking

Implementation notes (verified against the code, not just docs):
- IC id set before driver import (`config.__conn_ic_id__ = "NRF52"` → SD API v5).
- Vendor UUID base `98ed00xx-a541-…` registered via `ble_vs_uuid_add`; chars `0x0002`
  (write) / `0x0003` (notify); MTU config tag 1, `att_mtu_exchange(247)` after connect.
- Factory-reset rings require **link encryption before CCCD writes** (macOS observation
  in the cheatsheet) → the client subscribes, and on failure runs GAP `authenticate`
  (Just Works, bond) and retries.
- App auth: AES-128/ECB over the PKCS7-padded 15-byte nonce — **verified against the
  known-answer vector in `crates/oura-protocol/src/auth.rs`** (PASS).
- Uses the `cryptography` package already present in the nrf310 venv.

### Protocol reference (already in the repo — no need to re-derive)
- GATT (from `docs/horizon-ring3-protocol-cheatsheet.md`):
  - Service: `98ed0001-a541-11e4-b6a0-0002a5d5c51b`
  - Notify/read char: `98ed0003-a541-11e4-b6a0-0002a5d5c51b`
  - Write char: `98ed0002-a541-11e4-b6a0-0002a5d5c51b`
  - MTU 203; frames are `tag length payload…`, little-endian.
- Auth handshake + key install: `crates/oura-protocol/src/auth.rs` (AES-ECB/PKCS5 nonce),
  and the cheatsheet "App Auth" section.
- Event decoders (bytes → HR/HRV/temp/SpO2/motion/sleep): `crates/oura-protocol/src/events.rs`
  and the tables in `crates/README.md` / `docs/data-recovery-map.md`.
- Note: Gen 4 shares the Ring 3/5 GATT + auth flow but is **less battle-tested** in
  `open_oura` (proven on Ring 3 and Ring 5). Expect to validate the handshake on the 4.

---

## Key file locations
| Path | What |
|---|---|
| `~/projects/open_oura` | Rust toolkit + all protocol docs; built `target/release/oura.exe` |
| `~/projects/ring-local` | Working dir |
| `ring-local/scan.sh` `pair.sh` `info.sh` `sync.sh` | Wrappers around `oura.exe` (native-BLE path; only useful if a working Windows BT adapter appears) |
| `ring-local/blescan.py` | `bleak` all-devices scanner (native-BLE diagnostic) |
| `ring-local/nrfenv` | Python 3.11 venv — has `nrfutil.exe`; pc-ble-driver-py here is too old (0.11.4) |
| `ring-local/nrf310` | Python 3.10 venv — **pc-ble-driver-py 0.17.0** (the dongle-driver env) |
| `ring-local/firmware/` | Staged DFU packages — flash the **s132 v5** one (see above) |
| `ring-local/flash_dongle.sh` | Auto-detecting DFU flash helper (run after RESET press) |
| `ring-local/oura_ring.py` + `oura.sh` | **The Python ring client** (scan/pair/info/battery/events/raw) |
| `ring-local/oura_key.hex` | Created by `pair` — the owner-owned 16-byte auth key. Do not lose/commit. |
| `ring-local/decode_events.py` | **decode / export / ingest pipeline** (see current status) |
| `ring-local/openring/` | Vendored open_ring decoders (GPL-3.0, personal use — see NOTICE.md) |
| `ring-local/decoded_events.jsonl` | Decoded typed records with UTC timestamps |
| `ring-local/export/` | Oura-export-format CSVs + `oura_export_ring.zip` (Cracked-Oura importable) |
| `~/projects/Cracked-Oura` | Original Electron/React + FastAPI app (parked; cloud-export based, not what owner wants) |

## Notes / loose ends
- **Simplest alternative (rejected by owner, recorded for completeness):** a plain
  ~$12 USB Bluetooth 5 adapter would let `oura.exe` work immediately with **no reflashing
  and no Python port**. Owner has none and chose the Nordic dongle path.
- `Cracked-Oura/frontend/electron/main.ts` has a debug edit (DevTools auto-open +
  renderer console logging) that can be reverted; that app is parked.
- `Cracked-Oura/frontend/.npmrc` was added (git-bash script shell + legacy-peer-deps) to
  make its npm scripts run on Windows.

## Dashboard launch (2026-07-21 night): ✅ Cracked-Oura running detached
- `../Cracked-Oura/start_dashboard.sh` — launches vite + electron (electron spawns
  the FastAPI backend on :8000). Started with `nohup … > dev.log 2>&1 & disown`;
  verified: :5173 + :8000 listening, `/docs` 200, renderer issuing `/api/query`.
- A stale vite on :5173 from an earlier run had to be killed first (check
  `netstat -ano | grep 5173` before launching).
- `ring-local/sync_dashboard.sh` — ONE-SHOT auto sync: drain ring → decode →
  export → ingest into the dashboard DB. This is the only command the owner ever
  needs after wearing the ring.

## One-line status
**FULL PIPELINE WORKING + DASHBOARD RUNNING.** Ring → `events` drain → `decode_events.py ingest` →
Cracked-Oura dashboard DB, verified with real rows. Next:
1. **Owner wears the ring** for hours/overnight → re-run `./oura.sh events` then
   `./nrf310/Scripts/python.exe decode_events.py ingest`.
2. Once worn data arrives, extend the exporter: HR from `0x5d`/`0x60` is already
   wired but untested with real data; add SpO2 (`0x6f` → `dailyspo2.csv`), sleep
   summaries (`0x49/0x4c/0x4f` → `sleepmodel.csv`/`dailysleep.csv` rows), motion.
3. After the ring gets a time sync (`0x12` at pair; periodic `0x42` anchors), the
   host_fallback timestamps get replaced by exact anchor-based ones automatically.
4. Loose end: `events` always drains from cursor 0 (dedupe handles it, but a saved
   cursor in `oura_ring.py` would make drains incremental).
