# teemerchant

Windows Electron 商户桌面工作台，采用 MIT 开源许可。左侧导航包括账号与模型、客户授权、收入、调用记录、拒答记录和连接与身份。用户代理 [teeproxy](https://github.com/wangyi160/teeproxy) 仍是独立程序。

## 解压和使用

从 [Releases](https://github.com/wangyi160/teemerchant/releases) 下载 Windows x64 ZIP，完整解压到可写文件夹后运行 `teemerchant.exe`。只发布便携 ZIP，无需安装程序。程序内置管理引擎、Python 运行库及自身的 TEE 原生模块；使用者无需安装 Python、Node.js、teeproxy 或单独启动服务器。

窗口关闭后继续在系统托盘运行；点击托盘图标恢复窗口，通过托盘菜单的“退出”结束程序。已有商户启动时会使用保存的连接设置和信任策略尝试连接一次；失败后仍可离线管理本地账号，并在“连接与身份”中重试。待同步的账号修改不会因重新打开程序而自动重发。界面通过受限 IPC 和私有进程管道访问管理引擎，不启动 Web 管理服务器；仅在 ChatGPT/Grok 登录期间临时监听本机 OAuth 回调。Windows 程序尚未进行代码签名，发布页同时提供 SHA-256 校验文件。

配置、商户身份、TEE 策略、账号库、账单和界面缓存默认全部保存在 `teemerchant.exe` 旁的 `data/`，不依赖启动时的工作目录，也不自动读写旧版 AppData 商户目录。升级时先退出程序，保留并备份 `data/`，再用新 ZIP 覆盖程序文件。ZIP 不包含任何用户数据。移动程序时连同整个目录一起移动；删除目录会同时删除其中的数据。

从 0.1.3 及更早版本迁移时，先退出旧程序，将 `%LOCALAPPDATA%/teemerchant` 内的商户资料复制到新目录的 `data/`。启动新程序后重新选择已有的 TEE 策略并保存连接，策略会复制到 `data/policies/` 并以相对路径引用。确认身份、账号和账单完整后，再处理旧目录。程序对数据目录加锁，避免两个工作台同时写入；可用 `--data=绝对路径` 显式指定另一个数据目录。

1. 在“连接与身份”填写 Router HTTPS 地址，并选择自己审核的 TDX 或 SNP 策略文件。程序直接完成证明验证，无需选择其他程序。发行版支持 Intel TDX 和 AMD SEV-SNP。
2. 新商户先在 Router Portal 注册并等待管理员审核，注册无需 JSON。通过后，平台签发 Merchant ID 和 Merchant Secret 并同步至 Router。在 Portal「连接指南」显式查看、复制自己的接入凭证，填入工作台「连接与身份 → 商户接入凭证」。工作台不需要 Portal 登录名或密码。Secret 在本机由 Windows DPAPI 加密保存，只有 TEE 验证通过后才发送至 Router。平台使用 SQLCipher 加密保存签发凭证，Router 只接收身份摘要；上游账号凭证仍由商户本机管理。
3. 凭证保存后，点击“验证 TEE”和“连接商户”，再创建账号池、设置倍率和添加官方账号。“添加商户”可保存另一组平台凭证，也可以直接在“编辑凭证”中填写另一组。保存后切换到该商户；在“本机已保存的商户”下拉列表中可切回原商户，无需重新填写 Secret。每个商户的账号、凭证、待同步修改、账单、连接设置及已确认版本独立保存；切换不上传账号，也不转移 Router 上的资源或所有权。新商户沿用当前 Router 地址和已审核的 TEE 策略，切换后须点击“连接商户”认证。

“客户授权”只显示实际调用过本商户模型账号的用户，不显示平台预授权名单。首次调用须已获平台授权，并受默认客户权限及限额约束；调用后可单独调整权限、限速或停用。查询模型列表不会新增客户。已使用关系独立保存，归档和清空调用队列不会令客户消失。

“连接与身份”顶部展示已保存的 Merchant ID、遮挡的 Merchant Secret，以及是否正在使用这些凭证连接。ID 和 Secret 均可复制，Secret 可点击显示、隐藏；编辑会回填当前凭证。查看和复制从本机加密存储读取，不向 Router 请求，也不重新生成身份。普通状态查询不返回 Secret。新资料目录显示“尚未填写”，填写平台签发的凭证后才能连接商户。

原有商户继续使用 `data/` 中的文件，不迁移或覆盖原账号库。新增商户使用 `data/merchants/<Merchant ID>/`，各自保存加密设置、账号库、归档和策略；`data/active-merchant.json` 仅记录当前身份。新资料完整落盘后才原子切换当前身份，重启恢复最后选择。备份、移动或升级时保留整个 `data/`。本机接口 `POST /api/merchant-profile` 接收已保存的 `merchant_id`，受同样的 IPC 或同源、CSRF 保护。
4. 用户沿用平台 API key，选择 `/v1/models` 返回的商户模型 ID；本商户界面不签发用户平台 key。

全部上游账号和账号池归属商户，平台没有自营池。每个池只需设置一个倍率，例如 0.2 表示官方价格的 20%，1 表示官方价格。输入、输出、缓存和长上下文费率均乘以此倍率；平台统一维护官方基准价格，调用开始时保存基准价、倍率及版本。Workbuddy 使用测试基准。缺少倍率或模型基准价时拒绝调用；旧费率仅在可精确还原为全池统一倍率时自动迁移。

所有商户 API 由管理引擎在进程内调用自身的原生模块执行，模块源码位于 `native/`，不打包或启动 teeproxy。验证硬件证明、随机挑战、启动测量和 TLS 绑定后，才在同一条已验证连接发送商户凭证。验证失败不发送商户数据，写入请求丢失响应时不会自动重发。正式部署须使用独立审核的信任策略，不能只从服务器下载策略后直接信任。升级时自动移除旧版验证程序路径设置，账号与商户资料保持原样。

凭证统一使用 JSON（`provider`、`credential`）；OAuth 对象包含 `access_token` 等字段，API key 账号使用例如 `{ "provider": "workbuddy", "credential": { "api_key": "REPLACE_KEY" } }`，无需选择凭证类型。单个账号添加时须填写网络出口代理栏，支持 `http://`、`https://`、`socks5h://` 和 VLESS 地址；仅 Workbuddy 测试账号允许 `direct://`。代理地址是账号的网络出口，模型服务地址仍由 router 固定为官方域名。OpenAI OAuth 使用 `provider: openai`，由账号 credential 决定官方登录方式。支持 OpenAI、Anthropic、xAI；Workbuddy 需要平台单独启用测试权限。

身份、连接设置、账号资料和归档正文用当前 Windows 用户的 DPAPI 加密，默认保存在程序目录的 `data/`。账号库为 `accounts.dpapi`，包括凭证、代理、账号池配置和待同步修改；磁盘上不存明文凭证。SQLite 只在事务提交后确认 router 用量 / 拒答前缀。每 10 秒归档一次；写盘失败不确认删除，重复记录不重复保存，记录内容变化则停止同步。界面显示最近 200 条，数据库保存完整归档。

## 账号管理与批量导入

### 官方 OAuth 绑定

先在账号池列表中选中一个池，再点击 **添加账号**；添加窗口中的账号池固定为当前池，只读且不可切换。“全部账号”视图不提供单个账号的添加入口。桌面版将 **OAuth 授权** 与 **JSON 导入** 分为两个切换页，一次只显示一页，各自保留草稿，保存时只使用当前页的凭证。OAuth 页内点击独立的授权按钮，账号别名、代理和限额可稍后填写。ChatGPT/Grok 自动接收回调；Claude 登录后粘贴官方页面给出的授权码。授权成功后，access token、可选的 refresh token、到期时间等内容显示在 OAuth 页的凭证编辑框中，允许检查和修改；此时尚未写入账号。点击 **保存并同步** 才写入本地加密账号库，再通过已验证的 TEE 连接同步到 Router。取消或关闭窗口会丢弃未保存的编辑。Router 离线时显示等待同步。Workbuddy 只提供 JSON 导入。

已有 OAuth 账号可点击 **重新授权**，保留账号 ID、所属池、倍率、开放模型及启用状态。可选择是否保存 refresh token；重新授权默认沿用本地原凭证的选择，未保存时到期后手动重新授权。ChatGPT/Grok 保存 refresh token 后由 Router 续期；Claude 当前仍需手动重新授权，保存 refresh token 也不会自动续期。

授权登录使用本机浏览器和系统网络代理；表单中的账号代理供 Router 访问上游，支持现有的 HTTP(S)、SOCKS5h 和 VLESS 配置。每次绑定独立获取令牌，不读取其他 CLI 的本地登录缓存。取消、超时或退出会清理未保存的凭证。手动导入和批量导入继续保留。流程说明、参考实现和测试见 [OAuth 文档](docs/OAUTH.md)。

### 账号池与模型

选择账号池后，在下方“池内模型目录”点击“编辑开放模型”，直接在目录表中修改整池统一的开放名单，所有账号共用。账号池行的“对外模型”也会定位到此编辑区。可选择“开放全部可用模型”，或勾选指定模型（支持搜索、全选筛选结果、清空），再点“保存并同步”；取消编辑不会修改配置。页面内切换列表或导航保留当前编辑内容，保存时检查配置版本以防覆盖其他修改。

勾选模式下新发现的模型不会自动开放；清空表示暂停该池的全部模型服务。连接、刷新及已连接时每分钟检查 Router 目录：成功取得当前完整目录后，自动从开放名单删除已不存在的模型，先加密保存本地，再通过 TEE 通道同步 Router。目录获取失败、正在刷新或已过期时保留原名单；清理后为空仍表示全部关闭，不会变成开放全部。有待同步修改或远端版本冲突时暂停自动清理，避免覆盖编辑或重发凭证。Router 仍按自己的周期获取上游目录；桌面定期读取缓存，仅在发现待清理项时额外刷新上游确认，手动“更新目录”也会刷新上游。

Router 的用户目录只展示开放且通过能力、授权校验的模型；调用还需通过价格等校验。手工填写未开放的模型名、指定池或自动匹配均不能绕过名单。账号编辑、批量导入和倍率调整保留池级名单，新增或移入的账号继承目标池的名单。商户目录按池合并展示，保留未开放的官方模型供选择。

“账号与模型”先展示账号池、账号数量与倍率。点击账号池可筛选账号及模型目录。账号行显示启用情况、重新授权要求或目录状态；“目录已就绪”不代表额度、价格或所有模型调用均已验证。

在账号池列表点击“添加账号池”，填写池名称（路由标识）、服务类型及实际倍率。倍率统一在账号池中配置，Router 按账号池已保存并同步的实际倍率计费。商户工作台不再提供总限速设置；每个账号及客户的限额仍可单独调整。池可以独立存在且账号数量为 0；空池不提供模型服务。创建后自动选中该池，下方账号列表可“添加账号”或“批量导入”。账号表单只允许选择已有池，服务类型和倍率继承所属池；导入不会自动创建池。点击“编辑账号池”可调整全池倍率，池标识和服务类型固定，以保持既有路由和权限引用。移动账号只能选择同服务类型的已有池，原池即使变空也会保留。

### 收入与历史账单

“收入”页面显示累计、今日、本月应收（USD），待核算请求数、按账号池汇总和分页逐笔账单。可按日期范围和账号池筛选，查看请求当时的实际倍率、价格版本、token 计费项目及金额。“同步账单”通过已有 TEE 通道归档新记录，落盘成功后才确认清理 Router 队列。

统计读取全部本地加密归档，不受调用记录页最近 200 条的限制；内存索引增量更新，重启后由加密归档重建。相同请求跨 Router 重启重复归档只统计一次，内容冲突的记录转为待核算。金额以整数微美元计算，通过 API 用十进制字符串传递，避免浏览器浮点数损失精度。今日和本月按商户程序所在 Windows 的本地日期统计。

新记录采用 Router 保存的每次请求计费结果，并核对分项金额、报价版本、倍率和最终舍入。旧记录仅在完整的历史报价及用量存在时按原公式计算；不会用当前账号池倍率或最新官方报价重算旧账。计费未完成、失败、缺少用量或记录校验不通过的请求单列“待核算”，不计入金额，不能视为免费。应收不是实际到账；收款、提现和结算状态尚未接入。官方基准金额也不是商户实际采购成本。

本地接口 `GET /api/revenue` 支持 `start`、`end`（含结束日）、`pool`、`offset` 筛选，逐笔每页 50 条，概览始终统计全部本地归档。`GET /api/revenue/record?instance=...&seq=...` 读取原始账单，仍需本机同源和 CSRF 校验。

商户本地账号库负责保存和编辑账号资料，Router 使用同步过去的运行副本。“保存并同步 / 确认导入”先将资料和待同步修改一起加密落盘，再通过 TEE 通道提交 Router。本地写盘失败时不发出远端更新；Router 不可用、超时或版本冲突时，本地修改仍保留，页面明确显示“本地已保存 · 等待同步”，不会声称已同步成功，也不会自动重发凭证。可点击“同步账号配置”重试；版本冲突须刷新、核对配置后重新保存。调用和拒答记录页面的“同步”仍只用于归档。

首次连接后，本地会缓存该 Router 的商户配置。重启或离线时仍可管理账号池、查看已有账号或调整不涉及连接信息的设置，待连接后同步；不同 Router 地址使用隔离的本地记录。账号资料不会因刷新远端状态而丢失，过期的本地编辑窗口不能覆盖较新的本地修改。

单个账号添加的最后一步是 **测试连接**：完成 JSON 或 OAuth 凭证及代理后，由已验证的 Router 读取官方模型目录，并选择池内一个开放的文本模型发送短请求。成功显示模型和耗时后才能保存；修改凭证、代理、账号池或超过五分钟须重新测试。失败会显示凭证、网络、限流或模型响应错误，测试不会保存账号、刷新 refresh token 或计入用户消费；上游可能收取少量实际调用费用。新账号和连接信息的修改需要在线测试，不涉及连接信息的已有账号编辑仍可离线保存。批量导入继续执行格式校验，不提供逐个在线测试。

点击账号的“编辑”会直接从本地账号库读取并明文回填凭证和代理，不向 Router 请求凭证明文。可调整所属池、并发、RPM、启用状态和凭证；账号 ID、服务和凭证类型固定。凭证或代理未修改时不重复发送，留空仍表示保留原值。更换 OAuth 凭证时提交完整凭证对象，避免把不同身份的令牌拼接。共享凭证涉及多个部署时，界面会阻止只针对其中一个部署的凭证轮换及共享出口、限额变更。移动账号不会删除原池、原费率或历史记录。

ChatGPT/Grok OAuth 自动续期由 Router 执行，Claude 使用外部重新授权；本地保存最近导入、绑定或恢复的凭证版本，编辑其他字段不会把本地旧令牌覆盖到 Router。普通状态查询不包含凭证，账号编辑框按用户操作显示本地凭证或刚取得的 OAuth 凭证。旧版本未留存本地凭证的账号需要重新授权、从商户自己的加密备份恢复或补录，不能凭空回填。

也可只提供 `access_token`，省略 `refresh_token`，由商户手动更新。Router 不会自动刷新这类账号；到期后账号列表显示“已过期”，停止使用该账号。JWT 会读取自带 `exp`；其他令牌可提供 `expires_at`（Unix 秒），xai 也接受带时区的 ISO 时间。如果没有任何可读取的到期时间，只能在官方拒绝凭证后显示“需重新授权”。编辑已有账号时提交完整的 access-token-only 凭证对象，会替换原凭证并移除旧刷新令牌。已经过期的令牌不会因重新导入而延长有效期。

“批量导入”支持粘贴 JSON 数组或选择多个 JSON 文件。每个文件可包含一个账号对象或账号数组；每个账号必须有独立的 `id`（对外别名）、自己的 `proxy`，以及 `credential`（OAuth）或 `api_key`。代理和凭证放在同一个对象里，逐个绑定，不再提供默认代理。`pool` 可省略并使用界面选择的默认账号池；填写时必须是已有池。`provider` 可省略，填写时必须与池一致。下面示例须先创建服务类型为 `openai`、名称为 `openai` 的账号池：

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
    "api_key": "REPLACE_API_KEY"
  }
]
```

代理对象中 `scheme` 支持 `http`、`https`、`socks5h`；`host` 只填主机名或 IP（IPv6 用方括号），`port` 为 1–65535 的整数。无需认证时省略 `username` 和 `password`；需要认证时填写原始用户名和密码，程序会自动编码其中的 `@`、`:`、`/` 等特殊字符。也兼容每条账号中的 URL 写法，如 `"proxy": "socks5h://user:password@proxy.example:1080"`，URL 内的特殊字符需要自行百分号编码。仅获授权的 Workbuddy 测试账号可显式填写 `"proxy": "direct://"`。同一代理可显式绑定多个账号，但不会为缺少 `proxy` 的账号自动补值。

VLESS 请直接在账号代理栏或批量 JSON 的 `proxy` 中填写分享链接，例如（以下 UUID 是示例）：

```json
{"proxy":"vless://12345678-1234-1234-1234-123456789abc@node.example:443?encryption=none&security=tls&type=xhttp&sni=node.example&path=%2Ftunnel&mode=stream-one"}
```

支持 TLS + TCP / XHTTP；XHTTP 支持 auto、stream-one、stream-up、packet-up。节点证书必须有效，不允许跳过证书校验。REALITY、WS、gRPC、XHTTP extra 或未知参数会被明确拒绝。UUID、路径和参数只在本地编辑及经过 TEE 验证的配置同步中使用，导入预览不显示这些信息。模型请求、目录查询和刷新令牌均由 Router 经该节点访问；失败不会改为直连。需要已升级并支持 VLESS 的 Router，商户软件不负责转发流量或运行代理核心。

每项还可填写 `max_concurrent`（默认 16）、`rpm`（默认 0，不限）和 `enabled`（默认 true）。不能同时提供 `api_key` 和 `credential`。OpenAI 官方 `credential.account_id` 须保持真实值，与展示用的 `id` 独立。Codex `auth.json` 等其他客户端格式需要先转换。

### Grok 原生 OAuth 凭证

xai 账号池支持 Grok 的原生 OAuth 缓存 JSON。单个账号可在凭证框直接粘贴原始 JSON，另填账号别名和代理；也可继续使用 `{ "provider": "xai", "credential": {...} }` 统一格式。批量导入时，把原始 JSON 完整放入每条账号的 `credential`：

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

界面选择已有 xai 账号池；每条仍需自己的 `id` 和 `proxy`。导入时自动将 `key` 转为 `access_token`，ISO `expires_at` 转为 Unix 秒，并保留刷新令牌、scope 和 token_type 等凭证字段。编辑时显示转换后的本地凭证。缓存键是发行方和 OAuth 客户端的标识，不是商户账号别名，也不会用作转发地址；当前仅支持上述与 Router 内置配置一致的认证条目，多个条目或其他认证来源会明确报错。

过期时间按原值保留，不伪造新的有效期。格式校验成功只表示能够导入；是否能刷新令牌或调用模型取决于实际账号与网络出口。Router 沿用已有 xAI 刷新逻辑，刷新失败时须重新登录并补录。选择 xai 池后，“下载示例”会同时给出原生 OAuth 和 API key 账号示例，凭证内容均为占位值。Grok 的登录与凭证缓存说明见[官方文档](https://github.com/xai-org/grok-build/blob/main/crates/codegen/xai-grok-pager/docs/user-guide/02-authentication.md)。

### 校验与保存

先点击“校验并预览”，再确认保存到本地并同步。预览逐行展示账号对应的代理协议、地址和端口，隐藏代理认证信息及模型凭证。缺少或无效代理会指出第几个账号有问题，整批不提交；内容修改后需要重新校验。重复 ID、已有账号、不存在或服务类型不匹配的账号池也会阻止保存。每批最多 4096 项，文件总大小最多 12 MB。导入沿用目标账号池倍率；倍率在账号池的创建或编辑窗口单独设置。可在导入窗口下载当前所选池的账号示例，每条都有独立代理配置占位；真实凭证只放在自己的本机导入文件中。代理对象会转换成 URL 后随对应账号加密保存到本地，再同步给 Router；编辑账号时仍可查看和修改这个 URL。

本地资料绑定 Windows 用户。新电脑可在空白工作台填写原 Merchant ID 和 Merchant Secret；本地账单归档和未同步的账号资料仍需另行备份恢复。原人工登记商户应保留 Windows 用户配置与 DPAPI 恢复材料。商户费率不是签名金融凭证，记录用于外部账务与审计。更多边界和 API 见 [teerouter 多商户文档](https://github.com/wangyi160/teerouter/blob/main/docs/MERCHANTS.md)。

## 源码开发与构建

构建环境：Windows x64、Python 3.12+、Node.js 22+、Rust（版本见 `native/rust-toolchain.toml`）及所选工具链需要的 C/C++、CMake、Perl 构建工具。应用通过 [Electron IPC](https://www.electronjs.org/docs/latest/tutorial/ipc) 调用管理引擎；渲染进程启用沙箱、上下文隔离和严格 CSP，不提供 Node.js 或任意网络访问。业务模块由 PyInstaller 打包，TEE 原生库由管理引擎通过 C ABI 直接加载，验证和 TLS 会话均留在该进程内。没有独立的 TEE 传输子进程。

```powershell
python -m venv .venv
.venv/Scripts/python.exe -m pip install -r requirements-build.txt
npm ci
```

构建脚本直接从本仓库 `native/` 编译 `teemerchant_attestation.dll` 并放入管理引擎目录。只保留商户 API 白名单、TDX/SNP 验证和经过证明绑定的 HTTPS 通信，不包含用户代理服务。验证代码的来源与许可记录在 `native/NOTICE`；构建不需要另一个仓库或预编译的 teeproxy。

```powershell
cargo fetch --locked --manifest-path native/Cargo.toml
.venv/Scripts/python.exe scripts/build_engine.py
npm start
npm run pack
npm run dist
```

`pack` 输出 `release/win-unpacked`；`dist` 只输出便携 ZIP。不要在构建目录中运行默认配置后直接发布，该运行会创建 `data/`；打包测试应使用隔离目录。原生库来源、源码和二进制哈希、许可放入 `resources/licenses/native`。构建脚本从 Cargo 缓存收集锁定依赖的许可文件；非默认缓存目录可用 `--cargo-sources C:/path/to/registry/src` 指定，工具链包装器可用 `--cargo 路径` 指定。`scripts/build_icon.py` 可重新生成仓库中的程序图标。运行时无需额外 Python 包；构建依赖由 `requirements-build.txt`、`package-lock.json` 和 `native/Cargo.lock` 固定。

保留可选的旧版开发入口 `python app.py`，仅监听 `127.0.0.1:8910`，并检查 Host、Origin 和 CSRF token。该入口需要 Python 和本仓库构建出的 `native/target/release/teemerchant_attestation.dll`；请勿与桌面版同时使用同一数据目录。

## 验证

```powershell
.venv/Scripts/python.exe -m unittest -v
cargo test --locked --manifest-path native/Cargo.toml
npm test
npm run test:desktop
# 可选：验证已经打包的程序
$env:TEEMERCHANT_TEST_EXE = "$PWD/release/win-unpacked/teemerchant.exe"
npm run test:desktop
```

测试包括 DPAPI 持久化、归档先于确认、重复归档、写盘失败、身份与版本回退拒绝，以及桌面进程生命周期、IPC 路径限制、账号编辑、批量导入、收入显示、重启恢复和托盘操作。桌面测试需要先构建管理引擎，使用临时目录及测试凭证，不连接真实 Router。`native/` 测试覆盖签名证明、发布策略、随机挑战及 TLS 绑定、验证失败不发凭证、商户路径隔离和失败写入不重发。多商户权限、统一 key、价格、备份和隔离测试位于 teerouter。测试使用受控上游，不代表真实账号或生产审计。

账号配置测试覆盖批量校验、凭证保留及轮换、旧倍率保留、账号移动、共享凭证与别名冲突。本地账号库测试验证先落盘后发送、磁盘失败不发送、远端失败保留修改、重启和离线读取、Router 隔离、恢复范围及本地版本冲突。可选浏览器测试 `python -m unittest -v test_browser` 需要 Playwright 和 Microsoft Edge，使用独立临时目录及模拟 Router，不修改真实商户账号。
