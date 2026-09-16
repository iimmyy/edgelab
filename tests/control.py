#!/usr/bin/env python3
"""Fault tests for control work that must not stop admission or bounded shutdown."""

import argparse
import json
import signal
import subprocess
import threading
import time
from pathlib import Path

from verify import ROOT, Lab, resource, save


def wait_exit(proc, timeout=1):
    deadline = time.monotonic() + timeout
    while proc.poll() is None:
        if time.monotonic() >= deadline:
            raise AssertionError("process exceeded shutdown budget")
        time.sleep(0.001)


def blocked_logs(lab):
    lab.proxy.terminate()
    lab.proxy.wait(timeout=2)
    outcomes = []
    for resume in [True, False]:
        proc = subprocess.Popen(
            [
                str(ROOT / "target/release/edgelab-proxy"),
                "--config",
                str(lab.config),
                "--limit",
                "8",
                "--drain-ms",
                "500",
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
        )
        lab.children.append(proc)
        lab.proxy = proc
        lab.wait_port(lab.a)
        before = resource(proc.pid)
        lab.traffic(count=3000, concurrency=4, label="blocked-logs")
        b = lab.traffic(port=lab.b, app="b", instances="b1", count=30)
        assert b["max_ms"] < 250
        after = resource(proc.pid)
        assert after["rss_bytes"] < before["rss_bytes"] + 16 * 1024 * 1024
        if resume:
            lines = []

            def collect():
                for line in proc.stderr:
                    lines.append(line)

            reader = threading.Thread(target=collect)
            reader.start()
            time.sleep(1.2)
            proc.terminate()
            wait_exit(proc)
            reader.join(timeout=1)
            assert not reader.is_alive()
            data = b"".join(lines)
            events = [json.loads(line)["fields"] for line in data.splitlines()]
            drops = max(e.get("logs_dropped", 0) for e in events)
            assert drops > 0
            (lab.out / "logging-resumed.jsonl").write_bytes(data)
            outcomes.append(
                {
                    "resumed": True,
                    "drops": drops,
                    "b": b,
                    "before": before,
                    "after": after,
                }
            )
        else:
            held, _ = lab.hold()
            start = time.monotonic()
            proc.terminate()
            wait_exit(proc)
            elapsed = time.monotonic() - start
            assert 0.45 <= elapsed < 0.65
            assert held.recv(1) == b""
            held.close()
            (lab.out / "logging-blocked-at-exit.jsonl").write_bytes(proc.stderr.read())
            outcomes.append(
                {
                    "resumed": False,
                    "exit_seconds": elapsed,
                    "b": b,
                    "before": before,
                    "after": after,
                }
            )
        proc.stderr.close()
    lab.start_proxy()
    return outcomes


def reloads(lab):
    lab.proxy.terminate()
    lab.proxy.wait(timeout=2)
    lab.write_config([f"127.0.0.1:{lab.a1}"])
    previous = sum(e.get("event") == "ready" for e in lab.events())
    lab.proxy = lab.spawn(
        [
            str(ROOT / "target/release/edgelab-proxy"),
            "--config",
            str(lab.config),
            "--limit",
            "8",
            "--drain-ms",
            "500",
            "--reload-delay-ms",
            "750",
        ],
        "proxy",
    )
    lab.await_event("ready", previous)
    activations = sum(e.get("event") == "reloaded" for e in lab.events())
    reads = sum(e.get("event") == "reload_candidate_read" for e in lab.events())
    rejected = sum(e.get("event") == "reload_rejected" for e in lab.events())

    def signal_reload():
        previous = sum(e.get("event") == "reload_requested" for e in lab.events())
        lab.proxy.send_signal(signal.SIGHUP)
        lab.await_event("reload_requested", previous)

    lab.write_config([f"127.0.0.1:{lab.a2}"])
    signal_reload()
    lab.await_event("reload_candidate_read", reads)
    probes = lab.traffic(instances="a1", count=100)
    assert probes["max_ms"] < 250
    lab.write_config([f"127.0.0.1:{lab.a2}"])
    signal_reload()
    lab.config.write_text("{invalid newest candidate")
    signal_reload()
    lab.await_event("reload_rejected", rejected)
    assert sum(e.get("event") == "reloaded" for e in lab.events()) == activations
    lab.traffic(instances="a1", count=20)
    superseded = [e for e in lab.events() if e.get("event") == "reload_superseded"]
    assert superseded
    assert all(e.get("validations", 0) <= 1 for e in lab.events())
    lab.write_config([f"127.0.0.1:{lab.a2}"])
    lab.reload()
    lab.traffic(instances="a2", count=20)

    reads = sum(e.get("event") == "reload_candidate_read" for e in lab.events())
    signal_reload()
    lab.await_event("reload_candidate_read", reads)
    held, _ = lab.hold()
    start = time.monotonic()
    lab.proxy.terminate()
    wait_exit(lab.proxy)
    elapsed = time.monotonic() - start
    held.close()
    assert 0.45 <= elapsed < 0.65
    return {
        "probes": probes,
        "superseded": superseded,
        "invalid_newest_retained_active": True,
        "exit_during_validation_seconds": elapsed,
    }


parser = argparse.ArgumentParser()
parser.add_argument("--out", type=Path, required=True)
args = parser.parse_args()
args.out.mkdir(parents=True, exist_ok=False)
lab = Lab(args.out)
try:
    report = {"blocked_logging": blocked_logs(lab), "reload": reloads(lab)}
    save(args.out / "control-results.json", report)
    print(json.dumps(report))
finally:
    lab.close()
