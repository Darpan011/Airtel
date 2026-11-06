#!/usr/bin/env bash
# Project Udyam Digital — local run
set -e
python3 -m pip install -r requirements.txt
echo
echo "Open http://127.0.0.1:8000 in your browser"
python3 -m uvicorn app.main:app --reload --port 8000
