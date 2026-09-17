#!/usr/bin/env python3
import argparse
from contextlib import closing
import hashlib
import json
import shutil
import sqlite3
from pathlib import Path

SECRET_FIELDS = {
    "token",
    "admin_token",
    "router_admin_token",
    "credential",
    "authorization",
    "private_key",
}


def redact(value):
    changed = False
    if isinstance(value, dict):
        result = {}
        for key, item in value.items():
            if key.lower() in SECRET_FIELDS:
                result[key] = "<redacted>"
                changed = True
            else:
                result[key], nested = redact(item)
                changed |= nested
        return result, changed
    if isinstance(value, list):
        result = []
        for item in value:
            item, nested = redact(item)
            result.append(item)
            changed |= nested
        return result, changed
    return value, False


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--tree", type=Path, required=True)
    parser.add_argument(
        "--directory", action="append", default=[], help="label=directory"
    )
    args = parser.parse_args()
    args.out.mkdir(parents=True, exist_ok=False, mode=0o700)
    source = json.loads(args.tree.read_text())
    changes = {}
    shutil.copyfile(args.tree, args.out / "source-tree.json")
    for pair in args.directory:
        label, directory = pair.split("=", 1)
        if not label or "/" in label or label in {".", ".."}:
            raise ValueError("invalid evidence label")
        directory = Path(directory)
        for original in sorted(directory.rglob("*")):
            if not original.is_file():
                continue
            if original.is_symlink():
                raise ValueError("evidence links require explicit inspection")
            relative = Path(label) / original.relative_to(directory)
            target = args.out / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            if original.name.endswith(("-wal", "-shm")):
                continue
            data = original.read_bytes()
            if (
                data.startswith(b"SQLite format 3\0")
                and Path(str(original) + "-wal").exists()
            ):
                with (
                    closing(
                        sqlite3.connect(f"file:{original}?mode=ro", uri=True)
                    ) as reader,
                    closing(sqlite3.connect(target)) as writer,
                ):
                    reader.backup(writer)
                    writer.execute("PRAGMA journal_mode=DELETE")
                changes[str(relative)] = (
                    "offline SQLite backup including committed WAL frames"
                )
                continue
            try:
                value = json.loads(data)
            except (json.JSONDecodeError, UnicodeDecodeError):
                target.write_bytes(data)
                continue
            value, changed = redact(value)
            if changed:
                target.write_text(json.dumps(value, indent=2) + "\n")
                changes[str(relative)] = (
                    "credential fields redacted; original retained on execution VM"
                )
            else:
                target.write_bytes(data)
    files = {
        str(path.relative_to(args.out)): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in args.out.rglob("*")
        if path.is_file()
    }
    (args.out / "inventory.json").write_text(
        json.dumps(
            {
                "source_commit": source["commit"],
                "files": files,
                "transformations": changes,
            },
            indent=2,
        )
        + "\n"
    )
    print(
        json.dumps(
            {
                "files": len(files),
                "source_commit": source["commit"],
                "redacted_or_backed_up": len(changes),
            }
        )
    )


if __name__ == "__main__":
    main()
