#!/usr/bin/env python3
"""Local Oura Ring 4 client over an nRF52840 dongle running Nordic connectivity
firmware (connectivity_4.1.4_usb_with_s132_5.1.0), driven by pc-ble-driver-py.

Port of the documented open_oura BLE conversation (tag|len|payload frames,
AES-ECB app auth). Run inside the ring-local/nrf310 venv (Python 3.10):

    nrf310/Scripts/python.exe oura_ring.py [--port COMx] <command>

Commands:
    ports              list candidate serial ports
    scan               scan for BLE devices (Oura highlighted)
    pair               factory-reset ring only: install a fresh 16-byte auth key
                       and save it to oura_key.hex
    info               firmware/battery/serial after auth
    battery            battery after auth
    events             drain history events to events.jsonl (raw frames)
    raw <hex>          write a raw request after auth, print responses
"""
import argparse
import json
import secrets
import sys
import time
from pathlib import Path
from queue import Queue, Empty

HERE = Path(__file__).resolve().parent
KEY_FILE = HERE / "oura_key.hex"
EVENTS_FILE = HERE / "events.jsonl"
ADDR_FILE = HERE / "ring_addr.txt"      # ring MAC, saved after first successful connect
BOND_FILE = HERE / "ring_bond.json"     # peer LTK/ediv/rand, saved after pairing

# --- driver bootstrap (IC id must be set before importing ble_driver) --------
from pc_ble_driver_py import config

config.__conn_ic_id__ = "NRF52"

from pc_ble_driver_py.ble_driver import (  # noqa: E402
    BLEDriver,
    BLEAdvData,
    BLEUUID,
    BLEUUIDBase,
    BLEGapScanParams,
    BLEGapConnParams,
    BLEConfig,
    BLEConfigConnGatt,
)
from pc_ble_driver_py.ble_adapter import BLEAdapter  # noqa: E402
from pc_ble_driver_py.observers import BLEDriverObserver, BLEAdapterObserver  # noqa: E402

from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes  # noqa: E402

CFG_TAG = 1

# Oura GATT: base 98ed00XX-a541-11e4-b6a0-0002a5d5c51b (Ring 3/4/5).
OURA_BASE = BLEUUIDBase(
    [0x98, 0xED, 0x00, 0x01, 0xA5, 0x41, 0x11, 0xE4,
     0xB6, 0xA0, 0x00, 0x02, 0xA5, 0xD5, 0xC5, 0x1B]
)
UUID_WRITE = BLEUUID(0x0002, OURA_BASE)
UUID_NOTIFY = BLEUUID(0x0003, OURA_BASE)

HISTORY_EVENT_PREFIX = 0x41

_OURA_BASE_LE = bytes(reversed(OURA_BASE.base))


def _adv_has_oura_service(adv_data) -> bool:
    """True if the adv packet lists a 128-bit service UUID on the Oura base."""
    for rec in (BLEAdvData.Types.service_128bit_uuid_complete,
                BLEAdvData.Types.service_128bit_uuid_more_available):
        if rec not in adv_data.records:
            continue
        data = bytes(adv_data.records[rec])
        for i in range(0, len(data) - 15, 16):
            chunk = data[i:i + 16]
            if chunk[:12] == _OURA_BASE_LE[:12] and chunk[14:16] == _OURA_BASE_LE[14:16]:
                return True
    return False


def encrypt_nonce(key: bytes, nonce: bytes) -> bytes:
    """AES-128/ECB over the PKCS7-padded 15-byte nonce -> 16-byte response."""
    pad = 16 - len(nonce)
    block = nonce + bytes([pad]) * pad
    enc = Cipher(algorithms.AES(key), modes.ECB()).encryptor()
    return enc.update(block) + enc.finalize()


def frame(tag: int, payload: bytes = b"") -> bytes:
    return bytes([tag, len(payload)]) + payload


def parse_frame(data: bytes):
    if len(data) < 2:
        return None
    tag, length = data[0], data[1]
    return tag, bytes(data[2 : 2 + length]) if len(data) >= 2 + length else bytes(data[2:])


class OuraRing(BLEDriverObserver, BLEAdapterObserver):
    def __init__(self, serial_port: str, verbose: bool = False):
        driver = BLEDriver(
            serial_port=serial_port,
            auto_flash=False,
            baud_rate=1000000,
            log_severity_level="error",
        )
        self.adapter = BLEAdapter(driver)
        self.adapter.observer_register(self)
        self.adapter.driver.observer_register(self)
        self.adapter.default_mtu = 247
        self.verbose = verbose
        self.conn = None
        self.conn_q = Queue()
        self.rx = Queue()
        self.devices = {}  # addr_str -> (name, rssi, BLEGapAddr)
        self._target = None
        self._connecting_addr = None
        self._saved_addr = ADDR_FILE.read_text().strip() if ADDR_FILE.exists() else None

    # --- lifecycle ---------------------------------------------------------
    def open(self):
        self.adapter.driver.open()
        gatt_cfg = BLEConfigConnGatt()
        gatt_cfg.att_mtu = self.adapter.default_mtu
        gatt_cfg.tag = CFG_TAG
        self.adapter.driver.ble_cfg_set(BLEConfig.conn_gatt, gatt_cfg)
        self.adapter.driver.ble_enable()
        self.adapter.driver.ble_vs_uuid_add(OURA_BASE)

    def close(self):
        if self.conn is not None:
            try:
                self.adapter.disconnect(self.conn)
                time.sleep(0.3)
            except Exception:
                pass
        self.adapter.driver.close()

    # --- scanning / connecting --------------------------------------------
    def scan(self, seconds: int = 6):
        params = BLEGapScanParams(interval_ms=200, window_ms=150, timeout_s=seconds)
        self.adapter.driver.ble_gap_scan_start(scan_params=params)
        time.sleep(seconds + 0.5)
        return dict(self.devices)

    def connect(self, name_contains: str = "Oura", seconds: int = 30, attempts: int = 4):
        """Scan until a matching device appears, then connect + discover + subscribe.

        Link establishment can fail at the radio level
        (BLEHci.conn_failed_to_be_established) — retry the scan+connect cycle."""
        last_err = None
        for attempt in range(1, attempts + 1):
            self._target = name_contains.lower()
            params = BLEGapScanParams(interval_ms=200, window_ms=200, timeout_s=seconds)
            self.adapter.driver.ble_gap_scan_start(scan_params=params)
            try:
                self.conn = self.conn_q.get(timeout=seconds)
            except Empty:
                self._target = None
                raise RuntimeError(
                    f"no device with name containing {name_contains!r} found in {seconds}s"
                )
            try:
                time.sleep(0.3)  # let a failed link report its disconnect first
                if self.conn is None:
                    raise RuntimeError("link dropped during establishment")
                try:
                    mtu = self.adapter.att_mtu_exchange(self.conn, self.adapter.default_mtu)
                    if self.verbose:
                        print(f"[ble] att mtu: {mtu}")
                except Exception as e:
                    if self.conn is None:
                        raise RuntimeError("link dropped during establishment")
                    print(f"[ble] mtu exchange failed (continuing at 23): {e}")
                self.adapter.service_discovery(self.conn)
                self._subscribe_with_pairing_fallback()
                if self._connecting_addr and self._connecting_addr != self._saved_addr:
                    ADDR_FILE.write_text(self._connecting_addr + "\n")
                    self._saved_addr = self._connecting_addr
                    print(f"[ble] ring address saved: {self._connecting_addr}")
                return self.conn
            except Exception as e:
                last_err = e
                if self.conn is not None or attempt == attempts:
                    raise
                print(f"[ble] connect attempt {attempt}/{attempts} failed ({e}); retrying")
                time.sleep(1.0)
        raise RuntimeError(f"connect failed after {attempts} attempts: {last_err}")

    def reconnect(self, name_contains: str = "Oura"):
        """Drop the current link and connect again (persistence checks)."""
        if self.conn is not None:
            try:
                self.adapter.disconnect(self.conn)
                time.sleep(1.0)
            except Exception:
                pass
            self.conn = None
        return self.connect(name_contains=name_contains)

    def _check_link(self):
        if self.conn is None:
            raise RuntimeError("link dropped")

    def _subscribe_with_pairing_fallback(self):
        """The ring requires link encryption before CCCD writes.

        With a stored bond: encrypt FIRST (an unencrypted ATT write at a bonded
        ring can make it drop the link). Without: plain try -> fresh pairing."""
        if BOND_FILE.exists():
            if self._try_encrypt_with_saved_bond():
                self._check_link()
                self.adapter.enable_notification(self.conn, UUID_NOTIFY)
                return
            self._check_link()
        else:
            try:
                self.adapter.enable_notification(self.conn, UUID_NOTIFY)
                return
            except Exception as e:
                self._check_link()
                if self.verbose:
                    print(f"[ble] notify subscribe needs encryption ({e})")
        status = None
        for attempt in (1, 2):
            self._check_link()
            try:
                status = self.adapter.authenticate(self.conn, None, bond=True)
                break
            except Exception as e:
                self._check_link()
                if attempt == 2:
                    raise
                print(f"[ble] pairing attempt failed ({e}); retrying once")
                time.sleep(1.0)
        if self.verbose:
            print(f"[ble] pairing status: {status}")
        self._save_bond()
        self._check_link()
        self.adapter.enable_notification(self.conn, UUID_NOTIFY)

    def _try_encrypt_with_saved_bond(self) -> bool:
        if not BOND_FILE.exists() or self.conn is None:
            return False
        try:
            self._check_link()
            b = json.loads(BOND_FILE.read_text())
            self.adapter.encrypt(self.conn, b["ediv"], b["rand"], b["ltk"],
                                 auth=b.get("auth", 0), lesc=b.get("lesc", 0),
                                 ltk_len=b.get("ltk_len", 16))
            if self.verbose:
                print("[ble] link re-encrypted with stored bond")
            return True
        except Exception as e:
            print(f"[ble] stored-bond encrypt failed ({e})")
            if self.conn is not None:
                # No conn_sec_update = the ring no longer holds this LTK and the
                # SMP procedure is wedged. Drop the stale bond and start over on
                # a fresh connection (fresh pairing will run there).
                print("[ble] deleting stale bond; reconnecting for fresh pairing")
                BOND_FILE.unlink(missing_ok=True)
                try:
                    self.adapter.disconnect(self.conn)
                    time.sleep(0.5)
                except Exception:
                    pass
                self.conn = None
            return False

    def _save_bond(self):
        """Persist the peer-distributed LTK so later runs can re-encrypt."""
        try:
            ks = self.adapter.db_conns[self.conn]._keyset
            ek = ks.keys_peer.enc_key
            BOND_FILE.write_text(json.dumps({
                "ediv": ek.master_id.ediv,
                "rand": list(ek.master_id.rand),
                "ltk": list(ek.enc_info.ltk),
                "auth": ek.enc_info.auth,
                "lesc": ek.enc_info.lesc,
                "ltk_len": ek.enc_info.ltk_len,
            }) + "\n")
            print(f"[ble] bond keys saved to {BOND_FILE.name}")
        except Exception as e:
            print(f"[ble] could not save bond keys ({e}) — will re-pair next time")

    # --- request / response ------------------------------------------------
    def request(self, data: bytes, done=None, timeout: float = 10.0):
        """Write a request frame; collect notification frames.

        `done(tag, payload)` -> True stops collection. Default: stop at first frame,
        then drain anything that arrives within 300ms.
        """
        while not self.rx.empty():
            self.rx.get_nowait()
        if self.verbose:
            print(f">> {data.hex()}")
        # ATT Write Command (no response) — what the official app uses
        # (open_ring transport.py: response=False on every write). The ring
        # stops ACKing ATT Write Requests in some states.
        self._check_link()
        self.adapter.write_cmd(self.conn, UUID_WRITE, list(data))
        frames = []
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                raw = self.rx.get(timeout=max(0.05, deadline - time.monotonic()))
            except Empty:
                break
            parsed = parse_frame(raw)
            if parsed is None:
                continue
            tag, payload = parsed
            if self.verbose:
                print(f"<< {raw.hex()}")
            frames.append((tag, payload, raw))
            if done is not None:
                if done(tag, payload):
                    break
            else:
                # default: single response + 300ms grace drain
                grace = time.monotonic() + 0.3
                while time.monotonic() < grace:
                    try:
                        raw = self.rx.get(timeout=grace - time.monotonic())
                        parsed = parse_frame(raw)
                        if parsed:
                            if self.verbose:
                                print(f"<< {raw.hex()}")
                            frames.append((parsed[0], parsed[1], raw))
                    except Empty:
                        break
                break
        if not frames:
            raise RuntimeError(f"no response to {data.hex()} within {timeout}s")
        return frames

    @staticmethod
    def find(frames, tag, ext=None):
        for t, p, _ in frames:
            if t != tag:
                continue
            if ext is not None and (not p or p[0] != ext):
                continue
            return p
        return None

    # --- protocol ops ------------------------------------------------------
    def set_auth_key(self, key: bytes):
        frames = self.request(frame(0x24, key), done=lambda t, p: t == 0x25)
        p = self.find(frames, 0x25)
        if p is None or p[0] != 0x00:
            raise RuntimeError(f"set_auth_key failed: {p.hex() if p else 'no response'}")

    def authenticate(self, key: bytes):
        # Phone-faithful preamble (open_ring PROTOCOL.md §6.1 full-setup):
        # firmware probe then capability pages 0+1 BEFORE the nonce. Gen 4
        # swallows the auth proof if this negotiation never happened.
        for req, desc in ((bytes([0x08, 0x03, 0x00, 0x00, 0x00]), "fw probe"),
                          (bytes([0x2F, 0x02, 0x01, 0x00]), "caps page 0"),
                          (bytes([0x2F, 0x02, 0x01, 0x01]), "caps page 1")):
            try:
                self.request(req, timeout=4.0)
            except RuntimeError:
                if self.verbose:
                    print(f"[auth] preamble {desc}: no response (continuing)")
        frames = self.request(
            bytes([0x2F, 0x01, 0x2B]), done=lambda t, p: t == 0x2F and p[:1] == b"\x2c"
        )
        p = self.find(frames, 0x2F, ext=0x2C)
        if p is None:
            raise RuntimeError("no auth nonce response")
        nonce = p[1:16]
        enc = encrypt_nonce(key, nonce)
        try:
            frames = self.request(
                frame(0x2F, b"\x2d" + enc), done=lambda t, p: t == 0x2F and p[:1] == b"\x2e"
            )
            p = self.find(frames, 0x2F, ext=0x2E)
        except RuntimeError:
            p = None
        if p is None or len(p) < 2:
            # Gen 4 quirk: no 0x2e result frame observed on this ring. Probe a
            # gated command to learn the true auth state.
            probe = self.request(frame(0x0C), timeout=5.0)
            if self.find(probe, 0x0D) is not None:
                print("[auth] no 0x2e result frame, but gated commands work — "
                      "authenticated (Gen 4 quirk)")
                return
            raise RuntimeError("authenticate: no result frame and gated commands "
                               "still refused — auth failed")
        results = {0: "success", 1: "wrong key", 2: "ring in factory reset",
                   3: "not original onboarded device"}
        if p[1] != 0x00:
            raise RuntimeError(f"auth failed: {results.get(p[1], hex(p[1]))}")

    def firmware_info(self):
        frames = self.request(bytes([0x08, 0x03, 0x00, 0x00, 0x00]),
                              done=lambda t, p: t == 0x09)
        return self.find(frames, 0x09)

    def battery(self):
        frames = self.request(frame(0x0C), done=lambda t, p: t == 0x0D)
        return self.find(frames, 0x0D)

    def serial_number(self):
        frames = self.request(bytes([0x18, 0x03, 0x08, 0x00, 0x10]),
                              done=lambda t, p: t == 0x19)
        p = self.find(frames, 0x19)
        if p and p[0] == 0x00:
            return bytes(b for b in p[1:] if 0x20 <= b < 0x7F).decode(errors="replace")
        return None

    def sync_time(self):
        payload = int(time.time()).to_bytes(8, "little") + b"\x00"
        self.request(frame(0x12, payload), done=lambda t, p: t == 0x13)

    def drain_events(self, start_ds: int = 0, on_batch=None):
        """Fetch history events from cursor (deciseconds). Yields raw event frames."""
        total = 0
        cursor = start_ds
        for _ in range(100_000):
            payload = (cursor.to_bytes(4, "little") + bytes([0xFF])
                       + (0xFFFFFFFF).to_bytes(4, "little"))
            frames = self.request(frame(0x10, payload),
                                  done=lambda t, p: t == 0x11, timeout=30)
            batch, summary = [], None
            for t, p, raw in frames:
                if t == 0x11 and len(p) >= 6:
                    summary = {"events": p[0], "sleep_progress": p[1],
                               "bytes_left": int.from_bytes(p[2:6], "little")}
                elif t >= HISTORY_EVENT_PREFIX:
                    batch.append((t, p, raw))
            max_ts = cursor
            for t, p, raw in batch:
                if len(p) >= 4:
                    max_ts = max(max_ts, int.from_bytes(p[0:4], "little"))
                total += 1
                yield t, p, raw
            nxt = max_ts + 1
            progressed = bool(batch) and nxt > cursor
            if progressed:
                cursor = nxt
                if on_batch:
                    on_batch(cursor)
            if not summary or summary["bytes_left"] == 0 or not progressed:
                break

    # --- observers ---------------------------------------------------------
    def on_gap_evt_adv_report(self, ble_driver, conn_handle, peer_addr, rssi,
                              adv_type, adv_data):
        name = ""
        for rec in (BLEAdvData.Types.complete_local_name,
                    BLEAdvData.Types.short_local_name):
            if rec in adv_data.records:
                name = "".join(chr(e) for e in adv_data.records[rec])
                break
        addr = ":".join(f"{b:02X}" for b in peer_addr.addr)
        has_oura_uuid = _adv_has_oura_service(adv_data)
        if addr not in self.devices or (name and not self.devices[addr][0]):
            self.devices[addr] = (name, rssi, peer_addr)
            if self.verbose or self._target is None:
                tag = "  [oura service]" if has_oura_uuid else ""
                print(f"  {addr}  rssi {rssi:4}  {name}{tag}")
        if self._target is None:
            return
        match = ((name and self._target in name.lower())
                 or has_oura_uuid
                 or (self._saved_addr and addr == self._saved_addr))
        if match:
            self._target = None
            self._connecting_addr = addr
            # Generous supervision timeout: the ring's radio is weak and the
            # default params drop the link within ~1s of a few missed events.
            conn_params = BLEGapConnParams(min_conn_interval_ms=30,
                                           max_conn_interval_ms=60,
                                           conn_sup_timeout_ms=6000,
                                           slave_latency=0)
            self.adapter.connect(peer_addr, conn_params=conn_params, tag=CFG_TAG)

    def on_gap_evt_connected(self, ble_driver, conn_handle, peer_addr, role, conn_params):
        self.conn_q.put(conn_handle)

    def on_gap_evt_disconnected(self, ble_driver, conn_handle, reason):
        if conn_handle == self.conn:
            print(f"[ble] disconnected: {reason}")
            self.conn = None

    def on_notification(self, ble_adapter, conn_handle, uuid, data):
        self.rx.put(bytes(data))


# --- key management ---------------------------------------------------------
def load_key() -> bytes:
    if not KEY_FILE.exists():
        sys.exit(f"no auth key at {KEY_FILE} — run `pair` first (ring must be factory-reset)")
    return bytes.fromhex(KEY_FILE.read_text().strip())


# --- CLI ---------------------------------------------------------------------
def _pyserial_nordic_ports():
    """Fallback enumeration: pc-ble-driver's native enum sometimes returns nothing
    on Windows even when the dongle is present. pyserial sees it fine."""
    from serial.tools import list_ports
    out = []
    for p in list_ports.comports():
        if p.vid == 0x1915:  # Nordic Semiconductor
            out.append((p.device, p.pid))
    # prefer connectivity fw (PID 0xC00A)
    out.sort(key=lambda t: 0 if t[1] == 0xC00A else 1)
    return [d for d, _ in out]


def pick_port(args_port):
    if args_port:
        return args_port
    descs = BLEDriver.enum_serial_ports()
    for d in descs:
        if "nordic" in (d.manufacturer or "").lower() or "1915" in (d.serial_number or ""):
            return d.port
    if descs:
        return descs[0].port
    fallback = _pyserial_nordic_ports()
    if fallback:
        return fallback[0]
    sys.exit("no serial ports found — is the dongle flashed with connectivity fw?")


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--port", "-p", help="serial port (default: auto-detect Nordic)")
    ap.add_argument("--name", default="Oura", help="advertised name substring (default: Oura)")
    ap.add_argument("--verbose", "-v", action="store_true")
    ap.add_argument("command", choices=["ports", "scan", "pair", "info", "battery",
                                        "events", "raw", "probe"])
    ap.add_argument("arg", nargs="?", help="hex payload for `raw`")
    args = ap.parse_args()

    if args.command == "ports":
        for d in BLEDriver.enum_serial_ports():
            print(f"{d.port}  manuf={d.manufacturer!r}  sn={d.serial_number!r}")
        for dev in _pyserial_nordic_ports():
            print(f"{dev}  (pyserial fallback, Nordic VID 0x1915)")
        return

    ring = OuraRing(pick_port(args.port), verbose=args.verbose)
    ring.open()
    try:
        if args.command == "scan":
            print("scanning 6s...")
            devs = ring.scan(6)
            ouras = {a: v for a, v in devs.items() if "oura" in (v[0] or "").lower()}
            print(f"\n{len(devs)} device(s), {len(ouras)} Oura ring(s)")
            return

        ring.connect(name_contains=args.name)
        print("[ble] connected + subscribed")

        if args.command == "probe":
            # Unauthenticated battery: `0d..` = NO auth key installed on ring;
            # `2f022f01` = key installed, app auth required.
            frames = ring.request(frame(0x0C))
            for t, p, raw in frames:
                print(f"<< {raw.hex()}")
            p = ring.find(frames, 0x0D)
            if p is not None:
                print(f"ring answered battery WITHOUT auth ({p[0]}%) — no auth key installed")
            elif ring.find(frames, 0x2F, ext=0x2F) is not None:
                print("ring requires app auth — an auth key IS installed")
            return

        if args.command == "pair":
            if KEY_FILE.exists():
                sys.exit(f"{KEY_FILE} already exists — refusing to overwrite. "
                         "Delete it only if the ring was factory-reset again.")
            key = secrets.token_bytes(16)
            ring.set_auth_key(key)
            KEY_FILE.write_text(key.hex() + "\n")
            print(f"auth key installed and saved to {KEY_FILE}")
            ring.authenticate(key)
            print("authentication verified")
            ring.sync_time()
            print("ring clock synced")
            print("reconnecting to verify the key persisted...")
            ring.reconnect(name_contains=args.name)
            ring.authenticate(key)
            print("key persistence verified across reconnect — pairing complete")
            return

        key = load_key()
        ring.authenticate(key)
        print("[auth] ok")

        if args.command == "info":
            fw = ring.firmware_info()
            print(f"firmware raw: {fw.hex()}")
            if fw and len(fw) >= 12:
                print(f"  api {fw[1]}.{fw[3]}.{0}  fw {fw[4]}.{fw[5]}.{fw[6]}")
            sn = ring.serial_number()
            print(f"serial: {sn}")
            b = ring.battery()
            print(f"battery raw: {b.hex()}  ({b[0]}%)")
        elif args.command == "battery":
            b = ring.battery()
            print(f"battery: {b[0]}%  raw={b.hex()}")
        elif args.command == "events":
            n = 0
            with EVENTS_FILE.open("a") as f:
                for tag, payload, raw in ring.drain_events(0):
                    f.write(json.dumps({"ts": time.time(), "tag": tag,
                                        "raw": raw.hex()}) + "\n")
                    n += 1
            print(f"{n} event frame(s) appended to {EVENTS_FILE}")
        elif args.command == "raw":
            if not args.arg:
                sys.exit("raw requires a hex payload")
            frames = ring.request(bytes.fromhex(args.arg))
            for t, p, raw in frames:
                print(f"<< {raw.hex()}")
    finally:
        ring.close()


if __name__ == "__main__":
    main()
