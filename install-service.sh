#!/usr/bin/env bash
# Render paper-audiobook-web.service.template for THIS machine.
#
# Writes paper-audiobook-web.service.local (gitignored) and stops. It does not
# touch /etc and does not use sudo: read the rendered file, then install it
# yourself with the two commands printed at the end.
set -euo pipefail
cd "$(dirname "$0")"

TEMPLATE=paper-audiobook-web.service.template
OUT=paper-audiobook-web.service.local

[ -f "$TEMPLATE" ] || { echo "missing $TEMPLATE"; exit 1; }

DIR="$PWD"
USER_NAME="${SUDO_USER:-$(id -un)}"
GROUP_NAME="$(id -gn "$USER_NAME")"
HOME_DIR="$(getent passwd "$USER_NAME" | cut -d: -f6)"
HOST="${PA_HOST:-127.0.0.1}"

if [ "$USER_NAME" = "root" ]; then
  echo "refusing to render a unit that runs as root: run this as the user who owns the checkout" >&2
  exit 1
fi
[ -x "$DIR/venv/bin/python" ] || echo "NOTE: $DIR/venv/bin/python not found yet - run ./install.sh first."

sed -e "s|__USER__|$USER_NAME|g" \
    -e "s|__GROUP__|$GROUP_NAME|g" \
    -e "s|__DIR__|$DIR|g" \
    -e "s|__HOME__|$HOME_DIR|g" \
    -e "s|__HOST__|$HOST|g" \
    "$TEMPLATE" > "$OUT"

echo "wrote $OUT"
echo
if [ "$HOST" != "127.0.0.1" ]; then
  cat <<EOF
!! PA_HOST=$HOST -- this unit will expose an UNAUTHENTICATED upload endpoint
!! to every host that can reach this machine. That is a deliberate choice and
!! only safe on a network you trust. Read SECURITY.md. Never port-forward it.

EOF
fi
cat <<EOF
Review it, then install:

  sudo install -m 644 -o root -g root $OUT /etc/systemd/system/paper-audiobook-web.service
  sudo systemctl daemon-reload && sudo systemctl enable --now paper-audiobook-web
  journalctl -u paper-audiobook-web -f
EOF
