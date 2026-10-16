//! Public hardware-signed fixtures verify cryptography, not this deployment.
//! Policy-only synthetic reports deliberately never enter a business session.
use super::*;

const WHEN: u64 = 1791590400; // 2026-10-10; within the archived AMD CRL interval.
const DIR: &str = concat!(env!("CARGO_MANIFEST_DIR"), "/tests/fixtures/snp/");
fn fixture(name: &str) -> Vec<u8> {
    std::fs::read(format!("{DIR}{name}")).unwrap()
}
fn report(name: &str) -> AttestationReport {
    AttestationReport::from_bytes(&fixture(name)).unwrap()
}

#[test]
fn real_amd_vcek_signature_chain_and_crl() {
    let report = report("test-report.bin");
    let cert = parse_cert(&fixture("test-vcek.der")).unwrap();
    let chain = X509::stack_from_pem(&fixture("milan-chain.pem")).unwrap();
    verify_chain(
        &Product::Milan.root().unwrap(),
        &chain[0],
        &cert,
        &fixture("milan.crl"),
        WHEN,
    )
    .unwrap();
    verify_signed_report(&report, Product::Milan, &cert).unwrap();
    // This upstream fixture is old firmware v2, so crypto-valid still isn't approved.
    assert!(Product::from_report(&report).is_err());
    for offset in [80, 144, 192, 416, 672] {
        let mut bytes = fixture("test-report.bin");
        bytes[offset] ^= 1;
        let changed = AttestationReport::from_bytes(&bytes).unwrap();
        assert!(
            verify_signed_report(&changed, Product::Milan, &cert).is_err(),
            "accepted modified byte {offset}"
        );
    }
}

#[test]
fn archived_vlek_signature_is_valid_but_expired_certificate_is_rejected() {
    let report = report("test-vlek-report.bin");
    let product = Product::from_report(&report).unwrap();
    assert_eq!(product, Product::Milan);
    let cert = parse_cert(&fixture("test-vlek.der")).unwrap();
    let chain = X509::stack_from_pem(&fixture("milan-vlek-chain.pem")).unwrap();
    let root = product.root().unwrap();
    let root_cert: Certificate = root.clone().into();
    let intermediate: Certificate = chain[0].clone().into();
    let leaf: Certificate = cert.clone().into();
    (&root_cert, &intermediate).verify().unwrap();
    (&intermediate, &leaf).verify().unwrap();
    let expired =
        verify_chain(&root, &chain[0], &cert, &fixture("milan-vlek.crl"), WHEN).unwrap_err();
    assert!(expired.to_string().contains("certificate expired"));
    verify_signed_report(&report, product, &cert).unwrap();
    // Changing TLS binding invalidates the hardware signature, even when nonce matches.
    let mut changed = report;
    changed.report_data = [42; 64];
    assert!(verify_signed_report(&changed, product, &cert).is_err());
}

#[test]
fn bad_root_expired_or_corrupt_collateral_is_rejected() {
    let cert = parse_cert(&fixture("test-vcek.der")).unwrap();
    let chain = X509::stack_from_pem(&fixture("milan-chain.pem")).unwrap();
    let root = Product::Milan.root().unwrap();
    let crl = fixture("milan.crl");
    assert!(
        verify_chain(
            &Product::Genoa.root().unwrap(),
            &chain[0],
            &cert,
            &crl,
            WHEN
        )
        .is_err()
    );
    assert!(verify_chain(&root, &chain[0], &cert, &crl, 0).is_err());
    assert!(verify_chain(&root, &chain[0], &cert, &crl, 4102444800).is_err());
    let expired = verify_chain(&root, &chain[0], &cert, &crl, WHEN + 90 * 86400).unwrap_err();
    assert!(expired.to_string().contains("CRL expired"), "{expired:#}");
    let mut corrupt = crl;
    let end = corrupt.len() - 1;
    corrupt[end] ^= 1;
    assert!(verify_chain(&root, &chain[0], &cert, &corrupt, WHEN).is_err());
    for bytes in [vec![], vec![0; 5], vec![0; 32], vec![0; 20000]] {
        assert!(parse_cert(&bytes).is_err());
    }
}

fn policy_case() -> (AttestationReport, Policy) {
    let mut report = report("test-vlek-report.bin");
    // Synthetic policy case, not represented as a valid hardware signature.
    report.vmpl = 0;
    report.policy = (1_u64 << 17).into();
    report.measurement = [1; 48];
    report.host_data = [2; 32];
    let minimum = MinimumTcb {
        bootloader: 1,
        tee: 1,
        snp: 2,
        microcode: 3,
        fmc: None,
    };
    let tcb = TcbVersion::new(None, 1, 1, 2, 3);
    report.reported_tcb = tcb;
    report.current_tcb = tcb;
    report.launch_tcb = tcb;
    report.committed_tcb = tcb;
    let release = Release {
        id: "TEST ONLY".into(),
        product: Product::Milan,
        measurement: hex(&report.measurement),
        host_data: hex(&report.host_data),
        guest_policy: 1 << 17,
        minimum_guest_svn: report.guest_svn,
        minimum_tcb: minimum,
        required_platform_info: 0,
        required_launch_mitigation: 0,
        required_current_mitigation: 0,
    };
    (
        report,
        Policy {
            version: 1,
            not_before_unix: WHEN - 1,
            expires_at_unix: WHEN + 60,
            releases: vec![release],
        },
    )
}

#[test]
fn policy_enforces_binding_measurement_pair_debug_vmpl_and_all_tcb_components() {
    let (report, policy) = policy_case();
    let expected = report.report_data;
    assert!(check_release(&report, Product::Milan, &policy, &expected, WHEN).is_ok());
    let rejects = |r: &AttestationReport| {
        assert!(check_release(r, Product::Milan, &policy, &expected, WHEN).is_err())
    };
    for mutate in [
        |r: &mut AttestationReport| r.report_data[0] ^= 1,
        |r: &mut AttestationReport| r.measurement[0] ^= 1,
        |r: &mut AttestationReport| r.host_data[0] ^= 1,
        |r: &mut AttestationReport| r.vmpl = 1,
        |r: &mut AttestationReport| r.policy.set_debug_allowed(true),
        |r: &mut AttestationReport| r.policy.set_migrate_ma_allowed(true),
        |r: &mut AttestationReport| r.current_tcb.snp = 1,
        |r: &mut AttestationReport| r.launch_tcb.bootloader = 0,
        |r: &mut AttestationReport| r.committed_tcb.microcode = 2,
        |r: &mut AttestationReport| r.reported_tcb.tee = 0,
        |r: &mut AttestationReport| r.sig_algo = 0,
        |r: &mut AttestationReport| r.version = 6,
        |r: &mut AttestationReport| r.cpuid_mod_id = Some(0xff),
    ] {
        let mut bad = report;
        mutate(&mut bad);
        rejects(&bad);
    }
    assert!(check_release(&report, Product::Milan, &policy, &expected, WHEN + 61).is_err());
    assert!(check_release(&report, Product::Milan, &policy, &expected, WHEN - 2).is_err());
    let mut crossed = policy.clone();
    crossed.releases[0].host_data = "03".repeat(32);
    let mut second = policy.releases[0].clone();
    second.id = "other".into();
    second.measurement = "04".repeat(48);
    crossed.releases.push(second);
    assert!(check_release(&report, Product::Milan, &crossed, &expected, WHEN).is_err());
    let mut mitigation = policy.clone();
    mitigation.releases[0].required_launch_mitigation = 1;
    assert!(check_release(&report, Product::Milan, &mitigation, &expected, WHEN).is_err());
}

#[tokio::test]
async fn empty_policy_never_downloads_or_accepts_evidence() {
    let policy = Policy::load(Path::new(concat!(
        env!("CARGO_MANIFEST_DIR"),
        "/tests/fixtures/snp/deny-all.json"
    )))
    .unwrap();
    let verifier = SnpVerifier::new(policy).unwrap();
    let error = verifier
        .verify(&serde_json::json!({"verified":true}), &[0; 64])
        .await
        .unwrap_err();
    assert!(error.to_string().contains("No reviewed"));
}

#[test]
fn local_policy_cannot_add_roots_disable_debug_checks_or_approve_empty_hashes() {
    let (_, policy) = policy_case();
    let mut injected = serde_json::to_value(&policy).unwrap();
    injected["trusted_root"] = serde_json::json!("operator root");
    assert!(serde_json::from_value::<Policy>(injected).is_err());
    for mutate in [
        |p: &mut Policy| p.releases[0].guest_policy |= 1 << 19,
        |p: &mut Policy| p.releases[0].guest_policy |= 1 << 18,
        |p: &mut Policy| p.releases[0].host_data = "0".repeat(64),
        |p: &mut Policy| p.releases[0].minimum_tcb.fmc = Some(0),
        |p: &mut Policy| p.releases.push(p.releases[0].clone()),
    ] {
        let mut bad = policy.clone();
        mutate(&mut bad);
        assert!(SnpVerifier::new(bad).is_err());
    }
}
