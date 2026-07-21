#!/usr/bin/env bash
# Launch the Cracked-Oura dev app (vite + electron; electron spawns the
# FastAPI backend on :8000). Run detached:  nohup ./start_dashboard.sh &
cd "$(dirname "$0")/frontend"
exec npm run dev
