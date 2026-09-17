#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
export PATH="$HOME/.cargo/bin:$PATH"
mkdir -p bin
cargo build --release --locked
for name in echo traffic objects objects-check grow worker image-check; do go build -trimpath -o "bin/$name" "./cmd/$name"; done
