#!/usr/bin/env python3
import argparse
import json
import os
from pathlib import Path
import shutil

import linux as lab

ROOT = Path("/var/lib/edgelab-worker")


def save(name, value):
    p = ROOT / name
    with p.open("w") as f:
        json.dump(value, f)
        f.flush()
        os.fsync(f.fileno())
    fd = os.open(ROOT, os.O_DIRECTORY)
    os.fsync(fd)
    os.close(fd)


def guard():
    if os.geteuid() != 0 or ROOT.is_symlink():
        raise RuntimeError("requires owned Linux worker fixture")
    marker = json.loads((ROOT / "marker.json").read_text())
    if (
        marker["machine"] != lab.machine()
        or ROOT.stat().st_uid != 0
        or ROOT.stat().st_mode & 0o077
    ):
        raise RuntimeError("worker ownership mismatch")
    f = json.loads((ROOT / "pool.json").read_text())
    if lab.run("losetup", "-n", "-O", "BACK-FILE", f["device"]) != str(ROOT / "disk"):
        raise RuntimeError("worker device outside allowlist")
    if (
        lab.run(
            "lvs",
            "--devices",
            f["device"],
            "--noheadings",
            "-o",
            "lv_uuid",
            "edgelab_worker/pool",
        )
        != f["uuid"]
    ):
        raise RuntimeError("thinpool identity mismatch")
    return f


def up():
    facts = lab.preflight(8 * 1024**3)
    if ROOT.exists():
        return guard()
    if lab.run("vgs", "edgelab_worker", check=False):
        raise RuntimeError("worker VG name already occupied")
    ROOT.mkdir(mode=0o700)
    save("marker.json", facts)
    lab.run("fallocate", "-l", 4096 * 1024**2, ROOT / "disk")
    device = lab.run("losetup", "--find", "--show", ROOT / "disk")
    save("device.json", {"device": device})
    lab.run("pvcreate", "--devices", device, device)
    lab.run("vgcreate", "--devices", device, "edgelab_worker", device)
    lab.run(
        "lvcreate",
        "--devices",
        device,
        "--type",
        "thin-pool",
        "-L",
        "3072M",
        "--poolmetadatasize",
        "16M",
        "--chunksize",
        "64K",
        "-n",
        "pool",
        "edgelab_worker",
    )
    result = {
        "machine": lab.machine(),
        "device": device,
        "uuid": lab.run(
            "lvs",
            "--devices",
            device,
            "--noheadings",
            "-o",
            "lv_uuid",
            "edgelab_worker/pool",
        ),
    }
    save("pool.json", result)
    return result


def down():
    f = guard()
    mounts = ROOT / "mounts"
    if mounts.exists():
        for p in mounts.iterdir():
            if os.path.ismount(p):
                lab.run("umount", p)
    lab.run("vgremove", "--devices", f["device"], "-ff", "-y", "edgelab_worker")
    lab.run("losetup", "-d", f["device"])
    shutil.rmtree(ROOT)


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("action", choices=["up", "down", "status"])
    a = p.parse_args()
    if a.action == "up":
        print(json.dumps(up()))
    elif a.action == "down":
        down()
    else:
        f = guard()
        print(
            lab.run(
                "lvs",
                "--devices",
                f["device"],
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
