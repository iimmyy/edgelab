#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
export PATH="$HOME/.cargo/bin:$PATH"
output=${1:-.run/reproduction}
[[ ! -e "$output" ]] || { echo 'Use a new evidence directory' >&2; exit 1; }
[[ ! -e /var/lib/edgelab-r2 && ! -e /var/lib/edgelab-worker ]] || { echo 'Fresh fixture state required' >&2; exit 1; }
mkdir -p "$output"
cp .source-tree.json "$output/source-tree.json"
cp .source-revision "$output/source-commit"
uname -a > "$output/kernel.txt"
dpkg-query -W > "$output/packages.txt"
free -b > "$output/memory.txt"
lsblk --json -b > "$output/initial-block-devices.json"
step() {
  local name=$1
  shift
  echo "$name"
  "$@" > "$output/$name.log" 2>&1
}
step build bash lab/build.sh
step rust-tests cargo test --workspace --locked
step go-tests go test ./cmd/traffic ./cmd/worker
step proxy python3 tests/verify.py --out "$output/proxy" --benchmark-seconds 2
step controls python3 tests/control.py --out "$output/controls"
step linux-init sudo python3 lab/linux.py init --confirm-disposable
step linux-up sudo python3 lab/linux.py up
step network-storage sudo python3 tests/storage_network.py --out "$output/network-storage"
step growth-rounding sudo python3 tests/growth_rounding.py
step lifecycle sudo python3 tests/lab_lifecycle.py
step worker-up sudo python3 lab/worker.py up
step worker sudo python3 tests/worker.py --out "$output/worker"
step worker-down sudo python3 lab/worker.py down
step routing go run ./cmd/routing-check --router target/release/edgelab-routing --output "$output/routing"
step dynamic python3 tests/dynamic.py --out "$output/dynamic"
step operations sudo python3 tests/routing_ops.py --out "$output/operations"
step capstone sudo python3 tests/capstone.py --out "$output/capstone"
cmp .source-revision "$output/source-commit"
echo 'All scripted profiles completed; documentation and withheld diagnosis are separate.'
