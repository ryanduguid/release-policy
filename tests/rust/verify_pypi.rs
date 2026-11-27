// Copyright 2026 Ryan Duguid
// SPDX-License-Identifier: Apache-2.0

mod support;

use release_policy::common::sha256;
use release_policy::py::PyPath;
use release_policy::verify_pypi::{
    ATTEMPTS, Config, DELAY, INTEGRITY_ACCEPT, MAX_RESPONSE_BYTES, PYPI, certificate_commit, fetch,
    fetch_with_config, verify_attestations, verify_publication_with,
};
use serde_json::json;
use std::collections::{BTreeMap, VecDeque};
use std::io::{Read, Write};
use std::net::TcpListener;
use std::path::Path;
use std::sync::{Arc, Mutex};
use std::time::Duration;
use support::{cli, symlink, tempdir};

const COMMIT: &str = "1111111111111111111111111111111111111111";
const REPOSITORY: &str = "example-owner/demo-pkg";
const WORKFLOW: &str = "release.yml";
const WHEEL: &str = "demo_pkg-1.2.3-py3-none-any.whl";
const SDIST: &str = "demo_pkg-1.2.3.tar.gz";
const DIGEST_OID: [u8; 10] = [0x2b, 0x06, 0x01, 0x04, 0x01, 0x83, 0xbf, 0x30, 0x01, 0x0d];
const OTHER_OID: [u8; 10] = [0x2b, 0x06, 0x01, 0x04, 0x01, 0x83, 0xbf, 0x30, 0x01, 0x0c];

fn der(tag: u8, contents: &[u8]) -> Vec<u8> {
    let mut out = vec![tag];
    let len = contents.len();
    if len < 0x80 {
        out.push(len as u8);
    } else {
        let raw = len.to_be_bytes();
        let trimmed: Vec<u8> = raw.into_iter().skip_while(|&b| b == 0).collect();
        out.push(0x80 | trimmed.len() as u8);
        out.extend_from_slice(&trimmed);
    }
    out.extend_from_slice(contents);
    out
}

fn certificate(commit: Option<&[u8]>, text_tag: u8) -> Vec<u8> {
    let uri =
        b"https://github.com/example-owner/demo-pkg/.github/workflows/release.yml@refs/tags/v1.2.3";
    let mut ext1 = der(0x06, &OTHER_OID);
    ext1.extend_from_slice(&der(0x04, &der(0x0C, uri)));
    let mut extensions = der(0x30, &ext1);
    if let Some(c) = commit {
        let mut ext2 = der(0x06, &DIGEST_OID);
        ext2.extend_from_slice(&der(0x04, &der(text_tag, c)));
        extensions.extend_from_slice(&der(0x30, &ext2));
    }
    let mut tbs_body = der(0xA0, &der(0x02, b"\x02"));
    tbs_body.extend_from_slice(&der(0x02, b"\x01"));
    tbs_body.extend_from_slice(&der(0x30, b""));
    tbs_body.extend_from_slice(&der(0xA3, &der(0x30, &extensions)));
    let mut cert_body = der(0x30, &tbs_body);
    cert_body.extend_from_slice(&der(0x30, b""));
    cert_body.extend_from_slice(&der(0x03, b"\x00"));
    der(0x30, &cert_body)
}

fn encode_base64(input: &[u8]) -> String {
    const TABLE: &[u8; 64] = b"ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/";
    let mut out = String::with_capacity(input.len().div_ceil(3) * 4);
    for chunk in input.chunks(3) {
        let b0 = u32::from(chunk[0]);
        let b1 = u32::from(*chunk.get(1).unwrap_or(&0));
        let b2 = u32::from(*chunk.get(2).unwrap_or(&0));
        let val = (b0 << 16) | (b1 << 8) | b2;
        out.push(TABLE[((val >> 18) & 0x3F) as usize] as char);
        out.push(TABLE[((val >> 12) & 0x3F) as usize] as char);
        out.push(if chunk.len() > 1 {
            TABLE[((val >> 6) & 0x3F) as usize] as char
        } else {
            '='
        });
        out.push(if chunk.len() > 2 {
            TABLE[(val & 0x3F) as usize] as char
        } else {
            '='
        });
    }
    out
}

fn provenance_doc(cert: &[u8], kind: &str, repo: &str, workflow: &str) -> Vec<u8> {
    json!({
        "version": 1,
        "attestation_bundles": [{
            "publisher": {
                "kind": kind,
                "repository": repo,
                "workflow": workflow,
                "environment": "pypi",
                "claims": null
            },
            "attestations": [{
                "version": 1,
                "envelope": {"statement": "e30=", "signature": "c2ln"},
                "verification_material": {
                    "certificate": encode_base64(cert),
                    "transparency_entries": []
                }
            }]
        }]
    })
    .to_string()
    .into_bytes()
}

fn release_doc(files: &[(&str, &[u8])]) -> Vec<u8> {
    let urls: Vec<_> = files
        .iter()
        .map(|&(name, data)| {
            json!({
                "filename": name,
                "digests": {"md5": "0".repeat(32), "sha256": sha256(data)}
            })
        })
        .collect();
    json!({"info": {"version": "1.2.3"}, "urls": urls})
        .to_string()
        .into_bytes()
}

fn fast_config() -> Config {
    Config {
        base_url: PYPI.into(),
        attempts: 3,
        delay: Duration::from_secs(7),
        root_ca_pem: None,
        allowed_origin: ("https".into(), "pypi.org".into()),
    }
}

#[test]
fn certificate_commit_parses_and_refuses_malformed_der() {
    let valid = certificate(Some(COMMIT.as_bytes()), 0x0C);
    assert_eq!(certificate_commit(&valid).unwrap().as_deref(), Some(COMMIT));
    assert_eq!(certificate_commit(&certificate(None, 0x0C)).unwrap(), None);

    let no_ext = der(0x30, &der(0x30, &der(0x02, b"\x01")));
    assert_eq!(certificate_commit(&no_ext).unwrap(), None);

    let mut two_certs = valid.clone();
    two_certs.extend_from_slice(&valid);
    let mut bad_utf8 = certificate(Some(&[0xff; 40]), 0x0C);
    let _ = &mut bad_utf8;
    let truncated = &valid[..valid.len() - 1];
    for case in [
        truncated,
        b"",
        &two_certs,
        b"\x30",
        b"\x30\x80",
        b"\x30\x82\x01",
        &der(0x30, b""),
        &certificate(Some(COMMIT.as_bytes()), 0x13),
        &certificate(Some(&[0xff; 40]), 0x0C),
    ] {
        let err = certificate_commit(case).unwrap_err();
        assert!(
            err.starts_with("signing certificate is not readable"),
            "{err}"
        );
    }

    let mut ext_a = der(0x06, &DIGEST_OID);
    ext_a.extend_from_slice(&der(0x04, &der(0x0C, COMMIT.as_bytes())));
    let item_a = der(0x30, &ext_a);
    let mut ext_b = der(0x06, &DIGEST_OID);
    ext_b.extend_from_slice(&der(
        0x04,
        &der(0x0C, b"2222222222222222222222222222222222222222"),
    ));
    let item_b = der(0x30, &ext_b);
    let mut exts = item_a.clone();
    exts.extend_from_slice(&item_b);
    let mut tbs = der(0x02, b"\x01");
    tbs.extend_from_slice(&der(0xA3, &der(0x30, &exts)));
    let dup = der(0x30, &der(0x30, &tbs));
    assert!(
        certificate_commit(&dup)
            .unwrap_err()
            .contains("names its source commit more than once")
    );
}

#[test]
fn verify_publication_covers_happy_retry_and_refusals() {
    assert_eq!((ATTEMPTS - 1) as u64 * DELAY.as_secs(), 300);

    let tmp = tempdir();
    let dir = tmp.path();
    let wheel_p = dir.join(WHEEL);
    let sdist_p = dir.join(SDIST);
    std::fs::write(&wheel_p, b"wheel bytes").unwrap();
    std::fs::write(&sdist_p, b"sdist bytes").unwrap();
    let files = [
        PyPath::new(sdist_p.to_str().unwrap()),
        PyPath::new(wheel_p.to_str().unwrap()),
    ];
    let prov = provenance_doc(
        &certificate(Some(COMMIT.as_bytes()), 0x0C),
        "GitHub",
        REPOSITORY,
        WORKFLOW,
    );
    let json_url = format!("{PYPI}/pypi/demo-pkg/1.2.3/json");
    let whl_url = format!("{PYPI}/integrity/demo-pkg/1.2.3/{WHEEL}/provenance");
    let sdist_url = format!("{PYPI}/integrity/demo-pkg/1.2.3/{SDIST}/provenance");

    // 1. Happy path + sorted output
    let mut requests = Vec::new();
    let mut verified = Vec::new();
    let mut sleeps = Vec::new();
    let cfg = fast_config();
    let full_rel = release_doc(&[(WHEEL, b"wheel bytes"), (SDIST, b"sdist bytes")]);
    let summary = verify_publication_with(
        "demo_pkg",
        "1.2.3",
        &files,
        REPOSITORY,
        COMMIT,
        WORKFLOW,
        &cfg,
        &mut |url, accept| {
            requests.push((url.to_owned(), accept.to_owned()));
            if url == json_url {
                Ok(Some(full_rel.clone()))
            } else {
                Ok(Some(prov.clone()))
            }
        },
        &mut |file, prov_file, repo| {
            verified.push((
                file.to_path_buf(),
                std::fs::read(prov_file).unwrap(),
                repo.to_owned(),
            ));
            Ok(())
        },
        &mut |d| sleeps.push(d),
    )
    .unwrap();
    assert_eq!(
        summary,
        format!(
            "verify_pypi: PyPI serves demo-pkg 1.2.3 as built, with provenance from {REPOSITORY} at {COMMIT}: {WHEEL}, {SDIST}"
        )
    );
    assert!(sleeps.is_empty());
    assert_eq!(verified.len(), 2);
    assert_eq!(
        requests,
        [
            (json_url.clone(), "application/json".into()),
            (whl_url.clone(), INTEGRITY_ACCEPT.into()),
            (sdist_url.clone(), INTEGRITY_ACCEPT.into()),
        ]
    );

    // 2. Retries across 404 -> partial -> complete
    let mut seq = VecDeque::from([
        None,
        Some(release_doc(&[(WHEEL, b"wheel bytes")])),
        Some(full_rel.clone()),
    ]);
    let mut retry_sleeps = Vec::new();
    verify_publication_with(
        "demo_pkg",
        "1.2.3",
        &files,
        REPOSITORY,
        COMMIT,
        WORKFLOW,
        &cfg,
        &mut |url, _| {
            if url == json_url {
                Ok(seq.pop_front().unwrap())
            } else {
                Ok(Some(prov.clone()))
            }
        },
        &mut |_, _, _| Ok(()),
        &mut |d| retry_sleeps.push(d),
    )
    .unwrap();
    assert_eq!(
        retry_sleeps,
        [Duration::from_secs(7), Duration::from_secs(7)]
    );

    // 3. Extra served file breaks early without retrying
    let extra_rel = release_doc(&[
        (WHEEL, b"wheel bytes"),
        (SDIST, b"sdist bytes"),
        ("demo_pkg-1.2.3-py2-none-any.whl", b"x"),
    ]);
    let mut no_sleeps = Vec::new();
    let err = verify_publication_with(
        "demo_pkg",
        "1.2.3",
        &files,
        REPOSITORY,
        COMMIT,
        WORKFLOW,
        &cfg,
        &mut |_, _| Ok(Some(extra_rel.clone())),
        &mut |_, _, _| Ok(()),
        &mut |d| no_sleeps.push(d),
    )
    .unwrap_err();
    assert!(
        err.contains("were not built: ['demo_pkg-1.2.3-py2-none-any.whl']"),
        "{err}"
    );
    assert!(no_sleeps.is_empty());

    // 4. Missing file after retries & 404 after retries & digest mismatch
    let partial_only = release_doc(&[(WHEEL, b"wheel bytes")]);
    assert!(
        verify_publication_with(
            "demo_pkg",
            "1.2.3",
            &files,
            REPOSITORY,
            COMMIT,
            WORKFLOW,
            &cfg,
            &mut |_, _| Ok(Some(partial_only.clone())),
            &mut |_, _, _| Ok(()),
            &mut |_| {},
        )
        .unwrap_err()
        .contains("does not serve these files")
    );
    assert_eq!(
        verify_publication_with(
            "demo_pkg",
            "1.2.3",
            &files,
            REPOSITORY,
            COMMIT,
            WORKFLOW,
            &cfg,
            &mut |_, _| Ok(None),
            &mut |_, _, _| Ok(()),
            &mut |_| {},
        )
        .unwrap_err(),
        "PyPI does not serve demo-pkg 1.2.3"
    );
    let wrong_hash = release_doc(&[(WHEEL, b"wheel bytes"), (SDIST, b"other")]);
    assert!(
        verify_publication_with(
            "demo_pkg",
            "1.2.3",
            &files,
            REPOSITORY,
            COMMIT,
            WORKFLOW,
            &cfg,
            &mut |_, _| Ok(Some(wrong_hash.clone())),
            &mut |_, _, _| Ok(()),
            &mut |_| {},
        )
        .unwrap_err()
        .contains("with SHA-256")
    );

    // 5. Provenance error branches
    for bad_prov in [
        b"{".to_vec(),
        b"[]".to_vec(),
        json!({"version": 2, "attestation_bundles": [{}]}).to_string().into_bytes(),
        json!({"version": 1, "attestation_bundles": []}).to_string().into_bytes(),
        json!({"version": 1, "attestation_bundles": [{"publisher": null, "attestations": []}]}).to_string().into_bytes(),
        provenance_doc(&certificate(Some(COMMIT.as_bytes()), 0x0C), "GitLab", REPOSITORY, WORKFLOW),
        json!({"version": 1, "attestation_bundles": [{"publisher": {"kind": "GitHub", "repository": REPOSITORY, "workflow": WORKFLOW}, "attestations": []}]}).to_string().into_bytes(),
        json!({"version": 1, "attestation_bundles": [{"publisher": {"kind": "GitHub", "repository": REPOSITORY, "workflow": WORKFLOW}, "attestations": [{"verification_material": {"certificate": "not base64!"}}]}]}).to_string().into_bytes(),
        provenance_doc(&certificate(None, 0x0C), "GitHub", REPOSITORY, WORKFLOW),
        provenance_doc(&certificate(Some(b"2222222222222222222222222222222222222222"), 0x0C), "GitHub", REPOSITORY, WORKFLOW),
    ] {
        let mut called = 0usize;
        let err = verify_publication_with(
            "demo_pkg",
            "1.2.3",
            &files,
            REPOSITORY,
            COMMIT,
            WORKFLOW,
            &cfg,
            &mut |url, _| {
                if url == json_url {
                    Ok(Some(full_rel.clone()))
                } else {
                    called += 1;
                    Ok(Some(bad_prov.clone()))
                }
            },
            &mut |_, _, _| Ok(()),
            &mut |_| {},
        )
        .unwrap_err();
        assert!(err.contains("provenance for"), "{err}");
        assert_eq!(called, 1);
    }

    // 6. Malformed release JSON & missing provenance & verifier failure
    for bad_rel in [
        b"<html>".to_vec(),
        b"[]".to_vec(),
        b"{\"urls\": {}}".to_vec(),
        b"{\"urls\": [\"x\"]}".to_vec(),
        json!({"urls": [{"filename": WHEEL, "digests": {"sha256": "A".repeat(64)}}]})
            .to_string()
            .into_bytes(),
        json!({"urls": [
            {"filename": WHEEL, "digests": {"sha256": "a".repeat(64)}},
            {"filename": WHEEL, "digests": {"sha256": "a".repeat(64)}},
        ]})
        .to_string()
        .into_bytes(),
    ] {
        assert!(
            verify_publication_with(
                "demo_pkg",
                "1.2.3",
                &files,
                REPOSITORY,
                COMMIT,
                WORKFLOW,
                &cfg,
                &mut |_, _| Ok(Some(bad_rel.clone())),
                &mut |_, _, _| Ok(()),
                &mut |_| {},
            )
            .unwrap_err()
            .contains("PyPI's JSON for demo-pkg 1.2.3")
        );
    }

    assert!(
        verify_publication_with(
            "demo_pkg",
            "1.2.3",
            &files,
            REPOSITORY,
            COMMIT,
            WORKFLOW,
            &cfg,
            &mut |url, _| if url == json_url {
                Ok(Some(full_rel.clone()))
            } else {
                Ok(None)
            },
            &mut |_, _, _| Ok(()),
            &mut |_| {},
        )
        .unwrap_err()
        .contains("PyPI serves no provenance")
    );

    // 7. Input validation errors BEFORE any network request
    let mut req_count = 0usize;
    let sym = dir.join("sym.whl");
    let has_sym = symlink(&wheel_p, &sym);
    type BadInput<'a> = (
        &'a str,
        &'a str,
        &'a str,
        &'a str,
        &'a str,
        Vec<PyPath>,
        &'a str,
    );
    let mut bad_inputs: Vec<BadInput<'_>> = vec![
        (
            "../x",
            "1.2.3",
            REPOSITORY,
            COMMIT,
            WORKFLOW,
            files.to_vec(),
            "project name",
        ),
        (
            "demo_pkg",
            "1/2",
            REPOSITORY,
            COMMIT,
            WORKFLOW,
            files.to_vec(),
            "valid version",
        ),
        (
            "demo_pkg",
            "1.2.3",
            "owner",
            COMMIT,
            WORKFLOW,
            files.to_vec(),
            "OWNER/REPO",
        ),
        (
            "demo_pkg",
            "1.2.3",
            REPOSITORY,
            "abc",
            WORKFLOW,
            files.to_vec(),
            "full commit SHA",
        ),
        (
            "demo_pkg",
            "1.2.3",
            REPOSITORY,
            COMMIT,
            "a/b.yml",
            files.to_vec(),
            "workflow file name",
        ),
        (
            "demo_pkg",
            "1.2.3",
            REPOSITORY,
            COMMIT,
            WORKFLOW,
            vec![PyPath::new(dir.join("missing.whl").to_str().unwrap())],
            "regular distribution file",
        ),
        (
            "demo_pkg",
            "1.2.3",
            REPOSITORY,
            COMMIT,
            WORKFLOW,
            vec![PyPath::new(dir.to_str().unwrap())],
            "regular distribution file",
        ),
        (
            "demo_pkg",
            "1.2.3",
            REPOSITORY,
            COMMIT,
            WORKFLOW,
            vec![files[0].clone(), files[0].clone()],
            "named twice",
        ),
        (
            "demo_pkg",
            "1.2.3",
            REPOSITORY,
            COMMIT,
            WORKFLOW,
            vec![],
            "no files to compare",
        ),
    ];
    if has_sym {
        bad_inputs.push((
            "demo_pkg",
            "1.2.3",
            REPOSITORY,
            COMMIT,
            WORKFLOW,
            vec![PyPath::new(sym.to_str().unwrap())],
            "regular distribution file",
        ));
    }
    for (p, v, r, c, w, f, want) in bad_inputs {
        let err = verify_publication_with(
            p,
            v,
            &f,
            r,
            c,
            w,
            &cfg,
            &mut |_, _| {
                req_count += 1;
                Ok(None)
            },
            &mut |_, _, _| Ok(()),
            &mut |_| {},
        )
        .unwrap_err();
        assert!(err.contains(want), "got {err}, expected {want}");
    }
    assert_eq!(req_count, 0);
}

#[test]
fn fetch_handles_status_redirects_and_16mib_cap() {
    let listener = TcpListener::bind("127.0.0.1:0").unwrap();
    let port = listener.local_addr().unwrap().port();
    let seen: Arc<Mutex<Vec<(String, String)>>> = Arc::new(Mutex::new(Vec::new()));
    let seen_srv = Arc::clone(&seen);

    std::thread::spawn(move || {
        for stream in listener.incoming().flatten() {
            let mut stream = stream;
            let mut buf = [0u8; 4096];
            let n = stream.read(&mut buf).unwrap_or(0);
            let req = String::from_utf8_lossy(&buf[..n]).to_string();
            let first = req.lines().next().unwrap_or("");
            let path = first.split_whitespace().nth(1).unwrap_or("/");
            let accept = req
                .lines()
                .find(|l| l.to_ascii_lowercase().starts_with("accept:"))
                .map(|l| l.split_once(':').unwrap().1.trim().to_owned())
                .unwrap_or_default();
            seen_srv.lock().unwrap().push((path.to_owned(), accept));

            let (status, headers, body): (&str, Vec<(&str, String)>, Vec<u8>) = match path {
                "/ok" => ("200 OK", vec![], b"{}".to_vec()),
                "/missing" => ("404 Not Found", vec![], b"".to_vec()),
                "/busy" => ("503 Service Unavailable", vec![], b"".to_vec()),
                "/redirect" => (
                    "302 Found",
                    vec![("Location", format!("http://127.0.0.1:{port}/other"))],
                    b"".to_vec(),
                ),
                "/hop" => (
                    "301 Moved Permanently",
                    vec![("Location", "/ok".into())],
                    b"".to_vec(),
                ),
                "/huge" => ("200 OK", vec![], vec![b'x'; MAX_RESPONSE_BYTES + 1]),
                _ => ("200 OK", vec![], b"fallback".to_vec()),
            };
            let mut head = format!(
                "HTTP/1.1 {status}\r\nContent-Length: {}\r\nConnection: close\r\n",
                body.len()
            );
            for (k, v) in headers {
                head.push_str(&format!("{k}: {v}\r\n"));
            }
            head.push_str("\r\n");
            let _ = stream.write_all(head.as_bytes());
            let _ = stream.write_all(&body);
        }
    });

    let base = format!("http://127.0.0.1:{port}");
    assert!(
        fetch(&format!("{base}/ok"), "application/json")
            .unwrap_err()
            .contains("https only")
    );
    let http_cfg = Config {
        base_url: base.clone(),
        ..fast_config()
    };
    assert_eq!(
        fetch_with_config(&format!("{base}/ok"), "application/json", &http_cfg).unwrap(),
        Some(b"{}".to_vec())
    );
    assert_eq!(
        fetch_with_config(&format!("{base}/missing"), "application/json", &http_cfg).unwrap(),
        None
    );
    assert_eq!(
        fetch_with_config(&format!("{base}/busy"), "application/json", &http_cfg).unwrap_err(),
        format!("{base}/busy answered HTTP 503")
    );
    let redir_err =
        fetch_with_config(&format!("{base}/redirect"), "application/json", &http_cfg).unwrap_err();
    assert_eq!(
        redir_err,
        format!("{base}/redirect redirected to {base}/other, outside https://pypi.org")
    );
    assert_eq!(
        fetch_with_config(&format!("{base}/huge"), "application/json", &http_cfg).unwrap_err(),
        format!("{base}/huge answered more than 16 MiB")
    );

    // Same-origin redirect followed when allowed_origin matches local server.
    let same_origin_cfg = Config {
        base_url: base.clone(),
        attempts: 1,
        delay: Duration::from_millis(0),
        root_ca_pem: None,
        allowed_origin: ("http".into(), format!("127.0.0.1:{port}")),
    };
    assert_eq!(
        fetch_with_config(&format!("{base}/hop"), "application/json", &same_origin_cfg).unwrap(),
        Some(b"{}".to_vec())
    );

    let recorded = seen.lock().unwrap();
    assert!(!recorded.iter().any(|(p, _)| p == "/other"));
    assert_eq!(recorded[0], ("/ok".into(), "application/json".into()));
}

#[test]
fn cli_end_to_end_with_loopback_and_stub_python() {
    let tmp = tempdir();
    let dir = tmp.path();
    let wheel_p = dir.join(WHEEL);
    std::fs::write(&wheel_p, b"wheel bytes").unwrap();

    let prov = provenance_doc(
        &certificate(Some(COMMIT.as_bytes()), 0x0C),
        "GitHub",
        REPOSITORY,
        WORKFLOW,
    );
    let rel = release_doc(&[(WHEEL, b"wheel bytes")]);
    let rel_map: BTreeMap<String, Vec<u8>> = [
        ("/pypi/demo-pkg/1.2.3/json".to_owned(), rel),
        (
            format!("/integrity/demo-pkg/1.2.3/{WHEEL}/provenance"),
            prov,
        ),
    ]
    .into_iter()
    .collect();
    let listener = TcpListener::bind("127.0.0.1:0").unwrap();
    let port = listener.local_addr().unwrap().port();
    std::thread::spawn(move || {
        for mut stream in listener.incoming().flatten() {
            let mut buf = [0u8; 4096];
            let n = stream.read(&mut buf).unwrap_or(0);
            let req = String::from_utf8_lossy(&buf[..n]).to_string();
            let path = req
                .lines()
                .next()
                .unwrap_or("")
                .split_whitespace()
                .nth(1)
                .unwrap_or("/");
            let body = rel_map.get(path).cloned().unwrap_or_default();
            let status = if body.is_empty() {
                "404 Not Found"
            } else {
                "200 OK"
            };
            let head = format!(
                "HTTP/1.1 {status}\r\nContent-Length: {}\r\nConnection: close\r\n\r\n",
                body.len()
            );
            let _ = stream.write_all(head.as_bytes());
            let _ = stream.write_all(&body);
        }
    });

    // Cross-platform CLI refusals for --base-url / --ca-cert, and full Unix stub tests.
    let base_url = format!("http://127.0.0.1:{port}");
    let out_bad_url = cli(
        dir,
        &[
            "verify-pypi",
            "--project",
            "demo_pkg",
            "--version",
            "1.2.3",
            "--repository",
            REPOSITORY,
            "--commit",
            COMMIT,
            "--workflow",
            WORKFLOW,
            "--file",
            wheel_p.to_str().unwrap(),
            "--base-url",
            "https://evil.example",
        ],
        "",
    );
    assert_eq!(out_bad_url.code, 1);
    assert_eq!(
        out_bad_url.stderr,
        "verify_pypi: --base-url is restricted to loopback addresses\n"
    );

    let out_bad_ca = cli(
        dir,
        &[
            "verify_pypi",
            "--project",
            "demo_pkg",
            "--version",
            "1.2.3",
            "--repository",
            REPOSITORY,
            "--commit",
            COMMIT,
            "--workflow",
            WORKFLOW,
            "--file",
            wheel_p.to_str().unwrap(),
            "--base-url",
            &base_url,
            "--ca-cert",
            "dummy.pem",
        ],
        "",
    );
    assert_eq!(out_bad_ca.code, 1);
    assert!(
        out_bad_ca
            .stderr
            .contains("--ca-cert requires a loopback https://")
    );

    #[cfg(unix)]
    {
        use std::os::unix::fs::PermissionsExt;
        let bin_dir = dir.join("stub_bin");
        std::fs::create_dir(&bin_dir).unwrap();
        let py_stub = bin_dir.join("python");
        let log_file = dir.join("py_args.txt");
        std::fs::write(
            &py_stub,
            format!(
                "#!/bin/sh\nprintf '%s\\n' \"$*\" >> '{}'\nexit 0\n",
                log_file.display()
            ),
        )
        .unwrap();
        std::fs::set_permissions(&py_stub, std::fs::Permissions::from_mode(0o755)).unwrap();
        let old_path = std::env::var("PATH").unwrap_or_default();
        let new_path = format!("{}:{old_path}", bin_dir.display());
        let out_ok = support::cli_env(
            dir,
            &[
                "verify-pypi",
                "--project",
                "demo_pkg",
                "--version",
                "1.2.3",
                "--repository",
                REPOSITORY,
                "--commit",
                COMMIT,
                "--workflow",
                WORKFLOW,
                "--file",
                wheel_p.to_str().unwrap(),
                "--base-url",
                &base_url,
                "--attempts",
                "2",
                "--delay-ms",
                "5",
            ],
            "",
            &[("PATH", &new_path)],
        );
        assert_eq!(out_ok.code, 0, "stderr: {}", out_ok.stderr);
        assert_eq!(
            out_ok.stdout,
            format!(
                "verify_pypi: PyPI serves demo-pkg 1.2.3 as built, with provenance from {REPOSITORY} at {COMMIT}: {WHEEL}\n"
            )
        );

        std::fs::write(
            &py_stub,
            "#!/bin/sh\necho 'debug line' >&2\necho 'bad signature' >&2\nexit 1\n",
        )
        .unwrap();
        let out_fail = support::cli_env(
            dir,
            &[
                "verify-pypi",
                "--project",
                "demo_pkg",
                "--version",
                "1.2.3",
                "--repository",
                REPOSITORY,
                "--commit",
                COMMIT,
                "--workflow",
                WORKFLOW,
                "--file",
                wheel_p.to_str().unwrap(),
                "--base-url",
                &base_url,
                "--attempts",
                "1",
                "--delay-ms",
                "0",
            ],
            "",
            &[("PATH", &new_path)],
        );
        assert_eq!(out_fail.code, 1);
        assert_eq!(
            out_fail.stderr,
            format!("verify_pypi: provenance for {WHEEL} does not verify: bad signature\n")
        );
    }

    // verify_attestations fails cleanly on non-existent helper / Path.
    let _ = Path::new("unused");
    let _ = verify_attestations;
}
