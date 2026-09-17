# EdgeLab

A small edge-hosting lab that I can deploy, break, poke around inside, and put back together. Built of course with AI assistance (I mean come on lol) and shaped by the public Fly.io exercises. It's my own project and nobody's grading it. Just thought it would be fun.

The first three releases cover a Rust TCP proxy, private networking, object storage that actually sticks around, and a Go worker that gets images ready on thin storage. Distributed routing and the combined demo are what's coming next.

Why I made the calls I made, along with the library contracts I'm leaning on, is all in the [design notes](NOTES.md). The [incident record](INCIDENT.md) though walks through how I chased down a connection timeout :D

## Build and run

The lab runs on an ARM64 Ubuntu VM. I edit the source on my Mac and keep builds and runtime data over on the VM. The one I'm using right now is `Multipass`,  set up with an astonishing 4 CPUs, 8 GiB RAM and a 64 GiB disk. (I know, impressive specs)

Commit your source locally first, then sync and build:

```sh
bash lab/sync.sh
multipass exec infra-lab -- bash -lc 'cd ~/edgelab && bash lab/build.sh'
```

The build wants Rust 1.98.1, Go 1.22 or newer, and a C compiler. The tests want Python 3 on top of that. For networking and storage you'll also need `wireguard-tools`, `nftables`, `lvm2`, `e2fsprogs`, `tcpdump`, and `curl`.

Run everything below inside the VM, from `~/edgelab`. Give every run a brand new evidence directory.

### Proxy

```sh
python3 tests/verify.py --out .run/proxy-review --benchmark-seconds 2
python3 tests/control.py --out .run/control-review
```

That essentially covers forwarding, fallback, half-close, overload isolation, reloads and shutdown. The short profile tells you the behaviour is right. If you want real numbers out of it, run `--benchmark-seconds 60`.

### Networking and storage

Start from a fresh networking and storage fixture:

```sh
sudo python3 lab/linux.py init --confirm-disposable
sudo python3 lab/linux.py up
sudo python3 tests/storage_network.py --out .run/storage-review
sudo python3 tests/lab_lifecycle.py
```

The last command checks setup and cleanup, then pulls the fixture it owns back out. `lab/linux.py status` lets you look at it while it's still running. If there's already a fixture sitting there and you want to start over, `sudo python3 lab/linux.py down` clears it out, disposable storage and data included.

### Image preparation

Start from a fresh worker fixture:

```sh
sudo python3 lab/worker.py up
sudo python3 tests/worker.py --out .run/image-review
sudo python3 lab/worker.py status
```

The suite builds its own image store and checks the mounted files against hashes it worked out on its own, so nothing is being graded against its own homework. `sudo python3 lab/worker.py down` takes out the worker's pool and state. That fixture is its own thing, completely separate from the networking and storage one.

## Results

| Release | What what made it | Output |
| --- | --- | --- |
| 1 | P01 through P10 and the control tests all passed. The full measurement did catch seven failures across 4,038,346 proxied attempts, (0_0) and nothing at all across 3,043,141 direct ones. (^.^) | [Proxy record](evidence/release.json) |
| 2 | Network, storage and lifecycle gates did pass. All 955 acknowledged objects came back verified after recovery. | [Storage record](evidence/release-2.json) |
| 3 | W01 through W07 passed, 18 interruption points were included, with 21 distinct snapshots then registered. | [Worker record](evidence/release-3.json) |

Those seven proxy failures and what they actually mean are laid out in the incident record. Everything here is local measurement and fault replays I wrote myself. Killing a process doesn't tell you much about whole-VM or physical-host durability. Though image tests prove filesystem preparation and snapshot activation. They don't make this a VM runtime.

Every evidence record says which commit it came from and which raw archive goes with it. The big archives stay local under `evidence/raw/`. The checkers in `lab/` take an extracted run and verify it against those commits. The [original acceptance inventory](lab/acceptance-manifest.json) stays separate from the executed results, and that's on purpose.