#!/usr/bin/env python3
import argparse
import json
import subprocess
import time
import urllib.request
from pathlib import Path


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--unit", required=True)
    parser.add_argument("--status", required=True)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    if (
        not args.unit.startswith("edgelab-")
        or "/" in args.unit
        or not args.unit.endswith(".service")
    ):
        raise SystemExit("expected owned EdgeLab unit")
    args.out.mkdir(parents=True, exist_ok=False)
    failures = []
    for _ in range(2):
        started = time.monotonic()
        try:
            with urllib.request.urlopen(args.status, timeout=0.5) as response:
                json.load(response)
            raise SystemExit("target responsive; no mitigation applied")
        except OSError as error:
            failures.append(
                {"elapsed_ms": (time.monotonic() - started) * 1000, "error": str(error)}
            )
        time.sleep(0.2)
    pid = subprocess.check_output(
        ["systemctl", "show", "-p", "MainPID", "--value", args.unit], text=True
    ).strip()
    if not pid.isdecimal() or int(pid) <= 1:
        raise SystemExit("no owned target process")
    evidence = {
        "unit": args.unit,
        "pid": pid,
        "failures": failures,
        "restarts_allowed": 1,
        "status": Path("/proc", pid, "status").read_text(),
        "wchan": Path("/proc", pid, "wchan").read_text(),
    }
    (args.out / "alert.json").write_text(json.dumps(evidence, indent=2) + "\n")
    with (args.out / "journal.jsonl").open("w") as output:
        subprocess.run(
            ["journalctl", "-u", args.unit, "-n", "100", "-o", "json", "--no-pager"],
            stdout=output,
            check=True,
            timeout=3,
        )
    subprocess.run(
        ["systemctl", "kill", "--signal=SIGCONT", args.unit], check=True, timeout=3
    )
    subprocess.run(["systemctl", "restart", args.unit], check=True, timeout=10)
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(args.status, timeout=0.5) as response:
                status = json.load(response)
            if status.get("available"):
                (args.out / "recovery.json").write_text(
                    json.dumps({"restart_count": 1, "status": status}, indent=2) + "\n"
                )
                return
        except OSError:
            pass
        time.sleep(0.1)
    raise SystemExit(
        "one restart did not restore service; stopped automatic mitigation"
    )


if __name__ == "__main__":
    main()
