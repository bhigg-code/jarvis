#!/bin/bash
# Jarvis startup script
# Edit the values below or export them from your shell / systemd unit.

set -a
# Load .env if present
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
if [ -f "$SCRIPT_DIR/.env" ]; then
  source "$SCRIPT_DIR/.env"
fi
set +a

pkill -f "uvicorn server:app" 2>/dev/null
sleep 1

cd "$SCRIPT_DIR"

# Activate venv if a local one exists
if [ -f "venv/bin/activate" ]; then
  source venv/bin/activate
fi

# Choose whether to use TLS (requires authbind + ssl/ certs) or plain HTTP
if [ -f "$SCRIPT_DIR/ssl/cert.pem" ] && [ -f "$SCRIPT_DIR/ssl/key.pem" ]; then
  echo "Starting Jarvis with TLS on port 443..."
  authbind --deep uvicorn server:app \
    --host 0.0.0.0 \
    --port 443 \
    --ssl-keyfile ssl/key.pem \
    --ssl-certfile ssl/cert.pem >> jarvis.log 2>&1 &
else
  echo "Starting Jarvis on port 8080 (no TLS)..."
  uvicorn server:app \
    --host 0.0.0.0 \
    --port 8080 >> jarvis.log 2>&1 &
fi

echo "Started PID $!"
