#!/usr/bin/env bash
# ── EurekaClaw Docker Entrypoint ─────────────────────────────────────────────
#
# Routing logic:
#   docker run ... eurekaclaw              → eurekaclaw ui (default)
#   docker run ... eurekaclaw ui           → eurekaclaw ui --host 0.0.0.0
#   docker run ... eurekaclaw prove "..."  → eurekaclaw prove "..."
#   docker run ... eurekaclaw bash         → interactive shell
#   docker run ... eurekaclaw dev          → make dev (frontend hot-reload)
#   docker run ... eurekaclaw <any>        → exec <any> (arbitrary command)
# ──────────────────────────────────────────────────────────────────────────────
set -e

# If no arguments, default to "ui"
if [ $# -eq 0 ]; then
    set -- "ui"
fi

case "$1" in
    # Interactive shell
    bash|sh|zsh)
        exec "$@"
        ;;

    # Development mode: Vite + Python backend (bind to 0.0.0.0 for Docker)
    dev)
        cd /app
        exec npx concurrently -n backend,frontend -c cyan,magenta \
            "eurekaclaw ui --host 0.0.0.0 --port 7860" \
            "cd frontend && npx vite --host 0.0.0.0"
        ;;

    # UI mode: bind to 0.0.0.0 so it's accessible from host
    ui)
        shift
        exec eurekaclaw ui --host 0.0.0.0 --port 8080 "$@"
        ;;

    # Any eurekaclaw subcommand (prove, explore, from-papers, etc.)
    prove|explore|from-papers|eval-session|onboard|skills|install-skills|login|pause|resume|replay-theory-tail|test-paper-reader)
        exec eurekaclaw "$@"
        ;;

    # Fallback: run as arbitrary command
    *)
        exec "$@"
        ;;
esac
