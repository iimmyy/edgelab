# EdgeLab

This is a small edge-hosting lab that I made to deploy, break, poke around inside, and put back together. Built of course with AI assistance (I mean come on lol) and shaped by the public Fly.io exercises. It's my own project and nobody's grading it. Just thought it would be fun.

All five implementation releases are hooked together now. A Go worker gets an image ready on thin storage, separately supervised applications run the binaries it verified, routing nodes take the worker's complete instance history, and the Rust proxies forward off their own local routing views. An independent client sits outside the whole thing checking which identities are actually serving and whether the object hashes line up, while the lab has a crisis underneath it.

To see why I made the calls I made, along with the library contracts I'm dependent on, is all in the [design notes](NOTES.md). The [incident record](INCIDENT.md) though walks through how I chase things down like for example, a connection timeout :D

## Build and run

The lab runs on an ARM64 Ubuntu VM. I which then edit the source on my Mac and keep builds and runtime data over on the VM. The one I'm using right now is `Multipass`,  set up with an astonishing 4 CPUs, 8 GiB RAM and a 64 GiB disk. (I know, impressive specs)

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

### Full replay (from the Mac)

For a fresh ARM64 Ubuntu VM with 4 CPUs, 8 GiB RAM and a 64 GiB disk:

    bash lab/sync.sh YOUR_VM
    multipass exec YOUR_VM -- bash -lc 'cd ~/edgelab && bash lab/bootstrap.sh'
    multipass exec YOUR_VM -- bash -lc 'cd ~/edgelab && bash lab/reproduce.sh .run/review'

The replay wants a new evidence directory and no EdgeLab storage fixtures already sitting there. It builds the pinned Rust dependencies, runs the earlier releases, puts routing and operations through their paces, then finishes with the integrated demo. As well it only ever creates and removes resources marked as the lab's. Individual logs and client results stay in the output directory, and the VM is left up so you can go poke at it afterwards.

### Individual checks (inside the VM)

To run the newer pieces on their own after building:

    go run ./cmd/routing-check --router target/release/edgelab-routing --output .run/routing-review
    python3 tests/dynamic.py --out .run/dynamic-review
    sudo python3 tests/routing_ops.py --out .run/operations-review
    sudo python3 tests/capstone.py --out .run/capstone-review
    python3 tests/app_limits.py --out .run/limits-review

The capstone wants fresh networking/storage and image-worker fixtures. It builds its own topology and clears it out when it's done. All of these are automated replays.

### Application RTT

With an echo endpoint running, point bin/traffic at it with --count 1 --concurrency 1, the app and instance IDs you're expecting, and an --expected-identities file you wrote yourself. So:

    bin/traffic --address 127.0.0.2:8102 --app echo --instances echo-1,echo-2 --expected-identities expected-identities.json --count 1 --concurrency 1

The JSON that comes back gives you milliseconds as measured by that client, plus the identity it actually got handed. That number is application RTT, so connection setup, the greeting, payload transfer and the server's own work are all baked into it. It isn't an ICMP measurement and it isn't an isolated estimate of network latency. The endpoint and identity file in this example have to belong to a lab that's actually up, and keep in mind the capstone cleans up after itself as well.

## Results

| Release | What made it through | Output |
| --- | --- | --- |
| 1 | P01 through P10 and the control tests all passed. The full measurement did catch seven failures across 4,038,346 proxied attempts, (0_0) and nothing at all across 3,043,141 direct ones. (^.^) | [Proxy record](evidence/release.json) |
| 2 | Network, storage and lifecycle gates did pass. All 955 acknowledged objects came back verified after recovery. | [Storage record](evidence/release-2.json) |
| 3 | W01 through W07 passed, 18 interruption points were included, with 21 distinct snapshots then registered. | [Worker record](evidence/release-3.json) |

Release 4: R01 through R06 and O01 through O06 passed. That's 12 routing-foundation cases, seven cache cases and eleven operational ones. See the [Release 4 record](evidence/release-4.json).

Release 5: the fresh ARM64 VM replay passed against a96b0b87611797e79e14608c7f729d4916da4574. The audit verified 1,217 files. The nine-stage capstone checked 115 objects across 62 monitoring cycles. The per-application limit change went in after the first successful capstone and passed both its own regression and the final integrated replay. See the [Release 5 record](evidence/release-5.json) and [coverage map](evidence/release-coverage.json).

The first capstone attempt turned up a parsing mistake in my own harness. Worker stdout emits progress events and then a ready result at the end, and the harness had been written expecting a single JSON document. It parses the event stream now and insists on that final ready result. The failed run is still sitting on the execution VM.

Those seven proxy failures and what they actually mean are laid out in the incident record section. Everything here is local measurement and fault replays of things I wrote myself. As well killing a process will not tell you much about whole-VM or physical-host durability. Though image tests prove filesystem preparation and snapshot activation. They don't make this a VM runtime.

Every evidence record says which commit it came from and which raw archive goes with it. The big archives stay local under `evidence/raw/`. The checkers in `lab/` take an extracted run and verify it against those commits. The [original acceptance inventory](lab/acceptance-manifest.json) stays separate from the executed results.
