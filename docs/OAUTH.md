# 商户服务 OAuth

授权和续期在商户 HTTP 服务中执行。提供商端点与公开 CLI client ID 定义于 `oauth_service.py`，沿用此前审阅的 Codex、Claude Code、Grok CLI 授权参数。浏览器表单提供 OAuth / JSON 两个切换页；OAuth 只填充草稿，测试连接成功后才保存。

每次授权生成独立的 PKCE S256 verifier 和 state，Grok 另含 nonce。会话绑定 HTTP 登录会话、商户身份、Router 和账号池，十分钟过期。回调地址须匹配官方注册的 redirect URI，并校验 state。远程浏览器粘贴完整 localhost 回调 URL；Claude 支持官方返回的授权码或 code#state。错误、取消、退出登录或过期清理草稿；凭据结果只能取回一次，状态轮询不返回令牌。

授权码兑换和三家 refresh 请求使用账号配置的 HTTP(S)、SOCKS5h 或 VLESS 代理，HTTPS 校验开启，禁止重定向与环境代理，不做直连回退。错误信息不包含上游响应正文或 Token。

refresh token 只保存在商户加密账号库。续期前写入持久化意图，成功后先保存轮换后的完整凭据，再向 Router 上传 access-only 投影。崩溃遗留的 refreshing 状态不自动重放；明确拒绝或结果不明需要重新授权。已确认连接未建立的错误一分钟后重试。access token 上传失败不会重新触发 OAuth refresh。

Router 协议能力字段为 `merchant.credential_protocol: access-token-only-v1`。不兼容的 Router 在上传账号资料之前被拒绝。完整配置与测试接口不发送 refresh token；增量更新使用 `PUT /merchant/accounts/access`，包含 `runtime_id`、账号别名、`expected_access_sha256`、`credential`。无变更重试不增加账号版本；不同实例、旧摘要覆盖与 refresh 字段被拒绝。

升级前应先让旧客户端保存 Router 内最新凭据，再停用旧程序与升级 Router。旧版 Router 可能已经轮换了 refresh token，不能直接用更早的本地副本启动续期。用户可以保留仅 access token 的账号，到期后手动授权。

测试覆盖登录/CSRF/Host 隔离、回调 state、PKCE、单次结果领取、三家续期参数、轮换后的本地持久化、异常意图隔离、access-only 投影、受控增量同步。真实登录需要商户在浏览器完成，测试不冒充真实供应商授权。
