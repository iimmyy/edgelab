#!/usr/bin/env python3
import argparse
import json
import socket
import subprocess
import time
from pathlib import Path

from verify import Lab, ROOT, save

parser = argparse.ArgumentParser()
parser.add_argument("--out", type=Path, required=True)
args = parser.parse_args()
args.out.mkdir(parents=True, exist_ok=False)
lab = Lab(args.out)
held = []
try:
    lab.proxy.terminate()
    lab.proxy.wait(timeout=2)
    command = [
        str(ROOT / "target/release/edgelab-proxy"),
        "--config",
        str(lab.config),
        "--limit",
        "8",
        "--app-limit",
        "a=2",
        "--app-limit",
        "b=3",
        "--drain-ms",
        "500",
    ]
    previous = sum(e.get("event") == "ready" for e in lab.events())
    lab.proxy = lab.spawn(command, "proxy")
    lab.await_event("ready", previous)
    for port in [lab.a, lab.a_alt]:
        held.append(lab.hold(port)[0])
    rejected = []

    def reject(port):
        start = time.monotonic()
        with socket.create_connection(("127.0.0.1", port), 1) as conn:
            conn.settimeout(1)
            try:
                assert conn.recv(1) == b""
            except ConnectionResetError:
                pass
        elapsed = time.monotonic() - start
        assert elapsed < 0.25
        rejected.append({"port": port, "seconds": elapsed})

    reject(lab.a)
    reject(lab.a_alt)
    healthy = lab.traffic(port=lab.b, app="b", instances="b1", concurrency=1, count=30)
    for _ in range(3):
        held.append(lab.hold(lab.b)[0])
    reject(lab.b)
    lab.write_config([f"127.0.0.1:{lab.a2}"])
    lab.reload()
    reject(lab.a_alt)
    held[0].sendall(b"old-session")
    assert held[0].recv(11) == b"old-session"
    held[0].close()
    deadline = time.monotonic() + 2
    while True:
        try:
            connection, identity = lab.hold(lab.a)
            held.append(connection)
            assert identity["instance"] == "a2"
            break
        except AssertionError:
            assert time.monotonic() < deadline
            time.sleep(0.01)
    invalid = []
    for flags in [
        ["--app-limit", "a=0"],
        ["--app-limit", "a=4097"],
        ["--app-limit", "a=3"],
    ]:
        result = subprocess.run(
            command + flags, capture_output=True, text=True, timeout=2
        )
        assert result.returncode != 0
        invalid.append(
            {"flags": flags, "exit": result.returncode, "stderr": result.stderr}
        )
    save(
        args.out / "results.json",
        {
            "source_commit": (ROOT / ".source-revision").read_text().strip(),
            "limits": {"a": 2, "b": 3},
            "shared_listeners": True,
            "reload_preserved_admission": True,
            "rejections": rejected,
            "healthy_b": healthy,
            "invalid": invalid,
        },
    )
    print(
        "Per-application admission, listener sharing, reload and invalid overrides passed"
    )
finally:
    for conn in held:
        conn.close()
    lab.close()
