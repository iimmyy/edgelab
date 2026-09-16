# Backend connection deadlines under loopback churn

This is a lab investigation, not a production incident. Release 1 readiness is pending its resolution.

The first full measurement at `26f0877` recorded four failed proxied requests in 4,152,194 attempts; 3,042,279 direct requests completed without failure. Each failed request matched a proxy backend-establishment deadline near 500 ms. The client received a reset, so its timeout count was zero. P10 passed because it requires complete measurements, including failures; that did not establish release readiness.

I separated the possible causes: the proxy could miss a completed connection, the backend could stop accepting connections, or Linux could fail to complete a handshake under rapid local connection reuse. I added stage, local/remote endpoint, operating-system error and deadline-source fields before rerunning a bounded diagnostic. I kept the original timeout and kernel settings.

The diagnostic reproduced one failure in 925,851 attempts. At 20:39:05.959192 UTC on September 16, the proxy sent a SYN from `127.0.0.1:57684` to `127.0.0.1:35261`. No SYN-ACK appeared before its application deadline at 20:39:06.459034. `SO_ERROR` was zero: the kernel had not supplied an error. The client received a reset after 519.679 ms.

The packet capture covered this failure with no reported capture drops. Listen overflow/drop counters did not increase; the largest sampled accept queue was 63 of 4,096. The same endpoint tuple had completed handshakes repeatedly just beforehand. TIME_WAIT reached its 32,768 limit, and the trial added 136,338 TIME_WAIT overflows and two SYN challenges. These observations make TCP tuple reuse the leading explanation, but the SYN/RST-only capture cannot identify the exact kernel decision. A follow-up captures all TCP headers and relevant kernel drop reasons.

The diagnostic also includes a 15-second trial with 241,196 attempts and no failures. Its initial packet capture failed because tcpdump dropped privileges before opening its output; corrected capture began during the longer trial. That gap does not cover the reproduced failure, but prevents claiming complete packet coverage of both trials. The diagnostic used an instrumented binary with asynchronous logging, so it is not a controlled performance comparison with the first full measurement.

Customer update: a small number of fresh connections failed in a high-churn local test. Existing-stream replay is disabled. Failure counts remain in the evidence; I have not changed timeouts to make the result disappear.

Handoff: raw evidence is on `infra-lab` under `/home/ubuntu/edgelab/.run/release-1`, `connect-diagnostic`, and `connect-diagnostic-headers`. Correlate the next failure's full TCP exchange with kernel drop events before assigning a cause. Keep the measurement gate and release-readiness decision separate.
