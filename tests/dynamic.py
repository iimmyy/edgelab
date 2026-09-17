#!/usr/bin/env python3
import argparse
import json
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
    args.out.mkdir(parents=True, exist_ok=False)
    children, logs = [], []
    ports = set()
    while len(ports) < 4:
        ports.add(free_port())
    route, admin, public, backend = sorted(ports)
    token = "fixture-only-dynamic-proxy-test-token"
    cache = args.out / "cache.json"
    router_config = args.out / "router.json"
    save(
        router_config,
        {"owners": {"worker-1": {"token": token, "applications": ["echo"]}}},
    )
    proxy_config = args.out / "proxy.json"
    save(
        proxy_config,
        {
            "routers": [f"http://127.0.0.1:{route}"],
            "cache": str(cache),
            "management": f"127.0.0.1:{admin}",
            "applications": {"echo": [public]},
        },
    )

    def launch(name, command):
        log = (args.out / f"{name}-{len(children)}.log").open("w")
        logs.append(log)
        child = subprocess.Popen(command, stdout=log, stderr=log)
        children.append(child)
        return child

    def request(port, path, data=None):
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

    def until(check, seconds=5):
        deadline = time.monotonic() + seconds
        last = None
        while time.monotonic() < deadline:
            try:
                value = check()
                if value:
                    return value
            except (OSError, urllib.error.URLError) as error:
                last = error
            time.sleep(0.03)
        raise AssertionError(f"condition timed out: {last}")

    def start_proxy():
        child = launch(
            "proxy",
            [
                str(ROOT / "target/release/edgelab-proxy"),
                "--dynamic",
                str(proxy_config),
                "--drain-ms",
                "500",
            ],
        )
        until(lambda: request(admin, "/status"))
        return child

    def traffic(name):
        result = subprocess.run(
            [
                str(ROOT / "bin/traffic"),
                "--address",
                f"127.0.0.1:{public}",
                "--instances",
                "dynamic-one",
                "--count",
                "20",
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

    def unavailable():
        with socket.create_connection(("127.0.0.1", public), timeout=1) as connection:
            connection.settimeout(1)
            try:
                assert connection.recv(1) == b""
            except ConnectionResetError:
                pass

    results = []
    try:
        launch(
            "echo",
            [
                str(ROOT / "bin/echo"),
                "--listen",
                f"127.0.0.1:{backend}",
                "--instance",
                "dynamic-one",
            ],
        )
        proxy = start_proxy()
        assert request(admin, "/status")["available"] is False
        unavailable()
        results.append("missing-cache-management-alive-no-route")
        router = launch(
            "router",
            [
                str(ROOT / "target/release/edgelab-routing"),
                "--config",
                str(router_config),
                "--state",
                str(args.out / "router-state.json"),
                "--listen",
                f"127.0.0.1:{route}",
            ],
        )
        until(lambda: request(route, "/health"))
        snapshot = {
            "schema": 1,
            "owner": "worker-1",
            "incarnation": 1,
            "revision": 1,
            "records": {
                "one": {
                    "id": "one",
                    "app": "echo",
                    "endpoint": f"127.0.0.1:{backend}",
                    "revision": 1,
                    "deleted": False,
                }
            },
        }
        request(route, "/snapshot", snapshot)
        until(lambda: "worker-1" in request(admin, "/status")["owners"])
        traffic("connected")
        results.append("independent-identity-and-payload")
        router.kill()
        router.wait(timeout=3)
        until(
            lambda: request(admin, "/status")["owners"]["worker-1"]["stale"], seconds=7
        )
        traffic("partitioned")
        results.append("partition-continues-serving-stale")
        proxy.terminate()
        proxy.wait(timeout=2)
        proxy = start_proxy()
        assert request(admin, "/status")["owners"]["worker-1"]["stale"]
        traffic("restarted")
        results.append("restart-from-stale-durable-cache")
        proxy.terminate()
        proxy.wait(timeout=2)
        cache.write_text("{broken")
        proxy = start_proxy()
        assert request(admin, "/status")["available"] is False
        unavailable()
        results.append("corrupt-cache-management-alive-no-route")
        save(
            args.out / "results.json",
            {
                "scope": "dynamic proxy foundation, not full Release 4 acceptance",
                "passed": results,
                "source_commit": (ROOT / ".source-revision").read_text().strip(),
                "interruption": "process only",
            },
        )
        print(json.dumps(results))
    finally:
        for child in reversed(children):
            if child.poll() is None:
                child.terminate()
                try:
                    child.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    child.kill()
                    child.wait(timeout=2)
        for log in logs:
            log.close()


if __name__ == "__main__":
    main()
