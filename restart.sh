#!/bin/bash
# ============================================================
#  Hivemind — Agent OS: Start / Restart
# ============================================================
#  Usage:  ./restart.sh           (foreground — shows logs)
#          ./restart.sh --bg      (background — returns immediately)
#          ./restart.sh --no-clear (don't clear history)
# ============================================================
set -e

DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$DIR"

BG=false
CLEAR_HISTORY=true
for arg in "$@"; do
  case "$arg" in
    --bg)       BG=true ;;
    --no-clear) CLEAR_HISTORY=false ;;
  esac
done

PORT=${DASHBOARD_PORT:-8080}

# Detect local IP (cross-platform)
get_local_ip() {
  # macOS
  ipconfig getifaddr en0 2>/dev/null && return
  ipconfig getifaddr en1 2>/dev/null && return
  # Linux
  hostname -I 2>/dev/null | awk '{print $1}' && return
  # Fallback
  echo "localhost"
}
LOCAL_IP=$(get_local_ip)

# Bind to all interfaces so LAN devices can connect
export DASHBOARD_HOST="0.0.0.0"
export RATE_LIMIT_MAX_REQUESTS="300"
export RATE_LIMIT_BURST="100"

echo ""
echo "  ⚡ Hivemind — Agent OS"
echo "  ━━━━━━━━━━━━━━━━━━━"
echo ""

# ── 1. Stop existing server ─────────────────────────────────
echo "  🔄 Stopping existing server..."
lsof -ti :$PORT 2>/dev/null | xargs kill -9 2>/dev/null || true
pkill -9 -f "python3 server.py" 2>/dev/null || true
pkill -f "cloudflared tunnel" 2>/dev/null || true
sleep 1

# Verify port is free
if lsof -ti :$PORT >/dev/null 2>&1; then
  echo "  ❌ Port $PORT still in use."
  echo "     Run: lsof -ti :$PORT | xargs kill -9"
  exit 1
fi
echo "  ✅ Port $PORT is free"

# ── 2. Optionally clear history ─────────────────────────────
if $CLEAR_HISTORY; then
  echo "  🧹 Clearing agent history..."
  if [ -f data/platform.db ]; then
    sqlite3 data/platform.db "DELETE FROM agent_actions;" 2>/dev/null || true
    sqlite3 data/platform.db "DELETE FROM messages;" 2>/dev/null || true
    sqlite3 data/platform.db "VACUUM;" 2>/dev/null || true
  fi
  rm -f state_snapshot.json 2>/dev/null || true
  rm -rf .hivemind/agent_logs/* 2>/dev/null || true
  echo "  ✅ History cleared"
else
  echo "  ⏭️  Keeping existing history"
fi

# ── 3. Build frontend ───────────────────────────────────────
echo "  📦 Building frontend..."
cd frontend
npx vite build --logLevel error 2>/dev/null
cp public/manifest.json dist/manifest.json 2>/dev/null || true
cd ..
echo "  ✅ Frontend built"

# ── 4. Start server ─────────────────────────────────────────
LOG_FILE="/tmp/hivemind-server.log"

  # Find python (support both venv and .venv)
if [ -f ./venv/bin/python3 ]; then
  PY=./venv/bin/python3
elif [ -f ./.venv/bin/python3 ]; then
  PY=./.venv/bin/python3
else
  PY=python3
fi

if $BG; then
  echo "  🚀 Starting server (background)..."
  nohup $PY server.py > "$LOG_FILE" 2>&1 &
  SERVER_PID=$!

  # Wait for server to be ready
  for i in {1..20}; do
    if curl -s http://localhost:$PORT/api/health > /dev/null 2>&1; then
      echo "  ✅ Server running (PID: $SERVER_PID)"
      echo ""
      echo "  ┌─────────────────────────────────────────────┐"
      echo "  │  🌐 Local:   http://localhost:$PORT            │"
      echo "  │  📱 Network: http://$LOCAL_IP:$PORT"
      echo "  │  📋 Logs:    tail -f $LOG_FILE"
      echo "  └─────────────────────────────────────────────┘"
      echo ""

      # Wait for cloudflare tunnel URL
      if command -v cloudflared &>/dev/null; then
        echo "  ⏳ Waiting for public URL..."
        for j in {1..20}; do
          TUNNEL_URL=$(grep -o 'https://[a-z0-9-]*\.trycloudflare\.com' "$LOG_FILE" 2>/dev/null | head -1)
          if [ -n "$TUNNEL_URL" ]; then
            echo ""
            echo "  ┌─────────────────────────────────────────────┐"
            echo "  │  🌍 PUBLIC URL (use from anywhere):         │"
            echo "  │                                             │"
            echo "  │  $TUNNEL_URL"
            echo "  │                                             │"
            echo "  │  Open this link on your phone or any device │"
            echo "  └─────────────────────────────────────────────┘"
            echo ""
            # Copy to clipboard (macOS)
            echo "$TUNNEL_URL" | pbcopy 2>/dev/null && echo "  📋 Copied to clipboard!" || true
            break
          fi
          sleep 1
        done
        if [ -z "$TUNNEL_URL" ]; then
          echo "  ⚠️  Tunnel URL not found yet. Check logs: tail -f $LOG_FILE"
        fi
      fi
      echo ""
      exit 0
    fi
    sleep 1
  done
  echo "  ❌ Server failed to start. Check: tail -20 $LOG_FILE"
  tail -10 "$LOG_FILE" 2>/dev/null
  exit 1
else
  echo "  🚀 Starting server..."
  echo ""

  # Find python (support both venv and .venv)
  if [ -f ./venv/bin/python3 ]; then
    PY=./venv/bin/python3
  elif [ -f ./.venv/bin/python3 ]; then
    PY=./.venv/bin/python3
  else
    PY=python3
  fi

  # Run server in background, tail the log, and wait for the URL
  $PY server.py > "$LOG_FILE" 2>&1 &
  SERVER_PID=$!

  # Wait for server to be ready
  echo "  ⏳ Waiting for server..."
  for i in {1..30}; do
    if curl -s http://localhost:$PORT/api/health > /dev/null 2>&1; then
      echo "  ✅ Server running (PID: $SERVER_PID)"
      echo ""
      echo "  ┌─────────────────────────────────────────────┐"
      echo "  │  🌐 Local:   http://localhost:$PORT            │"
      echo "  │  📱 Network: http://$LOCAL_IP:$PORT            │"
      echo "  └─────────────────────────────────────────────┘"

      # Show access code
      ACCESS_CODE=$(grep "ACCESS CODE:" "$LOG_FILE" 2>/dev/null | tail -1 | sed 's/.*ACCESS CODE:  *//')
      if [ -n "$ACCESS_CODE" ]; then
        echo ""
        echo "  ┌─────────────────────────────────────────────┐"
        echo "  │  🔑 ACCESS CODE:  $ACCESS_CODE                    │"
        echo "  │  Enter this code in the browser to connect. │"
        echo "  └─────────────────────────────────────────────┘"
      fi

      # Wait for Cloudflare tunnel URL
      echo ""
      echo "  ⏳ Waiting for public URL..."
      for j in {1..30}; do
        TUNNEL_URL=$(grep -o 'https://[a-z0-9-]*\.trycloudflare\.com' "$LOG_FILE" 2>/dev/null | head -1)
        if [ -n "$TUNNEL_URL" ]; then
          echo ""
          echo "  ============================================================"
          echo "  🌍 PUBLIC URL (use from anywhere):"
          echo ""
          echo "     $TUNNEL_URL"
          echo ""
          echo "     Open this on your phone or any device."
          echo "  ============================================================"
          echo ""
          # Copy to clipboard (macOS)
          echo "$TUNNEL_URL" | pbcopy 2>/dev/null && echo "  📋 Copied to clipboard!" || true
          break
        fi
        sleep 1
      done
      if [ -z "$TUNNEL_URL" ]; then
        echo "  ⚠️  No public URL (cloudflared may not be installed)."
        echo "     Run ./setup.sh to install it."
      fi

      echo ""
      echo "  📋 Logs: tail -f $LOG_FILE"
      echo "  🛑 Stop: kill $SERVER_PID"
      echo ""

      # Follow logs
      tail -f "$LOG_FILE"
      exit 0
    fi
    sleep 1
  done
  echo "  ❌ Server failed to start. Check: tail -20 $LOG_FILE"
  tail -10 "$LOG_FILE" 2>/dev/null
  exit 1
fi
