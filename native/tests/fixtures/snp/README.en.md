<a id="公开验证夹具"></a>

# Public verification fixtures

[简体中文](README.md) | **English**

`test-*.bin` and `test-*.der` are public test vectors from Confidential Containers
Trustee at the exact commit in `sources.json`, licensed Apache-2.0.
`milan*.pem` / `milan*.crl` are public AMD endorsement material, downloaded from
the fixed AMD KDS URLs in that manifest. SHA-256 hashes record the imported bytes.

These are **not this project's Oracle measurements**, approved production releases,
or fresh evidence for a client connection. The VCEK report is version 2 (rejected by
our production verifier); the VLEK report is version 3 but VMPL 1, lacks enforced
InitData, and has a certificate that expired in December 2025. Its signatures are
checked separately and full certificate verification must reject it. Both exercise
real AMD signatures. Policy tests use explicitly synthetic
reports and never establish a business session.

Tests fix the collateral validation time to 2026-10-10 to keep archived fixtures
reproducible, and separately exercise expiry. Production always reads the client's
actual clock and fresh AMD collateral; it has no clock override or test trust root.
