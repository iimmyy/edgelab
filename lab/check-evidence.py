#!/usr/bin/env python3
import argparse
import hashlib
import json
import subprocess
from pathlib import Path

parser = argparse.ArgumentParser()
parser.add_argument("directory", type=Path)
parser.add_argument("--commit", required=True)
args = parser.parse_args()
p = args.directory
inventory = json.loads((p / "inventory.json").read_text())
assert inventory, "empty inventory"
actual = {
    str(file.relative_to(p))
    for file in p.rglob("*")
    if file.is_file() and file != p / "inventory.json"
}
assert set(inventory) == actual, (
    "inventory does not cover the complete evidence directory"
)
for name, digest in inventory.items():
    file = (p / name).resolve()
    assert file.is_relative_to(p.resolve()), "artifact outside evidence directory"
    assert file.is_file() and hashlib.sha256(file.read_bytes()).hexdigest() == digest, (
        f"missing or changed artifact: {name}"
    )
environment = json.loads((p / "environment.json").read_text())
assert environment["commit"] == args.commit, (
    "evidence belongs to a different source revision"
)
assert environment["source_sha256"] and environment["binary_sha256"], (
    "missing source or binary identity"
)
root = Path(__file__).resolve().parents[1]
if (root / ".git").exists():
    tracked = subprocess.check_output(
        ["git", "ls-tree", "-r", "--name-only", args.commit], cwd=root, text=True
    ).splitlines()
    assert set(environment["source_sha256"]) == {
        name for name in tracked if not name.startswith("evidence/")
    }, "source inventory differs from the committed tree"
    for name, digest in environment["source_sha256"].items():
        source = subprocess.check_output(
            ["git", "show", f"{args.commit}:{name}"], cwd=root
        )
        assert hashlib.sha256(source).hexdigest() == digest, f"source mismatch: {name}"
results = json.loads((p / "results.json").read_text())
assert [r["id"] for r in results] == [f"P{i:02}" for i in range(1, 11)], (
    "missing or duplicate gate"
)
assert all(
    r["status"] == "passed" and r["finished"] >= r["started"] for r in results
), "incomplete or failing run"
assert (p / "proxy.log").stat().st_size and (p / "resources.jsonl").stat().st_size, (
    "empty observations"
)
for file in p.glob("[0-9]*.json"):
    report = json.loads(file.read_text())
    history = file.with_suffix(".jsonl")
    assert history.name in inventory, f"missing history for {file.name}"
    attempts = failures = timeouts = 0
    with history.open() as lines:
        for line in lines:
            row = json.loads(line)
            attempts += 1
            failures += bool(row.get("error"))
            timeouts += row["timeout"]
            assert row["elapsed_ms"] >= 0
    assert attempts > 0 and attempts == report["attempts"]
    assert failures == report["failures"] and timeouts == report["timeouts"]
    assert attempts - failures == report["successes"]
print("Evidence is complete and internally consistent for " + args.commit)
