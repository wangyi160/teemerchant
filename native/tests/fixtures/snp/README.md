<a id="public-verification-fixtures"></a>

# 公开验证夹具

**简体中文** | [English](README.en.md)

`test-*.bin` 和 `test-*.der` 是来自 Confidential Containers Trustee 的公开测试向量，具体提交见 `sources.json`，许可为 Apache-2.0。`milan*.pem` / `milan*.crl` 是公开 AMD 背书材料，来自清单中固定的 AMD KDS URL。SHA-256 摘要记录导入时的原始字节。

这些材料**不是本项目的 Oracle 测量值**、已批准生产发布或某次客户端连接的新鲜证据。VCEK 报告为版本 2，生产验证器拒绝；VLEK 报告为版本 3，但 VMPL 为 1、缺少强制 InitData，证书已于 2025 年 12 月到期。其签名单独检查，完整证书验证必须拒绝该材料。两者均用于测试真实 AMD 签名。策略测试使用明确的合成报告，不建立业务会话。

测试将验证材料的校验时间固定为 2026-10-10，以便复现归档夹具，并单独测试过期情况。生产始终读取客户端实际时钟与新的 AMD 验证材料，不提供时钟覆盖或测试信任根。
