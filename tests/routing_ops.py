#!/usr/bin/env python3
import argparse
import json
import os
import secrets
import socket
import sqlite3
import subprocess
import time
import urllib.error
import urllib.request
from pathlib import Path

from verify import ROOT, free_port, save, resource


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    assert os.geteuid() == 0, "supervision test requires root in the lab VM"
    source_commit = (ROOT / ".source-revision").read_text().strip()
    args.out = args.out.resolve()
    args.out.mkdir(parents=True, exist_ok=False)
    ports = set()
    while len(ports) < 16:
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
                "admin_token": admin_token,
                "owners": {
                    f"worker-{i + 1}": {
                        "token": tokens[i],
                        "applications": ["echo"] + (["aux"] if i == 1 else []),
                    }
                    for i in range(3)
                },
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
                    "router_admin_token": admin_token,
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
            if i == 1:
                auxiliary = unit(
                    "aux",
                    [
                        ROOT / "bin/echo",
                        "--listen",
                        f"127.0.0.1:{ports[13]}",
                        "--worker",
                        worker,
                        "--instance",
                        "aux-1",
                        "--app",
                        "aux",
                        "--version",
                        "r4",
                    ],
                )
                workloads.append(auxiliary)
                changed = json.loads(config.read_text())
                changed["applications"].append("aux")
                changed["workloads"]["aux-1"] = {
                    "unit": auxiliary,
                    "app": "aux",
                    "endpoint": f"127.0.0.1:{ports[13]}",
                    "version": "r4",
                    "kind": "echo",
                }
                save(config, changed)
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
            if i == 1:
                aux_revision = request(
                    worker_ports[i],
                    "/instances",
                    {
                        "operation": "create-aux",
                        "id": "aux-1",
                        "app": "aux",
                        "endpoint": f"127.0.0.1:{ports[13]}",
                        "deleted": False,
                    },
                )["revision"]

        proxies = []
        for i in range(2):
            config = args.out / f"proxy-{i}.json"
            save(
                config,
                {
                    "routers": [f"http://127.0.0.1:{router_ports[i]}"],
                    "cache": str(args.out / f"cache-{i}.json"),
                    "management": f"127.0.0.1:{management_ports[i]}",
                    "applications": {"echo": [public_ports[i]], "aux": [ports[14 + i]]},
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
                    == 4
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
        backup_path = args.out / "worker-before-deletion.db"
        with (
            sqlite3.connect(args.out / "worker-0.db") as source,
            sqlite3.connect(backup_path) as backup,
        ):
            source.backup(backup)
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
        for worker_unit in workers:
            systemctl("kill", "--signal=SIGSTOP", worker_unit)
        stale_before = json.loads((args.out / "router-1.state").read_text())
        assert not stale_before["owners"]["worker-1"]["snapshot"]["records"][
            "instance-1"
        ]["deleted"]
        systemctl("kill", "--kill-whom=main", "--signal=SIGKILL", router_units[1])
        router_units[1] = unit(
            "router-stale-rejoin",
            [
                ROOT / "target/release/edgelab-routing",
                "--config",
                router_config,
                "--state",
                args.out / "router-1.state",
                "--listen",
                f"127.0.0.1:{router_ports[1]}",
            ],
        )
        until(lambda: request(router_ports[1], "/health"))
        stale_restarted = request(router_ports[1], "/view")
        assert stale_restarted["owners"]["worker-1"]["stale"]
        assert not stale_restarted["owners"]["worker-1"]["snapshot"]["records"][
            "instance-1"
        ]["deleted"]
        save(
            args.out / "stale-rejoin.json",
            {"before": stale_before, "restarted": stale_restarted},
        )
        for worker_unit in workers:
            systemctl("kill", "--signal=SIGCONT", worker_unit)
        until(lambda: route_deleted(router_ports[1]))
        time.sleep(0.7)
        traffic(1, "healed", "instance-2,instance-3")
        observations.append("partitioned-router-converges-to-deletion")
        systemctl("stop", workers[0])
        guard = args.out / "restore-0.guard"
        with guard.open("w") as file:
            file.write("restore-old-backup")
            file.flush()
            os.fsync(file.fileno())
        with (
            sqlite3.connect(backup_path) as backup,
            sqlite3.connect(args.out / "worker-0.db") as restored,
        ):
            backup.backup(restored)
        recovery = [
            str(ROOT / "bin/worker"),
            "recover",
            "--config",
            str(args.out / "worker-0.json"),
            "--state",
            str(args.out / "worker-0.db"),
            "--journal",
            str(args.out / "recovery.json"),
            "--operation",
            "restore-old-backup",
        ]
        interrupted = subprocess.run(
            [*recovery, "--crash-after-first-ack"],
            capture_output=True,
            text=True,
            timeout=10,
        )
        assert interrupted.returncode == 77, interrupted.stdout + interrupted.stderr
        assert guard.exists()
        first = request(router_ports[0], "/view")["owners"]["worker-1"]
        second = request(router_ports[1], "/view")["owners"]["worker-1"]
        assert (
            first["snapshot"]["incarnation"] == 2
            and second["snapshot"]["incarnation"] == 1
        )
        assert second["frozen_by"] == "restore-old-backup"
        systemctl("kill", "--signal=SIGSTOP", router_units[1])
        blocked = subprocess.run(recovery, capture_output=True, text=True, timeout=10)
        assert blocked.returncode != 0 and guard.exists()
        systemctl("kill", "--signal=SIGCONT", router_units[1])
        resumed = subprocess.run(recovery, capture_output=True, text=True, timeout=10)
        assert resumed.returncode == 0, resumed.stdout + resumed.stderr
        assert not guard.exists()
        recovered = [
            request(port, "/view")["owners"]["worker-1"] for port in router_ports
        ]
        assert recovered[0]["recovery"] == recovered[1]["recovery"]
        assert all(
            owner["snapshot"]["records"]["instance-1"]["deleted"] for owner in recovered
        )
        for port in router_ports:
            try:
                request(port, "/snapshot", recovered[0]["snapshot"], token=tokens[0])
            except urllib.error.HTTPError as error:
                assert error.code == 401
            else:
                raise AssertionError("old publishing credential accepted")
        unit(
            "worker-recovered",
            [
                ROOT / "bin/worker",
                "serve",
                "--config",
                args.out / "worker-0.json",
                "--state",
                args.out / "worker-0.db",
                "--listen",
                f"127.0.0.1:{worker_ports[0]}",
            ],
        )
        until(lambda: request(worker_ports[0], "/status"))
        time.sleep(3)
        traffic(0, "recovered", "instance-2,instance-3")
        assert systemctl("show", "-p", "MainPID", "--value", workloads[0]) in {"", "0"}
        save(
            args.out / "recovery-proof.json",
            {
                "interrupted_exit": interrupted.returncode,
                "blocked_exit": blocked.returncode,
                "resumed": json.loads(resumed.stdout),
                "matching_router_recovery": recovered[0]["recovery"],
                "deletion_preserved": True,
            },
        )
        observations.append(
            "backup-recovery-resumes-one-node-ack-with-deletion-and-credential-fencing"
        )

        canary_db = args.out / "canary.db"
        with (
            sqlite3.connect(args.out / "worker-1.db") as source,
            sqlite3.connect(canary_db) as target,
        ):
            source.backup(target)
        canary = subprocess.run(
            [
                str(ROOT / "bin/worker"),
                "serve",
                "--config",
                str(args.out / "worker-1.json"),
                "--state",
                str(canary_db),
                "--listen",
                f"127.0.0.1:{ports[12]}",
                "--status-version",
                "2",
            ],
            capture_output=True,
            text=True,
            timeout=5,
        )
        assert (
            canary.returncode != 0
            and "no such column: deployment_epoch" in canary.stdout
        )
        with socket.socket() as probe:
            assert probe.connect_ex(("127.0.0.1", ports[12])) != 0
        traffic(1, "canary-rejected", "instance-2,instance-3")
        save(
            args.out / "canary.json",
            {
                "exit": canary.returncode,
                "error": canary.stdout,
                "listener_opened": False,
                "active_profile": 1,
                "rejected_profile": 2,
            },
        )
        observations.append("schema-incompatible-canary-rejected-before-readiness")
        database = args.out / "worker-1.db"
        reader = sqlite3.connect(database)
        writer = sqlite3.connect(database, timeout=0.1)
        writer.execute("PRAGMA wal_autocheckpoint=0")
        reader.execute("BEGIN")
        reader.execute("SELECT count(*) FROM routing_operations").fetchone()
        background = subprocess.Popen(
            [
                str(ROOT / "bin/traffic"),
                "--address",
                f"127.0.0.1:{public_ports[1]}",
                "--instances",
                "instance-2,instance-3",
                "--duration",
                "3s",
                "--count",
                "4000000",
                "--concurrency",
                "4",
                "--history",
                str(args.out / "maintenance-traffic.jsonl"),
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        try:
            writer.executemany(
                "INSERT INTO routing_operations(id,request_hash,revision) VALUES(?,?,?)",
                [(f"maintenance-{i}", f"{i:064x}", 1) for i in range(4096)],
            )
            writer.commit()
            query = "SELECT revision FROM routing_operations WHERE request_hash=?"
            before = writer.execute(
                "EXPLAIN QUERY PLAN " + query, (f"{2000:064x}",)
            ).fetchall()
            blocked = writer.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
            peak_wal = Path(str(database) + "-wal").stat().st_size
            assert blocked[0] == 1 and peak_wal > 0
            reader.commit()
            writer.execute(
                "CREATE INDEX routing_operations_request_hash ON routing_operations(request_hash)"
            )
            writer.commit()
            after = writer.execute(
                "EXPLAIN QUERY PLAN " + query, (f"{2000:064x}",)
            ).fetchall()
            checkpoint = writer.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
            assert checkpoint[0] == 0
            assert any("SCAN" in row[-1] for row in before)
            assert any("SEARCH" in row[-1] for row in after)
            stdout, stderr = background.communicate(timeout=7)
            assert background.returncode == 0, stdout + stderr
            save(
                args.out / "maintenance.json",
                {
                    "seeded_rows": 4096,
                    "before_plan": before,
                    "after_plan": after,
                    "blocked_checkpoint": blocked,
                    "recovered_checkpoint": checkpoint,
                    "peak_wal_bytes": peak_wal,
                    "client": json.loads(stdout),
                    "remediation": "release the controlled reader, add the lookup index, checkpoint through SQLite",
                },
            )
        finally:
            reader.close()
            writer.close()
            if background.poll() is None:
                background.kill()
                background.wait()
        observations.append("query-plan-and-wal-maintenance-under-verified-traffic")
        router_pid = int(systemctl("show", "-p", "MainPID", "--value", router_units[1]))
        proxy_pid = int(systemctl("show", "-p", "MainPID", "--value", proxies[1]))
        baseline_resources = {
            "router": resource(router_pid),
            "proxy": resource(proxy_pid),
        }
        prior_generation = request(management_ports[1], "/status")["generation"]
        systemctl("kill", "--signal=SIGSTOP", proxies[1])
        samples = []
        for n in range(16):
            revision = request(
                worker_ports[1],
                "/instances",
                {
                    "operation": f"slow-consumer-{n}",
                    "id": "instance-2",
                    "app": "echo",
                    "endpoint": f"127.0.0.1:{echo_ports[1]}",
                    "deleted": False,
                },
            )["revision"]
            snapshot = {
                "schema": 1,
                "owner": "worker-2",
                "incarnation": 1,
                "revision": revision,
                "records": {
                    "instance-2": {
                        "id": "instance-2",
                        "app": "echo",
                        "endpoint": f"127.0.0.1:{echo_ports[1]}",
                        "revision": revision,
                        "deleted": False,
                    },
                    "aux-1": {
                        "id": "aux-1",
                        "app": "aux",
                        "endpoint": f"127.0.0.1:{ports[13]}",
                        "revision": aux_revision,
                        "deleted": False,
                    },
                },
            }
            until(
                lambda: request(router_ports[1], "/snapshot", snapshot, token=tokens[1])
            )
            samples.append(
                {
                    "router": resource(router_pid),
                    "proxy": resource(proxy_pid),
                    "queue": request(router_ports[1], "/health"),
                }
            )
        expected_generation = request(router_ports[1], "/view")["generation"]
        assert expected_generation > prior_generation + 1
        systemctl("kill", "--signal=SIGCONT", proxies[1])
        until(
            lambda: (
                request(management_ports[1], "/status")["generation"]
                >= expected_generation
            )
        )
        assert (
            json.loads((args.out / "cache-1.json").read_text())["owners"]["worker-2"][
                "snapshot"
            ]["revision"]
            == revision
        )
        with sqlite3.connect(args.out / "worker-1.db") as database:
            assert (
                database.execute(
                    "SELECT count(*) FROM routing_operations WHERE id LIKE 'slow-consumer-%'"
                ).fetchone()[0]
                == 16
            )
        for sample in samples:
            assert sample["queue"]["pending_view_capacity"] == 1
            for role in ["router", "proxy"]:
                assert (
                    sample[role]["rss_bytes"] - baseline_resources[role]["rss_bytes"]
                    < 32 * 1024 * 1024
                )
        assert request(router_ports[1], "/health")["coalesced_deliveries"] > 0
        save(
            args.out / "slow-subscriber.json",
            {
                "baseline": baseline_resources,
                "samples": samples,
                "final_generation": expected_generation,
                "resynced_revision": revision,
                "durable_operations": 16,
                "memory_delta_limit_bytes": 32 * 1024 * 1024,
            },
        )
        traffic(1, "subscriber-resynced", "instance-2,instance-3")
        observations.append(
            "slow-subscriber-bounded-memory-coalescing-and-complete-resync"
        )
        auxiliary_traffic = subprocess.Popen(
            [
                str(ROOT / "bin/traffic"),
                "--address",
                f"127.0.0.1:{ports[15]}",
                "--app",
                "aux",
                "--instances",
                "aux-1",
                "--duration",
                "1s",
                "--count",
                "4000000",
                "--history",
                str(args.out / "invalid-update-aux.jsonl"),
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        # The invalid update is rejected before it can poison the restart cache.
        try:
            request(
                router_ports[1],
                "/snapshot",
                {
                    "schema": 999,
                    "owner": "worker-2",
                    "incarnation": 1,
                    "revision": 1,
                    "records": {},
                },
                token=tokens[1],
            )
        except urllib.error.HTTPError as error:
            assert error.code == 409
        else:
            raise AssertionError("incompatible update accepted")
        auxiliary_output, auxiliary_error = auxiliary_traffic.communicate(timeout=5)
        assert auxiliary_traffic.returncode == 0, auxiliary_output + auxiliary_error
        save(
            args.out / "invalid-update.json",
            {"status": 409, "unrelated_app": json.loads(auxiliary_output)},
        )
        observations.append("invalid-app-update-preserves-unrelated-application")
        systemctl("kill", "--signal=SIGSTOP", proxies[1])
        subprocess.run(
            [
                "python3",
                str(ROOT / "lab/watchdog.py"),
                "--unit",
                proxies[1],
                "--status",
                f"http://127.0.0.1:{management_ports[1]}/status",
                "--out",
                str(args.out / "watchdog"),
            ],
            check=True,
            timeout=15,
        )
        traffic(1, "watchdog-recovered", "instance-2,instance-3")
        observations.append("independent-watchdog-captures-stall-and-restarts-once")
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
                "source_commit": source_commit,
            },
        )
        assert (ROOT / ".source-revision").read_text().strip() == source_commit, (
            "source changed during run"
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
