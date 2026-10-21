use crate::client::{self, Trust, VerifiedSession};
use anyhow::{Context, Result, ensure};
use http_body_util::{BodyExt, Limited};
use serde::Deserialize;
use serde_json::{Value, json};
use std::{path::Path, time::Duration};
use zeroize::Zeroizing;

pub fn allowed(method: &str, path: &str) -> bool {
    matches!(
        (method, path),
        (
            "GET",
            "/merchant/state" | "/merchant/usage" | "/merchant/refusals"
        ) | (
            "POST",
            "/merchant/unlock"
                | "/merchant/bind"
                | "/merchant/models/refresh"
                | "/merchant/usage/ack"
                | "/merchant/accounts/test"
        ) | ("PUT", "/merchant/config" | "/merchant/accounts/access")
            | ("DELETE", "/merchant/refusals")
    )
}

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Config {
    pub endpoint: String,
    pub tdx_policy: Option<String>,
    pub snp_policy: Option<String>,
}

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Request {
    pub method: String,
    pub path: String,
    pub token: String,
    pub body: Value,
}

pub struct Merchant {
    endpoint: String,
    trust: Trust,
    session: Option<VerifiedSession>,
}

impl Merchant {
    pub fn new(config: Config) -> Result<Self> {
        let url = reqwest::Url::parse(&config.endpoint)?;
        ensure!(
            url.scheme() == "https"
                && url.host_str().is_some()
                && url.username().is_empty()
                && url.password().is_none()
                && url.query().is_none()
                && url.fragment().is_none()
                && url.path() == "/",
            "HTTPS Router root required"
        );
        let mut trust = Trust::default();
        let mut releases = 0;
        if let Some(path) = config.tdx_policy {
            #[cfg(feature = "tdx-verifier")]
            {
                let verifier =
                    crate::tdx::TdxVerifier::new(crate::tdx::Policy::load(Path::new(&path))?)?;
                releases += verifier.approved_release_count();
                trust.tdx = Some(verifier);
            }
            #[cfg(not(feature = "tdx-verifier"))]
            {
                let _ = path;
                anyhow::bail!("TDX support unavailable");
            }
        }
        if let Some(path) = config.snp_policy {
            #[cfg(feature = "snp-verifier")]
            {
                let verifier =
                    crate::snp::SnpVerifier::new(crate::snp::Policy::load(Path::new(&path))?)?;
                releases += verifier.approved_release_count();
                trust.snp = Some(verifier);
            }
            #[cfg(not(feature = "snp-verifier"))]
            {
                let _ = path;
                anyhow::bail!("SNP support unavailable");
            }
        }
        ensure!(
            releases > 0,
            "Independently approved release policy required"
        );
        Ok(Self {
            endpoint: config.endpoint,
            trust,
            session: None,
        })
    }

    #[cfg(test)]
    pub fn fixture(endpoint: String, allow_test_evidence: bool) -> Self {
        Self {
            endpoint,
            trust: Trust {
                allow_test_evidence,
                ..Default::default()
            },
            session: None,
        }
    }

    pub async fn request(&mut self, request: Request) -> Value {
        let token = Zeroizing::new(request.token);
        let verify_only = request.method == "VERIFY" && request.path.is_empty();
        let valid_uri = request.path.parse::<hyper::Uri>().ok();
        if !verify_only
            && !valid_uri.as_ref().is_some_and(|uri| {
                uri.scheme().is_none()
                    && uri.authority().is_none()
                    && allowed(&request.method, uri.path())
            })
        {
            return json!({"error":"merchant_endpoint_forbidden"});
        }
        if self.session.as_ref().is_none_or(|s| !s.is_current()) {
            let verified = client::verify(&self.endpoint, &self.trust).await;
            self.session = verified.session;
            if self.session.is_none() {
                return json!({"error":"router_attestation_failed","checks":verified.report.checks});
            }
        }
        if verify_only {
            return json!({"status":200,"body":{"verified":true,"scope":"merchant"}});
        }
        let result: Result<Value> = async {
            let response = self
                .session
                .as_mut()
                .context("Missing verified session")?
                .send_merchant(
                    &request.method,
                    &request.path,
                    &token,
                    serde_json::to_vec(&request.body)?,
                )
                .await?;
            let status = response.status().as_u16();
            let body = tokio::time::timeout(
                Duration::from_secs(60),
                Limited::new(response.into_body(), 32 * 1024 * 1024).collect(),
            )
            .await?
            .map_err(anyhow::Error::from_boxed)?
            .to_bytes();
            Ok(json!({"status":status,"body":serde_json::from_slice::<Value>(&body)?}))
        }
        .await;
        match result {
            Ok(value) => value,
            Err(_) => {
                self.session = None;
                json!({"error":"merchant_request_failed","retry":"inspect_before_retrying_mutations"})
            }
        }
    }

    pub fn invalidate(&mut self) {
        self.session = None;
    }
}
