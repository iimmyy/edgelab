#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
git diff --exit-code --quiet
git diff --cached --exit-code --quiet
if [[ -n "$(git ls-files --others --exclude-standard)" ]]; then
  echo 'Commit the source before syncing a reproducible run.' >&2
  exit 1
fi
multipass exec infra-lab -- mkdir -p /home/ubuntu/edgelab
git archive HEAD | multipass exec infra-lab -- tar -xf - -C /home/ubuntu/edgelab
git rev-parse HEAD | multipass exec infra-lab -- sh -c 'cat > /home/ubuntu/edgelab/.source-revision'
