#!/bin/bash
# radiod-monitor.sh — start/stop/restart wrapper for the radiod-monitor app

APP_DIR="$(cd "$(dirname "$0")" && pwd)"
PIDFILE="$APP_DIR/.app.pid"
LOGFILE="$APP_DIR/backend.log"
VENV="$APP_DIR/venv"
CERT_DIR="$APP_DIR/certs"
CERT_FILE="$CERT_DIR/cert.pem"
KEY_FILE="$CERT_DIR/key.pem"

if [ ! -d "$VENV" ]; then
    echo "Error: Virtual environment not found at $VENV"
    echo "Run: python3 -m venv venv && venv/bin/pip install -e /path/to/ka9q-python && venv/bin/pip install -e ."
    exit 1
fi

ensure_certs() {
    # WebCodecs AudioDecoder in Chrome/Firefox requires a secure context
    # (HTTPS or localhost), so we auto-generate a self-signed cert on first
    # run if one isn't already present. Browsers will show a warning once
    # per client — accept it and proceed.
    if [ -f "$CERT_FILE" ] && [ -f "$KEY_FILE" ]; then
        return 0
    fi
    if ! command -v openssl >/dev/null 2>&1; then
        echo "Warning: openssl not found; running without TLS."
        echo "         Audio will only work from a localhost browser."
        return 0
    fi
    echo "No TLS cert found at $CERT_DIR — generating self-signed cert..."
    mkdir -p "$CERT_DIR"
    HOSTNAME_FQDN="$(hostname -f 2>/dev/null || hostname)"
    openssl req -x509 -newkey rsa:2048 -nodes \
        -keyout "$KEY_FILE" -out "$CERT_FILE" \
        -days 3650 \
        -subj "/CN=${HOSTNAME_FQDN}" \
        -addext "subjectAltName=DNS:${HOSTNAME_FQDN},DNS:localhost,IP:127.0.0.1" \
        >/dev/null 2>&1
    if [ $? -eq 0 ]; then
        chmod 600 "$KEY_FILE"
        echo "Self-signed cert created for CN=${HOSTNAME_FQDN} (valid 10 years)."
    else
        echo "Warning: openssl failed; running without TLS."
        rm -f "$CERT_FILE" "$KEY_FILE"
    fi
}

start() {
    if [ -f "$PIDFILE" ] && kill -0 "$(cat "$PIDFILE")" 2>/dev/null; then
        echo "Already running (PID $(cat "$PIDFILE"))"
        return 1
    fi

    ensure_certs
    echo "Starting radiod-monitor on port 8443..."
    source "$VENV/bin/activate"
    cd "$APP_DIR"
    # Roll the log if it has grown past 32 MB, keeping one previous copy.
    # backend.log is append-only across restarts and a stuck radiod can
    # generate a lot of it.
    if [ -f "$LOGFILE" ] && [ "$(stat -c %s "$LOGFILE" 2>/dev/null || echo 0)" -gt 33554432 ]; then
        mv -f "$LOGFILE" "$LOGFILE.1"
        echo "Rotated $LOGFILE -> $LOGFILE.1"
    fi
    nohup python3 -m backend.app >> "$LOGFILE" 2>&1 &
    echo $! > "$PIDFILE"
    echo "Started (PID $!), logging to $LOGFILE"
}

stop() {
    if [ ! -f "$PIDFILE" ]; then
        echo "Not running (no pidfile)"
        return 1
    fi
    PID=$(cat "$PIDFILE")
    if kill -0 "$PID" 2>/dev/null; then
        echo "Stopping radiod-monitor (PID $PID)..."
        # SIGTERM the reload worker first, then the reloader parent, so
        # uvicorn runs the app's lifespan shutdown -- that hook is what
        # releases this app's radiod channels.
        pkill -P "$PID" 2>/dev/null
        kill "$PID" 2>/dev/null
        # Then WAIT for it. The shutdown hook polls radiod to enumerate the
        # channels it owns before dropping them, which takes a second or two.
        # The old code slept 1 s and then SIGKILLed unconditionally, cutting
        # that off every single time -- which is why channels accumulated on
        # radiod across restarts until `control` was full of orphans.
        for _ in $(seq 1 24); do
            kill -0 "$PID" 2>/dev/null || break
            sleep 0.5
        done
        if kill -0 "$PID" 2>/dev/null; then
            echo "Did not exit within 12s; forcing (channels may be orphaned)."
            pkill -9 -P "$PID" 2>/dev/null
            kill -9 "$PID" 2>/dev/null
        fi
        echo "Stopped."
    else
        echo "Process $PID not running."
    fi
    rm -f "$PIDFILE"
}

restart() {
    stop
    sleep 1
    start
}

status() {
    if [ -f "$PIDFILE" ] && kill -0 "$(cat "$PIDFILE")" 2>/dev/null; then
        echo "Running (PID $(cat "$PIDFILE"))"
    else
        echo "Not running"
        [ -f "$PIDFILE" ] && rm -f "$PIDFILE"
    fi
}

case "${1:-}" in
    start)   start   ;;
    stop)    stop    ;;
    restart) restart ;;
    status)  status  ;;
    *)
        echo "Usage: $0 {start|stop|restart|status}"
        exit 1
        ;;
esac
