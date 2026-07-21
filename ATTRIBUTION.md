# Attribution

This repository is a fork of **[EIrno/Cracked-Oura](https://github.com/EIrno/Cracked-Oura)**
(local-first Oura dashboard: Electron/React frontend + FastAPI backend), extended
with a fully local BLE pipeline (`ringlink/`) that reads an Oura Ring 4 directly
over a Nordic nRF52840 dongle — no cloud account or subscription required — plus
a Claude-powered AI analyst using subscription OAuth.

None of this would have been possible without the open-source work below.

## Protocol & decoders

- **[LogosIsLife/open_ring](https://github.com/LogosIsLife/open_ring)** (GPL-3.0)
  — clean-room Python driver and ~720-line PROTOCOL.md for the Oura Ring 4.
  `ringlink/openring/` **vendors** its `framing.py`, `decoders.py`, and
  `enums.py` (inner-TLV framing, 50+ record decoders, event-type enums).
  The time-resolution algorithm (single anchor, 100 ms/tick, 0x42 anchors,
  0x41 invalidation), the ATT Write-Command requirement, the phone-faithful
  auth preamble, and the 0x80 green-IBI wire format all come from this
  project's reverse-engineering. **The vendored code and the `ringlink/`
  derivatives of it remain under GPL-3.0** (see `ringlink/openring/NOTICE.md`).

- **[Th0rgal/open_oura](https://github.com/Th0rgal/open_oura)** — Rust toolkit
  that first documented the local BLE conversation for Ring 3/5: the Oura GATT
  service/characteristic UUIDs, the AES-128/ECB auth handshake (including the
  known-answer test vector used to verify our port), frame format, and the
  cursor-advance event-drain loop that `ringlink/oura_ring.py` reimplements in
  Python.

- **ringverse** (`oura/BLE.md`) — independent Ring 4 packet notes used to
  cross-check the event-tag catalog.

## Hardware / driver

- **[NordicSemiconductor/pc-ble-driver-py](https://github.com/NordicSemiconductor/pc-ble-driver-py)**
  — Python bindings driving the nRF52840 USB dongle as a BLE central; the
  `connectivity_4.1.4_usb_with_s132_5.1.0` firmware ships inside its wheel
  (not redistributed here — see the wheel's `hex/` directory).

## AI analyst

- The Claude OAuth connector follows the subscription OAuth (PKCE) flow used
  by **Anthropic's Claude Code CLI** and the **pi coding harness** — Bearer
  tokens with the `oauth-2025-04-20` beta header instead of API keys.

## Upstream app

- **[EIrno/Cracked-Oura](https://github.com/EIrno/Cracked-Oura)** by Elmo
  Ahorinta — the dashboard, ingestion pipeline, widget system, and chat UI
  this fork builds on. The `ringlink/` pipeline deliberately exports data in
  the same CSV layout as Oura's official data export so the upstream ingestion
  code works unchanged.
