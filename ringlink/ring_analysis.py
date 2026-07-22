#!/usr/bin/env python3
"""Turn decoded Ring-4 frames into Oura-style daily summaries.

Consumes the decoded+time-resolved record list from decode_events.py and
produces rows for: sleepmodel.csv (per-session detail), dailysleep.csv
(score + contributors), dailyreadiness.csv (score + contributors),
dailyactivity.csv (MET-derived daily activity).

WHAT IS MEASURED vs APPROXIMATED (be honest with yourself later):
  measured : bedtime window (0x76 / 0x6a coverage), 30 s sleep periods with
             avg HR / breath / motion / sleep_state (0x6a), IBI stream (0x60),
             ring HRV pairs (0x5d), sleep skin temps (0x75), movement (0x72),
             per-minute MET (0x50).
  derived  : 4-stage hypnogram. The ring gives a 2-3 state classifier
             (sleep_state) + motion + breath variability; REM/deep are
             heuristics on top (REM = quiet + irregular breathing; deep =
             ring-quiet + low HR + still). Percentages validated plausible
             on real nights (deep front-loaded, REM back-loaded).
  scored   : 0-100 scores + contributors follow Oura's *structure*
             (per-contributor piecewise curves -> weighted sum, cf.
             Th0rgal/open_oura docs/algorithms) with our own curve points.
             They are honest approximations, not Oura's proprietary tables.

State carried across nights: ringlink/baselines.json (per-date aggregates;
key = ISO date -> recompute-idempotent). User profile: ringlink/profile.json.

GPL-3.0 (derives from open_ring-vendored decoders; see openring/NOTICE.md).
"""
from __future__ import annotations

import json
import statistics as st
import uuid
from collections import Counter
from datetime import datetime, timedelta
from pathlib import Path

HERE = Path(__file__).resolve().parent
PROFILE_FILE = HERE / "profile.json"
BASELINES_FILE = HERE / "baselines.json"

EPOCH_S = 30                      # 0x6a / stage grid
SESSION_GAP_S = 45 * 60           # 0x6a gap that splits two sessions
MIN_SESSION_S = 20 * 60           # ignore blips shorter than this
NAP_MAX_S = 3 * 3600              # < 3 h asleep -> 'sleep' (nap), else long_sleep

DEEP, LIGHT, REM, AWAKE = "1", "2", "3", "4"


# ---------------------------------------------------------------------------
# profile + baselines
# ---------------------------------------------------------------------------

def load_profile() -> dict:
    defaults = {"age": 30, "weight_kg": 80.0, "height_cm": 178, "sex": "male",
                "sleep_need_h": 8.0, "active_cal_target": 500}
    if PROFILE_FILE.exists():
        try:
            defaults.update(json.loads(PROFILE_FILE.read_text()))
        except Exception:
            pass
    else:
        PROFILE_FILE.write_text(json.dumps(defaults, indent=2))
    return defaults


def load_baselines() -> dict:
    if BASELINES_FILE.exists():
        try:
            return json.loads(BASELINES_FILE.read_text())
        except Exception:
            pass
    return {}


def save_baselines(b: dict):
    BASELINES_FILE.write_text(json.dumps(b, indent=2, sort_keys=True))


def _trailing(baselines: dict, day_iso: str, key: str, days: int = 14):
    """Values of `key` for the `days` days strictly BEFORE day_iso."""
    out = []
    for d, v in baselines.items():
        if d >= day_iso or key not in v or v[key] is None:
            continue
        if (datetime.fromisoformat(day_iso) - datetime.fromisoformat(d)).days <= days:
            out.append(v[key])
    return out


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _pl(x: float, points: list[tuple[float, float]]) -> float:
    """Piecewise-linear y(x) over sorted (x, y) points, clamped at the ends.
    Same structure as ecore's per-contributor interpolators."""
    if x <= points[0][0]:
        return points[0][1]
    for (x0, y0), (x1, y1) in zip(points, points[1:]):
        if x <= x1:
            return y0 + (y1 - y0) * (x - x0) / (x1 - x0)
    return points[-1][1]


def _clamp(v, lo, hi):
    return max(lo, min(hi, v))


def _majority(chars: str) -> str:
    return Counter(chars).most_common(1)[0][0] if chars else AWAKE


# ---------------------------------------------------------------------------
# session detection + staging
# ---------------------------------------------------------------------------

def detect_sessions(records: list[dict]) -> list[dict]:
    """Cluster 0x6a sleep-period frames into sessions; widen with 0x76."""
    periods = sorted((r for r in records
                      if r["type"] == 0x6A and r.get("utc_ms")),
                     key=lambda r: r["utc_ms"])
    sessions, cur = [], []
    for r in periods:
        if cur and r["utc_ms"] - cur[-1]["utc_ms"] > SESSION_GAP_S * 1000:
            sessions.append(cur)
            cur = []
        cur.append(r)
    if cur:
        sessions.append(cur)

    # 0x76 BEDTIME_PERIOD: ring's own in-bed window (ring_time units, 100 ms).
    # Convert via the emitting record's own (ring_time, utc_ms) anchor.
    windows = []
    for r in records:
        if r["type"] == 0x76 and r.get("utc_ms") and r.get("ring_time") is not None:
            d = r["data"]
            rt, utc = r["ring_time"], r["utc_ms"]
            windows.append((utc - (rt - d["start_ring_time"]) * 100,
                            utc - (rt - d["end_ring_time"]) * 100))

    out = []
    for sess in sessions:
        start = sess[0]["utc_ms"]
        end = sess[-1]["utc_ms"]
        if end - start < MIN_SESSION_S * 1000:
            continue
        for w0, w1 in windows:  # widen to the ring's own window when overlapping
            if w0 < end and w1 > start:
                start, end = min(start, w0), max(end, w1)
        out.append({"start_ms": int(start), "end_ms": int(end), "periods": sess})
    return out


def stage_session(sess: dict) -> dict:
    """30 s grid from bedtime start->end; stage each epoch; gaps = awake."""
    start, end = sess["start_ms"], sess["end_ms"]
    n_epochs = max(1, int(round((end - start) / 1000 / EPOCH_S)))
    grid: list[dict | None] = [None] * n_epochs
    for r in sess["periods"]:
        i = int((r["utc_ms"] - start) / 1000 / EPOCH_S)
        if 0 <= i < n_epochs:
            grid[i] = r["data"]

    filled = [d for d in grid if d]
    med_hr = st.median(d["average_hr"] for d in filled) if filled else 0
    bvs = sorted(d["breath_v"] for d in filled)
    p60bv = bvs[int(0.6 * len(bvs))] if bvs else 0

    stages = []
    for d in grid:
        if d is None:
            stages.append(AWAKE)               # no sleep-period frame = awake
        elif d["motion_count"] >= 20:
            stages.append(AWAKE)
        elif d["sleep_state"] >= 1 and d["average_hr"] <= med_hr \
                and d["motion_count"] <= 2:
            stages.append(DEEP)
        elif d["sleep_state"] == 0 and d["motion_count"] <= 3 \
                and d["breath_v"] >= p60bv:
            stages.append(REM)
        else:
            stages.append(LIGHT)
    # 5-epoch majority smoothing (kills single-epoch flicker)
    smoothed = [_majority("".join(stages[max(0, i - 2):i + 3]))
                for i in range(n_epochs)]
    # movement 1..4 from motion_count
    movement = []
    for d in grid:
        m = d["motion_count"] if d else 15
        movement.append("1" if m == 0 else "2" if m <= 4 else "3" if m <= 15 else "4")
    return {"stages_30s": "".join(smoothed), "movement_30s": "".join(movement),
            "grid": grid}


# ---------------------------------------------------------------------------
# per-session vitals (HR / HRV / breath / temp)
# ---------------------------------------------------------------------------

def session_vitals(sess: dict, records: list[dict]) -> dict:
    start, end = sess["start_ms"], sess["end_ms"]
    n_buckets = max(1, int((end - start) / 300_000) + 1)
    hr_b: list[list] = [[] for _ in range(n_buckets)]
    rm_b: list[list] = [[] for _ in range(n_buckets)]

    for r in records:
        u = r.get("utc_ms")
        if u is None or not (start <= u <= end):
            continue
        if r["type"] == 0x60:                       # IBI stream -> HR + RMSSD
            ibis = [v for v in r["data"]["ibi_ms"] if 300 <= v <= 1800]
            b = int((u - start) / 300_000)
            if len(ibis) >= 2:
                diffs = [b1 - b0 for b0, b1 in zip(ibis, ibis[1:])
                         if abs(b1 - b0) < 200]
                if diffs:
                    rm_b[b].append((st.mean(d * d for d in diffs)) ** 0.5)
            if ibis:
                hr_b[b].append(60000 / st.mean(ibis))
        elif r["type"] == 0x5D:                     # ring's own 5-min HR/RMSSD
            pairs = r["data"].get("samples_5min") or []
            for i, p in enumerate(pairs):
                pu = u - (len(pairs) - 1 - i) * 300_000
                b = int((pu - start) / 300_000)
                if 0 <= b < n_buckets:
                    if p.get("hr_bpm"):
                        hr_b[b].append(("ring", p["hr_bpm"]))
                    if p.get("rmssd_ms"):
                        rm_b[b].append(("ring", p["rmssd_ms"]))

    def _resolve(bucket):
        ring = [v for v in bucket if isinstance(v, tuple)]
        if ring:                                    # ring-computed value wins
            return round(st.mean(v[1] for v in ring), 1)
        return round(st.mean(bucket), 1) if bucket else None

    hr_items = [_resolve(b) for b in hr_b]
    rm_items = [_resolve(b) for b in rm_b]
    hr_vals = [v for v in hr_items if v]
    rm_vals = [v for v in rm_items if v]

    temps = [t for r in records
             if r["type"] == 0x75 and r.get("utc_ms")
             and start <= r["utc_ms"] <= end
             for t in r["data"]["temps_c"] if 30 <= t <= 42]

    lowest_i = hr_items.index(min(hr_vals)) if hr_vals else None
    return {
        "hr_items": hr_items, "hrv_items": rm_items,
        "average_heart_rate": round(st.mean(hr_vals), 1) if hr_vals else None,
        "lowest_heart_rate": int(min(hr_vals)) if hr_vals else None,
        "average_hrv": int(st.mean(rm_vals)) if rm_vals else None,
        "lowest_hr_min_from_end": (len(hr_items) - 1 - lowest_i) * 5
                                  if lowest_i is not None else None,
        "temp_c": round(st.mean(temps), 2) if temps else None,
    }


# ---------------------------------------------------------------------------
# summaries + scores
# ---------------------------------------------------------------------------

def summarize_session(sess: dict, staging: dict, vitals: dict,
                      profile: dict) -> dict:
    stages = staging["stages_30s"]
    n = len(stages)
    cnt = Counter(stages)
    tib_s = n * EPOCH_S
    asleep_s = (n - cnt[AWAKE]) * EPOCH_S

    # latency: first run of >=10 consecutive non-awake epochs
    latency_s, run = 0, 0
    for i, s in enumerate(stages):
        run = run + 1 if s != AWAKE else 0
        if run >= 10:
            latency_s = (i - run + 1) * EPOCH_S
            break

    # restless: clusters of high-motion epochs inside sleep
    restless, in_burst = 0, False
    for d in staging["grid"]:
        hot = d is not None and d["motion_count"] >= 10
        if hot and not in_burst:
            restless += 1
        in_burst = hot

    grid = staging["grid"]
    breaths = [d["breath"] for d in grid if d and 4 <= d["breath"] <= 30]

    start_dt = datetime.fromtimestamp(sess["start_ms"] / 1000)
    end_dt = datetime.fromtimestamp(sess["end_ms"] / 1000)
    return {
        "id": f"ring-{start_dt:%Y%m%d%H%M}",
        "day": end_dt.date().isoformat(),          # Oura: night belongs to wake day
        "bedtime_start": start_dt, "bedtime_end": end_dt,
        "type": "long_sleep" if asleep_s >= NAP_MAX_S else "sleep",
        "time_in_bed": tib_s, "total_sleep_duration": asleep_s,
        "awake_time": cnt[AWAKE] * EPOCH_S,
        "deep_sleep_duration": cnt[DEEP] * EPOCH_S,
        "light_sleep_duration": cnt[LIGHT] * EPOCH_S,
        "rem_sleep_duration": cnt[REM] * EPOCH_S,
        "efficiency": int(round(100 * asleep_s / tib_s)) if tib_s else None,
        "latency": latency_s, "restless_periods": restless,
        "average_breath": round(st.mean(breaths), 1) if breaths else None,
        **vitals,
        "stages_30s": stages,
        "stages_5min": "".join(_majority(stages[i:i + 10])
                               for i in range(0, n, 10)),
        "movement_30s": staging["movement_30s"],
    }


def sleep_score(s: dict, profile: dict, baselines: dict) -> tuple[int, dict]:
    need_h = profile.get("sleep_need_h", 8.0)
    total_h = s["total_sleep_duration"] / 3600
    tib_h = s["time_in_bed"] / 3600
    deep_pct = 100 * s["deep_sleep_duration"] / max(1, s["total_sleep_duration"])
    rem_pct = 100 * s["rem_sleep_duration"] / max(1, s["total_sleep_duration"])
    awake_frac = s["awake_time"] / max(1, s["time_in_bed"])

    mid = s["bedtime_start"] + timedelta(seconds=s["time_in_bed"] / 2)
    mid_h = mid.hour + mid.minute / 60          # ideal sleep midpoint 02:00-04:00
    mid_off = min(abs(((mid_h - 3) + 12) % 24 - 12), 12)

    c = {
        "total_sleep": _pl(total_h, [(4, 30), (6, 60), (need_h - 1, 85),
                                     (need_h - 0.5, 95), (need_h + 1, 100)]),
        "efficiency": _pl(s["efficiency"] or 0,
                          [(65, 30), (75, 60), (85, 85), (95, 100)]),
        "restfulness": _pl(awake_frac + s["restless_periods"] / 60,
                           [(0.05, 100), (0.15, 85), (0.3, 55), (0.5, 25)]),
        "rem_sleep": _pl(rem_pct, [(5, 40), (12, 70), (18, 90), (23, 100)]),
        "deep_sleep": _pl(deep_pct, [(5, 40), (10, 70), (15, 90), (20, 100)]),
        "latency": _pl(s["latency"] / 60, [(0, 70), (5, 90), (8, 100),
                                           (20, 100), (35, 70), (60, 40)]),
        "timing": _pl(mid_off, [(1.5, 100), (3, 85), (5, 60), (8, 30)]),
    }
    c = {k: int(round(v)) for k, v in c.items()}
    w = {"total_sleep": 30, "efficiency": 10, "restfulness": 10,
         "rem_sleep": 10, "deep_sleep": 10, "latency": 10, "timing": 20}
    score = int(round(sum(c[k] * w[k] for k in c) / sum(w.values())))
    return score, c


def readiness_score(s: dict, profile: dict, baselines: dict,
                    day_iso: str) -> tuple[int, dict, float | None]:
    rhr, rmssd, temp = s["lowest_heart_rate"], s["average_hrv"], s["temp_c"]
    base_rhr = _trailing(baselines, day_iso, "rhr")
    base_rmssd = _trailing(baselines, day_iso, "rmssd")
    base_temp = _trailing(baselines, day_iso, "temp_c")
    base_sleep = _trailing(baselines, day_iso, "sleep_s")
    base_score = _trailing(baselines, day_iso, "sleep_score", days=1)

    temp_dev = None
    if temp is not None and base_temp:
        temp_dev = round(temp - st.mean(base_temp), 2)

    c = {}
    if rhr and base_rhr:
        c["resting_heart_rate"] = _pl((rhr - st.mean(base_rhr)) / st.mean(base_rhr),
                                      [(-0.10, 100), (-0.02, 95), (0.02, 85),
                                       (0.08, 60), (0.15, 30)])
    else:
        c["resting_heart_rate"] = 85
    if rmssd and base_rmssd:
        c["hrv_balance"] = _pl((rmssd - st.mean(base_rmssd)) / st.mean(base_rmssd),
                               [(-0.30, 40), (-0.10, 70), (0.0, 85), (0.10, 100)])
    else:
        c["hrv_balance"] = 85
    c["body_temperature"] = _pl(abs(temp_dev), [(0.2, 100), (0.5, 80), (1.0, 50),
                                                (1.5, 25)]) if temp_dev is not None else 90
    c["recovery_index"] = _pl(s["lowest_hr_min_from_end"] or 0,
                              [(30, 40), (90, 70), (180, 95), (240, 100)])
    c["previous_night"] = base_score[-1] if base_score else \
        _pl(s["total_sleep_duration"] / 3600, [(4, 40), (6, 65), (7.5, 90), (8.5, 100)])
    if base_sleep:
        avg_h = st.mean(base_sleep + [s["total_sleep_duration"]]) / 3600
        c["sleep_balance"] = _pl(avg_h / profile.get("sleep_need_h", 8.0),
                                 [(0.7, 40), (0.85, 70), (0.95, 90), (1.0, 100)])
    else:
        c["sleep_balance"] = 85
    act = _trailing(baselines, day_iso, "active_cal", days=1)
    c["previous_day_activity"] = _pl(act[-1] / max(1, profile["active_cal_target"]),
                                     [(0.3, 60), (0.7, 85), (1.0, 100), (2.5, 100),
                                      (3.5, 70)]) if act else 80
    acts = _trailing(baselines, day_iso, "active_cal")
    c["activity_balance"] = _pl(st.mean(acts) / max(1, profile["active_cal_target"]),
                                [(0.3, 55), (0.6, 75), (0.9, 95),
                                 (1.2, 100)]) if acts else 80
    c = {k: int(round(v)) for k, v in c.items()}
    w = {"resting_heart_rate": 20, "hrv_balance": 20, "body_temperature": 12,
         "recovery_index": 13, "previous_night": 15, "sleep_balance": 10,
         "previous_day_activity": 5, "activity_balance": 5}
    score = int(round(sum(c[k] * w[k] for k in c) / sum(w.values())))
    return score, c, temp_dev


# ---------------------------------------------------------------------------
# activity (0x50 per-minute MET)
# ---------------------------------------------------------------------------

def analyze_activity(records: list[dict], profile: dict) -> dict:
    """day_iso -> daily activity row from 0x50 MET-minute samples."""
    per_min: dict[str, dict[int, float]] = {}
    for r in records:
        if r["type"] != 0x50 or not r.get("utc_ms"):
            continue
        mets = [b / 10.0 for b in bytes.fromhex(r["data"]["trailing_hex"])]
        if not mets:
            continue
        end = datetime.fromtimestamp(r["utc_ms"] / 1000)
        for i, m in enumerate(mets):        # samples end at the record time
            t = end - timedelta(minutes=len(mets) - 1 - i)
            day = t.date().isoformat()
            per_min.setdefault(day, {})[t.hour * 60 + t.minute] = m

    weight = profile["weight_kg"]
    out = {}
    for day, minutes in per_min.items():
        met_grid = [minutes.get(i, 0.0) for i in range(1440)]
        worn = [m for m in met_grid if m > 0]
        kcal_min = lambda m: m * 3.5 * weight / 200          # standard MET formula
        active_cal = int(sum(kcal_min(m) for m in worn if m >= 1.5))
        bmr = int(10 * weight + 6.25 * profile["height_cm"]
                  - 5 * profile["age"] + (5 if profile["sex"] == "male" else -161))
        cls = "".join(
            "0" if m == 0 else "1" if m < 1.05 else "2" if m < 2 else
            "3" if m < 4 else "4" if m < 7 else "5"
            for m in (st.mean(met_grid[i:i + 5] or [0]) for i in range(0, 1440, 5)))
        low_t = sum(60 for m in worn if 2 <= m < 4)
        med_t = sum(60 for m in worn if 4 <= m < 7)
        high_t = sum(60 for m in worn if m >= 7)
        c = {
            "stay_active": _pl(sum(60 for m in worn if m < 1.5) / 3600,
                               [(5, 100), (8, 85), (11, 60), (14, 30)]),
            "move_every_hour": 90, "recovery_time": 95,
            "meet_daily_targets": _pl(active_cal / max(1, profile["active_cal_target"]),
                                      [(0.3, 40), (0.7, 70), (1.0, 100)]),
            "training_frequency": _pl(med_t + high_t, [(0, 50), (1200, 80), (3600, 100)]),
            "training_volume": _pl(sum(m for m in worn if m >= 4),
                                   [(0, 50), (100, 80), (300, 100)]),
        }
        c = {k: int(round(v)) for k, v in c.items()}
        out[day] = {
            "id": f"ring-act-{day}", "day": day,
            "score": int(round(st.mean(c.values()))),
            "active_calories": active_cal,
            "total_calories": bmr + active_cal,
            "average_met_minutes": round(st.mean(worn), 2) if worn else 0,
            "met_items": met_grid, "class_5_min": cls,
            "low_activity_time": low_t, "medium_activity_time": med_t,
            "high_activity_time": high_t,
            "sedentary_time": sum(60 for m in worn if 1.05 <= m < 2),
            "resting_time": sum(60 for m in worn if m < 1.05),
            "non_wear_time": sum(60 for m in met_grid if m == 0),
            "contributors": c, "target_calories": profile["active_cal_target"],
        }
    return out


# ---------------------------------------------------------------------------
# entry point
# ---------------------------------------------------------------------------

def analyze(records: list[dict]) -> dict:
    """Returns {'sessions': [...], 'daily_sleep': [...], 'daily_readiness': [...],
    'daily_activity': [...]} and updates baselines.json."""
    profile = load_profile()
    baselines = load_baselines()
    activity = analyze_activity(records, profile)

    sessions, daily_sleep, daily_readiness = [], {}, {}
    for sess in detect_sessions(records):
        staging = stage_session(sess)
        vitals = session_vitals(sess, records)
        s = summarize_session(sess, staging, vitals, profile)
        sessions.append(s)
        if s["type"] != "long_sleep":
            continue
        day = s["day"]
        # baselines first (previous nights only feed *this* night via _trailing)
        b = baselines.setdefault(day, {})
        b.update({"rhr": s["lowest_heart_rate"], "rmssd": s["average_hrv"],
                  "temp_c": s["temp_c"], "sleep_s": s["total_sleep_duration"]})
        if day in activity:
            b["active_cal"] = activity[day]["active_calories"]

        sc, sc_c = sleep_score(s, profile, baselines)
        rd, rd_c, temp_dev = readiness_score(s, profile, baselines, day)
        b["sleep_score"] = sc
        s["readiness"] = {"score": rd, "contributors": rd_c,
                          "temperature_deviation": temp_dev}
        daily_sleep[day] = {"id": f"ring-sleep-{day}", "day": day, "score": sc,
                            "timestamp": s["bedtime_end"].isoformat(),
                            "contributors": sc_c}
        daily_readiness[day] = {"id": f"ring-rdy-{day}", "day": day, "score": rd,
                                "temperature_deviation": temp_dev,
                                "temperature_trend_deviation": temp_dev,
                                "contributors": rd_c}
    save_baselines(baselines)
    return {"sessions": sessions,
            "daily_sleep": list(daily_sleep.values()),
            "daily_readiness": list(daily_readiness.values()),
            "daily_activity": list(activity.values())}
