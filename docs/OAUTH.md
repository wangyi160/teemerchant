# Desktop OAuth binding

Open **添加账号** and choose **OAuth 授权** under **添加方式** for an existing
ChatGPT, Claude or Grok pool. The same form offers **JSON 导入** and **API key**;
switching methods preserves the shared account fields and unsubmitted JSON.
Existing OAuth accounts offer **重新授权**. Workbuddy continues to use an API key.
Choose a pool, an account alias and the account's egress proxy, then sign in in the
official site opened in the system browser. No provider password is entered into
teemerchant. Manual and batch JSON import remain available.

| Pool provider | Authorization | Callback | Renewal in Router |
| --- | --- | --- | --- |
| `openai` (ChatGPT / Codex) | Authorization code with PKCE S256 | `http://localhost:1455/auth/callback` | Supported when a refresh token is saved |
| `anthropic` (Claude / Claude Code) | Authorization code with PKCE S256 | Official hosted page; paste its code or `code#state` | External renewal: click **重新授权** after expiry |
| `xai` (Grok) | OIDC discovery + authorization code with PKCE S256 | `http://127.0.0.1:56121/callback` | Supported when a refresh token is saved |

After authorization, click **保存并同步**. Credentials go through the same local
Windows DPAPI encrypted account store and verified TEE synchronization as manual
imports. An unavailable Router leaves an explicit pending local change. OAuth
success alone does not mean an account is saved, enabled or usable for every model.
Reauthorization keeps its deployment ID, credential reference, enabled state,
pool, pool multiplier and model policy. It never creates a second deployment.

The refresh-token checkbox is explicit. New ChatGPT/Grok bindings default to
retaining it, and renewals default to the existing local credential's preference.
Claude defaults to access-only because the current Router does not refresh Claude
credentials. Selecting access-only replaces the full stored credential and removes
an old refresh token. Local credentials remain the last imported/bound version;
the Router's subsequently rotated tokens are not read back by this feature.

## Network and lifecycle

Authorization uses the desktop's system network/proxy configuration via a separate,
non-persistent Electron session. The official login page uses the system browser.
The per-account proxy is saved for **Router → official upstream** traffic; it is
not used for local OAuth login or token exchange. VLESS, SOCKS or HTTP proxy links
continue to use the existing per-account validation.

The main process generates public CLI client IDs, PKCE secrets, state and URLs.
Client IDs are not secrets. Only HTTPS official hosts are allowed for discovery
and token exchange; redirects, arbitrary endpoints and certificate bypasses are
not supported. Responses are bounded and response bodies are never surfaced as
errors. The renderer receives only authorization progress, the temporary browser
URL, expiry and refresh capability. It receives no OAuth tokens through this flow.

ChatGPT/Grok listeners bind only to loopback during an active login. The handler
checks method, path, Host, state and a single code, and consumes each code once.
If a port is occupied, close another pending CLI login and retry. A callback URL
may also be pasted as a fallback. Claude requires pasting the code from the
official callback page; this flow does not read another browser's cookies or
inspect the user's existing Codex/Claude/Grok credential caches.

One flow is active at a time; it expires after ten minutes. Cancel, reload,
configuration mutation and application exit dispose of transient credentials and
listeners. Saving checks the merchant identity, Router address, pool provider,
target account fingerprint and local/remote revisions. Writes are never replayed
automatically. The existing isolated renderer, IPC validation and TEE checks remain
in effect. Browser profile/session state belongs to the user's browser.

## References and verification

Provider routes and public CLI client metadata were checked against:

- [OpenAI Codex authentication](https://developers.openai.com/codex/auth)
- [Claude Code authentication](https://code.claude.com/docs/en/authentication)
- [LiteLLM ChatGPT provider](https://docs.litellm.ai/docs/providers/chatgpt)
- OmniRoute revision `411aead932f5dc3f51ea9e41262378f4ace31e25`:
  [Codex](https://github.com/diegosouzapw/OmniRoute/blob/411aead932f5dc3f51ea9e41262378f4ace31e25/src/lib/oauth/providers/codex.ts),
  [Claude](https://github.com/diegosouzapw/OmniRoute/blob/411aead932f5dc3f51ea9e41262378f4ace31e25/src/lib/oauth/providers/claude.ts),
  [Grok](https://github.com/diegosouzapw/OmniRoute/blob/411aead932f5dc3f51ea9e41262378f4ace31e25/src/lib/oauth/providers/xai-oauth.ts),
  [constants](https://github.com/diegosouzapw/OmniRoute/blob/411aead932f5dc3f51ea9e41262378f4ace31e25/src/lib/oauth/constants/oauth.ts).

These are CLI-compatible flows, not a provider stability guarantee for third-party
OAuth clients. Official login and token endpoints may change independently.

`npm test` exercises PKCE, state, duplicate callbacks, cancellation, expiry,
credential normalization, host restrictions and stale-target protection with mock
token endpoints. `npm run test:oauth-desktop` drives the packaged or development
Electron interface using isolated data, real loopback callbacks and mock provider
responses, including encrypted persistence and restart. Actual provider login and
consent require the account owner and are not replaced by these tests.
