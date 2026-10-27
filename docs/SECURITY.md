# Security

The authenticated platform issues a separate Router admission token. It is kept in the encrypted merchant profile, hidden from public local state, and sent only as `X-Router-Access` during attestation. Merchant control and account credentials are sent only after the same TLS connection has passed hardware and release-policy verification. The service renews admission before expiry; platform outages do not invalidate an unexpired token.

Merchant identities use local Ed25519 keys and IDs derived from their public keys. Registration sends only the public key; login signs a single-use challenge bound to the origin, role and purpose. Local IDs survive logout and session expiry. Private seeds are encrypted in `data/identities/` and are never returned to the browser or platform. Existing financial/profile identifiers remain internal so migration preserves account pools, withdrawals and revenue.
