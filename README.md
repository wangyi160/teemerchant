# teemerchant

Windows Electron 商户桌面工作台，采用 MIT 开源许可。左侧导航包括账号与模型、客户授权、限速、收入、调用记录、拒答记录和连接与身份。用户代理 [teeproxy](https://github.com/wangyi160/teeproxy) 仍是独立程序。

## 安装和使用

从 [Releases](https://github.com/wangyi160/teemerchant/releases) 下载 Windows x64 安装包，安装后打开桌面上的 teemerchant。也可解压 ZIP 后运行 `teemerchant.exe`，请保留解压目录中的其他文件。安装包内置管理引擎、Python 运行库及 teeproxy TEE 验证组件；使用者无需安装 Python、Node.js 或单独启动服务器。

窗口关闭后继续在系统托盘运行；点击托盘图标恢复窗口，通过托盘菜单的“退出”结束程序。已有商户启动时会使用保存的连接设置和信任策略尝试连接一次；失败后仍可离线管理本地账号，并在“连接与身份”中重试。待同步的账号修改不会因重新打开程序而自动重发。程序不提供本地 HTTP 服务，界面通过受限 IPC 和私有进程管道访问管理引擎。此版本的 Windows 安装包尚未进行代码签名，发布页同时提供 SHA-256 校验文件。

旧版本升级时，先关闭原来的 Python 商户服务。桌面版自动沿用 `%LOCALAPPDATA%/teemerchant` 中当前 Windows 用户的身份、连接设置、加密账号库和账单归档，不需要重新登记商户。程序对数据目录加锁，避免两个工作台同时写入。可用 `--data=绝对路径` 指定另一个数据目录；不同目录代表不同的本地商户身份。卸载程序保留本地商户资料。

1. 在“连接与身份”填写 Router HTTPS 地址，并选择自己审核的 TDX 或 SNP 策略文件。默认使用内置验证组件，也可指定兼容的 teeproxy.exe；实际支持的证明类型取决于验证组件的编译特性。
2. 新商户先在 Router Portal 用登录名、密码和显示名称注册，等待平台管理员审核；注册不需要填写 JSON。审核通过后，在工作台“连接与身份 → Portal 账号绑定”中填写 Portal 地址并登录同一账号，自动完成本地身份绑定。平台只接收公开身份摘要，工作台不保存 Portal 密码或登录会话。公网 Portal 必须使用 HTTPS，HTTP 仅允许本机回环地址；绑定请求不跟随重定向。已有已连接商户继续使用原身份，原人工登记入口保留在折叠的“管理员手动登记信息”中。
3. Portal 显示身份同步完成后，点击“验证 TEE”和“连接商户”，先创建账号池并设置服务类型与倍率，再向池内添加官方账号、设置默认客户权限。Portal 绑定不会修改本机 Router 地址、TEE 策略或已有账号资料。

“客户授权”只显示实际调用过本商户模型账号的用户，不显示平台预授权名单。首次调用须已获平台授权，并受默认客户权限及限额约束；调用后可单独调整权限、限速或停用。查询模型列表不会新增客户。已使用关系独立保存，归档和清空调用队列不会令客户消失。
4. 用户沿用平台 API key，选择 `/v1/models` 返回的商户模型 ID；本商户界面不签发用户平台 key。

全部上游账号和账号池归属商户，平台没有自营池。每个池只需设置一个倍率，例如 0.2 表示官方价格的 20%，1 表示官方价格。输入、输出、缓存和长上下文费率均乘以此倍率；平台统一维护官方基准价格，调用开始时保存基准价、倍率及版本。Workbuddy 使用测试基准。缺少倍率或模型基准价时拒绝调用；旧费率仅在可精确还原为全池统一倍率时自动迁移。

所有商户 API 通过 teeproxy 私有 stdin/stdout 管道执行。客户端验证证明、启动测量和 TLS 绑定后，才在同一连接发送凭证；不会信任平台返回的“验证通过”字样。验证失败不发送商户数据。正式部署须使用独立审核的信任策略，不能只从服务器下载策略后直接信任。

API key 直接填入；OAuth 导入使用 router 账号格式（`provider`、`credential`），不是任意第三方客户端的凭证文件格式。每个账号须配置网络出口代理，可在代理栏或 OAuth JSON 的 `proxy` 中填写 `http://`、`https://` 或 `socks5h://` 地址；仅 Workbuddy 测试账号允许 `direct://`。代理地址是账号的网络出口，模型服务地址仍由 router 固定为官方域名。OpenAI OAuth 使用 `provider: openai`，由账号 credential 决定官方登录方式。支持 OpenAI、Anthropic、xAI；Workbuddy 需要平台单独启用测试权限。

身份、连接设置、账号资料和归档正文用当前 Windows 用户的 DPAPI 加密，默认保存在 `%LOCALAPPDATA%/teemerchant`。账号库为 `accounts.dpapi`，包括凭证、代理、账号池配置和待同步修改；磁盘上不存明文凭证。SQLite 只在事务提交后确认 router 用量 / 拒答前缀。每 10 秒归档一次；写盘失败不确认删除，重复记录不重复保存，记录内容变化则停止同步。界面显示最近 200 条，数据库保存完整归档。

## 账号管理与批量导入

选择账号池后，在下方“池内模型目录”点击“编辑开放模型”，直接在目录表中修改整池统一的开放名单，所有账号共用。账号池行的“对外模型”也会定位到此编辑区。可选择“开放全部可用模型”，或勾选指定模型（支持搜索、全选筛选结果、清空），再点“保存并同步”；取消编辑不会修改配置。页面内切换列表或导航保留当前编辑内容，保存时检查配置版本以防覆盖其他修改。

勾选模式下新发现的模型不会自动开放；清空表示暂停该池的全部模型服务。目录暂时获取失败时保留已选项，不会自动扩大范围。修改先加密保存本地，再通过经过 TEE 验证的商户通道同步 Router；界面显示同步结果。

Router 的用户目录只展示开放且通过能力、授权校验的模型；调用还需通过价格等校验。手工填写未开放的模型名、指定池或自动匹配均不能绕过名单。账号编辑、批量导入和倍率调整保留池级名单，新增或移入的账号继承目标池的名单。商户目录按池合并展示，保留未开放的官方模型供选择。

“账号与模型”先展示账号池、账号数量与倍率。点击账号池可筛选账号及模型目录。账号行显示启用情况、重新授权要求或目录状态；“目录已就绪”不代表额度、价格或所有模型调用均已验证。

在账号池列表点击“添加账号池”，填写池名称（路由标识）、服务类型及实际倍率。倍率统一在账号池中配置，Router 按账号池已保存并同步的实际倍率计费。“限速”页面仅配置商户总并发、RPM 和 TPM。池可以独立存在且账号数量为 0；空池不提供模型服务。创建后自动选中该池，下方账号列表可“添加账号”或“批量导入”。账号表单只允许选择已有池，服务类型和倍率继承所属池；导入不会自动创建池。点击“编辑账号池”可调整全池倍率，池标识和服务类型固定，以保持既有路由和权限引用。移动账号只能选择同服务类型的已有池，原池即使变空也会保留。

### 收入与历史账单

“收入”页面显示累计、今日、本月应收（USD），待核算请求数、按账号池汇总和分页逐笔账单。可按日期范围和账号池筛选，查看请求当时的实际倍率、价格版本、token 计费项目及金额。“同步账单”通过已有 TEE 通道归档新记录，落盘成功后才确认清理 Router 队列。

统计读取全部本地加密归档，不受调用记录页最近 200 条的限制；内存索引增量更新，重启后由加密归档重建。相同请求跨 Router 重启重复归档只统计一次，内容冲突的记录转为待核算。金额以整数微美元计算，通过 API 用十进制字符串传递，避免浏览器浮点数损失精度。今日和本月按商户程序所在 Windows 的本地日期统计。

新记录采用 Router 保存的每次请求计费结果，并核对分项金额、报价版本、倍率和最终舍入。旧记录仅在完整的历史报价及用量存在时按原公式计算；不会用当前账号池倍率或最新官方报价重算旧账。计费未完成、失败、缺少用量或记录校验不通过的请求单列“待核算”，不计入金额，不能视为免费。应收不是实际到账；收款、提现和结算状态尚未接入。官方基准金额也不是商户实际采购成本。

本地接口 `GET /api/revenue` 支持 `start`、`end`（含结束日）、`pool`、`offset` 筛选，逐笔每页 50 条，概览始终统计全部本地归档。`GET /api/revenue/record?instance=...&seq=...` 读取原始账单，仍需本机同源和 CSRF 校验。

商户本地账号库负责保存和编辑账号资料，Router 使用同步过去的运行副本。“保存并同步 / 确认导入”先将资料和待同步修改一起加密落盘，再通过 TEE 通道提交 Router。本地写盘失败时不发出远端更新；Router 不可用、超时或版本冲突时，本地修改仍保留，页面明确显示“本地已保存 · 等待同步”，不会声称已同步成功，也不会自动重发凭证。可点击“同步账号配置”重试；版本冲突须刷新、核对配置后重新保存。调用和拒答记录页面的“同步”仍只用于归档。

首次连接后，本地会缓存该 Router 的商户配置。重启或离线时仍可管理账号池、查看或编辑已有账号及添加账号，待连接后同步；不同 Router 地址使用隔离的本地记录。账号资料不会因刷新远端状态而丢失，过期的本地编辑窗口不能覆盖较新的本地修改。

点击账号的“编辑”会直接从本地账号库读取并明文回填凭证和代理，不向 Router 请求凭证明文。可调整所属池、并发、RPM、启用状态和凭证；账号 ID、服务和凭证类型固定。凭证或代理未修改时不重复发送，留空仍表示保留原值。更换 OAuth 凭证时提交完整凭证对象，避免把不同身份的令牌拼接。共享凭证涉及多个部署时，界面会阻止只针对其中一个部署的凭证轮换及共享出口、限额变更。移动账号不会删除原池、原费率或历史记录。

OAuth 自动续期目前仍由 Router 执行，本地保存最近导入或恢复的凭证版本；编辑其他字段不会把本地旧令牌覆盖到 Router。普通状态查询不包含凭证，只有编辑该账号时才读取本地明文。旧版本未留存本地凭证的账号会明确提示缺失，需要从商户自己的加密备份恢复或补录，不能凭空回填。

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

每项还可填写 `max_concurrent`（默认 16）、`rpm`（默认 0，不限）和 `enabled`（默认 true）。不能同时提供 `api_key` 和 `credential`。OpenAI 官方 `credential.account_id` 须保持真实值，与展示用的 `id` 独立。Codex `auth.json` 等其他客户端格式需要先转换。

### Grok 原生 OAuth 凭证

xai 账号池支持 Grok 的原生 OAuth 缓存 JSON。单个账号选择“OAuth 账号 JSON”，可在凭证框直接粘贴原始 JSON，另填账号别名和代理；也可继续使用 `{ "provider": "xai", "credential": {...} }` 统一格式。批量导入时，把原始 JSON 完整放入每条账号的 `credential`：

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

当前身份绑定 Windows 用户，本版尚无跨设备密钥迁移界面。保护 Windows 用户配置与 DPAPI 恢复材料；丢失身份会无法解锁商户恢复胶囊。商户费率不是签名金融凭证，记录用于外部账务与审计。更多边界和 API 见 [teerouter 多商户文档](https://github.com/wangyi160/teerouter/blob/main/docs/MERCHANTS.md)。

## 源码开发与构建

构建环境：Windows x64、Python 3.12+、Node.js 22+。应用通过 [Electron IPC](https://www.electronjs.org/docs/latest/tutorial/ipc) 调用管理引擎；渲染进程启用沙箱、上下文隔离和严格 CSP，不提供 Node.js 或任意网络访问。原有业务模块由 PyInstaller 打包；账号管理、DPAPI 存储与 TEE 通道复用同一套实现。管理引擎退出时，其 Windows Job Object 会结束关联的 TEE 传输子进程。

```powershell
python -m venv .venv
.venv/Scripts/python.exe -m pip install -r requirements-build.txt
npm ci
```

先从 [teeproxy](https://github.com/wangyi160/teeproxy) 的已审阅版本准备 Windows x64 验证组件和对应源码。独立核对二进制 SHA-256 后传给构建脚本；不要仅复制未经审核的二进制并把本地计算值当作可信依据。此发行版内置 teeproxy v0.14.1（源码 `75665881d39e2bfddcc52dd1268bf9ea3c45ab08`），二进制 SHA-256 为 `ebe04e9967b599b9cd92233f3e1767d25fdfc80e239149058fb07ed54f26d3f4`。

```powershell
.venv/Scripts/python.exe scripts/build_engine.py --teeproxy C:/build/teeproxy.exe --teeproxy-source C:/src/teeproxy --teeproxy-sha256 <已核对的SHA256>
npm start
npm run pack
npm run dist
```

`pack` 输出 `release/win-unpacked`；`dist` 输出安装包和 ZIP。组件来源、哈希和许可随程序放入 `resources`。构建脚本从 Cargo 缓存收集 teeproxy 锁定依赖的许可文件；先在 teeproxy 源码中运行 `cargo fetch --locked`，非默认缓存目录可用 `--cargo-sources C:/path/to/registry/src` 指定。`scripts/build_icon.py` 可重新生成仓库中的程序图标。运行时无需额外 Python 包；构建依赖由 `requirements-build.txt` 和 `package-lock.json` 固定。

保留可选的旧版开发入口 `python app.py`，仅监听 `127.0.0.1:8910`，并检查 Host、Origin 和 CSRF token。该入口需要自行安装 Python 和 teeproxy，连接设置中必须填写实际 teeproxy.exe 路径，不能使用桌面版的 `builtin` 标记；请勿与桌面版同时使用同一数据目录。

## 验证

```powershell
.venv/Scripts/python.exe -m unittest -v
npm test
npm run test:desktop
# 可选：验证已经打包的程序
$env:TEEMERCHANT_TEST_EXE = "$PWD/release/win-unpacked/teemerchant.exe"
npm run test:desktop
```

测试包括 DPAPI 持久化、归档先于确认、重复归档、写盘失败、身份与版本回退拒绝，以及桌面进程生命周期、IPC 路径限制、账号编辑、批量导入、收入显示、重启恢复和托盘操作。桌面测试需要先构建管理引擎，使用临时目录及测试凭证，不连接真实 Router。TEE / TLS 传输测试位于 teeproxy；多商户权限、统一 key、价格、备份和隔离测试位于 teerouter。测试使用受控上游，不代表真实账号或生产审计。

账号配置测试覆盖批量校验、凭证保留及轮换、旧倍率保留、账号移动、共享凭证与别名冲突。本地账号库测试验证先落盘后发送、磁盘失败不发送、远端失败保留修改、重启和离线读取、Router 隔离、恢复范围及本地版本冲突。可选浏览器测试 `python -m unittest -v test_browser` 需要 Playwright 和 Microsoft Edge，使用独立临时目录及模拟 Router，不修改真实商户账号。
