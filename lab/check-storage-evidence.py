#!/usr/bin/env python3
import argparse
import hashlib
import json
import subprocess
from pathlib import Path

p = argparse.ArgumentParser()
p.add_argument("directory", type=Path)
p.add_argument("--commit", required=True)
a = p.parse_args()
root = a.directory
inventory = json.loads((root / "inventory.json").read_text())
assert inventory["source_commit"] == a.commit, "wrong source commit"
for name, digest in inventory["files"].items():
    assert Path(name).name == name, "invalid evidence path"
    assert hashlib.sha256((root / name).read_bytes()).hexdigest() == digest, name
source = json.loads((root / "source.json").read_text())
assert source["tree"]["commit"] == a.commit
for name, expected in source["tree"]["files"].items():
    content = subprocess.check_output(["git", "show", f"{a.commit}:{name}"])
    assert hashlib.sha256(content).hexdigest() == expected, name
results = json.loads((root / "results.json").read_text())
assert len(results) == 9 and {r["id"] for r in results} == {
    "N01",
    "N02",
    "N03",
    "S01",
    "S02",
    "S03",
    "S04",
    "S05",
    "S06",
}
assert all(r["status"] == "passed" for r in results)
assert len(results[1]["details"]["faults"]) == 7
acknowledged = [
    json.loads(line) for line in (root / "ack.jsonl").read_text().splitlines()
]
assert len(acknowledged) == 955, "missing acknowledged writes"
for name in ["after-crash", "owner-returned"]:
    history = [
        json.loads(line)
        for line in (root / (name + ".stdout")).read_text().splitlines()
    ]
    assert len(history) == len(acknowledged)
    assert all(r["ok"] and r["method"] == "GET" for r in history)
    assert sorted(r["id"] for r in history) == sorted(r["id"] for r in acknowledged)
assert (root / "wireguard.pcap").stat().st_size > 24
print(
    json.dumps(
        {
            "status": "passed",
            "commit": a.commit,
            "gates": 9,
            "acknowledged_objects": len(acknowledged),
            "files_verified": len(inventory["files"]),
        }
    )
)
