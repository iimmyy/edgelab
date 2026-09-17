# EdgeLab

A small edge-hosting lab I can deploy, break, inspect, and recover. Built with AI assistance. This is an independent project inspired by public Fly.io exercises, not an official assessment.

Release 1 contains a Rust TCP proxy, Go identity/echo backends, and an independent Go traffic verifier. Release 2 adds an isolated WireGuard network, immutable object storage, and restartable LVM growth. The worker and routing releases remain planned.

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

The short profile checks the same behaviors with shorter measurements; its timings are not a substitute for the full profile. Verification requires 1 GiB available memory and 6 GiB free disk for raw evidence. At source commit `624c4bf`, P01–P10 and the additional control tests passed. The full profile recorded seven failures in 4,038,346 proxied attempts and none in 3,043,141 direct attempts. A later diagnostic matched a reproduced timeout to Linux dropping a SYN on a closed TCP socket during rapid loopback reuse. Release 1 retains this disclosed limitation; the earlier individual failures lack equivalent traces. See the [incident investigation](INCIDENT.md) and [machine-readable release record](evidence/release.json).

The committed evidence contains summaries and every unexpected failed request. Full attempt histories, logs and diagnostic captures are archived locally in `evidence/raw/`; smaller histories and proof files also remain on the VM under `.run/`. Duplicate packet rings were removed from the VM only after their archive hashes matched; archive hashes are in the release record. These large archives are excluded from Git. To validate the full exported run, extract `evidence/raw/release-1-async.tar.gz` into a temporary directory and run `lab/check-evidence.py` against its `release-1-async` directory with commit `624c4bf3b3a07fbabcc2f731c5b2f5cda651fcba`.

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

The authorized Multipass VM is `infra-lab`: 4 CPUs, 8 GiB RAM, and a 64 GiB disk. Source is edited here; compilation and tests run on its Linux filesystem. Commit changes, then:

```sh
bash lab/sync.sh
multipass exec infra-lab -- bash -lc 'cd ~/edgelab && bash lab/build.sh'
multipass exec infra-lab -- bash -lc 'cd ~/edgelab && python3 tests/verify.py --out .run/review --benchmark-seconds 2'
```

The sync records the source commit without requiring a second Git working tree. The VM currently supplies Ubuntu's Go compiler; the evidence records its exact version. Rust is pinned and Rust dependencies are locked. No cloud account is needed.

## Network and storage demonstration

Release 2 uses four physically allocated 1,800 MiB loopback disks and two network namespaces. The object client retains its own hashes; the controller records an absolute LV target in SQLite before changing storage. The default growth step is 500 MiB at 80% usage, with a two-second poll/cooldown, a 6,144 MiB cap, and a 512 MiB VG reserve.

On the disposable ARM64 Ubuntu VM, install `wireguard-tools`, `nftables`, `lvm2`, `e2fsprogs`, `tcpdump`, and `curl`. Build and sync the committed source as above. From a fresh fixture:

```sh
sudo python3 lab/linux.py init --confirm-disposable
sudo python3 lab/linux.py up
sudo python3 tests/storage_network.py --out .run/storage-review
sudo python3 tests/lab_lifecycle.py
```

The final command tests refusal paths and scoped cleanup, then removes the owned fixture. For another full run, initialize it again and choose a new evidence directory. `lab/linux.py status` reports routes, public WireGuard state, firewall counters, process identity, and storage identity. `fault --name endpoint` breaks a running tunnel; `baseline` restores the known network configuration. `down` deletes only this lab's namespaces and disposable storage.

These are authored fault replays, not withheld diagnosis exercises. The MTU case simulates an underlay size limit with a packet filter and repairs it by lowering the tunnel MTU. A complete replay checks seven network faults, an in-place composite repair, recovery of existing data, growth under writes, competing controllers, a process interruption after LV expansion, exhausted backing space, and sole-owner failure. The machine record is [Release 2 evidence](evidence/release-2.json). The fresh replay passed N01–N03 and S01–S06, retaining 955 acknowledged objects (3,943,628,800 bytes) with no unexpected object failures. L01/L02 and the rounded-target interruption regression also passed. Full evidence is archived in `evidence/raw/release-2.tar.gz`; extract it and run `lab/check-storage-evidence.py` against `release-2-final` with commit `671ebb64a7728ff43f616518e755f62cf7c4144b`.

## Read the implementation

The proxy owns admission and forwarding. The echo fixture reports identity and returns bytes. The verifier independently checks both. Python orchestrates faults and reads Linux `/proc`; it does not replace the Go traffic generator.

[Design and approved next releases](NOTES.md) · [Original acceptance inventory](lab/acceptance-manifest.json)

Limits: one Linux VM, static routing, and bounded application-space buffers rather than a bound on all kernel memory. Storage interruption tests cover process termination; whole-VM and physical-host durability remain untested. Full management APIs and telemetry belong to later releases. The original acceptance inventory remains unchanged; executed results live separately.
