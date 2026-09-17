"""Owned systemd units for the integrated lab."""

import re
import subprocess
from pathlib import Path

DIRECTORY = Path("/run/systemd/system")


def command(*args):
    return subprocess.check_output(["systemctl", *args], text=True, timeout=12).strip()


def path(name):
    if not re.fullmatch(r"edgelab-[a-zA-Z0-9_.-]+\.service", name):
        raise ValueError("expected an owned EdgeLab unit name")
    return DIRECTORY / name


def start(name, arguments, namespace=None):
    def quote(value):
        value = str(value)
        if any(c in value for c in "\n\r\0"):
            raise ValueError("invalid unit argument")
        return (
            '"'
            + value.replace("\\", "\\\\").replace('"', '\\"').replace("%", "%%")
            + '"'
        )

    content = "[Unit]\nDescription=EdgeLab owned workload\n[Service]\nType=simple\n"
    content += "ExecStart=:" + " ".join(map(quote, arguments)) + "\n"
    content += "KillMode=control-group\nTimeoutStopSec=7s\nRestart=no\n"
    if namespace:
        if namespace not in {"el-client", "el-server"}:
            raise ValueError("unowned network namespace")
        content += f"NetworkNamespacePath=/run/netns/{namespace}\n"
    target = path(name)
    try:
        with target.open("x") as file:
            file.write(content)
    except FileExistsError:
        if target.read_text() != content:
            raise RuntimeError("unit name already has another definition")
    command("daemon-reload")
    command("start", name)
    return content


def remove(name):
    target = path(name)
    if not target.exists():
        return
    command("stop", name)
    target.unlink()
    command("daemon-reload")
