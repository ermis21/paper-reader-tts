#!/usr/bin/env bash
# Narrate everything in the manifest with two workers, balanced by chunk count.
#
# Resumable: cached chunks are skipped, so re-running after any interruption
# picks up where it left off. Safe to run repeatedly.
#
#   ./run_all.sh          two workers (the default; more is usually slower)
#   ./run_all.sh 4        four workers, if you have the cores to spare
#
# Two workers rather than one because a single Kokoro process does not saturate
# a multi-core CPU, and rather than many because they contend for memory
# bandwidth. OMP_NUM_THREADS is divided among them.
set -uo pipefail
cd "$(dirname "$0")"

WORKERS="${1:-2}"
TOTAL_THREADS="${OMP_NUM_THREADS:-$(nproc 2>/dev/null || echo 4)}"
PER=$(( TOTAL_THREADS / WORKERS )); [ "$PER" -lt 1 ] && PER=1

[ -x ./venv/bin/python ] || { echo "no venv yet - run ./install.sh first"; exit 1; }

# Ask synth.py what the work is, then greedily assign the largest remaining
# paper to the least-loaded worker. Previously this was a hand-written list of
# ids, which only ever balanced one particular corpus.
mapfile -t ASSIGN < <(
  ./venv/bin/python synth.py --plan 2>/dev/null |
  awk 'NF==3 && $2 ~ /^[0-9]+$/ {print $2, $1}' |
  sort -rn |
  awk -v w="$WORKERS" '
    { best=0; for (i=1; i<w; i++) if (load[i] < load[best]) best=i
      load[best] += $1; print best, $2 }'
)

if [ "${#ASSIGN[@]}" -eq 0 ]; then
  echo "nothing to narrate: no extracted text found."
  echo "run ./extract.py first (and ./fetch.py before that, if you use a manifest)."
  exit 0
fi

echo "${#ASSIGN[@]} papers across $WORKERS workers, ${PER} threads each"

run() {
  local worker="$1"
  for entry in "${ASSIGN[@]}"; do
    [ "${entry%% *}" = "$worker" ] || continue
    OMP_NUM_THREADS="$PER" ./venv/bin/python synth.py --only "${entry#* }" 2>&1 |
      grep -Ev "Warning|warn\(|dropout|weight_norm|RNN module|super\(\)|repo_id"
  done
}

pids=()
for ((w = 0; w < WORKERS; w++)); do
  run "$w" &
  pids+=($!)
done
rc=0
for p in "${pids[@]}"; do wait "$p" || rc=1; done

echo "=== ALL SYNTHESIS DONE $(date -Is) ==="
echo "next: ./build.py --m4b     (or ./build.py for per-paper WAVs only)"
exit "$rc"
