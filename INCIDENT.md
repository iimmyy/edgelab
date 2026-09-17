# Backend connection deadlines under loopback churn

I reproduced the failure and it came down to Linux throwing away a new SYN on a closed TCP socket while the same endpoint tuple was getting reused over and over. Nothing came back to the proxy, no SYN-ACK and no reset, so the proxy did exactly what it was told and enforced its 500 ms establishment deadline. I didn't find a defect in the proxy or the backend for this one. Release 1 is ready for joint review as long as this workload limitation goes on the record with it.

This is just a lab investigation. Nothing here took down production. The original full measurement had four failed proxied requests out of 4,152,194 attempts. The updated one at `624c4bf` had seven out of 4,038,346, and all 3,043,141 direct requests went through fine. Those earlier failures don't have kernel traces to match against, so I can't go back after the fact and pin this cause on each of them (yea immy has integrity. P10 asks for complete measurements with the failures left in. 

## The matched failure

The diagnostic that finally caught it saved TCP control packets and kernel SYN-drop events before any traffic started. Its second proxied run had one failure across 1,402,998 attempts, at roughly 23,382 requests a second. Packet capture said zero drops, and the listener overflow and drop counters never moved.

Here's what happened on `127.0.0.1:40186 → 127.0.0.1:55779`, on September 17, 2026:

| UTC time | What happened |
| --- | --- |
| 00:01:54.093578 | Previous connection: backend sends its FIN. |
| 00:01:54.093580 | Client acknowledges that FIN. |
| 00:01:54.093585 | New connection: SYN goes out with sequence `3594003782`. |
| Around 00:01:54.093597 | Kernel throws away that exact tuple and sequence with `TCP_CLOSE`, inside `tcp_rcv_state_process`. |
| 00:01:54.593708 | Proxy reports a TCP establishment deadline, `SO_ERROR=0`. |

The packet record and the kernel record are pointing at the same operation. `SO_ERROR=0` only means the kernel never handed back a socket error. The [Linux 6.8 receive path](https://github.com/torvalds/linux/blob/v6.8/net/ipv4/tcp_input.c#L6203-L6215) straight up discards packets delivered to a socket sitting in `TCP_CLOSE` lol.

[Matched evidence](evidence/diagnostics/handshake-light/summary.json) · [Packet excerpt](evidence/diagnostics/handshake-light/failure-packet-excerpt.txt) · [Selected packet capture](evidence/diagnostics/handshake-light/failure-tuple.pcap)

I never traced the exact ordering of the socket lookup and the close. So this pins down the immediate kernel drop path and that's as far as it goes. It isn't me claiming there's an upstream kernel bug, which heh you know, there probably is.

## Investigation and decision

I had three explanations to pull apart: a proxy that missed a handshake which had actually completed, a backend that stopped accepting, and a kernel handshake failure under local churn. An early SYN-only capture showed me a SYN nobody answered, but it couldn't really tell me what became of it. Narrow kernel probes picked up LAST_ACK challenges on other connections. Those weren't matches, and I wasn't going to just accept them as the cause.

Full packet capture with TIME_WAIT probes gave me five clean trials, 4,580,678 attempts in total, and dragged throughput down to about 15,000 requests a second. So I pulled the high-frequency probes back out and put the original alternating direct/proxy workload back in. The lighter capture got the original traffic rate back and caught the matching `TCP_CLOSE` drop. 

I left the 500 ms timeout alone, left the kernel settings alone, as well i left the initial-connect fallback behaviour alone. The full-backlog fault I already had gives me a repeatable unanswered-handshake case, so its regression now checks the TCP stage, the application deadline, the zero pending socket error, a successful fallback with the right identity and payload, and bounded latency. The updated short P01 through P10 suite passed, with fallback finishing inside 567 ms on that run. What that regression actually checks is how the proxy reacts to a stalled handshake.

Customer update: high-churn local traffic can definitely lose a fresh connection before the application ever accepts it. The proxy ends that attempt when its deadline hits and moves on to another configured target if there is one. With only one target, the client sees a failure. Existing streams never get replayed, and every failure I observed is still sitting in the evidence of course.

Handoff: have a look at the matched tuple, the deadline contract, and the measurement errors I've disclosed. The [release record](evidence/release.json) links the results and the checksummed raw archives. The big captures stay local and on `infra-lab`, and the private GitHub repo carries the small proof package.