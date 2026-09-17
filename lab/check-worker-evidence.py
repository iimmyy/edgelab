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


def read(name):
    return json.loads((root / name).read_text())


def volumes(inventory):
    return {x["lv_name"]: x for group in inventory["report"] for x in group["lv"]}


inventory = read("inventory.json")
assert inventory["source_commit"] == a.commit
source = read("source.json")
assert source["commit"] == source["tree"]["commit"] == a.commit
for name, sha in source["tree"]["files"].items():
    assert (
        hashlib.sha256(
            subprocess.check_output(["git", "show", f"{a.commit}:{name}"])
        ).hexdigest()
        == sha
    ), name
actual = {str(p.relative_to(root)) for p in root.rglob("*") if p.is_file()}
blobs = {
    str(p.relative_to(root))
    for p in (root / "store/blobs/sha256").iterdir()
    if p.is_file()
}
assert actual == set(inventory["files"]) | blobs | {"inventory.json"}
corrupt = read("corrupt-manifest.json")["layers"][-1]["digest"].split(":")[1]
for name in blobs:
    data = (root / name).read_bytes()
    assert (hashlib.sha256(data).hexdigest() == Path(name).name) == (
        Path(name).name != corrupt
    ), name
for name, sha in inventory["files"].items():
    assert not Path(name).is_absolute() and ".." not in Path(name).parts
    assert hashlib.sha256((root / name).read_bytes()).hexdigest() == sha, name
results = read("results.json")
assert len(results) == 7 and {r["id"] for r in results} == {
    f"W{i:02}" for i in range(1, 8)
}
assert all(r["status"] == "passed" for r in results)
assert len(results[1]["details"]) == 3 and len(results[2]["details"]["cases"]) == 6
assert (
    results[2]["details"]["sentinel_sha256"]
    == hashlib.sha256(b"untouched\n").hexdigest()
)
points = {
    "requested",
    "manifest",
    "config",
    "layer-0",
    "layer-1",
    "verified",
    "lv",
    "format",
    "mount",
    "unpack-partial",
    "unpack",
    "unmounted",
    "seal",
    "sealed",
    "snapshot",
    "writable",
    "activated",
    "registered",
}
cases = results[5]["details"]["cases"]
assert len(cases) == len(points) and {c["point"] for c in cases} == points


def filesystem(label, expected):
    r = read(label)
    assert r["exit"] == 0
    observed = json.loads(r["stdout"])
    assert observed["ok"]
    target = read(expected)
    for name, want in target["files"].items():
        assert (
            observed["files"][name]["ok"] and observed["files"][name]["sha256"] == want
        )
    for name in target["absent"]:
        assert observed["files"][name]["absent"]


filesystem("filesystem-verification.json", "valid-expected.json")
filesystem("origin-after-write.json", "valid-expected.json")
for case in cases:
    point = case["point"]
    name = "crash-" + point
    assert read(name + "-interrupted.json")["exit"] == 77
    assert (case["phase_after_interruption"] == "registered") == (point == "registered")
    filesystem(name + "-verified.json", name + "-expected.json")
    result = case["result"]
    assert result["ready"]
    snapshot = Path(result["snapshot"]).name
    origin = Path(result["origin"]).name
    before = volumes(case["before"])
    after = volumes(case["after"])
    assert set(after) - set(before) <= {snapshot, origin}
    assert (
        after[snapshot]["lv_uuid"] == result["snapshot_uuid"]
        and after[snapshot]["origin"] == origin
    )
    if point in {"snapshot", "writable", "activated", "registered"}:
        assert before[snapshot]["lv_uuid"] == after[snapshot]["lv_uuid"]
    repeated = json.loads(read(name + "-repeated.json")["stdout"].splitlines()[-1])
    assert repeated["snapshot_uuid"] == result["snapshot_uuid"]
    assert after[snapshot]["lv_attr"][4] == "a"
for variant in ["valid"] + ["crash-" + point for point in points]:
    m = read(variant + "-manifest.json")
    for d in [m["config"], *m["layers"]]:
        data = (root / "store/blobs/sha256" / d["digest"].split(":")[1]).read_bytes()
        assert (
            len(data) == d["size"]
            and "sha256:" + hashlib.sha256(data).hexdigest() == d["digest"]
        )
operations = read("operations.json")
registered = [r for r in operations if r["phase"] == "registered"]
assert len(registered) == 21
assert len({r["uuid"] for r in registered}) == 21
pressure = results[6]["details"]
assert (
    "data threshold" in pressure["data_refusal"]["error"]
    and "metadata threshold" in pressure["metadata_refusal"]["error"]
)
assert "pressure" not in volumes(pressure["after_cleanup"])
print(
    json.dumps(
        {
            "status": "passed",
            "commit": a.commit,
            "gates": 7,
            "interruption_points": 18,
            "registered_snapshots": len(registered),
            "files_verified": len(inventory["files"]),
        }
    )
)
