#!/usr/bin/env bash
set -euo pipefail
[[ $(uname -s) == Linux && $(uname -m) == aarch64 ]] || { echo 'ARM64 Linux required' >&2; exit 1; }
sudo apt-get update
sudo env DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
  build-essential pkg-config libsqlite3-dev golang-go python3 curl ca-certificates \
  iproute2 wireguard-tools nftables lvm2 e2fsprogs tcpdump sqlite3
if [[ ! -x "$HOME/.cargo/bin/rustup" ]]; then
  installer=$(mktemp -d)
  trap 'rm -rf "$installer"' EXIT
  curl --fail --location --output "$installer/rustup-init" \
    https://static.rust-lang.org/rustup/archive/1.29.1/aarch64-unknown-linux-gnu/rustup-init
  curl --fail --location --output "$installer/expected.sha256" \
    https://static.rust-lang.org/rustup/archive/1.29.1/aarch64-unknown-linux-gnu/rustup-init.sha256
  expected=$(cut -d ' ' -f 1 "$installer/expected.sha256")
  actual=$(sha256sum "$installer/rustup-init" | cut -d ' ' -f 1)
  [[ "$expected" == "$actual" ]] || { echo 'rustup installer digest mismatch' >&2; exit 1; }
  chmod 700 "$installer/rustup-init"
  "$installer/rustup-init" -y --profile minimal --default-toolchain none
fi
"$HOME/.cargo/bin/rustup" toolchain install 1.98.1 --profile minimal --component rustfmt --component clippy
