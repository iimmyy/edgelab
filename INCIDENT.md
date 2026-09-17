# Backend connection deadlines under loopback churn

A reproduced failure came from Linux dropping a new SYN on a closed TCP socket during rapid reuse of the same endpoint tuple. No SYN-ACK or reset reached the proxy, which enforced its configured 500 ms establishment deadline. No proxy or backend application defect was identified in this event. Release 1 is ready for joint review with this workload limitation disclosed.

This is a lab investigation, not a production incident. The original full measurement recorded four failed proxied requests in 4,152,194 attempts. The updated full measurement at `624c4bf` recorded seven in 4,038,346; its 3,043,141 direct requests succeeded. Those earlier failures did not have equivalent kernel traces, so I cannot retrospectively assign each one this cause. P10 requires complete measurements, including failures, rather than zero errors.

## The matched failure

The decisive diagnostic preserved TCP control packets and kernel SYN-drop events before starting traffic. Its second proxied run recorded one failure in 1,402,998 attempts, at approximately 23,382 requests/second. Packet capture reported zero drops; listener overflow/drop counters did not increase.

For `127.0.0.1:40186 → 127.0.0.1:55779`, on September 17, 2026:

| UTC time | Observation |
| --- | --- |
| 00:01:54.093578 | Previous connection: backend sends FIN. |
| 00:01:54.093580 | Client acknowledges that FIN. |
| 00:01:54.093585 | New connection: SYN with sequence `3594003782`. |
| Approximately 00:01:54.093597 | Kernel discards that exact tuple and sequence with `TCP_CLOSE`, in `tcp_rcv_state_process`. |
| 00:01:54.593708 | Proxy reports a TCP establishment deadline; `SO_ERROR=0`. |

The packet and kernel records identify the same operation. `SO_ERROR=0` means the kernel had not supplied a socket error, not that establishment succeeded. The [Linux 6.8 receive path](https://github.com/torvalds/linux/blob/v6.8/net/ipv4/tcp_input.c#L6203-L6215) explicitly discards packets delivered to a socket in `TCP_CLOSE`.

[Matched evidence](evidence/diagnostics/handshake-light/summary.json) · [Packet excerpt](evidence/diagnostics/handshake-light/failure-packet-excerpt.txt) · [Selected packet capture](evidence/diagnostics/handshake-light/failure-tuple.pcap)

The precise ordering of socket lookup and close was not traced. This establishes the immediate kernel drop path; it does not establish an upstream kernel bug.

## Investigation and decision

I separated three explanations: a proxy that missed a completed handshake, a backend that stopped accepting, and a kernel handshake failure under local churn. An early SYN-only capture showed an unanswered SYN but could not explain its disposal. Narrow kernel probes observed LAST_ACK challenges on other connections; those were not matches and were not accepted as the cause.

Full packet capture plus TIME_WAIT probes produced five clean trials totaling 4,580,678 attempts, but reduced throughput to roughly 15,000 requests/second. I removed the high-frequency probes and restored the original alternating direct/proxy workload. The lighter capture recovered the original traffic rate and caught the matching `TCP_CLOSE` drop. The earlier clean runs did not establish a fix.

I kept the 500 ms timeout, kernel settings and initial-connect fallback behavior unchanged. The existing full-backlog fault gives a repeatable unanswered-handshake case. Its regression now checks the TCP stage, application deadline, zero pending socket error, successful fallback with correct identity/payload, and bounded latency. The updated short P01–P10 suite passed; fallback completed within 567 ms in this run. This regression checks the proxy's response to a stalled handshake, not deterministic reproduction of the kernel close path. Runtime binary hashes remain identical to the full measurement.

Customer update: high-churn local traffic can lose a fresh connection before the application accepts it. The proxy ends that attempt at its deadline and tries another configured target when available. With a single target, the client sees failure. Existing streams are never replayed, and all observed failures remain in the evidence.

Handoff: review the matched tuple, deadline contract and disclosed measurement errors. The [release record](evidence/release.json) links the results and checksummed raw archives. Large captures remain local and on `infra-lab`; the private GitHub repository includes the small proof package. No further release work starts before the joint review.
