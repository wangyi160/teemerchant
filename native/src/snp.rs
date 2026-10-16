//! CoCo SNP evidence verification. Trust anchors come from the locked sev crate,
//! release policy from the user's local installation, never from the gateway.
use std::{
    cmp::Ordering,
    io::Read,
    path::Path,
    time::{Duration, SystemTime, UNIX_EPOCH},
};

use anyhow::{Context, Result, bail, ensure};
use futures_util::StreamExt;
use openssl::{
    asn1::Asn1Time,
    nid::Nid,
    stack::Stack,
    x509::{
        CrlStatus, X509, X509Crl, X509StoreContext, store::X509StoreBuilder,
        verify::X509VerifyParam,
    },
};
use serde::{Deserialize, Serialize};
use sev::{
    certs::snp::{Certificate, Verifiable, builtin},
    firmware::{
        guest::AttestationReport,
        host::{CertTableEntry, CertType, TcbVersion},
    },
    parser::ByteParser,
};
use subtle::ConstantTimeEq;
use x509_parser::{
    asn1_rs::{FromDer, Integer, OctetString},
    prelude::X509Certificate,
};

use crate::protocol::{Check, MAX_EVIDENCE, hex};

#[derive(Clone, Copy, Debug, Deserialize, Serialize, PartialEq, Eq)]
pub enum Product {
    Milan,
    Genoa,
    Turin,
}

impl Product {
    fn name(self) -> &'static str {
        match self {
            Self::Milan => "Milan",
            Self::Genoa => "Genoa",
            Self::Turin => "Turin",
        }
    }
    fn root(self) -> Result<X509> {
        Ok(match self {
            Self::Milan => builtin::milan::ark()?,
            Self::Genoa => builtin::genoa::ark()?,
            Self::Turin => builtin::turin::ark()?,
        }
        .into())
    }
    fn from_report(report: &AttestationReport) -> Result<Self> {
        ensure!(
            (3..=5).contains(&report.version),
            "SNP report version must be 3..=5; older firmware cannot identify the CPU safely"
        );
        match (report.cpuid_fam_id, report.cpuid_mod_id) {
            (Some(0x19), Some(0x00..=0x0f)) => Ok(Self::Milan),
            (Some(0x19), Some(0x10..=0x1f | 0xa0..=0xaf)) => Ok(Self::Genoa),
            (Some(0x1a), Some(0x00..=0x11)) => Ok(Self::Turin),
            _ => bail!("Unsupported SNP CPU family/model"),
        }
    }
}

#[derive(Clone, Debug, Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
pub struct MinimumTcb {
    pub bootloader: u8,
    pub tee: u8,
    pub snp: u8,
    pub microcode: u8,
    pub fmc: Option<u8>,
}

impl MinimumTcb {
    fn check(&self, actual: &TcbVersion) -> bool {
        actual.bootloader >= self.bootloader
            && actual.tee >= self.tee
            && actual.snp >= self.snp
            && actual.microcode >= self.microcode
            && match (self.fmc, actual.fmc) {
                (Some(min), Some(value)) => value >= min,
                (None, None) => true,
                _ => false,
            }
    }
}

/// An entry is a reviewed *pair* of launch measurement and enforced InitData.
/// HOST_DATA is chosen by the host: approving its hash alone proves nothing.
#[derive(Clone, Debug, Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
pub struct Release {
    pub id: String,
    pub product: Product,
    pub measurement: String,
    pub host_data: String,
    pub guest_policy: u64,
    pub minimum_guest_svn: u32,
    pub minimum_tcb: MinimumTcb,
    pub required_platform_info: u64,
    pub required_launch_mitigation: u64,
    pub required_current_mitigation: u64,
}

#[derive(Clone, Debug, Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
pub struct Policy {
    pub version: u32,
    pub not_before_unix: u64,
    pub expires_at_unix: u64,
    pub releases: Vec<Release>,
}

impl Policy {
    pub fn load(path: &Path) -> Result<Self> {
        let mut data = Vec::new();
        std::fs::File::open(path)?
            .take(256 * 1024 + 1)
            .read_to_end(&mut data)?;
        ensure!(data.len() <= 256 * 1024, "SNP policy file too large");
        let policy: Self = serde_json::from_slice(&data).context("Invalid local SNP policy")?;
        policy.validate()?;
        Ok(policy)
    }
    fn validate(&self) -> Result<()> {
        ensure!(self.version == 1, "Unknown SNP policy version");
        ensure!(
            self.not_before_unix < self.expires_at_unix,
            "Invalid policy validity interval"
        );
        ensure!(self.releases.len() <= 128, "Too many approved releases");
        let mut ids = std::collections::HashSet::new();
        for r in &self.releases {
            ensure!(
                !r.id.is_empty() && r.id.len() <= 128 && ids.insert(&r.id),
                "Invalid or duplicate release ID"
            );
            for (value, len) in [(&r.measurement, 96), (&r.host_data, 64)] {
                ensure!(
                    value.len() == len
                        && value
                            .bytes()
                            .all(|b| b.is_ascii_digit() || (b'a'..=b'f').contains(&b))
                        && value.bytes().any(|b| b != b'0'),
                    "Release hashes must be nonzero lowercase hex of the correct length"
                );
            }
            ensure!(
                r.guest_policy & ((1 << 18) | (1 << 19)) == 0 && r.guest_policy & (1 << 17) != 0,
                "Release policy must forbid debug and migration, and set reserved bit 17"
            );
            ensure!(
                (r.product == Product::Turin) == r.minimum_tcb.fmc.is_some(),
                "FMC minimum must be specified exactly for Turin"
            );
        }
        Ok(())
    }
    fn current(&self, now: u64) -> Result<()> {
        self.validate()?;
        ensure!(
            now >= self.not_before_unix && now < self.expires_at_unix,
            "Local SNP release policy expired or not yet valid"
        );
        ensure!(
            !self.releases.is_empty(),
            "No reviewed OS/router/InitData release is approved in the local SNP policy"
        );
        Ok(())
    }
}

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Evidence {
    pub attestation_report: AttestationReport,
    pub cert_chain: Option<Vec<CertTableEntry>>,
}

pub struct SnpVerifier {
    policy: Policy,
    http: reqwest::Client,
}

fn now() -> Result<u64> {
    Ok(SystemTime::now().duration_since(UNIX_EPOCH)?.as_secs())
}

impl SnpVerifier {
    pub fn approved_release_count(&self) -> usize {
        self.policy.releases.len()
    }

    pub fn new(policy: Policy) -> Result<Self> {
        policy.validate()?;
        Ok(Self {
            policy,
            http: reqwest::Client::builder()
                .no_proxy()
                .https_only(true)
                .redirect(reqwest::redirect::Policy::none())
                .connect_timeout(Duration::from_secs(5))
                .timeout(Duration::from_secs(12))
                .build()?,
        })
    }

    /// Fixed AMD HTTPS origin only. No URL from evidence/certificate extensions is fetched.
    async fn amd(&self, path: &str) -> Result<Vec<u8>> {
        let response = self
            .http
            .get(format!("https://kdsintf.amd.com/{path}"))
            .send()
            .await
            .context("Cannot fetch AMD endorsement material")?
            .error_for_status()?;
        let mut stream = response.bytes_stream();
        let mut bytes = Vec::new();
        while let Some(chunk) = stream.next().await {
            let chunk = chunk?;
            ensure!(
                bytes.len() + chunk.len() <= MAX_EVIDENCE,
                "AMD endorsement material too large"
            );
            bytes.extend_from_slice(&chunk);
        }
        Ok(bytes)
    }

    pub async fn verify(
        &self,
        value: &serde_json::Value,
        expected: &[u8; 64],
    ) -> Result<Vec<Check>> {
        self.policy.current(now()?)?;
        let evidence: Evidence =
            serde_json::from_value(value.clone()).context("Invalid CoCo SNP evidence")?;
        let report = &evidence.attestation_report;
        let product = Product::from_report(report)?;
        // Reject unsafe/unapproved data before requesting potentially chip-specific collateral.
        check_release(report, product, &self.policy, expected, now()?)?;
        let kind = match report.key_info.signing_key() {
            0 => "vcek",
            1 => "vlek",
            _ => bail!("Unsupported SNP signing key"),
        };
        let entries = evidence.cert_chain.unwrap_or_default();
        ensure!(entries.len() <= 16, "Too many SNP certificates");
        let mut leaf = None;
        for entry in entries {
            if (kind == "vcek" && entry.cert_type == CertType::VCEK)
                || (kind == "vlek" && entry.cert_type == CertType::VLEK)
            {
                ensure!(leaf.is_none(), "Duplicate SNP endorsement certificate");
                leaf = Some(parse_cert(&entry.data)?);
            }
        }
        let product_name = product.name();
        let chain_path = format!("{kind}/v1/{product_name}/cert_chain");
        let crl_path = format!("{kind}/v1/{product_name}/crl");
        let (chain, crl) = tokio::try_join!(self.amd(&chain_path), self.amd(&crl_path))?;
        let leaf = match leaf {
            Some(cert) => cert,
            None => {
                ensure!(kind == "vcek", "CoCo AA must supply the VLEK certificate");
                let tcb = &report.reported_tcb;
                let chip = if product == Product::Turin {
                    &report.chip_id[..8]
                } else {
                    &report.chip_id[..]
                };
                let mut path = format!(
                    "vcek/v1/{product_name}/{}?blSPL={}&teeSPL={}&snpSPL={}&ucodeSPL={}",
                    hex(chip),
                    tcb.bootloader,
                    tcb.tee,
                    tcb.snp,
                    tcb.microcode
                );
                if let Some(fmc) = tcb.fmc {
                    path.push_str(&format!("&fmcSPL={fmc}"));
                }
                parse_cert(&self.amd(&path).await?)?
            }
        };
        let certificates = X509::stack_from_pem(&chain)?;
        ensure!(certificates.len() == 2, "Unexpected AMD certificate chain");
        let root = product.root()?;
        ensure!(
            certificates[1].to_der()? == root.to_der()?,
            "AMD chain root differs from the compiled trust anchor"
        );
        let now = now()?;
        verify_chain(&root, &certificates[0], &leaf, &crl, now)?;
        verify_signed_report(report, product, &leaf)?;
        // Policy freshness is checked again after network I/O. Only verified claims are displayed.
        let release = check_release(report, product, &self.policy, expected, now)?;
        Ok(vec![
            Check::new(
                "AMD 硬件签名与证书链",
                "passed",
                format!(
                    "{} / {kind}；信任根固定在客户端；报告签名、证书有效期及 AMD CA 吊销列表通过",
                    product.name()
                ),
            ),
            Check::new(
                "SNP 固件与运行策略",
                "passed",
                "VMPL 0；禁用调试和迁移；TCB、平台位及缓解措施符合本机策略",
            ),
            Check::new(
                "获准的软件与启动策略",
                "passed",
                format!(
                    "本机发布清单：{}；启动 measurement 与 InitData HOST_DATA 成对匹配",
                    release.id
                ),
            ),
        ])
    }
}

fn parse_cert(bytes: &[u8]) -> Result<X509> {
    ensure!(
        !bytes.is_empty() && bytes.len() <= 16 * 1024,
        "Invalid certificate size"
    );
    Ok(if bytes.starts_with(b"-----BEGIN CERTIFICATE-----") {
        X509::from_pem(bytes)?
    } else {
        X509::from_der(bytes)?
    })
}

fn verify_chain(root: &X509, intermediate: &X509, leaf: &X509, crl: &[u8], now: u64) -> Result<()> {
    let timestamp = i64::try_from(now)?;
    let time = Asn1Time::from_unix(timestamp)?;
    for cert in [root, intermediate, leaf] {
        ensure!(
            cert.not_before().compare(&time)? != Ordering::Greater
                && cert.not_after().compare(&time)? == Ordering::Greater,
            "AMD certificate expired or not yet valid"
        );
    }
    let mut store = X509StoreBuilder::new()?;
    store.add_cert(root.clone())?; // No OS trust store and no supplied root.
    let mut params = X509VerifyParam::new()?;
    params.set_time(timestamp as _);
    store.set_param(&params)?;
    let mut chain = Stack::new()?;
    chain.push(intermediate.clone())?;
    let mut context = X509StoreContext::new()?;
    ensure!(
        context.init(&store.build(), leaf, &chain, |ctx| ctx.verify_cert())?,
        "AMD certificate path validation failed: {}",
        context.error()
    );
    let key = root.public_key()?;
    let (rest, parsed_crl) = x509_parser::revocation_list::CertificateRevocationList::from_der(crl)
        .map_err(|e| anyhow::anyhow!("Invalid AMD CA CRL: {e}"))?;
    ensure!(rest.is_empty(), "Trailing AMD CRL bytes");
    ensure!(
        parsed_crl
            .extensions()
            .iter()
            .all(|e| !e.critical
                && !matches!(e.oid.to_id_string().as_str(), "2.5.29.27" | "2.5.29.28")),
        "Unsupported critical, delta or indirect AMD CRL"
    );
    let crl = X509Crl::from_der(crl)?;
    ensure!(
        crl.issuer_name().to_der()? == root.subject_name().to_der()? && crl.verify(&key)?,
        "Invalid AMD CA CRL issuer/signature"
    );
    ensure!(
        crl.last_update().compare(&time)? != Ordering::Greater
            && crl
                .next_update()
                .context("AMD CRL has no expiry")?
                .compare(&time)?
                == Ordering::Greater,
        "AMD CA CRL expired or not yet valid"
    );
    // AMD publishes an ARK-signed CA CRL. It revokes ASK/ASVK, not individual VCEKs.
    ensure!(
        matches!(crl.get_by_cert(intermediate), CrlStatus::NotRevoked),
        "AMD signing CA is revoked"
    );
    Ok(())
}

fn verify_signed_report(report: &AttestationReport, product: Product, leaf: &X509) -> Result<()> {
    ensure!(
        report.sig_algo == 1 && !report.key_info.mask_chip_key(),
        "SNP report must carry an ECDSA P-384 signature"
    );
    let public = leaf.public_key()?;
    ensure!(
        public.ec_key()?.group().curve_name() == Some(Nid::SECP384R1),
        "Unexpected endorsement key curve"
    );
    let cert: Certificate = leaf.clone().into();
    (&cert, report)
        .verify()
        .context("Invalid AMD SNP report signature")?;
    let der = leaf.to_der()?;
    let (rest, parsed) = X509Certificate::from_der(&der)
        .map_err(|e| anyhow::anyhow!("Invalid endorsement certificate: {e}"))?;
    ensure!(rest.is_empty(), "Trailing certificate data");
    let extension = |oid: &str| -> Result<&[u8]> {
        let mut matches = parsed
            .extensions()
            .iter()
            .filter(|ext| ext.oid.to_id_string() == oid);
        let value = matches
            .next()
            .context("Missing AMD certificate extension")?
            .value;
        ensure!(
            matches.next().is_none(),
            "Duplicate AMD certificate extension"
        );
        Ok(value)
    };
    let integer = |suffix: u8| -> Result<u8> {
        let (rest, value) =
            Integer::from_der(extension(&format!("1.3.6.1.4.1.3704.1.3.{suffix}"))?)
                .map_err(|e| anyhow::anyhow!("Invalid AMD TCB integer: {e}"))?;
        ensure!(rest.is_empty(), "Trailing AMD TCB extension bytes");
        Ok(value.as_u8()?)
    };
    let t = &report.reported_tcb;
    for (oid, value) in [(1, t.bootloader), (2, t.tee), (3, t.snp), (8, t.microcode)] {
        ensure!(
            integer(oid)? == value,
            "SNP reported TCB differs from endorsement certificate"
        );
    }
    if product == Product::Turin {
        ensure!(
            Some(integer(9)?) == t.fmc,
            "SNP FMC differs from endorsement certificate"
        );
    }
    match report.key_info.signing_key() {
        0 => {
            let raw = extension("1.3.6.1.4.1.3704.1.4")?;
            let chip = if raw.len() == 64 {
                raw.to_vec()
            } else {
                let (rest, value) = OctetString::from_der(raw)
                    .map_err(|e| anyhow::anyhow!("Invalid AMD HWID: {e}"))?;
                ensure!(rest.is_empty(), "Trailing HWID extension bytes");
                value.as_ref().to_vec()
            };
            ensure!(
                bool::from(chip.as_slice().ct_eq(&report.chip_id)),
                "SNP chip ID differs from VCEK certificate"
            );
        }
        1 => {} // VLEK intentionally has no chip-specific HWID extension.
        _ => bail!("Unsupported endorsement key"),
    }
    Ok(())
}

fn check_release<'a>(
    report: &AttestationReport,
    product: Product,
    policy: &'a Policy,
    expected: &[u8; 64],
    now: u64,
) -> Result<&'a Release> {
    policy.current(now)?;
    ensure!(
        Product::from_report(report)? == product,
        "SNP processor mismatch"
    );
    ensure!(
        report.vmpl == 0 && !report.policy.debug_allowed() && !report.policy.migrate_ma_allowed(),
        "SNP must run at VMPL 0 with debug and migration disabled"
    );
    ensure!(
        !report.key_info.mask_chip_key()
            && report.key_info.signing_key() <= 1
            && report.sig_algo == 1,
        "Unsigned or unsupported SNP report"
    );
    ensure!(
        bool::from(report.report_data.ct_eq(expected)),
        "SNP REPORT_DATA does not bind this nonce and TLS certificate"
    );
    let measurement = hex(&report.measurement);
    let host_data = hex(&report.host_data);
    // Compare only canonical signed fields, not serde-only optional claims.
    let canonical = AttestationReport::from_bytes(&report.to_bytes()?)?;
    policy.releases.iter().find(|r| {
        r.product == product && r.measurement == measurement && r.host_data == host_data
            && r.guest_policy == u64::from(canonical.policy)
            && canonical.guest_svn >= r.minimum_guest_svn
            && [&canonical.current_tcb, &canonical.reported_tcb, &canonical.committed_tcb, &canonical.launch_tcb].into_iter().all(|t| r.minimum_tcb.check(t))
            && canonical.plat_info.0 & r.required_platform_info == r.required_platform_info
            && canonical.launch_mit_vector.unwrap_or(0) & r.required_launch_mitigation == r.required_launch_mitigation
            && canonical.current_mit_vector.unwrap_or(0) & r.required_current_mitigation == r.required_current_mitigation
    }).context("SNP OS/router/InitData measurement or security version is not approved by the local release policy")
}

#[cfg(test)]
#[path = "snp_tests.rs"]
mod tests;
