# teemerchant

[简体中文](README.md) | **English**

A merchant-operated local management application for Windows and Linux, licensed under MIT. Use the local browser to manage account pools, OAuth authorization and customers, and query platform revenue/records. LAN access is not supported. No Electron is required; [teeproxy](https://github.com/wangyi160/teeproxy) remains the separate user proxy.

<a id="解压和启动"></a>

## Extract and start

**Register new ID (注册新 ID)** generates an Ed25519 key pair locally and registers only the public key, without a username/password. Identities live in the adjacent `data/identities/` directory; any number of local IDs can be retained. Selecting an ID authenticates and connects automatically. Logout ends the session without deleting identity. Migration preserves balances, pools and history. The platform receives only public keys and single-use challenge signatures, never private keys. Temporary outages preserve identities for retry after recovery; private data is hidden until verification. Preserve the entire `data/` directory during upgrades.

Download a Windows x64 ZIP or Linux x64 tar.gz from [Releases](https://github.com/wangyi160/teemerchant/releases). Run `teemerchant.exe` on Windows or `./teemerchant` on Linux and open `http://127.0.0.1:8910`. Linux requires glibc 2.36 or later. Packages include Python, native TEE libraries and VLESS components; no separate Python, Node.js or teeproxy installation is needed.

Windows opens a native service window showing local service status and **Open management page (打开管理页面)** / **Open data directory (打开数据目录)** buttons, without requiring login to that window. Minimize/close hides to the tray and keeps the service running. Click the tray icon to restore it; the menu opens the page or exits. **Exit service (退出服务)** waits for active requests and token renewal to finish. Starting the same data directory again restores the existing window instead of starting another renewal worker. Window status describes the local HTTP service; Router/account status appears in the browser.

`teemerchant.exe --headless` disables the Windows window/tray while retaining HTTP service. Linux uses this mode by default. All releases run after extraction without installation.

The service listens only on `127.0.0.1` or `::1`. Opening its local page creates a browser session without a local administrator username/password. APIs retain session, Host, Origin and CSRF checks; cross-site pages cannot automatically acquire sessions or call credential APIs. Local-ID login manages Router admission credentials automatically; no manual Merchant ID / Secret entry is needed. Legacy HTTP-password files are ignored.

To change the local port:

```sh
./teemerchant --port 8911
```

On Windows, use `teemerchant.exe --port 8911`. Alternatively copy `service.example.json` to adjacent `service.json` for automatic loading. `--config` selects another file; CLI options override its corresponding fields. Relative data/certificate paths resolve against the configuration directory. Old `0.0.0.0`, `::`, LAN bind addresses or remote `public_urls` are rejected; migrate to loopback and remove remote-address settings. Optional `--tls-cert` and `--tls-key` enable local HTTPS.

Configuration and accounts default to adjacent `data/`, overridable with `--data`. Preserve the full directory while replacing program files. The application locks the directory to prevent concurrent renewal workers. Windows retains DPAPI compatibility with the former desktop release after that process exits. Linux uses AES-GCM with `data/.vault-key`, readable only by the service user. Backups must include the key; Windows DPAPI data cannot be directly decrypted on Linux.

**Account and connection (账号与连接)** provides ID registration/selection, Router connection and platform address settings. Merchants are admitted automatically after registration; the platform registers Router access and the client manages admission credentials. Expired sessions reauthenticate through the local key; explicit logout preserves the ID. Unverified identities or identities without the merchant's permission cannot view cached pools, credentials, customers, revenue, withdrawals or receipts.

Merchant login, pools, revenue and Router registration consistently use `m_sig_` plus public-key SHA-256; customers use `u_sig_` IDs. Migration preserves existing private keys, independent control keys and upstream credentials. Backups of migrated encrypted profiles are under `data/identity-migration-backup/`; historical signed receipts retain their original bytes.

Revenue details query platform bills directly. Withdrawals query available revenue, fee tiers and application status, supporting USDT network/address/amount entry and cancellation of pending applications. **Consumption receipts (消费凭据)** provides pagination and on-demand signed-evidence downloads. The platform persists accounting and handles manual payouts. Independent merchant control keys and OAuth refresh tokens remain only in the merchant service; the platform cannot obtain account credentials. Existing identities/accounts remain usable without re-registration. Upload an independently reviewed TEE policy before connecting; platform login does not replace attestation.

**Revenue (收入)** combines summaries, available funds, applications, payout history and bill details; there is no separate sidebar withdrawal page. Refresh, filtering and pagination preserve unsubmitted withdrawal forms.

Entering revenue, receipts, calls or refusals loads the current platform page immediately, including returning to a recently visited page, without waiting for the one-minute refresh interval or an earlier slow request. Visible pages refresh each minute while preserving pagination and unsubmitted addresses/amounts. Submitting/cancelling a withdrawal refreshes immediately. There is no synchronization button, bill-download job or local financial/signature replica. Closing the browser stops these queries. Network failures display errors and may temporarily retain the last results; logout, identity switches or page closure clear transient data, and old requests cannot overwrite a newer page. Legacy archives remain untouched and are no longer read/updated; manually downloaded receipt JSON can be retained. All page queries, including overview, pools, catalogs, health and customers, use the platform's sanitized Router snapshots, normally saved every five minutes. Successful configuration changes update snapshots immediately; older snapshots cannot overwrite newer locally confirmed revisions.

With Router address and policies configured, login, identity switching and saving connection settings automatically attest and connect, displaying connecting/connected/failed status. No separate verification/connect button is needed. The running service connects, renews tokens and synchronizes access tokens independently of the browser. Stopping the service stops renewal; Router-held access tokens may last until expiry. After Router restart, the platform restores users and the merchant service restores account configuration.

**Customer authorization (客户授权)** lists users who actually called this merchant's accounts, not the platform preauthorization list. A first call requires platform authorization and default customer permissions/limits. Afterwards permissions, limits or enabled state can be adjusted individually. Catalog reads do not create customer relationships. Previously used relationships persist separately, surviving record archival or queue cleanup.

Default customer permissions can set a refusal threshold, with 0 meaning unlimited. Individual customers inherit or override it. Historical refusals accumulate per user/merchant; exceeding the threshold blocks all that user's keys at this merchant, without affecting others. Only explicit upstream refusals count, not network failures or ordinary rate limits. The list shows counts, thresholds and automatic blocking. **Reset and unblock (清零并解除限制)** updates the baseline and enables the customer, preserving historical bills and other permissions/limits. Refusals added after reading the count remain. Concurrent requests already forwarded may finish.

Account/connection does not expose admission keys. **Register new ID**, **Local ID** and **Use this ID** manage saved identities; switching reauthenticates and reads the corresponding profile. Logout, session expiry and switching clear page caches, close sensitive dialogs and discard stale responses. Browser sessions are isolated; a saved profile identifier does not bypass platform challenge authentication. Status refresh preserves connection fields being edited for the same identity. Legacy `/account`, `/account/login` and `/account/register` paths redirect to the unified page.

Existing merchants retain their original `data/` files without overwriting account stores. New profiles use `data/merchants/<Merchant ID>/`, each with encrypted settings, accounts and policies; historical archive files remain but are no longer updated. `data/active-merchant.json` identifies the service's active profile. A new profile is fully persisted before atomic switching; restart restores the last selection. Preserve all `data/` during backup, moves and upgrades. Besides browser-cookie, same-origin and CSRF checks, management APIs require the browser's platform session to own the active merchant. `POST /account-api/select` uses `account_id` and accepts only identities authenticated in that browser; legacy credential APIs or direct saved-profile IDs cannot bypass login. Browser logout preserves encrypted data and the service's active background work; exit the program to stop it.

Users call through TeeProxy local identities and request signatures, using the local TeeProxy key in their Agent and a model ID from `/v1/models`. This workbench does not issue user platform keys.

All upstream accounts/pools belong to merchants; the platform has no proprietary pool. Empty pools remain local. Adding the first account publishes the pool; removing/moving the last withdraws it while preserving local name, multiplier and allowlist. Disabling is not deleting. An unpublished local pool can be tested without creating a public pool or persisting credentials. Each pool has one multiplier, e.g. 0.2 means 20% of the official baseline and 1 means 100%. Input/output/cache/long-context rates all use it. The platform maintains baseline prices; admission freezes price, multiplier and revision. Missing pricing rejects calls. Old rates migrate only if exactly representable by one pool-wide multiplier.

Merchant APIs are implemented by the service's in-process native module in `native/`; teeproxy is neither bundled nor launched. Hardware evidence, challenge, boot measurements and TLS binding must pass before merchant credentials are sent on that same connection. Failure sends no merchant data. A lost write response does not trigger automatic credential resubmission. Use independently reviewed policies, not unreviewed policies downloaded from a server. Upgrades remove obsolete verifier-program path settings while preserving profiles/accounts.

Credentials use JSON with `provider` and `credential`; OAuth objects contain `access_token` and related fields. Single-account addition requires an egress proxy: `http://`, `https://`, `socks5h://` or VLESS, never `direct://`. This is the account's network exit, while Router fixes official model-service domains. OpenAI OAuth uses `provider: openai`, with the credential determining the official login method. Supported providers are OpenAI, Anthropic and xAI.

Identities, connections and accounts use Windows DPAPI or Linux AES-GCM in `data/`. `accounts.dpapi` contains credentials, proxies, pool settings and pending synchronization, without plaintext credentials on disk. Calls, refusals, revenue, withdrawals and consumption receipts are queried page by page from the platform, without a local accounting replica. Legacy archives remain untouched and are no longer page-data sources.

<a id="账号管理与批量导入"></a>

## Account management and bulk import

<a id="官方-oauth-绑定"></a>

### Official OAuth authorization

Select a pool, then **Add account (添加账号)**. The dialog's pool is fixed and read-only; the all-accounts view cannot add a single account. **OAuth authorization (OAuth 授权)** and **JSON import (JSON 导入)** are separate tabs retaining independent drafts; saving uses only the active tab. Enter the account proxy before authorization. ChatGPT uses **Device-code authorization (设备码授权)**: copy the code, enter it on the official page and authorize. The service retrieves the result without a callback URL. Device-code login must be enabled in ChatGPT security/workspace settings. Grok requires the complete browser callback URL; Claude requires the official authorization code. On success, access token, optional refresh token, expiry and other fields populate the editable OAuth draft without saving it. After **Test connection (测试连接)** succeeds, **Save and synchronize (保存并同步)** first writes the encrypted local store, then sends access credentials through the attested channel. Closing/cancelling discards unsaved edits. Offline Router status indicates pending synchronization.

Existing OAuth accounts can **Reauthorize (重新授权)** while retaining account ID, pool, multiplier, allowlist and enabled status. Saving a refresh token is optional and initially follows the existing credential's choice. Without one, reauthorize manually after expiry. With one, the merchant service renews through that account's proxy for all three providers.

The authorization page uses the browser's network. Code exchange and renewal use the account proxy in the merchant service, including HTTP(S), SOCKS5h and VLESS, without direct fallback. Each authorization obtains independent tokens, without reading other CLIs' login caches. Cancellation, timeout or logout clears unsaved credentials. Manual and bulk imports remain available. See [OAuth](docs/OAUTH.en.md) for flows, references and tests.

Before saving a single account, the service reloads the latest local configuration to preserve unrelated account/multiplier/allowlist changes made during authorization. A local revision conflict can be reconciled and retried once. Changes to the same account's configuration, credentials or proxy stop submission while preserving the dialog and its authorization draft, avoiding forced reauthorization. Uncertain Router writes retain local pending state without automatically resending credentials.

<a id="账号池与模型"></a>

### Pools and models

After selecting a pool, use **Edit allowed models (编辑开放模型)** in the model catalog to edit its shared allowlist. The pool row's public-model action opens the same area. Choose all available models or select particular models, with search, select-filtered and clear actions, then save/synchronize. Cancel leaves configuration unchanged. Navigation preserves edits; saving checks revisions to avoid overwriting other changes.

In selected-model mode, newly discovered models do not automatically become public. Clearing the list pauses all models in that pool. Connection, refresh and minute-by-minute checks while connected inspect Router catalogs. After obtaining a complete current catalog, disappeared models are removed, persisted encrypted locally and synchronized through TEE. Failed, refreshing or expired catalogs preserve the allowlist. An emptied list stays closed, never becoming allow-all. Pending local changes or remote revision conflicts pause cleanup. Router refreshes upstream catalogs on its own schedule; the merchant normally reads cached catalogs, refreshing upstream additionally to confirm cleanup candidates. Manual catalog refresh also refreshes upstream.

User catalogs show only allowed models passing capability/authorization checks; calls still require pricing and other checks. Manually entering disallowed model IDs or choosing fixed/automatic routing cannot bypass allowlists. Account edits, imports and multiplier changes preserve pool allowlists; new/moved accounts inherit their destination pool's policy. Merchant catalogs merge by pool and retain nonpublic official models for selection.

The accounts/models page begins with pools, counts and multipliers. Pool selection filters accounts/models. Account rows distinguish enabled state, reauthorization requirements and catalog status. “Catalog ready” does not prove quota, pricing or every model call has been verified.

**Add pool (添加账号池)** takes a routing identifier, provider and actual multiplier. Router bills using the saved/synchronized multiplier. There is no workbench-wide total rate-limit setting; per-account/customer limits remain. Zero-account pools can exist locally but cannot serve models. Creation selects the pool, exposing add/import actions. Account forms require an existing pool and inherit its provider/multiplier; imports never create pools. **Edit pool (编辑账号池)** changes its multiplier, with identifier/provider fixed to preserve routing/permission references. Accounts move only to existing same-provider pools; emptied source pools remain locally.

<a id="收入与历史账单"></a>

### Revenue and historical bills

**Revenue** shows total, daily and monthly receivables in USD, pending-accounting request counts, pool totals and paginated bills. Filter by date/pool to inspect each request's actual multiplier, pricing revision, token components and amount. Platform queries are merchant-scoped, without Router access or deletion of platform records.

The platform calculates aggregates over all bills, independently of page size; the client does not recount or rebill them. API amounts are decimal strings of integer micro-USD, avoiding browser floating-point loss. Date aggregation uses Beijing time. Call/refusal records paginate by decreasing sequence, 50 per page; receipt details load only when opened/downloaded.

Revenue follows the platform's saved settlement status/amount, without client-side recalculation. Request-time billing metadata accompanies each bill; later manual settlement appears on the next query. Unfinished billing or missing usage remains **Pending accounting**, not free. Receivables are not actual payouts; withdrawal views show confirmed balances and payment status. Official baseline amounts are not the merchant's actual acquisition cost.

Configure Router and platform addresses separately. Platform requests carry only platform-issued merchant admission credentials, never the independent control key, OAuth tokens or account proxies. Remote platforms require HTTPS; local platforms may use loopback HTTP. Redirects are prohibited. Financial queries and Router token synchronization are independent; query failures do not stop renewal.

Local `GET /api/revenue` forwards `start`, `end` (inclusive), `pool` and `offset` filters to the platform, with 50 detail rows per page and platform-wide aggregates. `GET /api/revenue/record?instance=platform&seq=...` retrieves a record's metadata, subject to local same-origin/CSRF checks.

The local account store owns editable account data; Router uses a synchronized runtime copy. **Save and synchronize / Confirm import** first persists data and pending changes together in the encrypted store, then submits through TEE. Local write failure prevents remote updates. Router unavailability, timeouts or revision conflicts retain local changes and explicitly show **Saved locally · Awaiting synchronization (本地已保存 · 等待同步)**, without claiming success or automatically resending credentials. Use the account-configuration synchronization action to retry; reconcile revision conflicts by refreshing, reviewing and saving again.

After first connection, local merchant configuration is cached per Router address. Restart/offline operation can manage pools, inspect accounts or change nonconnection settings for later synchronization. Remote refresh does not discard local accounts, and stale edit dialogs cannot overwrite newer local revisions.

Single-account addition finishes with **Test connection**. Given JSON/OAuth credentials and proxy, the attested Router reads the official catalog and sends a short request to an allowed text model. Success shows model and latency and enables saving. Changes to credentials, proxy, pool or a delay over five minutes require retesting. Failures distinguish credential, network, rate-limit and response errors. Tests neither save accounts, refresh tokens nor create user consumption, but the upstream may charge a small actual-call cost. New accounts/connection changes require online testing; existing nonconnection edits can be saved offline. Bulk import validates formats without individual online inference tests.

**Edit** loads and displays credentials/proxy from the local store, without asking Router for plaintext. Pool, concurrency, RPM, enabled state and credentials are editable; account ID, provider and credential type are fixed. Unchanged credentials/proxy are not resent; blank fields retain existing values. OAuth replacements submit complete credential objects rather than combining tokens from different identities. Shared credentials across deployments prevent single-deployment credential rotation and shared-egress/limit changes. Moving accounts preserves old pools, rates and history.

Router's `access-token-only-v1` rejects refresh tokens, including empty or nested Claude fields. The merchant retains full credentials; import, recovery, tests and synchronization project only `access_token`, `expires_at` and optional `account_id`. The platform, Router and user proxy never receive refresh tokens.

The service checks accounts every ten seconds and renews through their proxies when expiry is within five minutes. It persists intent before the request, then stores new access/refresh tokens before uploading access tokens individually through the merchant's TEE channel. Runtime ID and previous-access digest prevent stale overwrites. Disabled accounts stop automatic renewal.

**Last access-token synchronization (最近 Access Token 同步)** records local time, to the second, when Router acknowledges the token, displayed in the browser time zone. Automatic renewal, manual import and recovery record actual successes; authentication checks, catalog refresh, token-free configuration saves and failures do not update it. The timestamp is encrypted with the account store. Failures preserve the last success and separately mark unsynchronized new tokens. Older versions without timestamps perform one confirmed upload of the current token to establish a record, never fabricate a historical time; unchanged tokens are not subsequently resent.

Before synchronizing new/manually changed production accounts, the service checks authentication through the account's own proxy using an official read-only endpoint. This is not an inference request and does not replace Router's shared provider catalog. Success is cached until credential/proxy changes, rather than repeating every ten seconds. Bulk imports check accounts independently; one failure does not block healthy accounts. Disabled credentials, expired tokens, official authentication failures and unavailable catalogs have distinct states.

Automatic renewal persists progress across **refresh → check new token → upload Router**. Failures resume the failed step after 30, 60, 120 and 300 seconds, observed by the approximately ten-second worker cycle. The entire cycle shares four retries; exhaustion pauses the account. Successfully saved new tokens are not refreshed again and completed authentication checks are not repeated. Uploads use Router's idempotency support. Retry waits retain existing Router credentials while other accounts continue. Failed step, attempts and deadlines are encrypted and survive restart without resetting the budget. Lost refresh responses or restart during renewal also retry under this schedule; after exhaustion, the merchant decides whether to reauthorize.

Authentication/upload failures following new or manual account changes still persist the error and pause the account. HTTP 401/403/429, proxy failures and timeouts are distinguished. Paused accounts are excluded from later configuration sync/recovery and corresponding published deployments are withdrawn, retaining local credentials/pool configuration. Credential/proxy changes trigger rechecks. **Manual refresh (手动刷新)** actively refreshes healthy tokens, or immediately resumes the failed step and resets the four-retry budget while failed/waiting. Offline changes never attempted before are submitted automatically after reconnection.

Each account's **Error history (错误记录)** retains its latest 50 failures: time, step, attempt count, category, HTTP status, known OAuth error code such as `invalid_grant`, disposition and retry time. History survives successful recovery in the encrypted account store, excluding tokens, proxy passwords and raw upstream responses.

JSON may contain only an access token, which the merchant replaces manually; Router rejects it after expiry. JWT `exp` supplies expiry when not explicitly given. Opaque tokens need `expires_at` to schedule renewal in advance.

**Bulk import (批量导入)** accepts pasted JSON arrays or multiple JSON files, each holding one account or an array. Every account needs its own `id` alias, explicit `proxy`, and OAuth `credential` or `api_key`. Proxy and credentials are bound per object; there is no default proxy. Omitted `pool` uses the selected default, while supplied pools must already exist. Optional `provider` must match the pool. First create an `openai`-provider pool named `openai` for this example:

```json
[
  {
    "id": "openai-001",
    "pool": "openai",
    "proxy": {
      "scheme": "socks5h",
      "host": "proxy-1.example",
      "port": 1080,
      "username": "REPLACE_PROXY_USER_1",
      "password": "REPLACE_PROXY_PASSWORD_1"
    },
    "credential": {
      "access_token": "REPLACE_ACCESS_TOKEN",
      "refresh_token": "REPLACE_REFRESH_TOKEN",
      "id_token": "REPLACE_ID_TOKEN",
      "account_id": "REPLACE_OFFICIAL_ACCOUNT_ID"
    }
  },
  {
    "id": "openai-002",
    "pool": "openai",
    "proxy": {
      "scheme": "http",
      "host": "proxy-2.example",
      "port": 8080,
      "username": "REPLACE_PROXY_USER_2",
      "password": "REPLACE_PROXY_PASSWORD_2"
    },
    "credential": {
      "access_token": "REPLACE_ACCESS_TOKEN_2",
      "refresh_token": "REPLACE_REFRESH_TOKEN_2",
      "expires_at": 2000000000
    }
  }
]
```

Proxy-object `scheme` accepts `http`, `https` and `socks5h`. `host` is a hostname/IP only, with brackets for IPv6; `port` is an integer 1–65535. Omit username/password without authentication; otherwise enter their raw values and the program encodes characters such as `@`, `:` and `/`. Per-account URL notation also works, e.g. `"proxy": "socks5h://user:password@proxy.example:1080"`, with special characters percent-encoded by the caller. `direct://` is prohibited. Multiple accounts may explicitly share a proxy; missing values are not filled automatically.

For VLESS, paste the share link into the proxy field or each JSON `proxy`, for example with this dummy UUID:

```json
{"proxy":"vless://12345678-1234-1234-1234-123456789abc@node.example:443?encryption=none&security=tls&type=xhttp&sni=node.example&path=%2Ftunnel&mode=stream-one"}
```

TLS + TCP / XHTTP are supported; XHTTP modes are auto, stream-one, stream-up and packet-up. Valid node certificates are mandatory. REALITY, WS, gRPC, XHTTP extra and unknown parameters are rejected. UUID, paths and parameters are used only in local editing and attested synchronization and are hidden from import previews. Router uses the node for models/catalogs; the service's bundled VLESS component uses the same account proxy for code exchange/renewal. Failure never falls back to direct access.

Optional per-account fields are `max_concurrent` (default 16), `rpm` (default 0/unlimited) and `enabled` (default true). `api_key` and `credential` are mutually exclusive. OpenAI's real `credential.account_id` must remain distinct from the display alias `id`. Other-client formats such as Codex `auth.json` require conversion first.

<a id="grok-原生-oauth-凭证"></a>

### Native Grok OAuth credentials

xai pools accept Grok's native OAuth cache JSON. Paste it directly into a single-account credential field with a separate alias/proxy, or use unified `{ "provider": "xai", "credential": {...} }`. For bulk import, put the entire native object in each account's `credential`:

```json
[
  {
    "id": "grok-001",
    "proxy": {"scheme": "socks5h", "host": "proxy-1.example", "port": 1080},
    "credential": {
      "https://auth.x.ai::b1a00492-073a-47ea-816f-4c329264a828": {
        "key": "REPLACE_ACCESS_TOKEN",
        "refresh_token": "REPLACE_REFRESH_TOKEN",
        "expires_at": "2030-01-01T00:00:00.000Z",
        "token_type": "Bearer"
      }
    }
  }
]
```

Select an existing xai pool; each account still needs its own `id` and `proxy`. Import converts `key` to `access_token` and ISO `expires_at` to Unix seconds, preserving refresh token, scope and token_type. Editing displays the normalized local object. The cache key identifies the issuer/OAuth client, not a merchant alias or forwarding destination. Only the listed entry matching Router's built-in configuration is supported; multiple entries or other authentication sources fail explicitly.

Expiry values are preserved without inventing new validity. Passing format checks only establishes importability; refresh/model access depends on the actual account and network exit. The merchant service renews xAI tokens; reauthorize or replace credentials in the editor when needed. Downloaded examples contain placeholders only. See [official Grok authentication documentation](https://github.com/xai-org/grok-build/blob/main/crates/codegen/xai-grok-pager/docs/user-guide/02-authentication.md).

<a id="校验与保存"></a>

### Validate and save

First **Validate and preview (校验并预览)**, then confirm local saving/synchronization. Preview lists each proxy's scheme, host and port while hiding authentication and model credentials. Missing/invalid proxies identify the offending account and reject the entire batch. Editing requires validation again. Duplicate IDs, existing accounts, absent pools or provider mismatches also block submission. Limits are 4096 items and 12 MB of files per batch. Imports inherit pool multipliers, set separately during pool creation/editing. Downloadable examples reflect the selected pool and include per-account proxy placeholders; real credentials belong only in local import files. Proxy objects become URLs stored encrypted with the account and synchronized to Router; account editing can inspect/change the URL.

Initial connection generates an independent merchant control key on the service host and saves it encrypted before binding. The platform receives only its digest. Windows data depends on the Windows user; Linux data depends on `.vault-key`. Merchant admission credentials allow enrollment/public configuration, not secret upload, retrieval or replacement by themselves. A new host must recover the control key and account store; an admission secret alone cannot take over existing private data.

Account creation, edits, disabling, deletion and token upload go directly to Router over the attested channel, with Router's response determining synchronization success. Afterwards the service notifies the platform to refresh public snapshots. Failed notification remains pending for later notification or the five-minute snapshot cycle; it does not undo account changes or stop renewal. The platform never receives account secrets or confirms private credential submission. The first nonempty pool publication may carry initial policy; existing multipliers, allowlists and customer limits use a separate platform control channel. Access-token rotation also goes directly through TEE; refresh tokens remain local.

Router never exports merchant-account backups to the platform. After restart, the platform restores users/registrations, while teemerchant attests Router, uploads local credentials and restores public configuration through the platform. Accounts cannot recover while the merchant is disconnected or lacks local data; the platform cannot fabricate credentials. Preserve all `data/` and the relevant OS decryption material. See the [Router merchant protocol](https://github.com/wangyi160/teerouter/blob/main/docs/MERCHANTS.en.md).

<a id="源码运行与构建"></a>

## Running and building from source

```sh
python -m pip install -r requirements.txt
cargo build --release --locked --manifest-path native/Cargo.toml
cd egress
go build -trimpath -mod=readonly -o ../teemerchant-egress .
cd ..
python service.py
```

The Windows egress component is `teemerchant-egress.exe`. To package on the target OS, install `requirements-build.txt` and run `python scripts/build_service.py`. Outputs under `release/` exclude merchant data, keys and deployment policies. Development checks: `python -m unittest test_app test_local_accounts test_credentials test_owner_control test_revenue test_service`, `node --test test_accounts.cjs`, and `cargo test --locked --manifest-path native/Cargo.toml`. Browser regression checks use `python -m unittest test_browser`, requiring Playwright and Edge.
