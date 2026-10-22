# teemerchant

商户自行运行的本机管理程序，支持 Windows 和 Linux，采用 MIT 开源许可。通过本机浏览器管理账号池、OAuth 授权、客户、收入和归档，不支持局域网访问。无需 Electron；[teeproxy](https://github.com/wangyi160/teeproxy) 仍是独立的用户代理。

## 解压和启动

从 [Releases](https://github.com/wangyi160/teemerchant/releases) 下载 Windows x64 ZIP 或 Linux x64 tar.gz。Windows 直接运行 `teemerchant.exe`，Linux 运行 `./teemerchant`，访问 `http://127.0.0.1:8910`。Linux 发行包要求 glibc 2.36 或更高版本。运行包自带 Python、TEE 原生库和 VLESS 组件，无需安装 Python、Node.js 或 teeproxy。

Windows 默认显示原生服务窗口，仅显示服务是否运行，以及“打开管理页面”“打开数据目录”两个按钮，无需登录。最小化按钮和关闭按钮均收起到系统托盘，服务继续运行；点击托盘图标恢复窗口，右键菜单可以打开管理页面或退出服务。“退出服务”会等待正在处理的请求和令牌续期结束后退出。重复运行同一数据目录的程序会恢复已有窗口，不会启动第二个刷新任务。服务状态指本机 HTTP 服务，Router 连接及账号状态在管理页面查看。

Windows 从终端运行 `teemerchant.exe --headless` 可禁用窗口与托盘，保留 HTTP 服务；Linux 默认就是此模式。所有版本仍是解压运行，无需安装。

服务仅监听 `127.0.0.1` 或 `::1`，打开本机页面时自动建立浏览器会话，不设置管理用户名和密码。API 保留会话、Host、Origin 和 CSRF 检查，跨站页面不能自动取得会话或调用凭证接口。Merchant ID / Secret 仍用于绑定 Router 中的商户身份。旧版本留下的 HTTP 密码文件不再读取。

修改本机端口示例：

```sh
./teemerchant --port 8911
```

Windows 使用相同参数：`teemerchant.exe --port 8911`。也可复制 `service.example.json` 为程序旁的 `service.json`，启动时会自动读取；`--config` 可指定其他配置文件，命令行参数覆盖文件中的对应选项。配置文件中的相对数据和证书路径以该文件所在目录为准。旧配置中的 `0.0.0.0`、`::`、局域网监听地址或远程 `public_urls` 会被拒绝，升级时请改为回环地址并删除远程地址配置。`--tls-cert` 和 `--tls-key` 可选，用于本机 HTTPS。

配置和账号库默认全部保存在程序旁的 `data/`，可用 `--data` 指定目录；升级保留完整 `data/` 后覆盖程序文件。程序锁定数据目录，防止两个服务同时刷新同一份令牌。Windows 保留原 DPAPI 格式，原桌面版本退出后可以直接使用同一份数据；Linux 使用 AES-GCM，主密钥在 `data/.vault-key`，仅服务用户可读。备份必须包括密钥；Windows DPAPI 数据不能直接搬到 Linux 解密。

商户在 Portal 注册并通过审核后，把 Merchant ID、Merchant Secret 填入“连接与身份”，上传自己审核的 TEE 策略 JSON，保存并连接 Router。商户独立控制密钥只保存在商户服务中，平台不能获取账号凭据。已有身份、账号和归档可继续使用，无需重新注册。

服务运行时自动连接 Router、续期和同步 access token；关闭浏览器不影响后台任务。关闭商户服务后停止续期，Router 已有 access token 可以用到过期；Router 重启后需要管理平台恢复用户数据、商户服务恢复账号配置。

“客户授权”只显示实际调用过本商户模型账号的用户，不显示平台预授权名单。首次调用须已获平台授权，并受默认客户权限及限额约束；调用后可单独调整权限、限速或停用。查询模型列表不会新增客户。已使用关系独立保存，归档和清空调用队列不会令客户消失。

“连接与身份”顶部展示已保存的 Merchant ID、遮挡的 Merchant Secret，以及是否正在使用这些凭证连接。ID 和 Secret 均可复制，Secret 可点击显示、隐藏；编辑会回填当前凭证。查看和复制从本机加密存储读取，不向 Router 请求，也不重新生成身份。普通状态查询不返回 Secret。新资料目录显示“尚未填写”，填写平台签发的凭证后才能连接商户。

原有商户继续使用 `data/` 中的文件，不迁移或覆盖原账号库。新增商户使用 `data/merchants/<Merchant ID>/`，各自保存加密设置、账号库、归档和策略；`data/active-merchant.json` 仅记录当前身份。新资料完整落盘后才原子切换当前身份，重启恢复最后选择。备份、移动或升级时保留整个 `data/`。商户服务接口 `POST /api/merchant-profile` 接收已保存的 `merchant_id`，受同样的本机浏览器会话、同源、CSRF 保护。
用户沿用平台 API key，选择 `/v1/models` 返回的模型 ID；本商户界面不签发用户平台 key。

全部上游账号和账号池归属商户，平台没有自营池。每个池只需设置一个倍率，例如 0.2 表示官方价格的 20%，1 表示官方价格。输入、输出、缓存和长上下文费率均乘以此倍率；平台统一维护官方基准价格，调用开始时保存基准价、倍率及版本。Workbuddy 使用测试基准。缺少倍率或模型基准价时拒绝调用；旧费率仅在可精确还原为全池统一倍率时自动迁移。

所有商户 API 由管理引擎在进程内调用自身的原生模块执行，模块源码位于 `native/`，不打包或启动 teeproxy。验证硬件证明、随机挑战、启动测量和 TLS 绑定后，才在同一条已验证连接发送商户凭证。验证失败不发送商户数据，写入请求丢失响应时不会自动重发。正式部署须使用独立审核的信任策略，不能只从服务器下载策略后直接信任。升级时自动移除旧版验证程序路径设置，账号与商户资料保持原样。

凭证统一使用 JSON（`provider`、`credential`）；OAuth 对象包含 `access_token` 等字段，API key 账号使用例如 `{ "provider": "workbuddy", "credential": { "api_key": "REPLACE_KEY" } }`，无需选择凭证类型。单个账号添加时须填写网络出口代理栏，支持 `http://`、`https://`、`socks5h://` 和 VLESS 地址；仅 Workbuddy 测试账号允许 `direct://`。代理地址是账号的网络出口，模型服务地址仍由 router 固定为官方域名。OpenAI OAuth 使用 `provider: openai`，由账号 credential 决定官方登录方式。支持 OpenAI、Anthropic、xAI；Workbuddy 需要平台单独启用测试权限。

身份、连接设置、账号资料和归档正文用 Windows DPAPI 或 Linux AES-GCM 加密，默认保存在程序目录的 `data/`。账号库为 `accounts.dpapi`，包括凭证、代理、账号池配置和待同步修改；磁盘上不存明文凭证。流水、拒答和收入从管理平台获取，每 5 分钟按递增游标同步，也可手动触发。账单和游标在同一 SQLite 事务中保存；写盘失败不推进游标。后续补结算更新原账单，不重复累计。商户端不再读取或确认删除 Router 的流水。界面显示最近 200 条，数据库保存完整平台归档。旧版 Router 直拉归档保留在原表中，不混入新的平台收入合计。

## 账号管理与批量导入

### 官方 OAuth 绑定

先在账号池列表中选中一个池，再点击 **添加账号**；添加窗口中的账号池固定为当前池，只读且不可切换。“全部账号”视图不提供单个账号的添加入口。管理页面将 **OAuth 授权** 与 **JSON 导入** 分为两个切换页，一次只显示一页，各自保留草稿，保存时只使用当前页的凭证。OAuth 页内点击独立的授权按钮，账号别名、代理和限额可稍后填写。ChatGPT/Grok 登录后粘贴浏览器地址栏中的完整回调地址；Claude 粘贴官方页面给出的授权码。官方 localhost 回调可能显示连接失败，此时复制地址栏的 URL 即可。授权成功后，access token、可选的 refresh token、到期时间等内容显示在 OAuth 页的凭证编辑框中，允许检查和修改；此时尚未写入账号。点击 **保存并同步** 才写入本地加密账号库，再通过已验证的 TEE 连接同步到 Router。取消或关闭窗口会丢弃未保存的编辑。Router 离线时显示等待同步。Workbuddy 只提供 JSON 导入。

已有 OAuth 账号可点击 **重新授权**，保留账号 ID、所属池、倍率、开放模型及启用状态。可选择是否保存 refresh token；重新授权默认沿用本地原凭证的选择，未保存时到期后手动重新授权。保存 refresh token 后由商户服务自动续期，三家供应商的续期请求均使用该账号配置的代理。

授权页面使用当前浏览器的网络；授权码兑换与续期由商户服务使用表单中的账号代理执行，支持 HTTP(S)、SOCKS5h 和 VLESS，失败不直连。每次绑定独立获取令牌，不读取其他 CLI 的本地登录缓存。取消、超时或退出会清理未保存的凭证。手动导入和批量导入继续保留。流程说明、参考实现和测试见 [OAuth 文档](docs/OAUTH.md)。

单个账号保存前读取本地最新配置，保留授权期间发生的其他账号、倍率和开放模型修改；本地写入前发生版本冲突时，重新核对后最多重试一次。同一账号的配置、凭证或代理发生变化时停止提交，窗口及其中的授权凭证保留，不要求取消后重新授权。Router 写入结果不确定时仍先保留本地待同步资料，不自动重发凭证。

### 账号池与模型

选择账号池后，在下方“池内模型目录”点击“编辑开放模型”，直接在目录表中修改整池统一的开放名单，所有账号共用。账号池行的“对外模型”也会定位到此编辑区。可选择“开放全部可用模型”，或勾选指定模型（支持搜索、全选筛选结果、清空），再点“保存并同步”；取消编辑不会修改配置。页面内切换列表或导航保留当前编辑内容，保存时检查配置版本以防覆盖其他修改。

勾选模式下新发现的模型不会自动开放；清空表示暂停该池的全部模型服务。连接、刷新及已连接时每分钟检查 Router 目录：成功取得当前完整目录后，自动从开放名单删除已不存在的模型，先加密保存本地，再通过 TEE 通道同步 Router。目录获取失败、正在刷新或已过期时保留原名单；清理后为空仍表示全部关闭，不会变成开放全部。有待同步修改或远端版本冲突时暂停自动清理，避免覆盖编辑或重发凭证。Router 仍按自己的周期获取上游目录；商户服务定期读取缓存，仅在发现待清理项时额外刷新上游确认，手动“更新目录”也会刷新上游。

Router 的用户目录只展示开放且通过能力、授权校验的模型；调用还需通过价格等校验。手工填写未开放的模型名、指定池或自动匹配均不能绕过名单。账号编辑、批量导入和倍率调整保留池级名单，新增或移入的账号继承目标池的名单。商户目录按池合并展示，保留未开放的官方模型供选择。

“账号与模型”先展示账号池、账号数量与倍率。点击账号池可筛选账号及模型目录。账号行显示启用情况、重新授权要求或目录状态；“目录已就绪”不代表额度、价格或所有模型调用均已验证。

在账号池列表点击“添加账号池”，填写池名称（路由标识）、服务类型及实际倍率。倍率统一在账号池中配置，Router 按账号池已保存并同步的实际倍率计费。商户工作台不再提供总限速设置；每个账号及客户的限额仍可单独调整。池可以独立存在且账号数量为 0；空池不提供模型服务。创建后自动选中该池，下方账号列表可“添加账号”或“批量导入”。账号表单只允许选择已有池，服务类型和倍率继承所属池；导入不会自动创建池。点击“编辑账号池”可调整全池倍率，池标识和服务类型固定，以保持既有路由和权限引用。移动账号只能选择同服务类型的已有池，原池即使变空也会保留。

### 收入与历史账单

“收入”页面显示累计、今日、本月应收（USD），待核算请求数、按账号池汇总和分页逐笔账单。可按日期范围和账号池筛选，查看请求当时的实际倍率、价格版本、token 计费项目及金额。“同步账单”从管理平台读取本商户新增或更新的账单，不连接 Router、不清理平台记录。

统计读取当前管理平台的全部本地加密归档，不受调用记录页最近 200 条的限制；按平台账单 ID 更新和去重。金额以整数微美元计算，通过 API 用十进制字符串传递，避免浏览器浮点数损失精度。今日和本月按商户程序所在 服务主机的本地日期统计。

收入以管理平台保存的结算状态和金额为准，不在客户端重复计费。请求当时的计费元数据随账单展示，后续人工结算通过更新游标同步。计费未完成、失败、缺少用量或记录校验不通过的请求单列“待核算”，不计入金额，不能视为免费。应收不是实际到账；收款、提现和结算状态尚未接入。官方基准金额也不是商户实际采购成本。

连接与身份中分别填写 Router 地址和管理平台地址。平台请求只携带平台签发的 Merchant Secret，不携带商户独立控制密钥、OAuth token 或账号代理。远程平台必须使用 HTTPS，本机平台允许回环 HTTP；禁止跟随重定向。平台归档任务与 Router 令牌同步分别运行，平台离线不阻塞 Router 控制通道。

本地接口 `GET /api/revenue` 支持 `start`、`end`（含结束日）、`pool`、`offset` 筛选，逐笔每页 50 条，概览始终统计全部本地归档。`GET /api/revenue/record?instance=...&seq=...` 读取平台导出的账单元数据，仍需本机同源和 CSRF 校验。

商户本地账号库负责保存和编辑账号资料，Router 使用同步过去的运行副本。“保存并同步 / 确认导入”先将资料和待同步修改一起加密落盘，再通过 TEE 通道提交 Router。本地写盘失败时不发出远端更新；Router 不可用、超时或版本冲突时，本地修改仍保留，页面明确显示“本地已保存 · 等待同步”，不会声称已同步成功，也不会自动重发凭证。可点击“同步账号配置”重试；版本冲突须刷新、核对配置后重新保存。调用和拒答记录页面的“同步”仍只用于归档。

首次连接后，本地会缓存该 Router 的商户配置。重启或离线时仍可管理账号池、查看已有账号或调整不涉及连接信息的设置，待连接后同步；不同 Router 地址使用隔离的本地记录。账号资料不会因刷新远端状态而丢失，过期的本地编辑窗口不能覆盖较新的本地修改。

单个账号添加的最后一步是 **测试连接**：完成 JSON 或 OAuth 凭证及代理后，由已验证的 Router 读取官方模型目录，并选择池内一个开放的文本模型发送短请求。成功显示模型和耗时后才能保存；修改凭证、代理、账号池或超过五分钟须重新测试。失败会显示凭证、网络、限流或模型响应错误，测试不会保存账号、刷新 refresh token 或计入用户消费；上游可能收取少量实际调用费用。新账号和连接信息的修改需要在线测试，不涉及连接信息的已有账号编辑仍可离线保存。批量导入继续执行格式校验，不提供逐个在线测试。

点击账号的“编辑”会直接从本地账号库读取并明文回填凭证和代理，不向 Router 请求凭证明文。可调整所属池、并发、RPM、启用状态和凭证；账号 ID、服务和凭证类型固定。凭证或代理未修改时不重复发送，留空仍表示保留原值。更换 OAuth 凭证时提交完整凭证对象，避免把不同身份的令牌拼接。共享凭证涉及多个部署时，界面会阻止只针对其中一个部署的凭证轮换及共享出口、限额变更。移动账号不会删除原池、原费率或历史记录。

Router 使用 `access-token-only-v1` 协议，拒绝 refresh token（包括空值和 Claude 嵌套形式）。商户服务保留完整凭据，所有导入、恢复、账号测试和配置同步都只投影 `access_token`、`expires_at` 和可选 `account_id`。平台、Router 和用户代理均不接收 refresh token。

商户服务每 10 秒检查账号，距到期不足 5 分钟时经账号代理续期。请求前持久化续期意图，成功后先保存新 access/refresh token，再通过商户专属 TEE 通道上传 access token。独立上传接口通过运行实例和旧 access token 摘要防止陈旧覆盖，响应丢失可以重试，不需要重新续期。账号停用后停止自动续期。官方明确拒绝时显示“需重新授权”；网络或进程中断导致续期结果不明时也暂停续期，防止重复使用可能已轮换的 refresh token。明确未建立连接的失败稍后重试。

JSON 可以仅包含 access token；这种账号由商户手动替换，过期后 Router 拒绝继续使用。对于没有显式过期时间的 JWT，读取 `exp`；不透明令牌需提供 `expires_at`，否则无法提前安排续期。

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
    "credential": {
      "access_token": "REPLACE_ACCESS_TOKEN_2",
      "refresh_token": "REPLACE_REFRESH_TOKEN_2",
      "expires_at": 2000000000
    }
  }
]
```

代理对象中 `scheme` 支持 `http`、`https`、`socks5h`；`host` 只填主机名或 IP（IPv6 用方括号），`port` 为 1–65535 的整数。无需认证时省略 `username` 和 `password`；需要认证时填写原始用户名和密码，程序会自动编码其中的 `@`、`:`、`/` 等特殊字符。也兼容每条账号中的 URL 写法，如 `"proxy": "socks5h://user:password@proxy.example:1080"`，URL 内的特殊字符需要自行百分号编码。仅获授权的 Workbuddy 测试账号可显式填写 `"proxy": "direct://"`。同一代理可显式绑定多个账号，但不会为缺少 `proxy` 的账号自动补值。

VLESS 请直接在账号代理栏或批量 JSON 的 `proxy` 中填写分享链接，例如（以下 UUID 是示例）：

```json
{"proxy":"vless://12345678-1234-1234-1234-123456789abc@node.example:443?encryption=none&security=tls&type=xhttp&sni=node.example&path=%2Ftunnel&mode=stream-one"}
```

支持 TLS + TCP / XHTTP；XHTTP 支持 auto、stream-one、stream-up、packet-up。节点证书必须有效，不允许跳过证书校验。REALITY、WS、gRPC、XHTTP extra 或未知参数会被明确拒绝。UUID、路径和参数只在本地编辑及经过 TEE 验证的配置同步中使用，导入预览不显示这些信息。模型请求和目录查询由 Router 经该节点访问；OAuth 换码和令牌续期由商户服务内置的 VLESS 组件经同一账号代理完成。失败不会改为直连。

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

过期时间按原值保留，不伪造新的有效期。格式校验成功只表示能够导入；是否能刷新令牌或调用模型取决于实际账号与网络出口。商户服务负责 xAI 令牌续期，需重新授权时可在账号编辑页重新登录或补录。下载示例中的凭证均为占位值。Grok 的登录与凭证缓存说明见[官方文档](https://github.com/xai-org/grok-build/blob/main/crates/codegen/xai-grok-pager/docs/user-guide/02-authentication.md)。

### 校验与保存

先点击“校验并预览”，再确认保存到本地并同步。预览逐行展示账号对应的代理协议、地址和端口，隐藏代理认证信息及模型凭证。缺少或无效代理会指出第几个账号有问题，整批不提交；内容修改后需要重新校验。重复 ID、已有账号、不存在或服务类型不匹配的账号池也会阻止保存。每批最多 4096 项，文件总大小最多 12 MB。导入沿用目标账号池倍率；倍率在账号池的创建或编辑窗口单独设置。可在导入窗口下载当前所选池的账号示例，每条都有独立代理配置占位；真实凭证只放在自己的本机导入文件中。代理对象会转换成 URL 后随对应账号加密保存到本地，再同步给 Router；编辑账号时仍可查看和修改这个 URL。

首次连接会在服务主机生成独立的商户控制密钥，并先加密保存；平台只接收其摘要。Windows 数据绑定 Windows 用户，Linux 数据依赖本地 `.vault-key`。Merchant ID 和 Merchant Secret 用于接入登记，单独持有它们不能控制已绑定的账号池。新主机需要迁移商户控制密钥和账号库，不能仅凭 Portal 中的接入 Secret 接管旧资料。

Router 不向管理平台导出商户账号备份。Router 重启后，teemerchant 验证 TEE 和控制密钥绑定，再从本机恢复账号池、access token、代理、倍率和限速。商户未连接或本机资料缺失时，账号池不恢复；平台不能代替商户导入。请保留完整 `data/` 及对应系统的解密材料。更多边界和 API 见 [teerouter 多商户文档](https://github.com/wangyi160/teerouter/blob/main/docs/MERCHANTS.md)。

## 源码运行与构建

```sh
python -m pip install -r requirements.txt
cargo build --release --locked --manifest-path native/Cargo.toml
cd egress
go build -trimpath -mod=readonly -o ../teemerchant-egress .
cd ..
python service.py
```

Windows 的代理组件文件名为 `teemerchant-egress.exe`。打包时在对应操作系统安装 `requirements-build.txt`，执行 `python scripts/build_service.py`。产物位于 `release/`，不包含商户数据、密钥或部署策略。开发测试：`python -m unittest test_app test_local_accounts test_credentials test_owner_control test_revenue test_service`、`node --test test_accounts.cjs`、`cargo test --locked --manifest-path native/Cargo.toml`。浏览器回归测试为 `python -m unittest test_browser`（需要 Playwright 与 Edge）。
