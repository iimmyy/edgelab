#!/usr/bin/env python3
import hashlib
import json
from pathlib import Path
import subprocess

project = Path(__file__).resolve().parent.parent
out = project / ".run/growth-rounding"
out.mkdir(parents=True, exist_ok=False)
binary = project / "bin/grow"
commands = [
    ["--operation", "rounded-target", "--target-mib", "5597", "--crash-after", "lv"],
    ["--operation", "rounded-target", "--target-mib", "5597"],
    ["--operation", "rounded-target", "--target-mib", "5597"],
    ["--operation", "rounded-target", "--target-mib", "5601"],
]
records = []
for args, expected in zip(commands, [77, 0, 0, 1]):
    p = subprocess.run(
        [str(binary), *args], check=False, capture_output=True, text=True, timeout=30
    )
    record = {
        "arguments": args,
        "exit": p.returncode,
        "stdout": p.stdout,
        "stderr": p.stderr,
    }
    records.append(record)
    assert p.returncode == expected, record
    if p.returncode == 0:
        assert json.loads(p.stdout)["target_bytes"] == 5600 * 1024**2
result = {
    "source_commit": (project / ".source-revision").read_text().strip(),
    "binary_sha256": hashlib.sha256(binary.read_bytes()).hexdigest(),
    "status": "passed",
    "case": "5597 MiB normalizes to 5600 MiB; crash/retry preserves that target; a different normalized target conflicts",
    "records": records,
}
(out / "results.json").write_text(json.dumps(result, indent=2) + "\n")
print(json.dumps(result))
