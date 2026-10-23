#!/usr/bin/env bash
set -euo pipefail

echo "[bidpilot] initialising database…"
bidpilot initdb || true

if [ "${1:-run}" = "run" ]; then
  echo "[bidpilot] starting scheduler (mode=${BIDPILOT_MODE:-from config})"
  if [ -n "${BIDPILOT_MODE:-}" ]; then
    exec bidpilot run --config "${BIDPILOT_CONFIG:-config/config.yaml}" $( [ "$BIDPILOT_MODE" = "live" ] && echo "--live" )
  fi
  exec bidpilot run --config "${BIDPILOT_CONFIG:-config/config.yaml}"
fi

exec bidpilot "$@"
