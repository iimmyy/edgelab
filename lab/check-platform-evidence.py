#!/usr/bin/env python3
import argparse
import hashlib
import json
import sqlite3
import subprocess
from pathlib import Path


def read(path):
    return json.loads(path.read_text())


def routing(root, commit):
    operations = root / "operations"
    dynamic = root / "dynamic"
    candidates = list((root / "routing").glob("run-*"))
    foundation = candidates[0] if candidates else root / "routing"
    assert len(candidates) <= 1, "ambiguous routing run"
    result = read(foundation / "results.json")
    assert result["source_commit"] == commit and result["failed"] == 0
    required = {
        "authenticated-publication",
        "wrong-credential",
        "owner-impersonation",
        "unreconciled-incarnation-on-empty-node",
        "conflicting-revision-retains-active-view",
        "deletion-rejects-stale-replay",
        "new-incarnation-cannot-resurrect-backup",
        "retired-endpoint-reserved-across-owners",
        "delivery-does-not-refresh-source",
        "process-restart-retains-deletion-and-marks-stale",
        "record-capacity-preserves-deletion",
        "two-node-delay-duplicate-loss-reorder-convergence",
    }
    assert {row["case"] for row in result["observations"]} == required
    assert all(row["passed"] for row in result["observations"])
    convergence = read(foundation / "convergence-report.json")
    assert (
        len(convergence["node_hashes"]) == 2
        and len(set(convergence["node_hashes"])) == 1
    )
    assert (
        convergence["all_match"]
        and convergence["elapsed_ms"] <= convergence["bound_ms"] == 3000
    )
    history = read(foundation / "input-history.json")
    assert any(row["drop"] for row in history) and any(
        row["delay_ms"] for row in history
    )
    assert any(row["expected_status"] == 409 for row in history)
    assert convergence["expected"]["records"]["one"]["deleted"]
    dynamic_result = read(dynamic / "results.json")
    assert dynamic_result["source_commit"] == commit
    assert (
        len(dynamic_result["passed"]) == 7 and len(set(dynamic_result["passed"])) == 7
    )
    result = read(operations / "results.json")
    assert result["source_commit"] == commit
    expected = {
        "three-workers-two-routers-two-proxies-identity-verified",
        "failed-identity-query-reported-unknown",
        "workloads-survive-management-restart-without-duplicates",
        "partitioned-router-converges-to-deletion",
        "backup-recovery-resumes-one-node-ack-with-deletion-and-credential-fencing",
        "schema-incompatible-canary-rejected-before-readiness",
        "query-plan-and-wal-maintenance-under-verified-traffic",
        "slow-subscriber-bounded-memory-coalescing-and-complete-resync",
        "invalid-app-update-preserves-unrelated-application",
        "independent-watchdog-captures-stall-and-restarts-once",
        "router-restarts-while-proxy-drains-live-stream",
    }
    assert set(result["passed"]) == expected
    identities = read(operations / "expected-identities.json")
    for path in operations.glob("*.json"):
        report = read(path)
        if not isinstance(report, dict) or "stdout" not in report:
            continue
        try:
            sample = json.loads(report["stdout"])
        except json.JSONDecodeError:
            continue
        if not isinstance(sample, dict) or "attempts" not in sample:
            continue
        assert (
            report["exit"] == 0 and sample["attempts"] > 0 and sample["failures"] == 0
        )
        assert sample["identities"]
        for name, identity in sample["identities"].items():
            for key, value in identities[name].items():
                assert identity[key] == value, (path, name, key)
        rows = [
            json.loads(line)
            for line in path.with_suffix(".jsonl").read_text().splitlines()
        ]
        assert len(rows) == sample["attempts"] and all(
            not row.get("error") for row in rows
        )
    pids = read(operations / "workload-pids.json")
    assert pids["before"] == pids["after"] and all(
        int(pid) > 0 for pid in pids["after"]
    )
    failed = read(operations / "status-query-failure.json")
    assert failed["instances"]["aux-1"]["state"] == "unknown"
    recovery = read(operations / "recovery-proof.json")
    assert recovery["interrupted_exit"] == 77 and recovery["blocked_exit"] != 0
    assert (
        recovery["resumed"]["complete"] and recovery["resumed"]["acknowledgements"] == 2
    )
    journal = read(operations / "recovery.json")
    assert (
        "unaccepted" in journal["quarantined"]
        and "unaccepted" not in journal["snapshot"]["records"]
    )
    assert journal["snapshot"]["records"]["instance-1"]["deleted"]
    database = sqlite3.connect(
        f"file:{operations / 'worker-0.db'}?immutable=1", uri=True
    )
    state = json.loads(
        database.execute("SELECT body FROM routing_state WHERE id=1").fetchone()[0]
    )
    database.close()
    assert state["incarnation"] == 2 and state["records"]["instance-1"]["deleted"]
    assert "unaccepted" not in state["records"]
    slow = read(operations / "slow-subscriber.json")
    assert len(slow["samples"]) == 16 and slow["durable_operations"] == 16
    for sample in slow["samples"]:
        assert sample["queue"]["pending_view_capacity"] == 1
        for role in ["router", "proxy"]:
            assert (
                sample[role]["rss_bytes"] - slow["baseline"][role]["rss_bytes"]
                < slow["memory_delta_limit_bytes"]
            )
    maintenance = read(operations / "maintenance.json")
    assert (
        maintenance["seeded_rows"] == 4096 and maintenance["blocked_checkpoint"][0] == 1
    )
    assert (
        maintenance["recovered_checkpoint"][0] == 0
        and maintenance["peak_wal_bytes"] > 0
    )
    assert any("SCAN" in row[-1] for row in maintenance["before_plan"])
    assert any("SEARCH" in row[-1] for row in maintenance["after_plan"])
    assert (
        maintenance["client"]["failures"] == 0 and maintenance["client"]["attempts"] > 0
    )
    canary = read(operations / "canary.json")
    assert (
        canary["exit"] != 0
        and not canary["listener_opened"]
        and "no such column: deployment_epoch" in canary["error"]
    )
    assert read(operations / "watchdog/recovery.json")["restart_count"] == 1
    assert len(read(operations / "watchdog/alert.json")["failures"]) == 2
    assert read(operations / "independent-restart.json")["router_restart_seconds"] < 2
    assert len(list(operations.glob("*.unit"))) >= 10
    trace = read(operations / "trace.json")
    assert trace["operation"][1] == trace["record"]["revision"]
    return {
        "routing_gates": 6,
        "operations_gates": 6,
        "canary_profile": "worker status schema upgrade; proxy has no SQL dependency",
    }


def capstone(root, commit):
    source = read(root / "source.json")
    assert source["commit"] == commit and source["binary_sha256"]
    stages = read(root / "stages.json")
    names = {stage["stage"] for stage in stages}
    assert names == {
        "prepared-image-and-discovered-supervised-applications",
        "new-connection-fallback-and-existing-session-failure",
        "private-route-repaired-with-direct-path-still-blocked",
        "automatic-storage-growth-preserves-acknowledged-objects",
        "snapshot-side-effect-reconciled-without-duplication",
        "routing-partition-healed-without-deletion-resurrection",
        "canary-rejected-and-stalled-consumer-recovered",
        "immutable-image-fork-isolated",
        "integrated-demonstration-passed",
    }
    assert stages[-1]["stage"] == "integrated-demonstration-passed"
    assert read(root / "midstream-failure.json")["closed"]
    window = read(root / "network-window.json")
    assert window["direct_exit"] == 28
    monitor = read(root / "monitor-results.json")
    assert monitor
    assert any(row["objects_exit"] != 0 for row in monitor)
    for row in monitor:
        assert "error" not in row and row["echo_exit"] == 0
        if row["objects_exit"] != 0:
            assert (
                row["started"] <= window["finished"]
                and row["finished"] >= window["started"]
            )
        for role in ["echo", "objects"]:
            assert (root / f"monitor-{role}-{row['serial']}.stdout").stat().st_size > 0
    for manifest, proof, count in [
        ("growth-ack.jsonl", "grown-object-hashes.stdout", 110),
        ("monitor-ack.jsonl", "original-object-hashes.stdout", 5),
    ]:
        acknowledged = [
            json.loads(line) for line in (root / manifest).read_text().splitlines()
        ]
        verified = [
            json.loads(line) for line in (root / proof).read_text().splitlines()
        ]
        assert len(acknowledged) == len(verified) == count
        assert {r["id"] for r in acknowledged} == {r["id"] for r in verified}
        assert all(row["ok"] and row["method"] == "GET" for row in verified)
    resource = read(root / "worker-resource-proof.json")
    previous = {row["lv_uuid"] for row in resource["before"]}
    created = {row["lv_uuid"] for row in resource["interrupted"]} - previous
    assert created == {resource["resumed"]["snapshot_uuid"]}
    assert read(root / "worker-interrupted.json")["exit"] == 77
    for name in [
        "verify-serving-image",
        "fork-initial-hashes",
        "fork-isolated-hashes",
        "source-unmodified-hashes",
    ]:
        assert (
            read(root / (name + ".json"))["exit"] == 0
            and read(root / (name + ".stdout"))["ok"]
        )
    expected = read(root / "image-expected.json")["files"]
    assert expected["bin/echo"] == source["binary_sha256"]["bin/echo"]
    assert expected["bin/objects"] == source["binary_sha256"]["bin/objects"]
    fork = read(root / "fork-expected.json")["files"]
    assert fork["fork-only"] == hashlib.sha256(b"fork-private").hexdigest()
    assert all(read(root / "cleanup.json").values())
    return {
        "capstone_stages": len(stages),
        "monitor_cycles": len(monitor),
        "objects_verified": 115,
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("directory", type=Path)
    parser.add_argument("--commit", required=True)
    parser.add_argument(
        "--profile", choices=["routing", "capstone", "reproduction"], required=True
    )
    args = parser.parse_args()
    root = args.directory.resolve()
    inventory = read(root / "inventory.json")
    assert inventory["source_commit"] == args.commit
    files = {
        str(path.relative_to(root))
        for path in root.rglob("*")
        if path.is_file() and path != root / "inventory.json"
    }
    assert files == set(inventory["files"]), "incomplete inventory"
    for name, digest in inventory["files"].items():
        path = (root / name).resolve()
        assert (
            path.is_relative_to(root)
            and hashlib.sha256(path.read_bytes()).hexdigest() == digest
        ), name
    source = read(root / "source-tree.json")
    assert source["commit"] == args.commit
    project = Path(__file__).resolve().parents[1]
    tracked = subprocess.check_output(
        ["git", "ls-tree", "-r", "--name-only", args.commit], cwd=project, text=True
    ).splitlines()
    assert set(source["files"]) == {
        name for name in tracked if not name.startswith("evidence/")
    }
    for name, digest in source["files"].items():
        assert (
            hashlib.sha256(
                subprocess.check_output(
                    ["git", "show", f"{args.commit}:{name}"], cwd=project
                )
            ).hexdigest()
            == digest
        ), name
    if args.profile == "routing":
        result = routing(root, args.commit)
    elif args.profile == "capstone":
        result = capstone(root / "capstone", args.commit)
    else:
        run = root / "run"
        result = {
            **routing(run, args.commit),
            **capstone(run / "capstone", args.commit),
        }
        limits = read(run / "app-limits/results.json")
        assert limits["source_commit"] == args.commit
        assert limits["limits"] == {"a": 2, "b": 3}
        assert limits["shared_listeners"] and limits["reload_preserved_admission"]
        assert len(limits["rejections"]) == 4
        assert all(row["seconds"] < 0.25 for row in limits["rejections"])
        assert limits["healthy_b"]["failures"] == 0
        assert len(limits["invalid"]) == 3 and all(
            row["exit"] != 0 for row in limits["invalid"]
        )
        preflight = read(root / "lifecycle/preflight.json")
        assert preflight["status"] == "passed"
        assert {row["case"] for row in preflight["refusals"]} == {
            "unmarked",
            "capacity",
            "missing-feature",
            "non-allowlisted-device",
        }
        assert all(row["exit"] != 0 for row in preflight["refusals"])
        lifecycle = read(root / "lifecycle/cleanup.json")
        assert lifecycle["status"] == "passed"
        assert all(
            lifecycle[key]
            for key in [
                "unrelated_process_alive",
                "unrelated_file_preserved",
                "unrelated_network_table_preserved",
            ]
        )
        rounding = read(root / "growth-rounding/results.json")
        assert (
            rounding["source_commit"] == args.commit and rounding["status"] == "passed"
        )
        assert [row["exit"] for row in rounding["records"]] == [77, 0, 0, 1]
        result["subsequent_app_limit_change"] = "verified"
        for checker, directory in [
            ("check-evidence.py", "proxy"),
            ("check-storage-evidence.py", "network-storage"),
            ("check-worker-evidence.py", "worker"),
        ]:
            subprocess.run(
                [
                    "python3",
                    str(project / "lab" / checker),
                    str(run / directory),
                    "--commit",
                    args.commit,
                ],
                cwd=project,
                check=True,
            )
    print(
        json.dumps(
            {"verified_files": len(files), "source_commit": args.commit, **result}
        )
    )


if __name__ == "__main__":
    main()
