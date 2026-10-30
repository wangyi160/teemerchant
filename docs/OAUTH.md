<a id="merchant-service-oauth"></a>

# 商户服务 OAuth

**简体中文** | [English](OAUTH.en.md)

授权和续期在商户 HTTP 服务中执行。提供商端点与公开 CLI client ID 定义于 `oauth_service.py`，沿用此前审阅的 Codex、Claude Code、Grok CLI 授权参数。浏览器表单提供 OAuth / JSON 两个切换页；OAuth 只填充草稿，测试连接成功后才保存。

OpenAI 使用 Codex 设备码授权：通过账号代理向 `/api/accounts/deviceauth/usercode` 申请设备码，用户在 `https://auth.openai.com/codex/device` 输入设备码并授权。商户服务按官方 interval（至少 5 秒）轮询 `/api/accounts/deviceauth/token`，403/404 表示等待授权，最多等待 15 分钟。批准后使用官方返回的授权码和 PKCE verifier，向 `/oauth/token` 兑换凭证，redirect URI 为 `https://auth.openai.com/deviceauth/callback`。不监听 localhost 回调，不接收手动回调，不自动回退到浏览器 PKCE 登录。设备码登录需要在 ChatGPT 安全设置或工作区权限中启用。协议参考 [官方说明](https://learn.chatgpt.com/docs/auth#preferred-device-code-authentication-beta) 和 [Codex 官方实现](https://github.com/openai/codex/blob/main/codex-rs/login/src/device_code_auth.rs)。

Claude 和 Grok 仍使用独立的 PKCE S256 verifier 和 state，Grok 另含 nonce，十分钟过期。Grok 粘贴完整 localhost 回调 URL，须匹配官方注册的 redirect URI 和 state；Claude 支持官方返回的授权码或 code#state。所有授权会话均绑定本机浏览器会话、商户身份、Router 和账号池。设备码轮询不占用服务状态锁；取消、登录失效或配置变化后不再兑换或交付凭证，兑换请求不自动重放。错误、取消或过期清理服务端草稿；凭据结果只能取回一次，状态轮询不返回令牌。

设备码申请、授权轮询、授权码兑换和三家 refresh 请求使用账号配置的 HTTP(S)、SOCKS5h 或 VLESS 代理，HTTPS 校验开启，禁止重定向与环境代理，不做直连回退。错误信息不包含上游响应正文或 Token。

refresh token 只保存在商户加密账号库。续期前写入持久化意图，成功后先保存完整轮换凭据，再检查认证并向 Router 上传 access-only 投影。自动续期按“续期 → 检查新 Token → 上传 Router”记录进度，失败从对应步骤按 30、60、120、300 秒重试，共用四次重试额度；已成功步骤不重复执行。刷新响应丢失或重启发现未完成续期时，也按该策略重试，因此远端结果不明时仍存在再次刷新风险，不保证官方刷新幂等。全部失败后暂停账号，由商户决定手动重试或重新授权。手动刷新可立即继续失败步骤并重置额度；上传失败不会重新刷新已取得的 Token。

`account_auth.py` 用账号代理查询官方模型接口，只验证凭证是否被接受，不请求推理、不保存一份逐账号模型目录。新凭证、代理变更或手动重试触发检查；401 与 403、429、网络故障分别记录，错误正文和秘密不写入状态。`account_sync` 按部署记录暂停状态，绑定本地凭证和代理摘要；失败账号不参与后续自动上传或 Router 重启恢复，其余账号独立继续。

Router 协议能力字段为 `merchant.credential_protocol: access-token-only-v1`。不兼容的 Router 在上传账号资料之前被拒绝。完整配置与测试接口不发送 refresh token；增量更新使用 `PUT /merchant/accounts/access`，包含 `runtime_id`、账号别名、`expected_access_sha256`、`credential`。无变更重试不增加账号版本；不同实例、旧摘要覆盖与 refresh 字段被拒绝。

升级前应先让旧客户端保存 Router 内最新凭据，再停用旧程序与升级 Router。旧版 Router 可能已经轮换了 refresh token，不能直接用更早的本地副本启动续期。用户可以保留仅 access token 的账号，到期后手动授权。

测试覆盖设备码申请与轮询间隔、403/404 等待、错误和超时、授权期间取消/切换身份/退出、单次兑换与领取、设备码界面和测试后保存，以及本机会话/CSRF/Host 隔离、回调 state、PKCE、三家续期参数、轮换后的本地持久化、异常意图隔离、access-only 投影、受控增量同步。真实登录需要商户在浏览器完成，测试不冒充真实供应商授权。
