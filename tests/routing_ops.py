#!/usr/bin/env python3
import argparse
import json
import os
import secrets
import socket
import subprocess
import time
import urllib.error
import urllib.request
from pathlib import Path

from verify import ROOT, free_port, save


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    assert os.geteuid() == 0, "supervision test requires root in the lab VM"
    args.out = args.out.resolve()
    args.out.mkdir(parents=True, exist_ok=False)
    ports = set()
    while len(ports) < 13:
        ports.add(free_port())
    ports = list(sorted(ports))
    router_ports, worker_ports, echo_ports = ports[:2], ports[2:5], ports[5:8]
    management_ports, public_ports = ports[8:10], ports[10:12]
    prefix = "edgelab-r4-" + secrets.token_hex(4)
    units, observations = [], []
    admin_token = secrets.token_hex(32)
    tokens = [secrets.token_hex(32) for _ in range(3)]

    def systemctl(*args):
        return subprocess.check_output(
            ["systemctl", *args], text=True, timeout=12
        ).strip()

    def unit(name, command):
        name = prefix + "-" + name + ".service"
        subprocess.run(
            [
                "systemd-run",
                "--quiet",
                "--collect",
                "--unit",
                name,
                "--property=KillMode=control-group",
                "--property=TimeoutStopSec=7s",
                "--",
                *map(str, command),
            ],
            check=True,
            timeout=5,
        )
        units.append(name)
        return name

    def request(port, path, data=None, token=admin_token):
        req = urllib.request.Request(
            f"http://127.0.0.1:{port}{path}",
            data=None if data is None else json.dumps(data).encode(),
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {token}",
            },
        )
        with urllib.request.urlopen(req, timeout=3) as response:
            return json.load(response)

    def until(check, seconds=15):
        deadline = time.monotonic() + seconds
        last = None
        while time.monotonic() < deadline:
            try:
                result = check()
                if result:
                    return result
            except (OSError, urllib.error.URLError, KeyError) as error:
                last = error
            time.sleep(0.1)
        raise AssertionError(f"condition timed out: {last}")

    def traffic(index, name, identities="instance-1,instance-2,instance-3"):
        result = subprocess.run(
            [
                str(ROOT / "bin/traffic"),
                "--address",
                f"127.0.0.1:{public_ports[index]}",
                "--instances",
                identities,
                "--count",
                "30",
                "--concurrency",
                "2",
                "--history",
                str(args.out / f"{name}.jsonl"),
            ],
            capture_output=True,
            text=True,
            timeout=10,
        )
        save(
            args.out / f"{name}.json",
            {
                "exit": result.returncode,
                "stdout": result.stdout,
                "stderr": result.stderr,
            },
        )
        assert result.returncode == 0, result.stdout + result.stderr

    def route_deleted(port):
        return request(port, "/view")["owners"]["worker-1"]["snapshot"]["records"][
            "instance-1"
        ]["deleted"]

    held = None
    try:
        router_config = args.out / "routers.json"
        save(
            router_config,
            {
                "owners": {
                    f"worker-{i + 1}": {"token": tokens[i], "applications": ["echo"]}
                    for i in range(3)
                }
            },
        )
        router_units = [
            unit(
                f"router-{i}",
                [
                    ROOT / "target/release/edgelab-routing",
                    "--config",
                    router_config,
                    "--state",
                    args.out / f"router-{i}.state",
                    "--listen",
                    f"127.0.0.1:{port}",
                ],
            )
            for i, port in enumerate(router_ports)
        ]
        for port in router_ports:
            until(lambda port=port: request(port, "/health"))
        workers, workloads = [], []
        for i in range(3):
            worker, instance = f"worker-{i + 1}", f"instance-{i + 1}"
            workload = unit(
                f"echo-{i}",
                [
                    ROOT / "bin/echo",
                    "--listen",
                    f"127.0.0.1:{echo_ports[i]}",
                    "--worker",
                    worker,
                    "--instance",
                    instance,
                    "--region",
                    "west" if i == 0 else "east",
                    "--version",
                    "r4",
                ],
            )
            workloads.append(workload)
            config = args.out / f"worker-{i}.json"
            save(
                config,
                {
                    "owner": worker,
                    "applications": ["echo"],
                    "token": tokens[i],
                    "admin_token": admin_token,
                    "routers": [f"http://127.0.0.1:{port}" for port in router_ports],
                    "restore_guard": str(args.out / f"restore-{i}.guard"),
                    "workloads": {
                        instance: {
                            "unit": workload,
                            "app": "echo",
                            "endpoint": f"127.0.0.1:{echo_ports[i]}",
                            "version": "r4",
                            "kind": "echo",
                        }
                    },
                },
            )
            workers.append(
                unit(
                    f"worker-{i}",
                    [
                        ROOT / "bin/worker",
                        "serve",
                        "--config",
                        config,
                        "--state",
                        args.out / f"worker-{i}.db",
                        "--listen",
                        f"127.0.0.1:{worker_ports[i]}",
                    ],
                )
            )
            until(lambda i=i: request(worker_ports[i], "/status"))
            registration = {
                "operation": "create-one",
                "id": instance,
                "app": "echo",
                "endpoint": f"127.0.0.1:{echo_ports[i]}",
                "deleted": False,
            }
            first = request(worker_ports[i], "/instances", registration)
            repeated = request(worker_ports[i], "/instances", registration)
            assert first == repeated, "idempotent request changed its revision"
        proxies = []
        for i in range(2):
            config = args.out / f"proxy-{i}.json"
            save(
                config,
                {
                    "routers": [f"http://127.0.0.1:{router_ports[i]}"],
                    "cache": str(args.out / f"cache-{i}.json"),
                    "management": f"127.0.0.1:{management_ports[i]}",
                    "applications": {"echo": [public_ports[i]]},
                },
            )
            proxies.append(
                unit(
                    f"proxy-{i}",
                    [
                        ROOT / "target/release/edgelab-proxy",
                        "--dynamic",
                        config,
                        "--drain-ms",
                        "5000",
                    ],
                )
            )
            until(
                lambda i=i: len(request(management_ports[i], "/status")["owners"]) == 3
            )
            until(
                lambda i=i: (
                    sum(
                        len(o["snapshot"]["records"])
                        for o in request(router_ports[i], "/view")["owners"].values()
                    )
                    == 3
                )
            )
            time.sleep(0.6)
            traffic(i, f"initial-{i}")
        observations.append("three-workers-two-routers-two-proxies-identity-verified")
        before = [systemctl("show", "-p", "MainPID", "--value", u) for u in workloads]
        systemctl("restart", workers[0])
        until(lambda: request(worker_ports[0], "/status"))
        after = [systemctl("show", "-p", "MainPID", "--value", u) for u in workloads]
        assert before == after and all(int(pid) > 0 for pid in after)
        traffic(0, "worker-restarted")
        save(args.out / "workload-pids.json", {"before": before, "after": after})
        observations.append("workloads-survive-management-restart-without-duplicates")
        systemctl("kill", "--signal=SIGSTOP", router_units[1])
        deletion = {
            "operation": "delete-one",
            "id": "instance-1",
            "app": "echo",
            "endpoint": f"127.0.0.1:{echo_ports[0]}",
            "deleted": True,
        }
        request(worker_ports[0], "/instances", deletion)
        until(lambda: route_deleted(router_ports[0]))
        time.sleep(0.7)
        traffic(0, "deleted", "instance-2,instance-3")
        until(
            lambda: request(management_ports[1], "/status")["owners"]["worker-1"][
                "stale"
            ],
            seconds=8,
        )
        traffic(1, "partitioned-cache", "instance-2,instance-3")
        systemctl("kill", "--signal=SIGCONT", router_units[1])
        until(lambda: route_deleted(router_ports[1]))
        time.sleep(0.7)
        traffic(1, "healed", "instance-2,instance-3")
        observations.append("partitioned-router-converges-to-deletion")
        held = socket.create_connection(("127.0.0.1", public_ports[0]), timeout=2)
        held.recv(4096)
        proxy_pid = systemctl("show", "-p", "MainPID", "--value", proxies[0])
        systemctl("kill", "--signal=SIGTERM", proxies[0])
        started = time.monotonic()
        systemctl("restart", router_units[0])
        until(lambda: request(router_ports[0], "/health"), seconds=2)
        elapsed = time.monotonic() - started
        assert elapsed < 2 and Path("/proc", proxy_pid).exists()
        held.sendall(b"draining-session")
        assert held.recv(100) == b"draining-session"
        held.close()
        held = None
        save(
            args.out / "independent-restart.json",
            {"router_restart_seconds": elapsed, "draining_proxy_pid": proxy_pid},
        )
        observations.append("router-restarts-while-proxy-drains-live-stream")
        save(
            args.out / "results.json",
            {
                "scope": "routing and supervision integration; incomplete Release 4",
                "passed": observations,
                "source_commit": (ROOT / ".source-revision").read_text().strip(),
            },
        )
        print(json.dumps(observations))
    finally:
        if held:
            held.close()
        for name in units:
            subprocess.run(
                ["systemctl", "kill", "--signal=SIGCONT", name],
                capture_output=True,
                timeout=3,
            )
        for name in reversed(units):
            subprocess.run(
                ["journalctl", "-u", name, "--no-pager", "-o", "json"],
                stdout=(args.out / f"{name}.journal").open("w"),
                timeout=5,
            )
            subprocess.run(["systemctl", "stop", name], capture_output=True, timeout=10)


if __name__ == "__main__":
    main()
