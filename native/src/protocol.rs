use anyhow::{Result, ensure};
use serde::{Deserialize, Serialize};
use sha2::{Digest, Sha256, Sha512};
use subtle::ConstantTimeEq;

pub const ATTESTATION_PATH: &str = "/.well-known/teellm/attestation";
pub const MAX_EVIDENCE: usize = 512 * 1024;
pub const SESSION_SECONDS: u64 = 120;

pub fn hex(data: &[u8]) -> String {
    data.iter().map(|b| format!("{b:02x}")).collect()
}

pub fn random_hex() -> Result<String> {
    let mut bytes = [0_u8; 32];
    getrandom::getrandom(&mut bytes).map_err(|e| anyhow::anyhow!("随机数生成失败：{e}"))?;
    Ok(hex(&bytes))
}

pub fn nonce_bytes(value: &str) -> Result<[u8; 32]> {
    ensure!(
        value.len() == 64
            && value
                .bytes()
                .all(|b| b.is_ascii_digit() || (b'a'..=b'f').contains(&b)),
        "挑战值必须是 32 字节的随机数（64 位小写十六进制）"
    );
    let mut bytes = [0; 32];
    for (i, b) in bytes.iter_mut().enumerate() {
        *b = u8::from_str_radix(&value[i * 2..i * 2 + 2], 16)?;
    }
    Ok(bytes)
}

pub fn certificate_hash(cert: &[u8]) -> String {
    hex(&Sha256::digest(cert))
}

/// Versioned, unambiguous binding: domain || 32-byte nonce || certificate SHA-256.
/// AA receives exactly 64 bytes, compatible with SNP REPORT_DATA size.
pub fn report_data(nonce: &str, certificate: &[u8]) -> Result<[u8; 64]> {
    let mut digest = Sha512::new();
    digest.update(b"teellm:attested-channel:v1\0");
    digest.update(nonce_bytes(nonce)?);
    digest.update(Sha256::digest(certificate));
    Ok(digest.finalize().into())
}

#[derive(Debug, Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
pub struct Challenge {
    pub nonce: String,
}

#[derive(Debug, Clone, Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
pub struct EvidenceEnvelope {
    pub protocol_version: u32,
    pub evidence_type: String,
    pub nonce: String,
    pub certificate_sha256: String,
    pub evidence: serde_json::Value,
}

#[derive(Debug, Serialize)]
pub struct Check {
    pub name: String,
    pub status: String,
    pub detail: String,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub comparison: Option<serde_json::Value>,
}

impl Check {
    pub fn new(name: &str, status: &str, detail: impl Into<String>) -> Self {
        Self {
            name: name.into(),
            status: status.into(),
            detail: detail.into(),
            comparison: None,
        }
    }
}

pub fn verify_binding(envelope: &EvidenceEnvelope, nonce: &str, certificate: &[u8]) -> Result<()> {
    nonce_bytes(nonce)?;
    ensure!(envelope.protocol_version == 1, "不支持的证明协议版本");
    ensure!(
        envelope.nonce == nonce,
        "证据的挑战值不匹配：可能是旧证据或响应被替换"
    );
    let fingerprint = certificate_hash(certificate);
    ensure!(
        bool::from(
            envelope
                .certificate_sha256
                .as_bytes()
                .ct_eq(fingerprint.as_bytes())
        ),
        "证据绑定的 TLS 证书与本次连接不一致"
    );
    Ok(())
}
