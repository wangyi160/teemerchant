use super::*;
use crate::{
    merchant::{Merchant, Request},
    protocol::*,
};
use axum::body::{Body, to_bytes};
use hyper::{Request as HttpRequest, Response, service::service_fn};
use hyper_util::rt::TokioIo;
use std::{convert::Infallible, ffi::CStr, sync::atomic::AtomicUsize};
use tokio::{net::TcpListener, task::JoinHandle};

struct Server {
    endpoint: String,
    task: JoinHandle<()>,
    calls: Arc<Mutex<Vec<(usize, String, String)>>>,
    connections: Arc<AtomicUsize>,
}
impl Drop for Server {
    fn drop(&mut self) {
        self.task.abort();
    }
}

async fn server(mode: &'static str) -> Server {
    let rcgen::CertifiedKey { cert, signing_key } =
        rcgen::generate_simple_self_signed(vec!["localhost".into()]).unwrap();
    let certificate = cert.der().to_vec();
    let config = rustls::ServerConfig::builder_with_provider(Arc::new(
        rustls::crypto::ring::default_provider(),
    ))
    .with_protocol_versions(&[&rustls::version::TLS13])
    .unwrap()
    .with_no_client_auth()
    .with_single_cert(
        vec![cert.der().clone()],
        rustls::pki_types::PrivatePkcs8KeyDer::from(signing_key.serialize_der()).into(),
    )
    .unwrap();
    let acceptor = tokio_rustls::TlsAcceptor::from(Arc::new(config));
    let listener = TcpListener::bind("127.0.0.1:0").await.unwrap();
    let endpoint = format!("https://{}", listener.local_addr().unwrap());
    let calls = Arc::new(Mutex::new(Vec::new()));
    let capture = calls.clone();
    let connections = Arc::new(AtomicUsize::new(0));
    let count = connections.clone();
    let task = tokio::spawn(async move {
        let mut children = tokio::task::JoinSet::new();
        loop {
            tokio::select! {
                accepted=listener.accept()=>{
                    let (socket,_)=accepted.unwrap();let acceptor=acceptor.clone();let certificate=certificate.clone();let capture=capture.clone();
                    let id=count.fetch_add(1,Ordering::SeqCst);
                    children.spawn(async move {
                        let tls=acceptor.accept(socket).await.unwrap();
                        let handler=service_fn(move |request:HttpRequest<hyper::body::Incoming>| {
                            let certificate=certificate.clone();let capture=capture.clone();
                            async move {
                                let (parts,body)=request.into_parts();let raw=to_bytes(Body::new(body),1024*1024).await.unwrap();
                                if parts.uri.path()==ATTESTATION_PATH {
                                    assert!(!parts.headers.contains_key("authorization"));
                                    assert!(!String::from_utf8_lossy(&raw).contains("fixture-secret"));
                                    let challenge:Challenge=serde_json::from_slice(&raw).unwrap();
                                    let mut evidence=json!({"protocol_version":1,"evidence_type":"teemerchant-unit-test","nonce":challenge.nonce,
                                        "certificate_sha256":certificate_hash(&certificate),"evidence":{"binding":hex(&report_data(&challenge.nonce,&certificate).unwrap())}});
                                    if mode=="nonce" {evidence["nonce"]=json!("00".repeat(32));}
                                    if mode=="cert" {evidence["certificate_sha256"]=json!("00".repeat(32));}
                                    if mode=="binding" {evidence["evidence"]["binding"]=json!("00".repeat(64));}
                                    if mode=="redirect" {return Ok::<_,Infallible>(Response::builder().status(302).header("location","https://example.com").body(Body::empty()).unwrap());}
                                    return Ok(Response::new(Body::from(evidence.to_string())));
                                }
                                capture.lock().unwrap().push((id,parts.uri.to_string(),parts.headers["authorization"].to_str().unwrap().into()));
                                // A write can commit while its acknowledgement is invalid.
                                Ok(Response::new(Body::from(if mode=="lost-ack" {"not-json"} else {"{\"ok\":true}"})))
                            }
                        });
                        let _=hyper::server::conn::http1::Builder::new().serve_connection(TokioIo::new(tls),handler).await;
                    });
                },
                _=children.join_next(),if !children.is_empty()=>{}
            }
        }
    });
    Server {
        endpoint,
        task,
        calls,
        connections,
    }
}

fn request(method: &str, path: &str) -> Request {
    Request {
        method: method.into(),
        path: path.into(),
        token: "fixture-secret".into(),
        body: json!({}),
    }
}

#[tokio::test]
async fn secrets_use_only_the_original_attested_tls_connection() {
    let server = server("ok").await;
    let mut merchant = Merchant::fixture(server.endpoint.clone(), true);
    for (method, path) in [
        ("GET", "/merchant/state"),
        ("GET", "/merchant/usage?limit=2"),
        ("POST", "/merchant/accounts/test"),
    ] {
        assert_eq!(merchant.request(request(method, path)).await["status"], 200);
    }
    assert_eq!(server.connections.load(Ordering::SeqCst), 1);
    let calls = server.calls.lock().unwrap();
    assert_eq!(calls.len(), 3);
    assert!(
        calls
            .iter()
            .all(|(connection, _, token)| *connection == 0 && token == "Bearer fixture-secret")
    );
}

#[tokio::test]
async fn rejected_evidence_never_sends_credentials_or_business_requests() {
    for (mode, allow) in [
        ("nonce", true),
        ("cert", true),
        ("binding", true),
        ("redirect", true),
        ("ok", false),
    ] {
        let server = server(mode).await;
        let mut merchant = Merchant::fixture(server.endpoint.clone(), allow);
        assert_eq!(
            merchant.request(request("POST", "/merchant/unlock")).await["error"],
            "router_attestation_failed"
        );
        assert!(server.calls.lock().unwrap().is_empty());
    }
}

#[tokio::test]
async fn admin_and_model_apis_are_denied_before_network_activity() {
    let server = server("ok").await;
    let mut merchant = Merchant::fixture(server.endpoint.clone(), true);
    for path in [
        "/admin/config",
        "/v1/models",
        "https://evil.example/merchant/state",
        "//evil.example/merchant/state",
        "/merchant/../admin/config",
        "/merchant/%73tate",
    ] {
        assert_eq!(
            merchant.request(request("GET", path)).await["error"],
            "merchant_endpoint_forbidden"
        );
    }
    assert_eq!(server.connections.load(Ordering::SeqCst), 0);
}

#[tokio::test]
async fn failed_write_is_not_replayed_and_drops_the_session() {
    let server = server("lost-ack").await;
    let mut merchant = Merchant::fixture(server.endpoint.clone(), true);
    assert_eq!(
        merchant.request(request("PUT", "/merchant/config")).await["error"],
        "merchant_request_failed"
    );
    assert_eq!(server.calls.lock().unwrap().len(), 1);
    assert_eq!(merchant.request(request("VERIFY", "")).await["status"], 200);
    assert_eq!(server.connections.load(Ordering::SeqCst), 2);
    assert_eq!(server.calls.lock().unwrap().len(), 1);
}

fn decode(pointer: *mut c_char) -> Value {
    assert!(!pointer.is_null());
    let value = serde_json::from_slice(unsafe { CStr::from_ptr(pointer) }.to_bytes()).unwrap();
    unsafe { tm_free_response(pointer) };
    value
}

#[test]
fn ffi_rejects_invalid_handles_and_unapproved_policy_without_leaking_input() {
    assert_eq!(tm_abi_version(), 1);
    assert_eq!(
        decode(unsafe { tm_create(std::ptr::null(), 0) })["error"],
        "invalid_router_or_trust_policy"
    );
    for raw in [
        r#"{"endpoint":"https://router.example","tdx_policy":null,"snp_policy":null}"#,
        r#"{"endpoint":"http://router.example","allow_test_evidence":true,"token":"sensitive"}"#,
    ] {
        let result = decode(unsafe { tm_create(raw.as_ptr(), raw.len()) });
        assert_eq!(result["error"], "invalid_router_or_trust_policy");
        assert!(!result.to_string().contains("sensitive"));
    }
    let raw = serde_json::to_vec(
        &json!({"method":"GET","path":"/merchant/state","token":"sensitive","body":null}),
    )
    .unwrap();
    assert_eq!(
        decode(unsafe { tm_request(u64::MAX, raw.as_ptr(), raw.len()) })["error"],
        "invalid_session"
    );
    tm_close(u64::MAX);
    unsafe { tm_free_response(std::ptr::null_mut()) };
}
