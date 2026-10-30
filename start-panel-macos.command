#!/bin/sh
set -eu
cd "$(dirname "$0")"
python3 -B codex-channel/scripts/dashboard.py
