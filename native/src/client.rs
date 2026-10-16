use std::{
    sync::Arc,
    time::{Duration, Instant},
};

use anyhow::{Context, Result, ensure};
use bytes::Bytes;
use http_body_util::{BodyExt, Full, Limited};
use hyper::{Request, Response, body::Incoming, client::conn::http1::SendRequest};
use hyper_util::rt::TokioIo;
use rustls::{
    DigitallySignedStruct, SignatureScheme,
    client::danger::{HandshakeSignatureValid, ServerCertVerified, ServerCertVerifier},
    crypto::{CryptoProvider, verify_tls12_signature, verify_tls13_signature},
    pki_types::{CertificateDer, ServerName, UnixTime},
};
use serde::Serialize;
use tokio::{net::TcpStream, task::JoinHandle, time::timeout};
use tokio_rustls::TlsConnector;

use crate::protocol::*;

/// Bootstrap only: prove possession of the peer's TLS key, but do not yet trust
/// its identity. No credentials are sent until evidence verification succeeds.
#[derive(Debug)]
struct EvidenceBootstrap(Arc<CryptoProvider>);

impl ServerCertVerifier for EvidenceBootstrap {
    fn verify_server_cert(
        &self,
        _: &CertificateDer<'_>,
        _: &[CertificateDer<'_>],
        _: &ServerName<'_>,
        _: &[u8],
        _: UnixTime,
    ) -> Result<ServerCertVerified, rustls::Error> {
        Ok(ServerCertVerified::assertion())
    }
    fn verify_tls12_signature(
        &self,
        message: &[u8],
        cert: &CertificateDer<'_>,
        signature: &DigitallySignedStruct,
    ) -> Result<HandshakeSignatureValid, rustls::Error> {
        verify_tls12_signature(
            message,
            cert,
            signature,
            &self.0.signature_verification_algorithms,
        )
    }
    fn verify_tls13_signature(
        &self,
        message: &[u8],
        cert: &CertificateDer<'_>,
        signature: &DigitallySignedStruct,
    ) -> Result<HandshakeSignatureValid, rustls::Error> {
        verify_tls13_signature(
            message,
            cert,
            signature,
            &self.0.signature_verification_algorithms,
        )
    }
    fn supported_verify_schemes(&self) -> Vec<SignatureScheme> {
        self.0.signature_verification_algorithms.supported_schemes()
    }
}

struct Transport {
    sender: SendRequest<Full<Bytes>>,
    driver: JoinHandle<()>,
    authority: String,
}

impl Drop for Transport {
    fn drop(&mut self) {
        self.driver.abort();
    }
}

pub struct VerifiedSession {
    pub id: String,
    transport: Transport,
    verified_at: Instant,
}

impl VerifiedSession {
    pub fn is_current(&self) -> bool {
        self.verified_at.elapsed() < Duration::from_secs(SESSION_SECONDS)
            && !self.transport.driver.is_finished()
    }

    /// Only the merchant API allowlist may use an attested connection.
    pub async fn send_merchant(
        &mut self,
        method: &str,
        path: &str,
        token: &str,
        body: Vec<u8>,
    ) -> Result<Response<Incoming>> {
        let uri: hyper::Uri = path.parse()?;
        ensure!(
            uri.scheme().is_none()
                && uri.authority().is_none()
                && crate::merchant::allowed(method, uri.path()),
            "Unsupported merchant API"
        );
        let request = Request::builder()
            .method(method)
            .uri(uri)
            .header("content-type", "application/json")
            .header("authorization", format!("Bearer {token}"))
            .body(Full::new(Bytes::from(body)))?;
        self.send_verified(request, 16 * 1024 * 1024, Duration::from_secs(90))
            .await
    }

    async fn send_verified(
        &mut self,
        mut request: Request<Full<Bytes>>,
        limit: usize,
        response_timeout: Duration,
    ) -> Result<Response<Incoming>> {
        use hyper::body::Body;
        ensure!(
            request
                .body()
                .size_hint()
                .upper()
                .is_some_and(|n| n <= limit as u64),
            "Request too large"
        );
        request
            .headers_mut()
            .insert(hyper::header::HOST, self.transport.authority.parse()?);
        ensure!(self.is_current(), "Attested TLS session expired or closed");
        ensure!(
            matches!(
                timeout(Duration::from_secs(10), self.transport.sender.ready()).await,
                Ok(Ok(()))
            ),
            "Attested TLS session unavailable"
        );
        // Never replay a merchant write after transmission starts.
        timeout(
            response_timeout,
            self.transport.sender.send_request(request),
        )
        .await
        .context("Merchant response headers timed out")?
        .context("Merchant request outcome unknown; no automatic retry")
    }
}

#[derive(Serialize)]
pub struct VerificationReport {
    pub ok: bool,
    pub status: String,
    pub message: String,
    pub checks: Vec<Check>,
    pub certificate_sha256: Option<String>,
    pub nonce: Option<String>,
    pub evidence: Option<EvidenceEnvelope>,
    pub session_id: Option<String>,
    pub valid_for_seconds: u64,
}

pub struct VerificationAttempt {
    pub report: VerificationReport,
    pub session: Option<VerifiedSession>,
}

#[derive(Default)]
pub struct Trust {
    #[cfg(test)]
    pub(crate) allow_test_evidence: bool,
    #[cfg(feature = "tdx-verifier")]
    pub tdx: Option<crate::tdx::TdxVerifier>,
    #[cfg(feature = "snp-verifier")]
    pub snp: Option<crate::snp::SnpVerifier>,
}

/// Verify hardware evidence, local release policy, a fresh challenge and the
/// live TLS identity. Credentials can only be sent through a returned session.
pub async fn verify(endpoint: &str, trust: &Trust) -> VerificationAttempt {
    #[cfg(not(any(feature = "snp-verifier", feature = "tdx-verifier")))]
    let _ = trust;
    let mut report = VerificationReport {
        ok: false,
        status: "rejected".into(),
        message: String::new(),
        checks: vec![],
        certificate_sha256: None,
        nonce: None,
        evidence: None,
        session_id: None,
        valid_for_seconds: 0,
    };
    let result: Result<VerifiedSession> = async {
        let url = reqwest::Url::parse(endpoint).context("无效的网关地址")?;
        ensure!(
            url.scheme() == "https"
                && url.username().is_empty()
                && url.password().is_none()
                && url.query().is_none()
                && url.fragment().is_none()
                && url.path() == "/",
            "网关地址必须是 HTTPS 根地址，不能包含凭证、路径或查询参数"
        );
        let host = url
            .host_str()
            .context("缺少网关主机名")?
            .trim_start_matches('[')
            .trim_end_matches(']');
        let authority = endpoint
            .parse::<hyper::Uri>()?
            .authority()
            .context("缺少主机名")?
            .as_str()
            .to_owned();
        let provider = Arc::new(rustls::crypto::ring::default_provider());
        let config = rustls::ClientConfig::builder_with_provider(provider.clone())
            .with_protocol_versions(&[&rustls::version::TLS13])?
            .dangerous()
            .with_custom_certificate_verifier(Arc::new(EvidenceBootstrap(provider)))
            .with_no_client_auth();
        let tcp = timeout(
            Duration::from_secs(10),
            TcpStream::connect((host, url.port_or_known_default().unwrap_or(443))),
        )
        .await
        .context("连接网关超时")?
        .context("无法连接网关")?;
        let stream = timeout(
            Duration::from_secs(10),
            TlsConnector::from(Arc::new(config))
                .connect(ServerName::try_from(host.to_owned())?, tcp),
        )
        .await
        .context("TLS 握手超时")?
        .context("TLS 握手失败")?;
        let cert = stream
            .get_ref()
            .1
            .peer_certificates()
            .and_then(|certs| certs.first())
            .context("网关未提供 TLS 证书")?
            .to_vec();
        report.certificate_sha256 = Some(certificate_hash(&cert));
        report.checks.push(Check::new(
            "TLS 1.3 连接",
            "passed",
            "已校验握手签名；此时尚未信任服务器身份，也未发送 API key 或问题",
        ));
        let (sender, connection) =
            hyper::client::conn::http1::handshake(TokioIo::new(stream)).await?;
        let driver = tokio::spawn(async move {
            let _ = connection.await;
        });
        let mut transport = Transport {
            sender,
            driver,
            authority,
        };
        let nonce = random_hex()?;
        report.nonce = Some(nonce.clone());
        let request = Request::builder()
            .method("POST")
            .uri(ATTESTATION_PATH)
            .header("host", &transport.authority)
            .header("content-type", "application/json")
            .body(Full::new(Bytes::from(serde_json::to_vec(&Challenge {
                nonce: nonce.clone(),
            })?)))?;
        let envelope: EvidenceEnvelope = timeout(Duration::from_secs(15), async {
            let response = transport.sender.send_request(request).await?;
            ensure!(
                response.status().is_success(),
                "取证接口返回 HTTP {}",
                response.status().as_u16()
            );
            let bytes = Limited::new(response.into_body(), MAX_EVIDENCE)
                .collect()
                .await
                .map_err(|error| anyhow::anyhow!("证据响应读取失败：{error}"))?
                .to_bytes();
            Ok::<_, anyhow::Error>(serde_json::from_slice(&bytes)?)
        })
        .await
        .context("获取证据超时")??;
        report.evidence = Some(envelope.clone());
        verify_binding(&envelope, &nonce, &cert)?;
        if envelope.evidence_type == "dstack_tdx" {
            #[cfg(feature = "tdx-verifier")]
            {
                let verifier = trust.tdx.as_ref().context("未配置本机认可的 TDX 发布清单；请在 Trust.tdx 中加载审核后的生产 OS/router/启动策略")?;
                report.checks.extend(timeout(Duration::from_secs(60), verifier.verify(&envelope.evidence, &report_data(&nonce, &cert)?))
                    .await.context("Intel TDX 验证超时")??);
            }
            #[cfg(not(feature = "tdx-verifier"))]
            anyhow::bail!("此客户端构建未启用 tdx-verifier");
        } else if envelope.evidence_type == "snp" {
            #[cfg(feature = "snp-verifier")]
            {
                let verifier = trust.snp.as_ref().context("未配置本机认可的 SNP 发布清单；请在 Trust.snp 中加载审核后的 OS/router/InitData 策略")?;
                let checks = timeout(Duration::from_secs(40), verifier.verify(&envelope.evidence, &report_data(&nonce, &cert)?)).await.context("AMD SNP 验证超时")??;
                report.checks.extend(checks);
            }
            #[cfg(not(feature = "snp-verifier"))]
            anyhow::bail!("此客户端构建未启用 snp-verifier");
        } else {
            #[cfg(test)]
            {
                ensure!(trust.allow_test_evidence && envelope.evidence_type == "teemerchant-unit-test",
                    "Unsigned evidence rejected");
                ensure!(envelope.evidence["binding"] == hex(&report_data(&nonce, &cert)?), "Test binding mismatch");
            }
            #[cfg(not(test))]
            anyhow::bail!("Unsigned or unsupported evidence rejected");
        }
        report.checks.push(Check::new(
            "本次随机挑战",
            "passed",
            "响应挑战值与客户端本次生成值一致；REPORT_DATA 包含该挑战",
        ));
        report.checks.push(Check::new(
            "证据与 TLS 绑定",
            "passed",
            "REPORT_DATA 与本次 TLS 证书及挑战值的 SHA-512 绑定一致",
        ));
        report.checks.push(Check::new(
            "业务请求连接",
            "passed",
            "只允许使用刚完成验证的同一条 TLS 连接；断开或过期后必须重新验证",
        ));
        Ok(VerifiedSession {
            id: random_hex()?,
            transport,
            verified_at: Instant::now(),
        })
    }
    .await;
    let session = match result {
        Ok(session) => {
            report.ok = true;
            let tdx = report
                .evidence
                .as_ref()
                .is_some_and(|e| e.evidence_type == "dstack_tdx");
            report.status = if tdx { "tdx_verified" } else { "snp_verified" }.into();
            report.message = if tdx {
                "Intel TDX、生产 OS/router 发布策略与 TLS 绑定验证通过"
            } else {
                "AMD SNP 证据及本机获准发布策略通过；连接已绑定"
            }
            .into();
            report.session_id = Some(session.id.clone());
            report.valid_for_seconds = SESSION_SECONDS;
            Some(session)
        }
        Err(error) => {
            report.message = format!("{error:#}");
            report
                .checks
                .push(Check::new("验证未通过", "failed", &report.message));
            None
        }
    };
    VerificationAttempt { report, session }
}
