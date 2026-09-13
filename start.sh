#!/usr/bin/env bash
# Start the web UI in the foreground, with this machine's overrides applied.
#
# Reads ./local.env if it exists (gitignored, one KEY=value per line) so the
# settings that are specific to your machine -- above all PA_HOST -- live in a
# file you own rather than in the shipped code. See local.env.example.
#
#   ./start.sh                      # foreground, Ctrl-C to stop
#   PA_HOST=0.0.0.0 ./start.sh      # one-off override, wins over local.env
#
# To run it detached, and to stop it again, see the "Running it" section of
# the README: match on the PORT, never on the process name.
set -euo pipefail
cd "$(dirname "$0")"

if [ -f local.env ]; then
  set -a
  # shellcheck disable=SC1091
  . ./local.env
  set +a
fi

[ -x ./venv/bin/python ] || { echo "no venv yet - run ./install.sh first"; exit 1; }

echo "paper-audiobook: http://${PA_HOST:-127.0.0.1}:${PA_PORT:-3002}"
exec ./venv/bin/python ./webapp.py
