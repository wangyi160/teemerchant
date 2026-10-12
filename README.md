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
3. 点击“验证 TEE”和“连接商户”，再添加官方账号、客户授权和模型销售费率。
4. 用户沿用平台 API key，选择 `/v1/models` 返回的商户模型 ID；本商户界面不签发用户平台 key。

全部上游账号和账号池归属商户，平台没有自营池。商户费率支持输入、输出和缓存分类，以及按输入 tokens 阈值切换的长上下文阶梯；调用开始时保存完整报价版本。旧账号迁移保留原有倍率换算后的销售费率和阶梯。

所有商户 API 通过 teeproxy 私有 stdin/stdout 管道执行。客户端验证证明、启动测量和 TLS 绑定后，才在同一连接发送凭证；不会信任平台返回的“验证通过”字样。验证失败不发送商户数据。正式部署须使用独立审核的信任策略，不能只从服务器下载策略后直接信任。

API key 直接填入；OAuth 导入使用 router 账号格式（`provider`、`credential`），不是任意第三方客户端的凭证文件格式。每个账号须配置网络出口代理，可在代理栏或 OAuth JSON 的 `proxy` 中填写 `http://`、`https://` 或 `socks5h://` 地址；仅 Workbuddy 测试账号允许 `direct://`。代理地址是账号的网络出口，模型服务地址仍由 router 固定为官方域名。OpenAI OAuth 使用 `provider: openai`，由账号 credential 决定官方登录方式。支持 OpenAI、Anthropic、xAI；Workbuddy 需要平台单独启用测试权限。

身份、连接设置和归档正文用当前 Windows 用户的 DPAPI 加密，默认保存在 `%LOCALAPPDATA%/teemerchant`。SQLite 只在事务提交后确认 router 用量 / 拒答前缀。每 10 秒同步一次；写盘失败不确认删除，重复记录不重复保存，记录内容变化则停止同步。界面显示最近 200 条，数据库保存完整归档。

当前身份绑定 Windows 用户，本版尚无跨设备密钥迁移界面。保护 Windows 用户配置与 DPAPI 恢复材料；丢失身份会无法解锁商户恢复胶囊。商户费率不是签名金融凭证，记录用于外部账务与审计。更多边界和 API 见 [teerouter 多商户文档](https://github.com/wangyi160/teerouter/blob/main/docs/MERCHANTS.md)。

## 验证

```powershell
python -m unittest -v
node --check static/app.js
```

测试包括 DPAPI 持久化、归档先于确认、重复归档、写盘失败、身份与版本回退拒绝。TEE / TLS 传输测试位于 teeproxy；多商户权限、统一 key、价格、备份和隔离测试位于 teerouter。测试使用受控上游，不代表真实账号或生产审计。
