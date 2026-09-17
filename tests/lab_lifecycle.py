#!/usr/bin/env python3
import json
import os
import subprocess
import sys
from pathlib import Path

PROJECT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT / "lab"))
import linux as lab

out = PROJECT / ".run/lifecycle"
out.mkdir(parents=True, exist_ok=True)
results = []


def rejected(name, args, expected, env=None):
    p = subprocess.run(
        list(map(str, args)),
        text=True,
        check=False,
        capture_output=True,
        timeout=30,
        env=env,
    )
    record = {
        "case": name,
        "exit": p.returncode,
        "stdout": p.stdout,
        "stderr": p.stderr,
    }
    assert p.returncode != 0 and expected in p.stdout + p.stderr, record
    results.append(record)


lab.guard()
marker = lab.ROOT / "marker.json"
hidden = lab.ROOT / "marker.saved"
marker.rename(hidden)
try:
    rejected("unmarked", [sys.executable, PROJECT / "lab/linux.py", "up"], "unmarked")
finally:
    hidden.rename(marker)

code = f"import sys;sys.path.insert(0,{str(PROJECT / 'lab')!r});import linux;linux.preflight"
rejected(
    "capacity", [sys.executable, "-c", code + "(2**63)"], "insufficient free space"
)
rejected(
    "missing-feature",
    [sys.executable, "-c", code + "()"],
    "missing ip",
    {**os.environ, "PATH": "/nonexistent"},
)
state_path = lab.ROOT / "storage.json"
original = state_path.read_bytes()
state = json.loads(original)
state["devices"][0] = "/dev/sda"
lab.save("storage.json", state)
try:
    rejected("non-allowlisted-device", [PROJECT / "bin/grow"], "allowlist")
finally:
    state_path.write_bytes(original)

(out / "preflight.json").write_text(
    json.dumps(
        {
            "id": "L01",
            "status": "passed",
            "preflight": lab.preflight(),
            "refusals": results,
        },
        indent=2,
    )
)
print("L01 passed", flush=True)

sentinel_file = Path("/tmp/edgelab-unrelated-sentinel")
assert not sentinel_file.exists()
sentinel_file.write_text("unrelated\n")
sentinel = subprocess.Popen(["sleep", "300"])
namespace = "unrelated-edgelab-test"
lab.run("ip", "netns", "add", namespace)
try:
    lab.ns(namespace, "nft", "add", "table", "inet", "sentinel")
    before = lab.status()
    for _ in range(2):
        lab.storage()
        lab.network()
        lab.workloads()
        assert lab.status()["processes"] == before["processes"]
    lab.down()
    assert sentinel.poll() is None
    assert sentinel_file.read_text() == "unrelated\n"
    assert "sentinel" in lab.ns(namespace, "nft", "list", "tables")
    assert not lab.ROOT.exists()
    assert lab.CLIENT not in lab.run("ip", "netns", "list")
    assert lab.SERVER not in lab.run("ip", "netns", "list")
    (out / "cleanup.json").write_text(
        json.dumps(
            {
                "id": "L02",
                "status": "passed",
                "same_processes_after_two_up": before["processes"],
                "unrelated_process_alive": True,
                "unrelated_file_preserved": True,
                "unrelated_network_table_preserved": True,
            },
            indent=2,
        )
    )
    print("L02 passed", flush=True)
finally:
    sentinel.terminate()
    sentinel.wait(timeout=5)
    sentinel_file.unlink()
    lab.run("ip", "netns", "del", namespace)
