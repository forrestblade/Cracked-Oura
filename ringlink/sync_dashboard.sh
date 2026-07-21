#!/usr/bin/env bash
# Entry point for the 15-min scheduled sync (Task Scheduler: RingLocalSync).
cd "$(dirname "$0")"
exec ./venv310/Scripts/python.exe sync_ring.py "$@"
