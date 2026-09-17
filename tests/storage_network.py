#!/usr/bin/env python3
import argparse
import hashlib
import json
import subprocess
import sys
import time
from pathlib import Path

PROJECT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT / "lab"))
import linux as lab


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--out", default=".run/release-2")
    a = p.parse_args()
    out = (PROJECT / a.out).resolve()
    if out.exists() and any(out.iterdir()):
        raise RuntimeError("evidence directory must be empty")
    out.mkdir(parents=True, exist_ok=True)
    results = []

    def save(name, value):
        (out / name).write_text(json.dumps(value, indent=2) + "\n")

    def command(args, name, codes=(0,)):
        started = time.time()
        r = subprocess.run(
            list(map(str, args)),
            check=False,
            capture_output=True,
            text=True,
            timeout=180,
        )
        (out / (name + ".stdout")).write_text(r.stdout)
        (out / (name + ".stderr")).write_text(r.stderr)
        event = {
            "command": list(map(str, args)),
            "exit": r.returncode,
            "elapsed_seconds": time.time() - started,
        }
        save(name + ".json", event)
        assert r.returncode in codes, (
            f"{name}: {r.returncode}: {r.stderr[-1000:]} {r.stdout[-1000:]}"
        )
        return r

    def check(name, **kw):
        args = [
            "ip",
            "netns",
            "exec",
            lab.CLIENT,
            PROJECT / "bin/objects-check",
            "--manifest",
            out / "ack.jsonl",
        ]
        for key, value in kw.items():
            args.append("--" + key.replace("_", "-"))
            args.extend([] if value is True else [str(value)])
        return command(args, name)

    def request(url, timeout=1):
        r = subprocess.run(
            [
                "ip",
                "netns",
                "exec",
                lab.CLIENT,
                "curl",
                "-fsS",
                "--max-time",
                str(timeout),
                url,
            ],
            check=False,
            capture_output=True,
            text=True,
        )
        return {
            "url": url,
            "exit": r.returncode,
            "stdout": r.stdout,
            "stderr": r.stderr,
        }

    def paths():
        data = {
            key: request(url)
            for key, url in {
                "tunnel": "http://10.77.0.2:8101/health",
                "base": "http://172.30.77.2:8101/health",
                "direct": "http://172.30.77.2:9000/health",
                "management": "http://172.30.77.2:9001/health",
            }.items()
        }
        assert data["tunnel"]["exit"] == data["management"]["exit"] == 0, data
        assert data["base"]["exit"] != 0 and data["direct"]["exit"] != 0, data
        return data

    def gate(id, details):
        results.append({"id": id, "status": "passed", "details": details})
        save("results.json", results)
        print(id, "passed", flush=True)

    def grow(name, *args, codes=(0,)):
        return command([PROJECT / "bin/grow", *args], name, codes)

    lab.guard()
    lab.baseline()
    save("preflight.json", lab.preflight())
    before = lab.status()
    lab.storage()
    lab.network()
    lab.workloads()
    after = lab.status()
    assert before["processes"] == after["processes"]
    save("initial.json", after)
    # The VM owns the disks; the verifier owns acknowledged content and observations.
    check("seed", count=8, bytes=65536, seed=10)
    capture = open(out / "wireguard.pcap", "wb")
    tcp = subprocess.Popen(
        [
            "ip",
            "netns",
            "exec",
            lab.SERVER,
            "tcpdump",
            "-U",
            "-i",
            "el-s",
            "-w",
            "-",
            "udp",
            "port",
            "51820",
        ],
        stdout=capture,
        stderr=open(out / "capture.log", "w"),
    )
    time.sleep(0.2)
    try:
        gate("N01", paths())
        faults = []
        for index, name in enumerate(
            ["interface", "address", "endpoint", "prefix", "route", "firewall", "mtu"]
        ):
            lab.baseline()
            lab.fault(name)
            save(name + "-broken.json", lab.status())
            args = [
                "ip",
                "netns",
                "exec",
                lab.CLIENT,
                PROJECT / "bin/objects-check",
                "--manifest",
                out / "ack.jsonl",
                "--count",
                "1",
                "--bytes",
                "65536",
                "--seed",
                str(100 + index),
            ]
            broken = command(args, name + "-failure", codes=(1,))
            if name == "mtu":
                lab.ns(lab.CLIENT, "ip", "link", "set", "wg0", "mtu", "1200")
                lab.ns(lab.SERVER, "ip", "link", "set", "wg0", "mtu", "1200")
            elif name == "interface":
                lab.ns(lab.CLIENT, "ip", "link", "set", "wg0", "up")
                lab.ns(
                    lab.CLIENT, "ip", "route", "replace", "10.77.0.2/32", "dev", "wg0"
                )
            elif name == "address":
                lab.ns(lab.CLIENT, "ip", "addr", "del", "10.77.0.9/32", "dev", "wg0")
                lab.ns(lab.CLIENT, "ip", "addr", "add", "10.77.0.1/32", "dev", "wg0")
                lab.ns(
                    lab.CLIENT, "ip", "route", "replace", "10.77.0.2/32", "dev", "wg0"
                )
            elif name == "endpoint":
                lab.ns(
                    lab.CLIENT,
                    "wg",
                    "set",
                    "wg0",
                    "peer",
                    lab.public(lab.SERVER),
                    "endpoint",
                    "172.30.77.2:51820",
                )
            elif name == "prefix":
                lab.ns(
                    lab.CLIENT,
                    "wg",
                    "set",
                    "wg0",
                    "peer",
                    lab.public(lab.SERVER),
                    "allowed-ips",
                    "10.77.0.2/32",
                )
            elif name == "route":
                lab.ns(
                    lab.CLIENT, "ip", "route", "replace", "10.77.0.2/32", "dev", "wg0"
                )
            elif name == "firewall":
                rules = json.loads(
                    lab.ns(
                        lab.SERVER,
                        "nft",
                        "-j",
                        "-a",
                        "list",
                        "chain",
                        "inet",
                        "edgelab",
                        "input",
                    )
                )["nftables"]
                handle = [x["rule"]["handle"] for x in rules if "rule" in x][-1]
                lab.ns(
                    lab.SERVER,
                    "nft",
                    "delete",
                    "rule",
                    "inet",
                    "edgelab",
                    "input",
                    "handle",
                    handle,
                )
            command(args, name + "-repaired")
            post = paths()
            save(name + "-repaired-state.json", lab.status())
            faults.append(
                {"fault": name, "failure_exit": broken.returncode, "paths": post}
            )
        gate(
            "N02", {"kind": "authored replay, not withheld diagnosis", "faults": faults}
        )
        lab.baseline()
        old = lab.status()
        lab.fault("address")
        lab.fault("endpoint")
        save("composite-broken.json", lab.status())
        assert request("http://10.77.0.2:8101/health")["exit"] != 0
        lab.ns(
            lab.CLIENT,
            "wg",
            "set",
            "wg0",
            "peer",
            lab.public(lab.SERVER),
            "endpoint",
            "172.30.77.2:51820",
        )
        assert request("http://10.77.0.2:8101/health")["exit"] != 0
        lab.ns(lab.CLIENT, "ip", "addr", "del", "10.77.0.9/32", "dev", "wg0")
        lab.ns(lab.CLIENT, "ip", "addr", "add", "10.77.0.1/32", "dev", "wg0")
        lab.ns(lab.CLIENT, "ip", "route", "replace", "10.77.0.2/32", "dev", "wg0")
        new = lab.status()
        assert (
            old["processes"] == new["processes"]
            and old["namespaces"] == new["namespaces"]
        )
        gate(
            "N03",
            {
                "kind": "authored in-place replay",
                "paths": paths(),
                "processes": new["processes"],
            },
        )
    finally:
        tcp.send_signal(2)
        tcp.wait(timeout=5)
        capture.close()
    f = json.loads((lab.ROOT / "storage.json").read_text())
    allowed = ",".join(f["devices"])
    lab.stop("objects")
    lab.run("umount", f["mount"])
    lab.run("lvchange", "--devices", allowed, "-an", f["lv"])
    missing = command(
        [PROJECT / "bin/objects", "--root", f["mount"], "--owner", "objects-1"],
        "unmounted-owner",
        codes=(1,),
    )
    lab.storage()
    lab.workloads()
    check("recovered", verify=True)
    gate(
        "S01",
        {
            "lv_uuid": f["uuid"],
            "filesystem_uuid": f["filesystem_uuid"],
            "unmounted_start_exit": missing.returncode,
        },
    )
    grow("initial-growth", "--operation", "reach-4g", "--target-mib", "4096")
    check("after-4g", verify=True)
    gate(
        "S02",
        {
            "growth": json.loads((out / "initial-growth.stdout").read_text()),
            "physical_bytes": lab.status()["physical_bytes"],
        },
    )
    check("fill-threshold", count=830, bytes=4194304, seed=22)
    watchlog = open(out / "watch.jsonl", "w")
    watcherr = open(out / "watch.stderr", "w")
    watcher = subprocess.Popen(
        [str(PROJECT / "bin/grow"), "--watch"], stdout=watchlog, stderr=watcherr
    )
    try:
        check("writes-during-growth", count=20, bytes=4194304, seed=23)
        time.sleep(3)
    finally:
        watcher.terminate()
        watcher.wait(timeout=5)
        watchlog.close()
        watcherr.close()
    records = [json.loads(x) for x in (out / "watch.jsonl").read_text().splitlines()]
    growth = [x for x in records if "target_bytes" in x]
    assert len(growth) == 1 and growth[0]["target_bytes"] == 4596 * 1024**2, records
    check("after-auto-growth", verify=True)
    gate("S03", {"growth": growth, "writes_during_growth": 20})
    # Force a threshold crossing; both contenders use the same retained filesystem usage.
    check("fill-concurrent", count=90, bytes=4194304, seed=24)
    procs = []
    for i in range(2):
        procs.append(
            subprocess.Popen(
                [str(PROJECT / "bin/grow")],
                stdout=open(out / f"concurrent-{i}.json", "w"),
                stderr=subprocess.PIPE,
            )
        )
    exits = [p.wait(timeout=30) for p in procs]
    assert all(x in [0, 1] for x in exits)
    concurrent = [
        json.loads((out / f"concurrent-{i}.json").read_text()) for i in range(2)
    ]
    assert len([x for x in concurrent if "target_bytes" in x]) == 1, concurrent
    assert all(
        x.get("ok") or x.get("error") == "controller busy" for x in concurrent
    ), concurrent
    grow(
        "crash-lv",
        "--operation",
        "crash-absolute",
        "--target-mib",
        "5596",
        "--crash-after",
        "lv",
        codes=(77,),
    )
    lv_after = lab.run(
        "lvs",
        "--devices",
        allowed,
        "--noheadings",
        "--units",
        "b",
        "--nosuffix",
        "-o",
        "lv_size",
        f["lv"],
    )
    grow("resume-lv", "--operation", "crash-absolute", "--target-mib", "5596")
    lv_resumed = lab.run(
        "lvs",
        "--devices",
        allowed,
        "--noheadings",
        "--units",
        "b",
        "--nosuffix",
        "-o",
        "lv_size",
        f["lv"],
    )
    assert lv_after == lv_resumed
    check("after-crash", verify=True)
    gate(
        "S04",
        {
            "concurrent": concurrent,
            "crash_kind": "process exit after LV side effect",
            "lv_after_crash": lv_after,
            "lv_after_reconciliation": lv_resumed,
        },
    )
    lab.run(
        "lvcreate",
        "--devices",
        allowed,
        "-l",
        "100%FREE",
        "-n",
        "ballast",
        "edgelab_r2",
    )
    try:
        r = grow(
            "exhausted", "--operation", "no-space", "--target-mib", "6096", codes=(1,)
        )
        assert "insufficient backing space" in r.stdout
    finally:
        lab.run("lvremove", "--devices", allowed, "-f", "/dev/edgelab_r2/ballast")
    gate(
        "S05",
        {
            "failure": json.loads(r.stdout),
            "host_free_bytes": lab.shutil.disk_usage("/").free,
        },
    )
    lab.start(
        "empty",
        [
            "python3",
            "-m",
            "http.server",
            "9002",
            "--bind",
            "0.0.0.0",
            "--directory",
            "/tmp",
        ],
    )
    lab.stop("objects")
    failure = request("http://10.77.0.2:8101/health")
    assert failure["exit"] != 0
    empty = request("http://10.77.0.2:9002/")
    assert empty["exit"] == 0
    lab.workloads()
    check("owner-returned", verify=True)
    lab.stop("empty")
    gate(
        "S06",
        {"owner_unavailable": failure, "other_endpoint_reachable": empty["exit"] == 0},
    )
    save("final-state.json", lab.status())
    manifest = {
        str(p.relative_to(out)): hashlib.sha256(p.read_bytes()).hexdigest()
        for p in out.iterdir()
        if p.is_file()
    }
    save(
        "inventory.json",
        {
            "source_commit": (PROJECT / ".source-revision").read_text().strip(),
            "files": manifest,
        },
    )


if __name__ == "__main__":
    main()
