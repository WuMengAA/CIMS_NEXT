#!/usr/bin/env bash
# P2-124 backend test baseline runner (run from CIMS-backend root, e.g. Git Bash).
#
# Why the port overrides:
#   The tests boot the real apps (client/management/admin uvicorn + gRPC). If the
#   production backend is already running it owns 8096/8097/8098/8100, so the
#   suite dies at collection with:
#       RuntimeError: Failed to bind to address [::]:8100
#   Moving the test ports aside lets the whole suite run WITHOUT stopping prod.
#
# Why run per-file with a hard bound:
#   The full suite takes >15 min on this machine and a single slow file used to
#   hide every other result (a plain `pytest -q` with a global timeout yields no
#   summary at all). Per-file bounding keeps one slow file from masking the rest
#   and gives a stable, reproducible baseline.
#
# Usage:  bash scripts/run-tests-baseline.sh            (all files)
#         bash scripts/run-tests-baseline.sh tests/test_api.py   (one file)
set -u
cd "$(dirname "$0")/.."

export CIMS_CLIENT_PORT=18096
export CIMS_MANAGEMENT_PORT=18097
export CIMS_ADMIN_PORT=18098
export CIMS_GRPC_PORT=18100

PY=.venv/Scripts/python.exe
BOUND="${BOUND:-200}"   # seconds per file

files=("$@")
if [ "${#files[@]}" -eq 0 ]; then
  files=(tests/test_*.py)
fi

for f in "${files[@]}"; do
  echo "### $f"
  timeout "$BOUND" "$PY" -m pytest "$f" -q --no-header -p no:cacheprovider --tb=line 2>&1 \
    | grep -E "passed|failed|error|no tests ran" | tail -3
  rc=$?
  if [ "$rc" -eq 124 ]; then echo "  (TIMEOUT >${BOUND}s)"; fi
done
