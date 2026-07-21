#!/usr/bin/env python3
"""Push a solid default widget layout to the running Cracked-Oura backend
(POST /api/dashboard). Idempotent — safe to re-run; overwrites the layout.

Grid: react-grid-layout, 12 columns, rowHeight 60. Each widget needs BOTH a
`layout` entry {i,x,y,w,h} and (fallback) tailwind width/height classes.
Valid widget types: score, trend, metric, bar, table, radar, json.
Query domains: sleep, activity, readiness, resilience, cardiovascular_age,
sleep_session, workout, meditation, ring_battery, heart_rate, temperature.
"""
import json
import urllib.request

API = "http://127.0.0.1:8000/api/dashboard"

BLUE, GREEN, RED, AMBER, VIOLET = "#8AB4F8", "#4ade80", "#f87171", "#fbbf24", "#a78bfa"


def w(id_, type_, title, x, y, wid, hei, **config):
    """Return (widget, layout_entry)."""
    span = {3: "col-span-3", 4: "col-span-4", 6: "col-span-6",
            8: "col-span-8", 12: "col-span-12"}[wid]
    widget = {"id": id_, "type": type_, "title": title,
              "width": span, "height": "h-40", "config": config}
    layout = {"i": id_, "x": x, "y": y, "w": wid, "h": hei}
    return widget, layout


def rel(value, unit="days"):
    return {"type": "relative", "value": value, "unit": unit, "anchor": "today"}


TODAY_ONLY = {"type": "selected_day"}  # exactly the selected/current day


DAILY = [
    # Row 0 — the three daily scores
    w("1", "score", "Sleep score",     0, 0, 4, 3, dataKey="sleep.score", color=BLUE),
    w("2", "score", "Readiness score", 4, 0, 4, 3, dataKey="readiness.score", color=GREEN),
    w("3", "score", "Activity score",  8, 0, 4, 3, dataKey="activity.score", color=RED),
    # Row 1 — live ring data, TODAY ONLY (fed by the local BLE 15-min sync)
    w("4", "trend", "Heart rate (bpm)", 0, 3, 6, 4, dataKey="heart_rate.bpm",
      dataKeys=["heart_rate.bpm"], color=RED, showPoints=True, dateRange=TODAY_ONLY),
    w("5", "trend", "Skin temperature (°F)", 6, 3, 6, 4, dataKey="temperature.skin_temp",
      dataKeys=["temperature.skin_temp"], color=AMBER, showPoints=True, dateRange=TODAY_ONLY),
    # Row 2 — battery + nightly intraday curves
    w("6", "trend", "Ring battery (%)", 0, 7, 4, 4, dataKey="ring_battery.level",
      dataKeys=["ring_battery.level"], color=GREEN, showPoints=True, dateRange=TODAY_ONLY),
    w("7", "trend", "Sleep HR (nightly)", 4, 7, 4, 4, dataKey="sleep_session.hr_data",
      dataKeys=["sleep_session.hr_data"], color=BLUE),
    w("8", "trend", "Sleep HRV (nightly)", 8, 7, 4, 4, dataKey="sleep_session.hrv_data",
      dataKeys=["sleep_session.hrv_data"], color=VIOLET),
    # Row 3 — contributor radars
    w("9",  "radar", "Sleep contributors",     0, 11, 4, 4,
      dataKey="sleep.contributors", dataKeys=["sleep.contributors"], color=BLUE),
    w("10", "radar", "Readiness contributors", 4, 11, 4, 4,
      dataKey="readiness.contributors", dataKeys=["readiness.contributors"], color=GREEN),
    w("11", "radar", "Activity contributors",  8, 11, 4, 4,
      dataKey="activity.contributors", dataKeys=["activity.contributors"], color=RED),
    # Row 4 — long-term recovery trends
    w("12", "trend", "Resting HR trend (30d)", 0, 15, 6, 4,
      dataKey="sleep_session.lowest_heart_rate",
      dataKeys=["sleep_session.lowest_heart_rate"], color=RED,
      dateRange={"type": "last_30"}),
    w("13", "trend", "HRV trend (30d)", 6, 15, 6, 4,
      dataKey="sleep_session.average_hrv",
      dataKeys=["sleep_session.average_hrv"], color=VIOLET,
      dateRange={"type": "last_30"}),
    # Row 5 — sleep stats table
    w("14", "table", "Sleep stats (30d)", 0, 19, 12, 4,
      dataKey="sleep_session.total_sleep_duration",
      dataKeys=["sleep_session.total_sleep_duration", "sleep_session.efficiency",
                "sleep_session.lowest_heart_rate", "sleep_session.average_hrv",
                "sleep_session.average_breath"],
      color=BLUE, dateRange={"type": "last_30"}),
]

RIGHTNOW = [
    w("1", "trend", "Heart rate — last 3 hrs", 0, 0, 6, 5,
      dataKey="heart_rate.bpm", dataKeys=["heart_rate.bpm"],
      color=RED, showPoints=True, dateRange=rel(3, "hours")),
    w("2", "trend", "Skin temp (°F) — last 3 hrs", 6, 0, 6, 5,
      dataKey="temperature.skin_temp", dataKeys=["temperature.skin_temp"],
      color=AMBER, showPoints=True, dateRange=rel(3, "hours")),
    w("3", "trend", "Battery — last 3 hrs", 0, 5, 12, 4,
      dataKey="ring_battery.level", dataKeys=["ring_battery.level"],
      color=GREEN, showPoints=True, dateRange=rel(3, "hours")),
]

LONGTERM = [
    w("1", "trend", "Skin temperature (90d, °F)", 0, 0, 6, 4,
      dataKey="temperature.skin_temp", dataKeys=["temperature.skin_temp"],
      color=AMBER, showPoints=True, dateRange={"type": "last_90"}),
    w("2", "trend", "Heart rate (90d)", 6, 0, 6, 4,
      dataKey="heart_rate.bpm", dataKeys=["heart_rate.bpm"],
      color=RED, showPoints=True, dateRange={"type": "last_90"}),
    w("3", "trend", "Ring battery (90d)", 0, 4, 6, 4,
      dataKey="ring_battery.level", dataKeys=["ring_battery.level"],
      color=GREEN, showPoints=True, dateRange={"type": "last_90"}),
    w("4", "trend", "Cardiovascular age", 6, 4, 6, 4,
      dataKey="cardiovascular_age.vascular_age",
      dataKeys=["cardiovascular_age.vascular_age"], color=BLUE,
      dateRange={"type": "all"}),
]


def build():
    dashboards = []
    for did, name, spec in [("default", "Daily Overview", DAILY),
                            ("rightnow", "Right Now", RIGHTNOW),
                            ("longterm", "Long-term", LONGTERM)]:
        widgets, layout = zip(*spec)
        dashboards.append({"id": did, "name": name,
                           "widgets": list(widgets), "layout": list(layout)})
    return {"dashboards": dashboards, "activeDashboardId": "default"}


def main():
    body = json.dumps(build()).encode()
    req = urllib.request.Request(API, data=body,
                                 headers={"Content-Type": "application/json"},
                                 method="POST")
    with urllib.request.urlopen(req, timeout=10) as r:
        print("POST /api/dashboard ->", r.status, r.read().decode()[:100])
    with urllib.request.urlopen(API, timeout=10) as r:
        saved = json.load(r)
    names = [(d["id"], d["name"], len(d["widgets"])) for d in saved["dashboards"]]
    print("saved dashboards:", names, "| active:", saved.get("activeDashboardId"))


if __name__ == "__main__":
    main()
