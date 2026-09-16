#!/usr/bin/env python3
"""Linux integration driver. The Go client verifies traffic; /proc measures resources."""

import argparse
import concurrent.futures
import hashlib
import json
import os
import platform
import shutil
import signal
import socket
import subprocess
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def save(path, value):
    path.write_text(json.dumps(value, indent=2) + "\n")


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class FullBacklog:
    def __enter__(self):
        self.listener = socket.socket()
        self.listener.bind(("127.0.0.1", 0))
        self.listener.listen(1)
        self.address = self.listener.getsockname()
        self.connections = []
        for _ in range(5):
            try:
                self.connections.append(socket.create_connection(self.address, 0.1))
            except TimeoutError:
                break
        else:
            raise AssertionError("could not fill owned listener backlog")
        return f"127.0.0.1:{self.address[1]}"

    def __exit__(self, *args):
        for s in self.connections:
            s.close()
        self.listener.close()


def resource(pid):
    p = Path("/proc") / str(pid)
    fields = (p / "stat").read_text().split()
    status = (p / "status").read_text().splitlines()
    rss = next(int(x.split()[1]) * 1024 for x in status if x.startswith("VmRSS:"))
    return {
        "time": time.time(),
        "pid": pid,
        "rss_bytes": rss,
        "fds": len(list((p / "fd").iterdir())),
        "threads": len(list((p / "task").iterdir())),
        "cpu_seconds": (int(fields[13]) + int(fields[14])) / os.sysconf("SC_CLK_TCK"),
    }


class Lab:
    def __init__(self, out):
        self.out = out
        self.children = []
        self.files = []
        self.serial = 0
        ports = set()
        while len(ports) < 8:
            ports.add(free_port())
        self.a, self.a_alt, self.b, self.a1, self.a2, self.b1, self.dead, self.extra = (
            sorted(ports)
        )
        self.config = out / "config.json"
        self.proxy = None
        self.sampling = True
        self.samples = []
        self.lock = threading.Lock()
        self.sampler = threading.Thread(target=self.sample, daemon=True)
        self.sampler.start()
        self.echoes = {}
        for name, app, port in [
            ("a1", "a", self.a1),
            ("a2", "a", self.a2),
            ("b1", "b", self.b1),
        ]:
            self.echoes[name] = self.spawn(
                [
                    str(ROOT / "bin/echo"),
                    "--listen",
                    f"127.0.0.1:{port}",
                    "--app",
                    app,
                    "--instance",
                    name,
                ],
                name,
            )
            self.wait_port(port)
        self.write_config()
        self.start_proxy()

    def spawn(self, cmd, name):
        log = open(self.out / f"{name}.log", "a")
        self.files.append(log)
        proc = subprocess.Popen(cmd, stdout=log, stderr=log, cwd=ROOT)
        self.children.append(proc)
        return proc

    def sample(self):
        with open(self.out / "resources.jsonl", "w") as out:
            while self.sampling:
                for proc in list(self.children):
                    try:
                        row = resource(proc.pid)
                    except (OSError, StopIteration):
                        continue
                    out.write(json.dumps(row) + "\n")
                    with self.lock:
                        self.samples.append(row)
                out.flush()
                time.sleep(0.1)

    def wait_port(self, port):
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            try:
                with socket.create_connection(("127.0.0.1", port), 0.1):
                    return
            except OSError:
                time.sleep(0.02)
        raise AssertionError(f"listener {port} did not start")

    def events(self):
        events = []
        for line in (self.out / "proxy.log").read_text().splitlines():
            try:
                events.append(json.loads(line)["fields"])
            except (ValueError, KeyError):
                pass
        return events

    def await_event(self, event, previous=0):
        until = time.monotonic() + 5
        while time.monotonic() < until:
            if sum(e.get("event") == event for e in self.events()) > previous:
                return
            if self.proxy.poll() is not None:
                raise AssertionError("proxy exited")
            time.sleep(0.02)
        raise AssertionError(f"missing event {event}")

    def write_config(self, targets=None, apps=True, extra=False):
        data = {
            "Apps": [
                {
                    "Name": "a",
                    "Ports": [self.a, self.a_alt] + ([self.extra] if extra else []),
                    "Targets": targets
                    or [f"127.0.0.1:{self.a1}", f"127.0.0.1:{self.a2}"],
                }
            ]
        }
        if apps:
            data["Apps"].append(
                {"Name": "b", "Ports": [self.b], "Targets": [f"127.0.0.1:{self.b1}"]}
            )
        tmp = self.config.with_suffix(".tmp")
        save(tmp, data)
        tmp.replace(self.config)

    def reload(self, rejected=False):
        event = "reload_rejected" if rejected else "reloaded"
        previous = sum(e.get("event") == event for e in self.events())
        self.proxy.send_signal(signal.SIGHUP)
        self.await_event(event, previous)

    def start_proxy(self):
        previous = (
            sum(e.get("event") == "ready" for e in self.events())
            if (self.out / "proxy.log").exists()
            else 0
        )
        self.proxy = self.spawn(
            [
                str(ROOT / "target/release/edgelab-proxy"),
                "--config",
                str(self.config),
                "--limit",
                "8",
                "--drain-ms",
                "500",
            ],
            "proxy",
        )
        self.await_event("ready", previous)
        time.sleep(0.1)

    def traffic(
        self,
        port=None,
        app="a",
        instances="a1,a2",
        count=40,
        concurrency=4,
        size=1024,
        duration=0,
        expect=True,
        label="traffic",
    ):
        self.serial += 1
        stem = f"{self.serial:03d}-{label}"
        cmd = [
            str(ROOT / "bin/traffic"),
            "--address",
            f"127.0.0.1:{port or self.a}",
            "--app",
            app,
            "--instances",
            instances,
            "--count",
            str(count),
            "--concurrency",
            str(concurrency),
            "--bytes",
            str(size),
            "--history",
            str(self.out / f"{stem}.jsonl"),
        ]
        if duration:
            cmd += ["--duration", f"{duration}s"]
        proc = subprocess.Popen(
            cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True
        )
        self.children.append(proc)
        stdout, stderr = proc.communicate(timeout=max(20, duration + 10))
        assert stdout, stderr
        value = json.loads(stdout)
        value["client_pid"] = proc.pid
        save(
            self.out / f"{stem}.json",
            {"command": cmd, "exit": proc.returncode, **value},
        )
        if expect:
            assert proc.returncode == 0, value
        elif expect is False:
            assert proc.returncode != 0 and value["failures"] > 0, value
        return value

    def hold(self, port=None):
        s = socket.create_connection(("127.0.0.1", port or self.a), 2)
        s.settimeout(2)
        line = b""
        while not line.endswith(b"\n"):
            part = s.recv(1)
            if not part:
                raise AssertionError("connection rejected")
            line += part
        return s, json.loads(line)

    def close(self):
        for proc in reversed(self.children):
            if proc.poll() is None:
                proc.terminate()
        for proc in reversed(self.children):
            try:
                proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait()
        self.sampling = False
        self.sampler.join()
        for f in self.files:
            f.close()


def verify(lab, benchmark_seconds):
    results = []

    def gate(id, fn):
        started = time.time()
        try:
            details = fn() or {}
            row = {"id": id, "status": "passed", "details": details}
        except Exception as err:
            row = {"id": id, "status": "failed", "error": repr(err)}
        row.update(started=started, finished=time.time())
        results.append(row)
        save(lab.out / "results.json", results)
        print(json.dumps(row), flush=True)

    def p01():
        outputs = []
        for port in [lab.a, lab.a_alt]:
            r = lab.traffic(port=port)
            assert set(r["backends"]) == {"a1", "a2"}
            outputs.append(r)
        outputs.append(lab.traffic(port=lab.b, app="b", instances="b1"))
        lab.traffic(
            app="wrong",
            expect=False,
            count=1,
            concurrency=1,
            label="negative-identity-oracle",
        )
        with socket.socket() as fake:
            fake.bind(("127.0.0.1", 0))
            fake.listen()

            def corrupt():
                conn, _ = fake.accept()
                with conn:
                    conn.settimeout(3)
                    conn.sendall(
                        b'{"app":"a","instance":"a1","worker":"w","region":"r","version":"v"}\n'
                    )
                    while conn.recv(4096):
                        pass
                    conn.sendall(b"corrupted\nEOF\n")

            thread = threading.Thread(target=corrupt)
            thread.start()
            lab.traffic(
                port=fake.getsockname()[1],
                expect=False,
                count=1,
                concurrency=1,
                label="negative-payload-oracle",
            )
            thread.join(timeout=4)
            assert not thread.is_alive()
        return {"listeners": 3, "observations": outputs}

    gate("P01", p01)

    def p02():
        lab.write_config(
            ["does-not-exist.invalid:9", f"127.0.0.1:{lab.dead}", f"localhost:{lab.a1}"]
        )
        lab.reload()
        r = lab.traffic(instances="a1")
        assert r["max_ms"] < 2200
        assert any(
            e.get("event") == "connect_failed" and ".invalid" in e.get("target", "")
            for e in lab.events()
        )
        with FullBacklog() as blackhole:
            lab.write_config([blackhole, f"127.0.0.1:{lab.a1}"])
            lab.reload()
            slow = lab.traffic(
                instances="a1", count=4, concurrency=1, label="connect-timeout-fallback"
            )
            assert slow["max_ms"] < 2200
            assert any(
                e.get("event") == "connect_timeout" and e.get("target") == blackhole
                for e in lab.events()
            )
        lab.write_config()
        lab.reload()
        return {"dns_and_refusal": r, "timeout_fallback": slow}

    gate("P02", p02)

    def p03():
        lab.write_config([f"127.0.0.1:{lab.dead}", "does-not-exist.invalid:9"])
        lab.reload()
        before = resource(lab.proxy.pid)
        r = lab.traffic(expect=False, count=100)
        assert r["successes"] == 0 and r["max_ms"] < 2200
        time.sleep(0.2)
        after = resource(lab.proxy.pid)
        assert after["fds"] <= before["fds"] + 2
        with FullBacklog() as blackhole:
            lab.write_config([blackhole] * 8)
            lab.reload()
            timed = lab.traffic(
                expect=False, count=4, concurrency=2, label="total-connect-deadline"
            )
            assert timed["successes"] == 0 and 1900 < timed["max_ms"] < 2300
        lab.write_config()
        lab.reload()
        lab.traffic(count=10)
        return {
            "traffic": r,
            "deadline_traffic": timed,
            "before": before,
            "after": after,
        }

    gate("P03", p03)

    def p04():
        return lab.traffic(count=16, concurrency=4, size=262144, label="half-close")

    gate("P04", p04)

    def p05():
        held = [lab.hold(lab.a if i % 2 else lab.a_alt)[0] for i in range(8)]
        try:
            r = lab.traffic(expect=False, count=16)
            assert r["successes"] == 0
            b = lab.traffic(port=lab.b, app="b", instances="b1")
            assert b["max_ms"] < 250
            lab.reload()
            assert lab.traffic(expect=False, count=8)["successes"] == 0
            save(
                lab.config,
                {
                    "Apps": [
                        {
                            "Name": "b",
                            "Ports": [lab.b],
                            "Targets": [f"127.0.0.1:{lab.b1}"],
                        }
                    ]
                },
            )
            lab.reload()
            lab.write_config()
            lab.reload()
            assert lab.traffic(expect=False, count=8)["successes"] == 0
            return {"b_budget_ms": 250, "b": b, "rejected": r, "shared_limit": 8}
        finally:
            for s in held:
                s.close()
            time.sleep(0.2)

    gate("P05", p05)

    def p06():
        before = resource(lab.proxy.pid)
        held = [lab.hold()[0] for _ in range(8)]

        def flood(s):
            s.settimeout(1)
            try:
                s.sendall(b"x" * (8 * 1024 * 1024))
            except OSError:
                pass

        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
            futures = [pool.submit(flood, s) for s in held]
            try:
                for _ in range(5):
                    b = lab.traffic(port=lab.b, app="b", instances="b1", count=20)
                    assert b["max_ms"] < 250
                    time.sleep(0.3)
                after = resource(lab.proxy.pid)
                assert after["rss_bytes"] <= before["rss_bytes"] + 32 * 1024 * 1024
                assert after["fds"] <= before["fds"] + 20
                assert lab.traffic(expect=False, count=8)["successes"] == 0
                return {
                    "before": before,
                    "after": after,
                    "rss_growth_budget_bytes": 32 * 1024 * 1024,
                    "slow_connections": 8,
                    "attempted_bytes_each": 8 * 1024 * 1024,
                }
            finally:
                for s in held:
                    s.close()
                for f in futures:
                    f.result()
                time.sleep(0.3)

    gate("P06", p06)

    def p07():
        lab.traffic(count=100, concurrency=4)
        time.sleep(0.3)
        before = resource(lab.proxy.pid)
        rounds = []
        for _ in range(5):
            lab.traffic(count=200, concurrency=4)
            time.sleep(0.2)
            rounds.append(resource(lab.proxy.pid))
        assert all(r["fds"] <= before["fds"] + 2 for r in rounds)
        assert all(
            r["rss_bytes"] <= before["rss_bytes"] + 16 * 1024 * 1024 for r in rounds
        )
        time.sleep(1.1)
        active = [e for e in lab.events() if e.get("event") == "admission"][-2:]
        assert len(active) == 2 and all(e["active"] == 0 for e in active)
        tasks = [e for e in lab.events() if e.get("event") == "tasks"][-1]
        assert tasks["sessions"] == 0
        return {
            "baseline": before,
            "rounds": rounds,
            "final_tasks": tasks,
            "final_admission": active,
        }

    gate("P07", p07)

    def p08():
        lab.write_config([f"127.0.0.1:{lab.a1}"])
        lab.reload()
        old, identity = lab.hold()
        assert identity["instance"] == "a1"
        try:
            lab.config.write_text("{broken")
            lab.reload(rejected=True)
            lab.traffic(instances="a1", count=10)
            with socket.socket() as occupied:
                occupied.bind(("127.0.0.1", lab.extra))
                occupied.listen()
                lab.write_config(extra=True)
                lab.reload(rejected=True)
                lab.traffic(instances="a1", count=10)
            lab.write_config([f"127.0.0.1:{lab.a2}"], extra=True)
            lab.reload()
            lab.traffic(port=lab.extra, instances="a2", count=10)
            old.sendall(b"old-session")
            assert old.recv(11) == b"old-session"
            lab.traffic(instances="a2", count=10)
        finally:
            old.close()
        lab.write_config()
        lab.reload()
        return {
            "invalid_json_retained": True,
            "bind_failure_retained": True,
            "new_listener_verified": True,
            "old_session_preserved": True,
        }

    gate("P08", p08)

    def p09():
        lab.write_config()
        lab.reload()
        old, identity = lab.hold()
        if identity["instance"] != "a1":
            old.close()
            old, identity = lab.hold()
        assert identity["instance"] == "a1"
        old.sendall(b"prefix")
        assert old.recv(6) == b"prefix"
        lab.echoes["a1"].kill()
        lab.echoes["a1"].wait()
        try:
            rest = old.recv(1024)
            assert rest == b"", rest
        except ConnectionResetError:
            pass
        finally:
            old.close()
        lab.write_config()
        lab.reload()
        lab.traffic(instances="a2", count=10)
        graceful, _ = lab.hold()
        stuck, _ = lab.hold()
        start = time.monotonic()
        lab.proxy.terminate()
        lab.await_event("draining")
        try:
            admitted = socket.create_connection(("127.0.0.1", lab.a), 0.1)
        except OSError:
            pass
        else:
            admitted.close()
            raise AssertionError("listener admitted connection during drain")
        graceful.sendall(b"finish")
        graceful.shutdown(socket.SHUT_WR)
        reply = b""
        while True:
            part = graceful.recv(1024)
            if not part:
                break
            reply += part
        graceful.close()
        assert reply == b"finish\nEOF\n"
        lab.proxy.wait(timeout=2)
        elapsed = time.monotonic() - start
        assert 0.45 <= elapsed < 1.5
        assert stuck.recv(1) == b""
        stuck.close()
        assert any(e.get("event") == "drain_expired" for e in lab.events())
        lab.start_proxy()
        return {
            "midstream_replayed": False,
            "graceful_completed": True,
            "forced_drain_seconds": elapsed,
        }

    gate("P09", p09)

    def p10():
        # One surviving backend makes direct and proxied workloads comparable.
        lab.write_config([f"127.0.0.1:{lab.a2}"])
        lab.reload()
        lab.proxy.terminate()
        lab.proxy.wait(timeout=2)
        previous = sum(e.get("event") == "ready" for e in lab.events())
        lab.proxy = lab.spawn(
            [str(ROOT / "target/release/edgelab-proxy"), "--config", str(lab.config)],
            "proxy",
        )
        lab.await_event("ready", previous)
        observations = []
        for repetition in range(3):
            for route, port in [("direct", lab.a2), ("proxy", lab.a)]:
                before = {
                    str(p.pid): resource(p.pid) for p in [lab.proxy, lab.echoes["a2"]]
                }
                started = time.time()
                r = lab.traffic(
                    port=port,
                    instances="a2",
                    count=4000000,
                    concurrency=100,
                    duration=benchmark_seconds,
                    expect=None,
                    label=f"benchmark-{route}-{repetition}",
                )
                ended = time.time()
                assert r["seconds"] >= benchmark_seconds and r["successes"] > 0
                assert r["attempts"] == r["successes"] + r["failures"]
                after = {
                    str(p.pid): resource(p.pid) for p in [lab.proxy, lab.echoes["a2"]]
                }
                peaks = {}
                with lab.lock:
                    for pid in before:
                        samples = [
                            s
                            for s in lab.samples
                            if str(s["pid"]) == pid and started <= s["time"] <= ended
                        ]
                        assert samples
                        peaks[pid] = {
                            "rss_bytes": max(s["rss_bytes"] for s in samples),
                            "fds": max(s["fds"] for s in samples),
                            "cpu_seconds": after[pid]["cpu_seconds"]
                            - before[pid]["cpu_seconds"],
                        }
                with lab.lock:
                    client_samples = [
                        s for s in lab.samples if s["pid"] == r["client_pid"]
                    ]
                assert client_samples
                peaks[str(r["client_pid"])] = {
                    "role": "traffic client",
                    "rss_bytes": max(s["rss_bytes"] for s in client_samples),
                    "fds": max(s["fds"] for s in client_samples),
                    "sampled_cpu_seconds": max(
                        s["cpu_seconds"] for s in client_samples
                    ),
                }
                observations.append(
                    {
                        "route": route,
                        "repetition": repetition,
                        "traffic": r,
                        "resources": peaks,
                    }
                )
        save(lab.out / "benchmark.json", observations)
        return {
            "seconds_per_run": benchmark_seconds,
            "repetitions": 3,
            "concurrency": 100,
            "payload_bytes": 1024,
            "observations": observations,
        }

    gate("P10", p10)
    return results


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--benchmark-seconds", type=int, default=60)
    args = parser.parse_args()
    assert 1 <= args.benchmark_seconds <= 60
    assert platform.system() == "Linux", "resource verification requires Linux /proc"
    available = (
        int(
            next(
                x.split()[1]
                for x in Path("/proc/meminfo").read_text().splitlines()
                if x.startswith("MemAvailable:")
            )
        )
        * 1024
    )
    assert available >= 1024**3, "verification requires 1 GiB available memory"
    assert shutil.disk_usage(ROOT).free >= 6 * 1024**3, (
        "verification requires 6 GiB free disk for raw evidence"
    )
    args.out.mkdir(parents=True, exist_ok=False)
    sources = {
        str(p.relative_to(ROOT)): hashlib.sha256(p.read_bytes()).hexdigest()
        for p in ROOT.rglob("*")
        if p.is_file()
        and not any(
            x
            in {
                ".git",
                "target",
                "bin",
                "evidence",
                ".run",
                "__pycache__",
                ".source-revision",
            }
            for x in p.relative_to(ROOT).parts
        )
    }
    commit = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=ROOT, capture_output=True, text=True
    )
    save(
        args.out / "environment.json",
        {
            "uname": list(platform.uname()),
            "cpus": os.cpu_count(),
            "meminfo": Path("/proc/meminfo").read_text(),
            "commit": commit.stdout.strip()
            or (
                (ROOT / ".source-revision").read_text().strip()
                if (ROOT / ".source-revision").exists()
                else None
            ),
            "source_sha256": sources,
            "command": os.sys.argv,
            "limits": {
                "test_connections_per_app": 8,
                "benchmark_connections_per_app": 128,
                "buffer_bytes_per_direction": 8192,
                "target_ms": 500,
                "connect_ms": 2000,
                "test_drain_ms": 500,
            },
            "b_probe_max_ms": 250,
            "versions": {
                name: subprocess.check_output(command, text=True).strip()
                for name, command in {
                    "rust": [str(Path.home() / ".cargo/bin/rustc"), "--version"],
                    "go": ["go", "version"],
                }.items()
            },
            "binary_sha256": {
                str(p.relative_to(ROOT)): hashlib.sha256(p.read_bytes()).hexdigest()
                for p in [
                    ROOT / "target/release/edgelab-proxy",
                    ROOT / "bin/echo",
                    ROOT / "bin/traffic",
                ]
            },
        },
    )
    lab = None
    try:
        lab = Lab(args.out)
        results = verify(lab, args.benchmark_seconds)
    finally:
        if lab:
            lab.close()
    save(
        args.out / "inventory.json",
        {
            str(p.relative_to(args.out)): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in args.out.rglob("*")
            if p.is_file()
        },
    )
    raise SystemExit(0 if all(r["status"] == "passed" for r in results) else 1)


if __name__ == "__main__":
    main()
