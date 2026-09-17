#!/usr/bin/env python3
import argparse
import gzip
import hashlib
import io
import json
import os
import sqlite3
import subprocess
import sys
import tarfile
import time
from pathlib import Path

PROJECT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT / "lab"))
import linux as lab
import worker as fixture


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", default=".run/worker")
    a = parser.parse_args()
    out = PROJECT / a.out
    out.mkdir(parents=True, exist_ok=False)
    pool = fixture.guard()
    device = pool["device"]
    results = []
    store = out / "store"
    (store / "blobs/sha256").mkdir(parents=True)

    def save(name, value):
        (out / name).write_text(json.dumps(value, indent=2) + "\n")

    source = json.loads((PROJECT / ".source-tree.json").read_text())
    for name, expected in source["files"].items():
        assert hashlib.sha256((PROJECT / name).read_bytes()).hexdigest() == expected, (
            name
        )
    save(
        "source.json",
        {
            "commit": (PROJECT / ".source-revision").read_text().strip(),
            "tree": source,
            "binaries": {
                name: hashlib.sha256((PROJECT / "bin" / name).read_bytes()).hexdigest()
                for name in ["worker", "image-check"]
            },
        },
    )

    def blob(data, media):
        sha = hashlib.sha256(data).hexdigest()
        (store / "blobs/sha256" / sha).write_bytes(data)
        return {"mediaType": media, "digest": "sha256:" + sha, "size": len(data)}

    def image(variant, attack=None):
        files = {
            "etc/version": ("version-" + variant).encode(),
            "etc/keep": b"kept",
            "opaque/new": b"new",
        }
        layers = []
        diffs = []
        for entries in [
            [
                ("etc/", None),
                ("etc/version", ("old-" + variant).encode()),
                ("etc/keep", b"kept"),
                ("etc/remove", b"remove"),
                ("opaque/", None),
                ("opaque/old", b"old"),
            ],
            [
                ("opaque/new", b"new"),
                ("opaque/.wh..wh..opq", b""),
                ("etc/.wh.remove", b""),
                ("etc/version", files["etc/version"]),
            ],
        ]:
            buf = io.BytesIO()
            with tarfile.open(
                fileobj=buf, mode="w", format=tarfile.USTAR_FORMAT
            ) as tar:
                for name, data in entries:
                    h = tarfile.TarInfo(name)
                    h.uid = h.gid = 0
                    h.mtime = 1700000000
                    h.mode = 0o755 if data is None else 0o644
                    h.type = tarfile.DIRTYPE if data is None else tarfile.REGTYPE
                    h.size = 0 if data is None else len(data)
                    tar.addfile(h, None if data is None else io.BytesIO(data))
            raw = buf.getvalue()
            diffs.append("sha256:" + hashlib.sha256(raw).hexdigest())
            layers.append(
                blob(
                    gzip.compress(raw, mtime=0),
                    "application/vnd.oci.image.layer.v1.tar+gzip",
                )
            )
        if attack in [
            "traversal",
            "absolute",
            "symlink",
            "hardlink",
            "count",
            "expansion",
        ]:
            buf = io.BytesIO()
            with tarfile.open(
                fileobj=buf, mode="w", format=tarfile.USTAR_FORMAT
            ) as tar:
                n = 4100 if attack == "count" else 1
                for i in range(n):
                    h = tarfile.TarInfo(
                        "../sentinel"
                        if attack == "traversal"
                        else "/sentinel"
                        if attack == "absolute"
                        else f"hostile-{i}"
                    )
                    h.mode = 0o644
                    h.mtime = 1700000000
                    if attack in ["symlink", "hardlink"]:
                        h.type = (
                            tarfile.SYMTYPE if attack == "symlink" else tarfile.LNKTYPE
                        )
                        h.linkname = "/tmp/edgelab-worker-sentinel"
                    data = b"x" * (65 << 20) if attack == "expansion" else b"x"
                    h.size = len(data) if h.type == tarfile.REGTYPE else 0
                    tar.addfile(h, io.BytesIO(data) if h.size else None)
            raw = buf.getvalue()
            diffs = ["sha256:" + hashlib.sha256(raw).hexdigest()]
            layers = [
                blob(
                    gzip.compress(raw, mtime=0),
                    "application/vnd.oci.image.layer.v1.tar+gzip",
                )
            ]
        config = blob(
            json.dumps(
                {
                    "architecture": "arm64",
                    "os": "linux",
                    "rootfs": {"type": "layers", "diff_ids": diffs},
                },
                sort_keys=True,
            ).encode(),
            "application/vnd.oci.image.config.v1+json",
        )
        if attack == "unsupported":
            layers[-1]["mediaType"] = "application/unsupported"
        if attack == "missing":
            layers[-1]["digest"] = "sha256:" + "0" * 64
        if attack == "corrupt":
            target = store / "blobs/sha256" / layers[-1]["digest"].split(":")[1]
            data = bytearray(target.read_bytes())
            data[0] ^= 1
            target.write_bytes(data)
        m = {
            "schemaVersion": 2,
            "mediaType": "application/vnd.oci.image.manifest.v1+json",
            "config": config,
            "layers": layers,
        }
        d = blob(json.dumps(m, sort_keys=True).encode(), m["mediaType"])
        save(variant + "-manifest.json", m)
        expected = {
            "files": {
                name: hashlib.sha256(data).hexdigest() for name, data in files.items()
            },
            "absent": [
                "etc/remove",
                "opaque/old",
                "opaque/.wh..wh..opq",
                "etc/.wh.remove",
            ],
        }
        save(variant + "-expected.json", expected)
        return d

    serverlog = (out / "fixture-server.log").open("w")
    server = subprocess.Popen(
        [
            "python3",
            "-m",
            "http.server",
            "9200",
            "--bind",
            "127.0.0.1",
            "--directory",
            str(store),
        ],
        stdout=serverlog,
        stderr=serverlog,
    )
    time.sleep(0.3)
    assert server.poll() is None

    def invoke(op, d, case=None, extra=(), codes=(0,)):
        args = [
            str(PROJECT / "bin/worker"),
            "--operation",
            op,
            "--manifest",
            d["digest"],
            "--manifest-bytes",
            str(d["size"]),
            *extra,
        ]
        started = time.time()
        p = subprocess.run(
            args, check=False, capture_output=True, text=True, timeout=100
        )
        record = {
            "operation": op,
            "arguments": args,
            "exit": p.returncode,
            "elapsed_seconds": time.time() - started,
            "stdout": p.stdout,
            "stderr": p.stderr,
        }
        save((case or op) + ".json", record)
        assert p.returncode in codes, record
        lines = [json.loads(x) for x in p.stdout.splitlines()]
        return lines[-1] if lines else None

    def inventory():
        return json.loads(
            lab.run(
                "lvs",
                "--devices",
                device,
                "--reportformat",
                "json",
                "--units",
                "b",
                "-a",
                "-o",
                "lv_name,lv_uuid,lv_size,lv_attr,origin,pool_lv,data_percent,metadata_percent,lv_tags",
                "edgelab_worker",
            )
        )

    def verify(result, expected, label):
        mount = out / "inspect"
        mount.mkdir(exist_ok=True)
        lab.run("mount", "-o", "ro,nosuid,nodev,noexec", result["origin"], mount)
        try:
            p = subprocess.run(
                [
                    str(PROJECT / "bin/image-check"),
                    "--root",
                    str(mount),
                    "--expected",
                    str(out / (expected + "-expected.json")),
                ],
                check=False,
                capture_output=True,
                text=True,
            )
            save(label, {"exit": p.returncode, "stdout": p.stdout, "stderr": p.stderr})
            assert p.returncode == 0, p.stdout
        finally:
            lab.run("umount", mount)

    def gate(id, details):
        results.append({"id": id, "status": "passed", "details": details})
        save("results.json", results)
        print(id, "passed", flush=True)

    sentinel = Path("/tmp/edgelab-worker-sentinel")
    assert not sentinel.exists()
    sentinel.write_text("untouched\n")
    try:
        valid = image("valid")
        ready = invoke("valid", valid)
        assert ready["ready"]
        verify(ready, "valid", "filesystem-verification.json")
        gate("W01", ready)
        invalid = []
        for attack in ["corrupt", "missing", "unsupported"]:
            result = invoke(attack, image(attack, attack), codes=(1,))
            assert not result["ready"]
            assert {
                "corrupt": "blob digest mismatch",
                "missing": "fetch HTTP 404",
                "unsupported": "unsupported layer media type",
            }[attack] in result["error"], result
            invalid.append(result)
        gate("W02", invalid)
        hostile = []
        for attack in [
            "traversal",
            "absolute",
            "symlink",
            "hardlink",
            "count",
            "expansion",
        ]:
            result = invoke(attack, image(attack, attack), codes=(1,))
            assert not result["ready"]
            assert {
                "traversal": "path escape",
                "absolute": "path escape",
                "symlink": "unsupported link or special file",
                "hardlink": "unsupported link or special file",
                "count": "file count limit",
                "expansion": "expanded size limit",
            }[attack] in result["error"], result
            hostile.append(result)
        assert sentinel.read_text() == "untouched\n"
        gate(
            "W03",
            {
                "sentinel_sha256": hashlib.sha256(sentinel.read_bytes()).hexdigest(),
                "cases": hostile,
            },
        )
        mounted = out / "writable"
        mounted.mkdir()
        lab.run("mount", "-o", "nosuid,nodev,noexec", ready["snapshot"], mounted)
        try:
            with (mounted / "etc/version").open("wb") as f:
                f.write(b"snapshot-only")
                f.flush()
                os.fsync(f.fileno())
            assert (mounted / "etc/version").read_bytes() == b"snapshot-only"
        finally:
            lab.run("umount", mounted)
        verify(ready, "valid", "origin-after-write.json")
        gate(
            "W04",
            {
                "snapshot_contents": "snapshot-only",
                "origin_unchanged": True,
                "devices": inventory(),
            },
        )
        repeated = invoke("valid", valid, case="valid-repeat")
        assert repeated["snapshot_uuid"] == ready["snapshot_uuid"]
        procs = []
        for op in ["parallel-a", "parallel-b"]:
            procs.append(
                (
                    op,
                    subprocess.Popen(
                        [
                            str(PROJECT / "bin/worker"),
                            "--operation",
                            op,
                            "--manifest",
                            valid["digest"],
                            "--manifest-bytes",
                            str(valid["size"]),
                        ],
                        stdout=subprocess.PIPE,
                        stderr=subprocess.PIPE,
                        text=True,
                    ),
                )
            )
        concurrent = []
        finished = [(op, p, *p.communicate(timeout=100)) for op, p in procs]
        for op, p, stdout, stderr in finished:
            save(
                op + "-first.json",
                {"exit": p.returncode, "stdout": stdout, "stderr": stderr},
            )
            assert p.returncode in [0, 1]
            if p.returncode == 1:
                assert "worker busy" in stdout
            concurrent.append(invoke(op, valid, case=op + "-repeat"))
        assert concurrent[0]["snapshot_uuid"] != concurrent[1]["snapshot_uuid"]
        assert concurrent[0]["origin"] == concurrent[1]["origin"] == ready["origin"]
        gate("W05", {"repeat": repeated, "concurrent": concurrent})
        crashes = []
        for point in [
            "requested",
            "manifest",
            "config",
            "layer-0",
            "layer-1",
            "verified",
            "lv",
            "format",
            "mount",
            "unpack-partial",
            "unpack",
            "unmounted",
            "seal",
            "sealed",
            "snapshot",
            "writable",
            "activated",
            "registered",
        ]:
            name = "crash-" + point
            d = image(name)
            invoke(
                name,
                d,
                case=name + "-interrupted",
                extra=["--crash-after", point],
                codes=(77,),
            )
            before = inventory()
            with sqlite3.connect(fixture.ROOT / "intent.db") as state:
                observed_phase = state.execute(
                    "SELECT phase FROM operations WHERE id=?", (name,)
                ).fetchone()[0]
            assert (observed_phase == "registered") == (point == "registered"), (
                point,
                observed_phase,
            )
            r = invoke(name, d, case=name + "-resumed")
            assert r["ready"]
            verify(r, name, name + "-verified.json")
            repeated = invoke(name, d, case=name + "-repeated")
            assert repeated["snapshot_uuid"] == r["snapshot_uuid"]
            crashes.append(
                {
                    "point": point,
                    "phase_after_interruption": observed_phase,
                    "before": before,
                    "after": inventory(),
                    "result": r,
                }
            )
            print("recovered", point, flush=True)
        gate(
            "W06",
            {
                "kind": "process exit at each named phase/side-effect boundary",
                "cases": crashes,
            },
        )
        before = inventory()
        lab.run(
            "lvcreate",
            "--devices",
            device,
            "-V",
            "256M",
            "-T",
            "edgelab_worker/pool",
            "-n",
            "pressure",
            "--addtag",
            "edgelab_test",
        )
        try:
            lab.run(
                "dd",
                "if=/dev/zero",
                "of=/dev/edgelab_worker/pressure",
                "bs=1M",
                "count=128",
                "conv=fsync",
            )
            after = inventory()
            p = [x for x in after["report"][0]["lv"] if x["lv_name"] == "pool"][0]
            d = image("capacity")
            data = invoke(
                "data-limit",
                d,
                extra=["--data-limit", str(float(p["data_percent"]) + 1)],
                codes=(1,),
            )
            assert "data threshold" in data["error"]
            meta = invoke(
                "metadata-limit",
                d,
                extra=["--metadata-limit", str(float(p["metadata_percent"]) + 4.9)],
                codes=(1,),
            )
            assert "metadata threshold" in meta["error"]
        finally:
            lab.run("lvremove", "--devices", device, "-f", "edgelab_worker/pressure")
        gate(
            "W07",
            {
                "before": before,
                "pressure": after,
                "data_refusal": data,
                "metadata_refusal": meta,
                "after_cleanup": inventory(),
            },
        )
        db = sqlite3.connect(fixture.ROOT / "intent.db")
        rows = [
            dict(zip(["id", "image", "phase", "snapshot", "uuid", "error"], r))
            for r in db.execute(
                "SELECT id,image,phase,snapshot,uuid,error FROM operations"
            )
        ]
        db.close()
        save("operations.json", rows)
        assert all(
            r["phase"] != "registered"
            for r in rows
            if r["id"]
            in [
                "corrupt",
                "missing",
                "unsupported",
                "traversal",
                "absolute",
                "symlink",
                "hardlink",
                "count",
                "expansion",
                "data-limit",
                "metadata-limit",
            ]
        )
        save(
            "inventory.json",
            {
                "source_commit": (PROJECT / ".source-revision").read_text().strip(),
                "files": {
                    str(p.relative_to(out)): hashlib.sha256(p.read_bytes()).hexdigest()
                    for p in out.rglob("*")
                    if p.is_file()
                },
            },
        )
    finally:
        server.terminate()
        server.wait(timeout=5)
        serverlog.close()
        sentinel.unlink()


if __name__ == "__main__":
    main()
