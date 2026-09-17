# Decisions

## Release 1

I keep initial connection establishment separate from forwarding. Round-robin chooses the first target; subsequent targets are tried only before forwarding starts. One deadline covers DNS and all attempts, with a smaller deadline for each target. An established stream is never retried.

Tokio's bidirectional copy preserves half-close behavior and gives each direction an explicit buffer size. A permit stays with the forwarding task until that task finishes or is cancelled. The Go fixture emits identity, echoes bytes, and sends an EOF marker only after the client half-closes. The verifier checks the complete response.

An application's listeners share admission state, including through reloads and surviving sessions. Limits remain process options. An additional 1,024-session ceiling bounds connections across old and new configurations. Configuration is limited to 1 MiB, 64 applications, 256 listener ports, and 64 targets per application. I bind new listeners before replacing the active view; a failed validation or bind retains the old one. Existing streams keep their chosen backend. Candidate loads have a two-second timeout and run outside admission. There is one running validation and one replaceable pending request. Only the newest requested generation can activate; an invalid newest candidate retains the active view. The lab can delay validation to prove that admission continues and superseded results stay discarded.

Hickory handles asynchronous DNS, with a 256-response cache and 32 active requests per DNS connection. Using OS hostname lookup directly would leave blocking resolver jobs alive after a connection deadline. Literal addresses bypass DNS. At most 16 returned addresses are attempted within the target deadline. DNS is a shared dependency, not a claim of complete tenant isolation.

The global session ceiling, per-application permits, bounded messages, and bounded client workload are explicit lab limits. Kernel TCP buffers and listen backlogs are additional memory; resource tests measure process RSS and descriptors, not every kernel allocation. JSON logging uses a lossy 128-message queue feeding stderr. A blocked sink drops logs instead of blocking forwarding; periodic observations report the cumulative drop count when the sink recovers. Shutdown shares one deadline between draining and best-effort log flushing. Neither the logging thread nor abandoned filesystem work extends process shutdown.

The verifier limits concurrency to 256, payloads to 1 MiB, and attempts to four million. Its result includes every failure and timeout. Churn runs below the admission ceiling because client-observed EOF can precede final server task cleanup; saturation is tested separately. Benchmark success means a complete measurement, not zero errors. Unexplained failures keep release readiness pending. Connection diagnostics separate DNS from TCP establishment and distinguish application deadlines from operating-system errors; a deadline with SO_ERROR=0 means the kernel has not reported an error, not that the handshake succeeded. Direct and proxied workloads run sequentially on the same host with the same backend, client, payload, concurrency and duration. They are local observations, not global capacity claims.

## Release 2

The fixture owns four loop files, their LVM identities, two network namespaces, and separately tracked workload processes. It refuses an unmarked host or a loop device whose backing path is outside that inventory. Setup is idempotent after completion; an interruption during initial disk creation stops for inspection instead of guessing whether an existing volume may be formatted. Cleanup checks ownership and leaves unrelated resources alone.

Objects are immutable and addressed by SHA-256. A write synchronizes a temporary file, links it without replacing an existing object, removes the temporary name, and synchronizes the directory before acknowledgement. The service checks the mount and owner marker on each request. Its client generates content and retains acknowledged hashes independently. Application and management listeners are separate; only the application path is blocked on the base network.

The growth controller holds one nonblocking process lock. It verifies SQLite WAL/FULL, stores the operation ID and absolute target before effects, then reconciles LV size and filesystem size. A crash after `lvextend` leaves the same target pending; restart finishes `resize2fs` rather than adding another increment. The watched controller uses a two-second poll/cooldown and exits with a recorded error when growth cannot proceed. Four-MiB LVM extent rounding, the allocation cap, and backing reserve are explicit. Workload supervision by systemd belongs to Release 3; these fixture processes already survive controller restarts independently.

Network fault creation and repair are separate operations. Removing the only interface address or taking the interface down also removes its route on this kernel, so those repairs restore both facts. The simulated MTU fault keeps its size filter in place during verification of the smaller tunnel MTU. None of these authored replays is presented as a blind incident investigation.

## Approved contracts for later releases

**Worker:** persist intended absolute sizes and stable resource identities before effects. Reconcile external resources after interruption. SQLite uses verified WAL/FULL settings. Workloads have separate supervised units; management restart must neither kill nor duplicate them. Single-owner immutable object writes synchronize contents and directory metadata before acknowledgement.

**Restoration:** the harness explicitly disables publication. One restartable administrative operation gathers both routing nodes' accepted history, reconciles revisions and tombstones, quarantines unknown/conflicting resources, and persists a common result. Both nodes acknowledge the reconciled incarnation and fence old publication credentials before publication resumes. Partial recovery blocks and resumes by operation ID. A new incarnation cannot promote stale backup contents.

**Routing:** workers own instance authority. Authenticate updates, retain tombstones and endpoint reservations for the lab lifetime, reconcile full snapshots periodically, and retain one pending complete view per proxy subscriber. Defaults are 4,096 lifetime records per worker and a 16 MiB edge view. Reserve room for deletion at admission; refuse additions instead of truncating state. Preserve worker reconciliation time separately from proxy delivery time.

**Proxy cache:** serialize temporary-file write, file synchronization, same-directory rename, directory synchronization, then activation. Any failure retains the active in-memory view and reports persistence degradation. Restart validates the cache and reports it stale until refreshed; invalid or missing dynamic state does not fall back to static routes. Existing reachable cached routes do not expire automatically. Retired endpoints cannot belong to another instance during the lab lifetime. Fencing publisher credentials does not revoke disconnected caches.

**Independence:** routing and proxy are separate supervised processes without shutdown-order dependencies. Management and workload units are likewise independent. The verifier owns expectations rather than borrowing route-selection or recovery decisions.

Release 2 proves WireGuard and data-volume recovery; Release 3 adds OCI materialization and real snapshots; Release 4 adds distributed routing and operational failure cases; Release 5 joins them into the capstone. Private checkpoints make each working release inspectable without an approval pause. Optional extension lanes remain outside the core. Process-kill, whole-VM interruption, and physical-host guarantees stay distinct.

## Sources

The [public proxy prompt](https://github.com/fly-hiring/platform-challenge) supplies the configuration shape and opposite-language client requirement. The user's EdgeLab bundle supplies the proposed acceptance inventory. All implementation here is original; no candidate implementation was copied.

Library contracts: [Tokio bidirectional copying](https://docs.rs/tokio/1.53.1/tokio/io/fn.copy_bidirectional.html), [Hickory resolver](https://docs.rs/hickory-resolver/0.26.3/hickory_resolver/). Later durability and supervision contracts follow [fsync](https://man7.org/linux/man-pages/man2/fsync.2.html), [SQLite synchronous](https://www.sqlite.org/pragma.html#pragma_synchronous), and [systemd kill behavior](https://man7.org/linux/man-pages/man5/systemd.kill.5.html).
