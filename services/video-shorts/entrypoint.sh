#!/bin/bash
set -e

# ── Start bgutil PO Token HTTP server in background ──────────────
# Listens on 127.0.0.1:4416 — yt-dlp's bgutil plugin queries it
# automatically when a PO token is needed for YouTube downloads.
echo "[entrypoint] Starting bgutil PO Token server..."
node /opt/bgutil/server/build/main.js &
BGUTIL_PID=$!

# Give the server a moment to bind to port 4416
sleep 2

if kill -0 "$BGUTIL_PID" 2>/dev/null; then
    echo "[entrypoint] bgutil PO Token server running (PID $BGUTIL_PID)"
else
    echo "[entrypoint] WARNING: bgutil server failed to start — PO tokens unavailable"
fi

# ── Launch the main FastAPI application ───────────────────────────
exec uvicorn app:app --host 0.0.0.0 --port 8000 --workers 1
