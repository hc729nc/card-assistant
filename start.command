#!/bin/bash
# Double-click to start the card dashboard (Mac). First time: right-click > Open.
cd "$(dirname "$0")"
python3 -m pip install --quiet --disable-pip-version-check -r requirements.txt
python3 dashboard.py "$@"
