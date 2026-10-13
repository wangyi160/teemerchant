# TEE Merchant

Windows 上运行的独立商户工作台。左侧导航包括账号与模型、客户授权、费率与限速、调用记录、拒答记录和连接与身份。界面由本机 HTTP 服务提供，在浏览器打开；用户代理 teeproxy 仍是独立的 Windows 原生程序。

## 启动

安装 Python 3.12+ 和包含 `merchant-transport` 的 teeproxy，然后运行：

```powershell
python app.py
```

工作台默认打开 `http://127.0.0.1:8910`。仅监听本机，检查 Host、Origin 和页面 CSRF token。可用 `--port` 改端口、`--data` 指定数据目录、`--no-browser` 不打开窗口。运行时无第三方 Python 依赖。

1. 在“连接与身份”填 Router HTTPS 地址、teeproxy.exe 路径和自己审核的 TDX 或 SNP 策略文件路径。
2. 复制公开登记信息给平台管理员。平台登记商户和允许接入的用户；不要发送私钥或上游凭证给平台管理员。
3. 点击“验证 TEE”和“连接商户”，再添加官方账号、默认客户权限和账号池倍率。

“客户授权”只显示实际调用过本商户模型账号的用户，不显示平台预授权名单。首次调用须已获平台授权，并受默认客户权限及限额约束；调用后可单独调整权限、限速或停用。查询模型列表不会新增客户。已使用关系独立保存，归档和清空调用队列不会令客户消失。
4. 用户沿用平台 API key，选择 `/v1/models` 返回的商户模型 ID；本商户界面不签发用户平台 key。

全部上游账号和账号池归属商户，平台没有自营池。每个池只需设置一个倍率，例如 0.2 表示官方价格的 20%，1 表示官方价格。输入、输出、缓存和长上下文费率均乘以此倍率；平台统一维护官方基准价格，调用开始时保存基准价、倍率及版本。Workbuddy 使用测试基准。缺少倍率或模型基准价时拒绝调用；旧费率仅在可精确还原为全池统一倍率时自动迁移。

所有商户 API 通过 teeproxy 私有 stdin/stdout 管道执行。客户端验证证明、启动测量和 TLS 绑定后，才在同一连接发送凭证；不会信任平台返回的“验证通过”字样。验证失败不发送商户数据。正式部署须使用独立审核的信任策略，不能只从服务器下载策略后直接信任。

API key 直接填入；OAuth 导入使用 router 账号格式（`provider`、`credential`），不是任意第三方客户端的凭证文件格式。每个账号须配置网络出口代理，可在代理栏或 OAuth JSON 的 `proxy` 中填写 `http://`、`https://` 或 `socks5h://` 地址；仅 Workbuddy 测试账号允许 `direct://`。代理地址是账号的网络出口，模型服务地址仍由 router 固定为官方域名。OpenAI OAuth 使用 `provider: openai`，由账号 credential 决定官方登录方式。支持 OpenAI、Anthropic、xAI；Workbuddy 需要平台单独启用测试权限。

身份、连接设置和归档正文用当前 Windows 用户的 DPAPI 加密，默认保存在 `%LOCALAPPDATA%/teemerchant`。SQLite 只在事务提交后确认 router 用量 / 拒答前缀。每 10 秒同步一次；写盘失败不确认删除，重复记录不重复保存，记录内容变化则停止同步。界面显示最近 200 条，数据库保存完整归档。

## 账号管理与批量导入

“账号与模型”先展示账号池、账号数量与倍率。点击账号池可筛选账号及模型目录。账号行显示启用情况、重新授权要求或目录状态；“目录已就绪”不代表额度、价格或所有模型调用均已验证。

添加、编辑和批量导入的“保存并提交 / 确认导入”会立即通过 TEE 通道更新 Router，成功后显示配置版本。无需再点击“同步”；调用记录和拒答记录页面的“同步”仅用于本地归档。连接失败或版本冲突时不会显示保存成功，也不会自动重发写入；版本冲突须刷新后重新编辑。

点击账号的“编辑”可调整所属池、并发、RPM、启用状态，更换凭证或出口代理。凭证和代理不回显，留空保留 Router 中的原值；账号 ID、服务和凭证类型固定。更换 OAuth 凭证时提交完整凭证对象，避免把不同身份的令牌拼接。共享凭证涉及多个部署时，界面会阻止只针对其中一个部署的凭证轮换及共享出口、限额变更。移动账号不会删除原池、原费率或历史记录。

“批量导入”支持粘贴 JSON 数组或选择多个 JSON 文件。每个文件可包含一个账号对象或账号数组；每个账号必须有独立的 `id`（对外别名）。默认服务、账号池和出口代理可统一填写，单个账号的字段优先。示例：

```json
[
  {
    "id": "openai-001",
    "pool": "openai",
    "provider": "openai",
    "proxy": "socks5h://proxy.example:1080",
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
    "provider": "openai",
    "proxy": "socks5h://proxy.example:1080",
    "api_key": "REPLACE_API_KEY"
  }
]
```

每项还可填写 `max_concurrent`（默认 16）、`rpm`（默认 0，不限）和 `enabled`（默认 true）。不能同时提供 `api_key` 和 `credential`。OpenAI 官方 `credential.account_id` 须保持真实值，与展示用的 `id` 独立。Codex `auth.json` 等其他客户端格式需要先转换。

先点击“校验并预览”，再确认一次提交。预览不显示令牌和代理密码；内容修改后需要重新校验。重复 ID、已有账号或不匹配的账号池会阻止提交，不会悄悄覆盖账号。每批最多 4096 项，文件总大小最多 12 MB。已有账号池沿用原倍率，新建池使用界面明确填写的倍率（默认 1×）。可在导入窗口下载占位示例，真实凭证只放在自己的本机导入文件中。

当前身份绑定 Windows 用户，本版尚无跨设备密钥迁移界面。保护 Windows 用户配置与 DPAPI 恢复材料；丢失身份会无法解锁商户恢复胶囊。商户费率不是签名金融凭证，记录用于外部账务与审计。更多边界和 API 见 [teerouter 多商户文档](https://github.com/wangyi160/teerouter/blob/main/docs/MERCHANTS.md)。

## 验证

```powershell
python -m unittest -v
node --check static/app.js
node --check static/account-ui.js
node --test test_accounts.cjs
```

测试包括 DPAPI 持久化、归档先于确认、重复归档、写盘失败、身份与版本回退拒绝。TEE / TLS 传输测试位于 teeproxy；多商户权限、统一 key、价格、备份和隔离测试位于 teerouter。测试使用受控上游，不代表真实账号或生产审计。

账号配置测试覆盖批量校验、凭证保留及轮换、旧倍率保留、账号移动、共享凭证与别名冲突。可选浏览器测试 `python -m unittest -v test_browser` 需要 Playwright 和 Microsoft Edge，使用独立临时目录及模拟 Router，不修改真实商户账号。
