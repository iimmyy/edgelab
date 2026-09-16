# EdgeLab

A small edge-hosting lab I can deploy, break, inspect, and recover. Built with AI assistance. This is an independent project inspired by public Fly.io exercises, not an official assessment.

Release 1 contains a Rust TCP proxy, Go identity/echo backends, and an independent Go traffic verifier. The later network, storage, worker, and routing releases are **planned**, not implemented.

## Run the failure demonstration

In Linux, with Rust 1.98.1, Go 1.22 or newer, a C compiler, and Python 3:

```sh
bash lab/build.sh
python3 tests/verify.py --out .run/review --benchmark-seconds 2
python3 tests/control.py --out .run/control
```

The driver starts only its own processes, verifies payloads and identity, fills an application's connection budget, tests broken targets and configuration, kills a backend midstream, and drains the proxy. It writes every attempt, proxy logs, resource samples, and gate results. Cleanup stops only the processes it started. The output directory must be new.

For the full three-repetition comparison (100 concurrent requests, 1 KiB each, 60 seconds per direct/proxy run):

```sh
python3 tests/verify.py --out .run/full --benchmark-seconds 60
python3 lab/check-evidence.py .run/full --commit "$(git rev-parse HEAD)"
```

The short profile checks the same behaviors with shorter measurements; its timings are not a substitute for the full profile. Verification requires 1 GiB available memory and 6 GiB free disk for raw evidence. At source commit `624c4bf`, P01–P10 and the additional control tests passed. The full profile recorded seven failures in 4,038,346 proxied attempts and none in 3,043,141 direct attempts. Release readiness remains pending attribution of those backend-establishment failures. See the [incident investigation](INCIDENT.md) and [machine-readable release record](evidence/release.json).

The committed evidence contains summaries and every unexpected failed request. Full attempt histories, logs and diagnostic captures are retained locally in `evidence/raw/` and on the VM under `.run/`; archive hashes are in the release record. These large archives are excluded from Git. To validate the full exported run, extract `evidence/raw/release-1-async.tar.gz` into a temporary directory and run `lab/check-evidence.py` against its `release-1-async` directory with commit `624c4bf3b3a07fbabcc2f731c5b2f5cda651fcba`.

## Try it by hand

Run these in separate terminals:

```sh
bin/echo --listen 127.0.0.1:9101 --instance echo-a
bin/echo --listen 127.0.0.1:9102 --instance echo-b
target/release/edgelab-proxy --config lab/config.json
bin/traffic --address 127.0.0.1:8101 --instances echo-a,echo-b
```

Stop one backend and repeat the traffic command: new connections fall back to the survivor. Existing TCP streams cannot be replayed. Send SIGHUP to the proxy after editing the configuration; send SIGTERM for bounded draining. Logs are JSON on stderr through a bounded, lossy queue. A blocked log sink cannot extend the drain deadline. The control tests exercise blocked logging and overlapping reloads.

The proxy accepts `Apps`, `Name`, `Ports`, and `Targets`. Defaults are 128 connections per application, 8 KiB buffers in each direction, 500 ms per target, 2 seconds total establishment including DNS, and 5 seconds drain grace. `--help` lists the overrides. Listeners bind to loopback by default.

## Work from the Mac

The authorized Multipass VM is `infra-lab`. Source is edited here; compilation and tests run on its Linux filesystem. Commit changes, then:

```sh
bash lab/sync.sh
multipass exec infra-lab -- bash -lc 'cd ~/edgelab && bash lab/build.sh'
multipass exec infra-lab -- bash -lc 'cd ~/edgelab && python3 tests/verify.py --out .run/review --benchmark-seconds 2'
```

The sync records the source commit without requiring a second Git working tree. The VM currently supplies Ubuntu's Go compiler; the evidence records its exact version. Rust is pinned and Rust dependencies are locked. No cloud account is needed.

## Read the implementation

The proxy owns admission and forwarding. The echo fixture reports identity and returns bytes. The verifier independently checks both. Python orchestrates faults and reads Linux `/proc`; it does not replace the Go traffic generator.

[Design and approved next releases](NOTES.md) · [Original acceptance inventory](lab/acceptance-manifest.json)

Limits: one Linux VM, loopback TCP, static routing, bounded application-space buffers rather than a bound on all kernel memory, and no persistence or availability claim for process/VM death. Operational signals are logs; management APIs and full telemetry belong to later releases. The original acceptance inventory remains unchanged; executed results live separately.
