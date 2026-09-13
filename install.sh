#!/usr/bin/env bash
# Create the virtualenv and the working directories. No sudo, no systemd.
#
#   ./install.sh                 create ./venv and install requirements.txt
#   ./install.sh --cpu-torch     same, but pull the CPU-only torch build
#   ./install.sh --force         recreate ./venv even if one already exists
#
# This is CPU-only software. On Linux, plain `torch` from PyPI drags in about
# 3 GB of CUDA wheels that nothing here will ever load, so --cpu-torch is
# usually what you want; it is not the default only because it needs
# download.pytorch.org to be reachable.
#
# ffmpeg is an OPTIONAL, non-Python dependency. It is needed by `./build.py
# --m4b` and nothing else: extraction, narration and the per-paper WAVs in
# out/ all work without it, and the /m4b endpoint simply 404s.
set -euo pipefail
cd "$(dirname "$0")"

FORCE=0
CPU_TORCH=0
for arg in "$@"; do
  case "$arg" in
    --force) FORCE=1 ;;
    --cpu-torch) CPU_TORCH=1 ;;
    -h|--help) sed -n '2,15p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) echo "unknown option: $arg (try --help)" >&2; exit 2 ;;
  esac
done

command -v uv >/dev/null || {
  echo "This installer uses uv: https://docs.astral.sh/uv/getting-started/installation/" >&2
  exit 1
}

if [ -d venv ] && [ "$FORCE" -eq 0 ]; then
  echo "venv/ already exists. Re-run with --force to recreate it."
  echo "(Recreating it is safe for your data -- pdfs/, text/, cache/ and out/ are untouched --"
  echo " but it re-downloads several GB, so it is not the default.)"
  exit 1
fi

# 3.12 is the floor and the tested version. Do not reach for the newest
# CPython: at the time of writing 3.14 has no wheels for torch or the spaCy
# stack, so the install fails rather than the app failing later.
uv venv --python 3.12 venv
export VIRTUAL_ENV="$PWD/venv"
unset CONDA_PREFIX 2>/dev/null || true

if [ "$CPU_TORCH" -eq 1 ]; then
  echo "installing the CPU-only torch build first, so the CUDA wheels are never resolved"
  uv pip install --index-strategy unsafe-best-match \
    --extra-index-url https://download.pytorch.org/whl/cpu torch
fi

uv pip install -r requirements.txt

mkdir -p pdfs dropin text cache out files tmp

# Creates workspace.sqlite and an empty papers.json, so every stage has
# something valid to read on a fresh checkout.
venv/bin/python workspace.py --init

echo
command -v ffmpeg >/dev/null \
  || echo "NOTE: ffmpeg not found. Optional -- only ./build.py --m4b needs it."
cat <<'EOF'

Installed. Next:

  ./start.sh                       the web UI on http://127.0.0.1:3002
                                   (drag PDFs in; it extracts, narrates, assembles)

Or drive the CLI with a manifest:

  cp papers.example.json papers.json   # a starting reading list, edit freely
  ./fetch.py                           # download the open-access ones
  ./extract.py                         # PDFs -> speech-ready text + quality table
  ./run_all.sh                         # narrate (resumable; re-run to continue)
  ./build.py --m4b                     # assemble (M4B needs ffmpeg)

To expose the web UI beyond this machine, read SECURITY.md first, then set
PA_HOST in local.env (see local.env.example). It is unauthenticated.
EOF
