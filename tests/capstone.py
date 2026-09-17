#!/usr/bin/env python3
import argparse
import functools
import gzip
import hashlib
import http.server
import io
import json
import os
import secrets
import sqlite3
import subprocess
import sys
import tarfile
import threading
import time
from pathlib import Path

PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT / "lab"))
import linux as lab
import worker as storage
import units


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    out = args.out.resolve()
    out.mkdir(parents=True, exist_ok=False, mode=0o700)
    if lab.ROOT.exists() or storage.ROOT.exists():
        raise RuntimeError(
            "capstone requires fresh owned fixtures; preserve or explicitly clear previous runs first"
        )
    source = (PROJECT / ".source-revision").read_text().strip()
    prefix = "edgelab-capstone-" + secrets.token_hex(4)
    unit_names = []
    mounts = []
    stages = []
    done = threading.Event()
    monitor = None
    server = None
    held = None
    phase = "setup"
    monitor_results = []
    admin = secrets.token_hex(32)
    tokens = [secrets.token_hex(32) for _ in range(3)]
    definitions = {}

    def save(name, value):
        (out / name).write_text(json.dumps(value, indent=2) + "\n")

    def run(name, arguments, codes=(0,), input=None, timeout=120):
        started = time.time()
        result = subprocess.run(
            list(map(str, arguments)),
            input=input,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        (out / (name + ".stdout")).write_text(result.stdout)
        (out / (name + ".stderr")).write_text(result.stderr)
        save(
            name + ".json",
            {
                "command": list(map(str, arguments)),
                "started": started,
                "finished": time.time(),
                "exit": result.returncode,
            },
        )
        if result.returncode not in codes:
            raise RuntimeError(
                f"{name}: exit {result.returncode}: {result.stdout[-1000:]} {result.stderr[-1000:]}"
            )
        return result

    def stage(name, **details):
        stages.append({"stage": name, "time": time.time(), **details})
        save("stages.json", stages)
        print(name, flush=True)

    def start(name, arguments, namespace=lab.CLIENT):
        name = prefix + "-" + name + ".service"
        if name not in unit_names:
            unit_names.append(name)
        definitions[name] = units.start(name, arguments, namespace)
        save("unit-definitions.json", definitions)
        return name

    http_code = """import json,sys,urllib.request,urllib.error
p=json.load(sys.stdin)
r=urllib.request.Request(p['url'],data=None if p['data'] is None else json.dumps(p['data']).encode(),headers={'Content-Type':'application/json','Authorization':'Bearer '+p['token']})
try:
 with urllib.request.urlopen(r,timeout=3) as response: print(json.dumps({'status':response.status,'body':json.load(response)}))
except urllib.error.HTTPError as error: print(json.dumps({'status':error.code,'error':error.read().decode()}))
"""

    def request(port, path, data=None):
        result = subprocess.run(
            ["ip", "netns", "exec", lab.CLIENT, "python3", "-c", http_code],
            input=json.dumps(
                {"url": f"http://127.0.0.1:{port}{path}", "token": admin, "data": data}
            ),
            capture_output=True,
            text=True,
            timeout=5,
        )
        if result.returncode:
            raise RuntimeError(result.stderr)
        response = json.loads(result.stdout)
        if response["status"] != 200:
            raise RuntimeError(str(response))
        return response["body"]

    def until(check, seconds=15):
        deadline = time.monotonic() + seconds
        last = None
        while time.monotonic() < deadline:
            try:
                value = check()
                if value:
                    return value
            except (OSError, RuntimeError, KeyError) as error:
                last = error
            time.sleep(0.2)
        raise RuntimeError(f"condition timed out: {last}")

    def traffic(name, instances="echo-1,echo-2", count=20):
        return run(
            name,
            [
                "ip",
                "netns",
                "exec",
                lab.CLIENT,
                PROJECT / "bin/traffic",
                "--address",
                "127.0.0.2:8102",
                "--instances",
                instances,
                "--expected-identities",
                out / "expected-identities.json",
                "--count",
                str(count),
                "--concurrency",
                "2",
                "--history",
                out / (name + ".jsonl"),
            ],
        )

    def objects(name, manifest, verify=False, count=1, size=65536, seed=1, codes=(0,)):
        arguments = [
            "ip",
            "netns",
            "exec",
            lab.CLIENT,
            PROJECT / "bin/objects-check",
            "--url",
            "http://127.0.0.2:8101",
            "--manifest",
            out / manifest,
            "--count",
            str(count),
            "--bytes",
            str(size),
            "--seed",
            str(seed),
        ]
        if verify:
            arguments.append("--verify")
        return run(name, arguments, codes=codes)

    def observe():
        serial = 0
        while not done.is_set():
            serial += 1
            observed_phase = phase
            started = time.time()
            try:
                echo = traffic(f"monitor-echo-{serial}", count=5)
                object_result = objects(
                    f"monitor-objects-{serial}",
                    "monitor-ack.jsonl",
                    verify=True,
                    codes=(0, 1),
                )
                monitor_results.append(
                    {
                        "phase": observed_phase,
                        "started": started,
                        "finished": time.time(),
                        "echo_exit": echo.returncode,
                        "objects_exit": object_result.returncode,
                        "serial": serial,
                    }
                )
            except Exception as error:
                monitor_results.append(
                    {
                        "phase": observed_phase,
                        "started": started,
                        "finished": time.time(),
                        "error": str(error),
                        "serial": serial,
                    }
                )
            done.wait(0.3)

    def inventory():
        return json.loads(
            lab.run(
                "lvs",
                "--devices",
                pool["device"],
                "--reportformat",
                "json",
                "-a",
                "-o",
                "lv_name,lv_uuid,origin,lv_attr,pool_lv",
                "edgelab_worker",
            )
        )["report"][0]["lv"]

    try:
        lab.init()
        lab.storage()
        lab.network()
        lab.workloads()
        pool = storage.up()
        store = out / "image-store"
        (store / "blobs/sha256").mkdir(parents=True)
        expected = {}
        buffer = io.BytesIO()
        with tarfile.open(
            fileobj=buffer, mode="w", format=tarfile.USTAR_FORMAT
        ) as archive:
            directory = tarfile.TarInfo("bin")
            directory.type = tarfile.DIRTYPE
            directory.mode = 0o755
            directory.mtime = 1700000000
            archive.addfile(directory)
            for name in ["echo", "objects"]:
                data = (PROJECT / "bin" / name).read_bytes()
                header = tarfile.TarInfo("bin/" + name)
                header.mode = 0o755
                header.size = len(data)
                header.mtime = 1700000000
                archive.addfile(header, io.BytesIO(data))
                expected["bin/" + name] = hashlib.sha256(data).hexdigest()
        raw = buffer.getvalue()

        def blob(data, media):
            digest = hashlib.sha256(data).hexdigest()
            (store / "blobs/sha256" / digest).write_bytes(data)
            return {"mediaType": media, "digest": "sha256:" + digest, "size": len(data)}

        layer = blob(
            gzip.compress(raw, mtime=0), "application/vnd.oci.image.layer.v1.tar+gzip"
        )
        config = blob(
            json.dumps(
                {
                    "architecture": "arm64",
                    "os": "linux",
                    "rootfs": {
                        "type": "layers",
                        "diff_ids": ["sha256:" + hashlib.sha256(raw).hexdigest()],
                    },
                }
            ).encode(),
            "application/vnd.oci.image.config.v1+json",
        )
        manifest = blob(
            json.dumps(
                {
                    "schemaVersion": 2,
                    "mediaType": "application/vnd.oci.image.manifest.v1+json",
                    "config": config,
                    "layers": [layer],
                }
            ).encode(),
            "application/vnd.oci.image.manifest.v1+json",
        )
        save("image-manifest.json", manifest)
        save("image-expected.json", {"files": expected, "absent": []})
        handler = functools.partial(
            http.server.SimpleHTTPRequestHandler, directory=str(store)
        )
        server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        image_arguments = [
            PROJECT / "bin/worker",
            "--store",
            f"http://127.0.0.1:{server.server_port}",
            "--manifest",
            manifest["digest"],
            "--manifest-bytes",
            str(manifest["size"]),
        ]
        ready = json.loads(
            run(
                "prepare-image", [*image_arguments, "--operation", "capstone-serving"]
            ).stdout
        )
        image_mount = out / "application"
        image_mount.mkdir()
        lab.run("mount", "-o", "nosuid,nodev", ready["snapshot"], image_mount)
        mounts.append(image_mount)
        run(
            "verify-serving-image",
            [
                PROJECT / "bin/image-check",
                "--root",
                image_mount,
                "--expected",
                out / "image-expected.json",
            ],
        )
        save(
            "expected-identities.json",
            {
                f"echo-{i + 1}": {
                    "app": "echo",
                    "worker": f"worker-{i + 1}",
                    "region": "west" if i == 0 else "east",
                    "version": source,
                }
                for i in range(2)
            },
        )
        lab.stop("objects")
        lab.stop("proxy")
        object_unit = start(
            "objects",
            [
                image_mount / "bin/objects",
                "--root",
                lab.ROOT / "volume",
                "--owner",
                "objects-1",
                "--listen",
                "0.0.0.0:9000",
                "--management",
                "0.0.0.0:9001",
            ],
            lab.SERVER,
        )
        object_pid = int(units.command("show", "-p", "MainPID", "--value", object_unit))
        lab.save(
            "objects.pid",
            {
                "pid": object_pid,
                "start": Path(f"/proc/{object_pid}/stat").read_text().split()[21],
            },
        )
        echo_units = []
        for i in range(2):
            echo_units.append(
                start(
                    f"echo-{i + 1}",
                    [
                        image_mount / "bin/echo",
                        "--listen",
                        f"127.0.0.1:{19101 + i}",
                        "--app",
                        "echo",
                        "--instance",
                        f"echo-{i + 1}",
                        "--worker",
                        f"worker-{i + 1}",
                        "--region",
                        "west" if i == 0 else "east",
                        "--version",
                        source,
                    ],
                )
            )
        router_config = out / "routers.json"
        save(
            "routers.json",
            {
                "admin_token": admin,
                "owners": {
                    f"worker-{i + 1}": {
                        "token": tokens[i],
                        "applications": ["echo", "objects"] if i == 0 else ["echo"],
                    }
                    for i in range(3)
                },
            },
        )
        router_units = [
            start(
                f"router-{i + 1}",
                [
                    PROJECT / "target/release/edgelab-routing",
                    "--config",
                    router_config,
                    "--state",
                    out / f"router-{i}.state",
                    "--listen",
                    f"127.0.0.1:{18201 + i}",
                ],
            )
            for i in range(2)
        ]
        for port in [18201, 18202]:
            until(lambda port=port: request(port, "/health"))
        worker_units = []
        for i in range(3):
            workloads = {}
            if i < 2:
                workloads[f"echo-{i + 1}"] = {
                    "unit": echo_units[i],
                    "app": "echo",
                    "endpoint": f"127.0.0.1:{19101 + i}",
                    "version": source,
                    "kind": "echo",
                }
            if i == 0:
                workloads["objects-1"] = {
                    "unit": object_unit,
                    "app": "objects",
                    "endpoint": "10.77.0.2:9000",
                    "version": "",
                    "kind": "objects",
                }
            save(
                f"worker-{i}.json",
                {
                    "owner": f"worker-{i + 1}",
                    "applications": ["echo", "objects"] if i == 0 else ["echo"],
                    "token": tokens[i],
                    "admin_token": admin,
                    "router_admin_token": admin,
                    "routers": ["http://127.0.0.1:18201", "http://127.0.0.1:18202"],
                    "restore_guard": str(out / f"restore-{i}.guard"),
                    "workloads": workloads,
                },
            )
            worker_units.append(
                start(
                    f"worker-{i + 1}",
                    [
                        PROJECT / "bin/worker",
                        "serve",
                        "--config",
                        out / f"worker-{i}.json",
                        "--state",
                        out / f"worker-{i}.db",
                        "--listen",
                        f"127.0.0.1:{18301 + i}",
                    ],
                )
            )
            until(lambda i=i: request(18301 + i, "/status"))
            for identity, workload in workloads.items():
                request(
                    18301 + i,
                    "/instances",
                    {
                        "operation": "create-" + identity,
                        "id": identity,
                        "app": workload["app"],
                        "endpoint": workload["endpoint"],
                        "deleted": False,
                    },
                )
        proxies = []
        for i in range(2):
            save(
                f"proxy-{i}.json",
                {
                    "routers": [f"http://127.0.0.1:{18201 + i}"],
                    "cache": str(out / f"cache-{i}.json"),
                    "management": f"127.0.0.1:{18401 + i}",
                    "applications": {"echo": [8102], "objects": [8101]},
                },
            )
            proxies.append(
                start(
                    f"proxy-{i + 1}",
                    [
                        PROJECT / "target/release/edgelab-proxy",
                        "--dynamic",
                        out / f"proxy-{i}.json",
                        "--listen-ip",
                        f"127.0.0.{2 + i}",
                    ],
                )
            )
            until(lambda i=i: request(18401 + i, "/status")["available"])
        until(lambda: len(request(18201, "/view")["owners"]) == 3)
        until(
            lambda: (
                sum(
                    len(o["snapshot"]["records"])
                    for o in request(18201, "/view")["owners"].values()
                )
                == 3
            )
        )
        time.sleep(1)
        traffic("initial-identities")
        objects("initial-objects", "monitor-ack.jsonl", count=5)
        objects("initial-hashes", "monitor-ack.jsonl", verify=True)
        stage(
            "prepared-image-and-discovered-supervised-applications",
            snapshot_uuid=ready["snapshot_uuid"],
            execution="validated ELF files from a mounted thin snapshot; host processes, not VMs",
        )
        phase = "healthy"
        monitor = threading.Thread(target=observe)
        monitor.start()
        # Select an existing session by its observed identity before killing its backend.
        connector = """import socket,json,sys,time
for _ in range(20):
 s=socket.create_connection(('127.0.0.2',8102),2);s.settimeout(2)
 data=b''
 while not data.endswith(b'\\n'):data+=s.recv(1)
 if json.loads(data)['instance']=='echo-1':break
 s.close()
else:raise RuntimeError('target backend not selected')
s.sendall(b'before');assert s.recv(6)==b'before'
print('ready',flush=True)
sys.stdin.readline()
try:
 s.sendall(b'after');result=s.recv(100);assert result==b'';print(json.dumps({'closed':True,'bytes_after_failure':len(result)}))
except (ConnectionResetError,BrokenPipeError):print(json.dumps({'closed':True,'reset':True}))
"""
        held = subprocess.Popen(
            ["ip", "netns", "exec", lab.CLIENT, "python3", "-c", connector],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        assert held.stdout.readline().strip() == "ready"
        units.command("stop", worker_units[0])
        units.command("kill", "--kill-whom=main", "--signal=SIGKILL", echo_units[0])
        held.stdin.write("continue\n")
        held.stdin.flush()
        held.stdin.close()
        held_result = held.stdout.read()
        held_error = held.stderr.read()
        assert held.wait(timeout=5) == 0, held_error
        save("midstream-failure.json", json.loads(held_result))
        held = None
        traffic("survivor", "echo-2")
        stage("new-connection-fallback-and-existing-session-failure")
        phase = "network-fault"
        fault_start = time.time()
        lab.fault("route")
        save(
            "fault-route.json",
            {
                "route": lab.ns(lab.CLIENT, "ip", "route", "show"),
                "interface": lab.ns(lab.CLIENT, "ip", "-s", "link", "show", "wg0"),
            },
        )
        objects("network-failure", "monitor-ack.jsonl", verify=True, codes=(1,))
        until(
            lambda: any(row.get("objects_exit") == 1 for row in monitor_results),
            seconds=8,
        )
        lab.ns(lab.CLIENT, "ip", "route", "replace", "10.77.0.2/32", "dev", "wg0")
        fault_end = time.time()
        phase = "healthy"
        objects("network-repaired", "monitor-ack.jsonl", verify=True)
        direct = run(
            "direct-path-blocked",
            [
                "ip",
                "netns",
                "exec",
                lab.CLIENT,
                "curl",
                "--max-time",
                "1",
                "--silent",
                "--show-error",
                "http://172.30.77.2:9000/health",
            ],
            codes=(28,),
        )
        save(
            "network-window.json",
            {
                "started": fault_start,
                "finished": fault_end,
                "direct_exit": direct.returncode,
                "repair": "replace the single blackhole route with wg0",
            },
        )
        stage("private-route-repaired-with-direct-path-still-blocked")
        start("growth", [PROJECT / "bin/grow", "--watch"], None)
        before = lab.run(
            "lvs",
            "--devices",
            ",".join(json.loads((lab.ROOT / "storage.json").read_text())["devices"]),
            "--noheadings",
            "--units",
            "b",
            "--nosuffix",
            "-o",
            "lv_size",
            "/dev/edgelab_r2/data",
        )
        objects(
            "fill-through-proxy", "growth-ack.jsonl", count=110, size=8 << 20, seed=123
        )
        until(
            lambda: (
                float(
                    lab.run(
                        "lvs",
                        "--devices",
                        ",".join(
                            json.loads((lab.ROOT / "storage.json").read_text())[
                                "devices"
                            ]
                        ),
                        "--noheadings",
                        "--units",
                        "b",
                        "--nosuffix",
                        "-o",
                        "lv_size",
                        "/dev/edgelab_r2/data",
                    )
                )
                > float(before)
            )
        )
        objects("grown-object-hashes", "growth-ack.jsonl", verify=True)
        objects("original-object-hashes", "monitor-ack.jsonl", verify=True)
        stage(
            "automatic-storage-growth-preserves-acknowledged-objects",
            before_bytes=float(before),
            acknowledged_growth_objects=110,
        )
        before_resources = inventory()
        run(
            "worker-interrupted",
            [
                *image_arguments,
                "--operation",
                "capstone-interrupted",
                "--crash-after",
                "snapshot",
            ],
            codes=(77,),
        )
        interrupted_resources = inventory()
        old_uuids = {row["lv_uuid"] for row in before_resources}
        created = [
            row for row in interrupted_resources if row["lv_uuid"] not in old_uuids
        ]
        assert len(created) == 1 and created[0]["origin"]
        resumed = json.loads(
            run(
                "worker-resumed",
                [*image_arguments, "--operation", "capstone-interrupted"],
            ).stdout
        )
        assert resumed["snapshot_uuid"] == created[0]["lv_uuid"] and {
            r["lv_uuid"] for r in inventory()
        } == {r["lv_uuid"] for r in interrupted_resources}
        save(
            "worker-resource-proof.json",
            {
                "before": before_resources,
                "interrupted": interrupted_resources,
                "resumed": resumed,
            },
        )
        stage("snapshot-side-effect-reconciled-without-duplication")
        units.command("kill", "--signal=SIGSTOP", router_units[1])
        units.command("start", worker_units[0])
        until(lambda: request(18301, "/status"))
        request(
            18301,
            "/instances",
            {
                "operation": "delete-echo-1",
                "id": "echo-1",
                "app": "echo",
                "endpoint": "127.0.0.1:19101",
                "deleted": True,
            },
        )
        until(
            lambda: request(18201, "/view")["owners"]["worker-1"]["snapshot"][
                "records"
            ]["echo-1"]["deleted"]
        )
        until(
            lambda: request(18402, "/status")["owners"]["worker-1"]["stale"], seconds=8
        )
        objects("control-partition-data-reachable", "monitor-ack.jsonl", verify=True)
        units.command("kill", "--signal=SIGCONT", router_units[1])
        until(
            lambda: request(18202, "/view")["owners"]["worker-1"]["snapshot"][
                "records"
            ]["echo-1"]["deleted"]
        )
        stage("routing-partition-healed-without-deletion-resurrection")
        canary_db = out / "canary.db"
        with (
            sqlite3.connect(out / "worker-1.db") as old,
            sqlite3.connect(canary_db) as new,
        ):
            old.backup(new)
        canary = run(
            "canary-refused",
            [
                PROJECT / "bin/worker",
                "serve",
                "--config",
                out / "worker-1.json",
                "--state",
                canary_db,
                "--listen",
                "127.0.0.1:18999",
                "--status-version",
                "2",
            ],
            codes=(1,),
        )
        assert "no such column: deployment_epoch" in canary.stdout
        units.command("kill", "--signal=SIGSTOP", proxies[1])
        time.sleep(3)
        run(
            "watchdog",
            [
                "ip",
                "netns",
                "exec",
                lab.CLIENT,
                "python3",
                PROJECT / "lab/watchdog.py",
                "--unit",
                proxies[1],
                "--status",
                "http://127.0.0.1:18402/status",
                "--out",
                out / "watchdog",
            ],
            timeout=20,
        )
        traffic("after-canary-and-stall", "echo-2")
        stage("canary-rejected-and-stalled-consumer-recovered")
        fork = json.loads(
            run(
                "immutable-source-fork",
                [*image_arguments, "--operation", "capstone-fork"],
            ).stdout
        )
        fork_mount = out / "fork"
        fork_mount.mkdir()
        lab.run("mount", "-o", "nosuid,nodev,noexec", fork["snapshot"], fork_mount)
        mounts.append(fork_mount)
        run(
            "fork-initial-hashes",
            [
                PROJECT / "bin/image-check",
                "--root",
                fork_mount,
                "--expected",
                out / "image-expected.json",
            ],
        )
        with (fork_mount / "fork-only").open("wb") as file:
            file.write(b"fork-private")
            file.flush()
            os.fsync(file.fileno())
        directory = os.open(fork_mount, os.O_DIRECTORY)
        os.fsync(directory)
        os.close(directory)
        assert not (image_mount / "fork-only").exists()
        fork_expected = {
            "files": {
                **expected,
                "fork-only": hashlib.sha256(b"fork-private").hexdigest(),
            },
            "absent": [],
        }
        save("fork-expected.json", fork_expected)
        run(
            "fork-isolated-hashes",
            [
                PROJECT / "bin/image-check",
                "--root",
                fork_mount,
                "--expected",
                out / "fork-expected.json",
            ],
        )
        run(
            "source-unmodified-hashes",
            [
                PROJECT / "bin/image-check",
                "--root",
                image_mount,
                "--expected",
                out / "image-expected.json",
            ],
        )
        stage(
            "immutable-image-fork-isolated",
            source_origin=ready["origin"],
            fork_snapshot=fork["snapshot_uuid"],
        )
        done.set()
        monitor.join(timeout=15)
        assert not monitor.is_alive()
        assert monitor_results and all(
            "error" not in result and result["echo_exit"] == 0
            for result in monitor_results
        )
        for result in monitor_results:
            if result["objects_exit"] != 0:
                assert (
                    result["started"] <= fault_end and result["finished"] >= fault_start
                )
        save("monitor-results.json", monitor_results)
        save("final-routing.json", request(18201, "/view"))
        save("final-status.json", request(18401, "/status"))
        save(
            "source.json",
            {
                "commit": source,
                "tree": json.loads((PROJECT / ".source-tree.json").read_text()),
                "binary_sha256": {
                    name: hashlib.sha256((PROJECT / name).read_bytes()).hexdigest()
                    for name in [
                        "bin/echo",
                        "bin/objects",
                        "bin/worker",
                        "bin/traffic",
                        "target/release/edgelab-proxy",
                        "target/release/edgelab-routing",
                    ]
                },
            },
        )
        assert (PROJECT / ".source-revision").read_text().strip() == source
        stage(
            "integrated-demonstration-passed",
            monitor_cycles=len(monitor_results),
            faults="scripted replay; not withheld diagnosis",
            durability="process interruption only",
        )
    finally:
        done.set()
        if held and held.poll() is None:
            held.kill()
            held.wait()
        if monitor:
            monitor.join(timeout=20)
        for name in unit_names:
            subprocess.run(
                ["systemctl", "kill", "--signal=SIGCONT", name],
                capture_output=True,
                timeout=3,
            )
        for name in reversed(unit_names):
            with (out / (name + ".journal")).open("w") as file:
                subprocess.run(
                    ["journalctl", "-u", name, "-o", "json", "--no-pager"],
                    stdout=file,
                    timeout=5,
                )
            units.remove(name)
        for mount in reversed(mounts):
            lab.run("umount", mount)
        if server:
            server.shutdown()
            server.server_close()
        if storage.ROOT.exists():
            storage.down()
        if lab.ROOT.exists():
            lab.guard()
            lab.down()
        save(
            "cleanup.json",
            {
                "owned_units_removed": all(
                    not units.path(name).exists() for name in unit_names
                ),
                "storage_fixture_removed": not storage.ROOT.exists(),
                "network_fixture_removed": not lab.ROOT.exists(),
            },
        )


if __name__ == "__main__":
    main()
