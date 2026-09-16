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
python3 - <<'PYCODE' | multipass exec infra-lab -- sh -c 'cat > /home/ubuntu/edgelab/.source-tree.json'
import hashlib, json, subprocess
from pathlib import Path
files = subprocess.check_output(['git', 'ls-files', '-z']).decode().strip('\0').split('\0')
print(json.dumps({'commit': subprocess.check_output(['git','rev-parse','HEAD']).decode().strip(),
                  'files': {name: hashlib.sha256(Path(name).read_bytes()).hexdigest()
                            for name in files if not name.startswith('evidence/')}}))
PYCODE
