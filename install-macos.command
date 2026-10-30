#!/bin/sh
set -eu
cd "$(dirname "$0")"
python3 codex-channel/scripts/install.py
