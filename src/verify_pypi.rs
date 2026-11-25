// Copyright 2026 Ryan Duguid
// SPDX-License-Identifier: Apache-2.0

//! Read a publication back from PyPI and compare it with the files that were built.

use crate::py::{self, PyPath};
use crate::pyjson::{self, Json};
use crate::verify_skills::python_on_path;
use crate::{Result, common::*};
use regex::Regex;
use sha2::{Digest, Sha256};
use std::collections::{BTreeMap, BTreeSet};
use std::path::{Path, PathBuf};
use std::process::{Command, Stdio};
use std::sync::LazyLock;
use std::time::{Duration, Instant};
use ureq::Agent;
use ureq::tls::{Certificate, PemItem, RootCerts, TlsConfig, parse_pem};

pub const PYPI: &str = "https://pypi.org";
pub const INTEGRITY_ACCEPT: &str = "application/vnd.pypi.integrity.v1+json";
pub const MAX_RESPONSE_BYTES: usize = 16 * 1024 * 1024;
// A release PyPI does not list yet is read up to 21 times, 15 seconds apart: 5 minutes.
pub const ATTEMPTS: usize = 21;
pub const DELAY: Duration = Duration::from_secs(15);

// Fulcio's Source Repository Digest extension, 1.3.6.1.4.1.57264.1.13.
const SOURCE_DIGEST_OID: [u8; 10] = [0x2b, 0x06, 0x01, 0x04, 0x01, 0x83, 0xbf, 0x30, 0x01, 0x0d];
const MAX_REDIRECTS: usize = 10;

static PROJECT_RE: LazyLock<Regex> =
    LazyLock::new(|| Regex::new(r"\A[A-Za-z0-9](?:[A-Za-z0-9._-]*[A-Za-z0-9])?\z").unwrap());
static VERSION_RE: LazyLock<Regex> =
    LazyLock::new(|| Regex::new(r"\A[A-Za-z0-9][A-Za-z0-9.!_-]*\z").unwrap());
static FILENAME_RE: LazyLock<Regex> =
    LazyLock::new(|| Regex::new(r"\A[A-Za-z0-9][A-Za-z0-9._+!-]*\z").unwrap());
static REPOSITORY_RE: LazyLock<Regex> =
    LazyLock::new(|| Regex::new(r"\A[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+\z").unwrap());
static FULL_SHA_RE: LazyLock<Regex> = LazyLock::new(|| Regex::new(r"\A[0-9a-f]{40}\z").unwrap());
static SHA256_RE: LazyLock<Regex> = LazyLock::new(|| Regex::new(r"\A[0-9a-f]{64}\z").unwrap());
static WORKFLOW_RE: LazyLock<Regex> =
    LazyLock::new(|| Regex::new(r"\A[A-Za-z0-9_.-]+\.ya?ml\z").unwrap());
static NORMALISE_RE: LazyLock<Regex> = LazyLock::new(|| Regex::new(r"[-_.]+").unwrap());

#[derive(Clone, Debug)]
pub struct Config {
    pub base_url: String,
    pub attempts: usize,
    pub delay: Duration,
    pub root_ca_pem: Option<Vec<u8>>,
    pub allowed_origin: (String, String),
}

impl Default for Config {
    fn default() -> Self {
        Self {
            base_url: PYPI.to_owned(),
            attempts: ATTEMPTS,
            delay: DELAY,
            root_ca_pem: None,
            allowed_origin: ("https".into(), "pypi.org".into()),
        }
    }
}

fn build_agent(config: &Config) -> Result<Agent> {
    let mut builder = Agent::config_builder()
        .http_status_as_error(false)
        .https_only(config.base_url == PYPI)
        .max_redirects(0)
        .max_redirects_will_error(false)
        .user_agent(ureq::config::AutoHeaderValue::None)
        .timeout_global(Some(Duration::from_secs(60)));

    if let Some(pem) = &config.root_ca_pem {
        let mut certs: Vec<Certificate<'static>> = Vec::new();
        for item in parse_pem(pem) {
            match item.map_err(|e| format!("invalid CA certificate: {e}"))? {
                PemItem::Certificate(cert) => certs.push(cert),
                _ => return Err("CA file must contain certificates".into()),
            }
        }
        if certs.is_empty() {
            return Err("CA file contained no certificates".into());
        }
        let tls = TlsConfig::builder()
            .root_certs(RootCerts::new_with_certs(&certs))
            .build();
        builder = builder.tls_config(tls);
    }

    Ok(builder.build().new_agent())
}

fn scheme_and_authority(url: &str) -> Option<(String, String)> {
    let (scheme, rest) = url.split_once("://")?;
    let authority = rest.split(&['/', '?', '#'][..]).next().unwrap_or("");
    Some((scheme.to_ascii_lowercase(), authority.to_ascii_lowercase()))
}

fn resolve_redirect(base: &str, location: &str) -> String {
    if let Some(colon) = location.find(':') {
        let prefix = &location[..colon];
        if !prefix.is_empty()
            && prefix.as_bytes()[0].is_ascii_alphabetic()
            && prefix
                .bytes()
                .all(|b| b.is_ascii_alphanumeric() || matches!(b, b'+' | b'-' | b'.'))
        {
            return location.to_owned();
        }
    }
    let Some((scheme, rest)) = base.split_once("://") else {
        return location.to_owned();
    };
    if location.starts_with("//") {
        return format!("{scheme}:{location}");
    }
    let (authority, base_path_query) = match rest.find('/') {
        Some(idx) => (&rest[..idx], &rest[idx..]),
        None => (rest, "/"),
    };
    if location.is_empty() {
        return base.to_owned();
    }
    if location.starts_with('/') {
        return format!("{scheme}://{authority}{location}");
    }
    let base_path = base_path_query.split(&['?', '#'][..]).next().unwrap_or("/");
    if location.starts_with('?') {
        return format!("{scheme}://{authority}{base_path}{location}");
    }
    if location.starts_with('#') {
        let no_frag = base.split('#').next().unwrap_or(base);
        return format!("{no_frag}{location}");
    }
    let dir = match base_path.rfind('/') {
        Some(idx) => &base_path[..=idx],
        None => "/",
    };
    format!("{scheme}://{authority}{dir}{location}")
}

fn check_redirect(
    current: &str,
    status: u16,
    response: &ureq::http::Response<ureq::Body>,
    config: &Config,
) -> Result<Option<String>> {
    if !matches!(status, 301 | 302 | 303 | 307 | 308) {
        return Ok(None);
    }
    let Some(loc_val) = response.headers().get("location") else {
        return Ok(None);
    };
    let Ok(loc_str) = loc_val.to_str() else {
        return Ok(None);
    };
    let next = resolve_redirect(current, loc_str);
    let allowed = Some((
        config.allowed_origin.0.clone(),
        config.allowed_origin.1.clone(),
    ));
    if scheme_and_authority(&next) != allowed {
        return Err(format!(
            "{current} redirected to {next}, outside https://pypi.org"
        ));
    }
    Ok(Some(next))
}

fn read_limited_body(current: &str, response: ureq::http::Response<ureq::Body>) -> Result<Vec<u8>> {
    let body = response
        .into_body()
        .into_with_config()
        .limit((MAX_RESPONSE_BYTES + 1) as u64)
        .read_to_vec()
        .map_err(|e| match e {
            ureq::Error::BodyExceedsLimit(_) => format!("{current} answered more than 16 MiB"),
            other => format!("{current} could not be read: {other}"),
        })?;
    if body.len() > MAX_RESPONSE_BYTES {
        return Err(format!("{current} answered more than 16 MiB"));
    }
    Ok(body)
}

pub fn fetch_with_config(url: &str, accept: &str, config: &Config) -> Result<Option<Vec<u8>>> {
    let agent = build_agent(config)?;
    let mut current = url.to_owned();
    for _ in 0..=MAX_REDIRECTS {
        let response = agent
            .get(&current)
            .header("Accept", accept)
            .call()
            .map_err(|e| format!("{current} could not be read: {e}"))?;
        let status = response.status().as_u16();
        if let Some(next) = check_redirect(&current, status, &response, config)? {
            current = next;
            continue;
        }
        if status == 404 {
            return Ok(None);
        }
        if !(200..300).contains(&status) {
            return Err(format!("{current} answered HTTP {status}"));
        }
        return Ok(Some(read_limited_body(&current, response)?));
    }
    Err(format!("{url} could not be read: too many redirects"))
}

pub fn fetch(url: &str, accept: &str) -> Result<Option<Vec<u8>>> {
    fetch_with_config(url, accept, &Config::default())
}

fn wait_attestation_child(
    child: &mut std::process::Child,
    timeout: Duration,
    fname: &str,
) -> Result<std::process::ExitStatus> {
    let deadline = Instant::now() + timeout;
    loop {
        match child.try_wait().map_err(|e| e.to_string())? {
            Some(status) => return Ok(status),
            None if Instant::now() >= deadline => {
                let _ = child.kill();
                let _ = child.wait();
                return Err(format!(
                    "pypi-attestations did not finish verifying {fname}"
                ));
            }
            None => std::thread::sleep(Duration::from_millis(10)),
        }
    }
}

pub fn verify_attestations(file: &Path, provenance: &Path, repository: &str) -> Result<()> {
    let python = python_on_path();
    let repo_url = format!("https://github.com/{repository}");
    let mut child = Command::new(&python)
        .args(["-m", "pypi_attestations", "verify", "pypi", "--repository"])
        .arg(&repo_url)
        .arg("--provenance-file")
        .arg(provenance)
        .arg(file)
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
        .spawn()
        .map_err(|e| format!("pypi-attestations could not be run: {e}"))?;
    let mut out_pipe = child.stdout.take().expect("piped stdout");
    let mut err_pipe = child.stderr.take().expect("piped stderr");
    let out_thread = std::thread::spawn(move || std::io::read_to_string(&mut out_pipe));
    let err_thread = std::thread::spawn(move || std::io::read_to_string(&mut err_pipe));
    let fname = file
        .file_name()
        .map(|n| n.to_string_lossy().into_owned())
        .unwrap_or_default();
    let status = match wait_attestation_child(&mut child, Duration::from_secs(600), &fname) {
        Ok(status) => status,
        Err(err) => {
            let _ = out_thread.join();
            let _ = err_thread.join();
            return Err(err);
        }
    };
    let _ = out_thread.join();
    let stderr = err_thread.join().unwrap().map_err(|e| e.to_string())?;
    if !status.success() {
        let reason = py::splitlines(py::strip(&stderr))
            .last()
            .copied()
            .unwrap_or("no message");
        return Err(format!("provenance for {fname} does not verify: {reason}"));
    }
    Ok(())
}

fn element(data: &[u8], mut offset: usize) -> Result<(u8, &[u8], usize)> {
    if offset + 2 > data.len() {
        return Err("truncated DER element".into());
    }
    let tag = data[offset];
    let len_byte = data[offset + 1];
    offset += 2;
    let length = if len_byte & 0x80 != 0 {
        let count = usize::from(len_byte & 0x7F);
        if count == 0 || count > 4 || offset + count > data.len() {
            return Err("unsupported DER length".into());
        }
        let mut val = 0usize;
        for &b in &data[offset..offset + count] {
            val = (val << 8) | usize::from(b);
        }
        offset += count;
        val
    } else {
        usize::from(len_byte)
    };
    let Some(end) = offset.checked_add(length) else {
        return Err("truncated DER element".into());
    };
    if end > data.len() {
        return Err("truncated DER element".into());
    }
    Ok((tag, &data[offset..end], end))
}

fn elements(data: &[u8]) -> Result<Vec<(u8, &[u8])>> {
    let mut items = Vec::new();
    let mut offset = 0;
    while offset < data.len() {
        let (tag, contents, next) = element(data, offset)?;
        items.push((tag, contents));
        offset = next;
    }
    Ok(items)
}

fn parse_commit_inner(der: &[u8]) -> Result<Vec<String>> {
    let top = elements(der)?;
    let [(_, certificate)] = top.as_slice() else {
        return Err("expected one top-level DER element".into());
    };
    let cert_items = elements(certificate)?;
    let (_, tbs) = cert_items
        .first()
        .ok_or_else(|| "empty certificate sequence".to_owned())?;
    let mut commits = Vec::new();
    for (tag, block) in elements(tbs)? {
        if tag != 0xA3 {
            continue;
        }
        let wrapped = elements(block)?;
        let [(_, extensions)] = wrapped.as_slice() else {
            return Err("malformed extensions block".into());
        };
        for (_, extension) in elements(extensions)? {
            let parts = elements(extension)?;
            if parts.first() == Some(&(0x06, &SOURCE_DIGEST_OID[..])) {
                let ext_val = parts
                    .last()
                    .ok_or_else(|| "missing extension value".to_owned())?
                    .1;
                let inner = elements(ext_val)?;
                let [(text_tag, text)] = inner.as_slice() else {
                    return Err("malformed extension payload".into());
                };
                if *text_tag != 0x0C {
                    return Err("source commit is not a UTF8String".into());
                }
                let decoded = std::str::from_utf8(text).map_err(|e| e.to_string())?;
                commits.push(decoded.to_owned());
            }
        }
    }
    Ok(commits)
}

pub fn certificate_commit(der: &[u8]) -> Result<Option<String>> {
    let commits = parse_commit_inner(der)
        .map_err(|err| format!("signing certificate is not readable: {err}"))?;
    if commits.len() > 1 {
        return Err("signing certificate names its source commit more than once".into());
    }
    Ok(commits.into_iter().next())
}

fn decode_base64_strict(input: &str) -> Option<Vec<u8>> {
    let bytes = input.as_bytes();
    if !bytes.len().is_multiple_of(4) {
        return None;
    }
    if bytes.is_empty() {
        return Some(Vec::new());
    }
    let val = |b: u8| -> Option<u8> {
        match b {
            b'A'..=b'Z' => Some(b - b'A'),
            b'a'..=b'z' => Some(b - b'a' + 26),
            b'0'..=b'9' => Some(b - b'0' + 52),
            b'+' => Some(62),
            b'/' => Some(63),
            _ => None,
        }
    };
    let mut out = Vec::with_capacity((bytes.len() / 4) * 3);
    let (chunks, _) = bytes.as_chunks::<4>();
    let total_chunks = chunks.len();
    for (idx, chunk) in chunks.iter().enumerate() {
        let is_last = idx + 1 == total_chunks;
        let a = val(chunk[0])?;
        let b = val(chunk[1])?;
        match (chunk[2], chunk[3]) {
            (b'=', b'=') if is_last => {
                out.push((a << 2) | (b >> 4));
            }
            (c3, b'=') if is_last => {
                let c = val(c3)?;
                out.push((a << 2) | (b >> 4));
                out.push(((b & 0x0F) << 4) | (c >> 2));
            }
            (c3, c4) => {
                let c = val(c3)?;
                let d = val(c4)?;
                out.push((a << 2) | (b >> 4));
                out.push(((b & 0x0F) << 4) | (c >> 2));
                out.push(((c & 0x03) << 6) | d);
            }
        }
    }
    Some(out)
}

fn served_files(raw: &[u8], label: &str) -> Result<BTreeMap<String, String>> {
    let document =
        pyjson::loads_bytes(raw).map_err(|_| format!("PyPI's JSON for {label} is not JSON"))?;
    let Some(urls) = document.get("urls").and_then(Json::as_array) else {
        return Err(format!("PyPI's JSON for {label} has no file list"));
    };
    let mut served = BTreeMap::new();
    for entry in urls {
        let Some(name) = entry.get("filename").and_then(Json::as_str) else {
            return Err(format!(
                "PyPI's JSON for {label} has an unreadable file entry"
            ));
        };
        let Some(digest) = entry
            .get("digests")
            .and_then(|d| d.get("sha256"))
            .and_then(Json::as_str)
            .filter(|d| SHA256_RE.is_match(d))
        else {
            return Err(format!(
                "PyPI's JSON for {label} has an unreadable file entry"
            ));
        };
        if served.contains_key(name) {
            return Err(format!("PyPI's JSON for {label} lists {name} twice"));
        }
        served.insert(name.to_owned(), digest.to_owned());
    }
    Ok(served)
}

fn publisher_repr(publisher: Option<&Json>) -> String {
    let Some(obj @ Json::Object(_)) = publisher else {
        return "None".into();
    };
    let items: Vec<String> = ["kind", "repository", "workflow"]
        .into_iter()
        .map(|key| {
            let value = obj.get(key).map_or_else(|| "None".into(), Json::repr);
            format!("{}: {value}", py::repr_str(key))
        })
        .collect();
    format!("{{{}}}", items.join(", "))
}

fn check_bundle_publisher(
    bundle: &Json,
    name: &str,
    repository: &str,
    workflow: &str,
    expected: &str,
) -> Result<()> {
    let publisher = bundle
        .get("publisher")
        .filter(|p| matches!(p, Json::Object(_)));
    let actual_matches = publisher.is_some_and(|p| {
        p.get("kind").and_then(Json::as_str) == Some("GitHub")
            && p.get("repository").and_then(Json::as_str) == Some(repository)
            && p.get("workflow").and_then(Json::as_str) == Some(workflow)
    });
    if !actual_matches {
        return Err(format!(
            "provenance for {name} names publisher {}, expected {expected}",
            publisher_repr(publisher)
        ));
    }
    Ok(())
}

fn check_bundle_attestations(bundle: &Json, name: &str, commit: &str) -> Result<()> {
    let Some(attestations) = bundle
        .get("attestations")
        .and_then(Json::as_array)
        .filter(|a| !a.is_empty())
    else {
        return Err(format!(
            "provenance for {name} has a bundle with no attestations"
        ));
    };
    for attestation in attestations {
        let Some(cert_b64) = attestation
            .get("verification_material")
            .and_then(|vm| vm.get("certificate"))
            .and_then(Json::as_str)
        else {
            return Err(format!("provenance for {name} has no readable certificate"));
        };
        let Some(der) = decode_base64_strict(cert_b64) else {
            return Err(format!("provenance for {name} has no readable certificate"));
        };
        let signed_at = certificate_commit(&der)?;
        if signed_at.as_deref() != Some(commit) {
            return Err(format!(
                "provenance for {name} was signed at commit {}, expected {commit}",
                signed_at.as_deref().unwrap_or("None")
            ));
        }
    }
    Ok(())
}

fn check_provenance(
    raw: &[u8],
    name: &str,
    repository: &str,
    commit: &str,
    workflow: &str,
) -> Result<()> {
    let document =
        pyjson::loads_bytes(raw).map_err(|_| format!("provenance for {name} is not JSON"))?;
    let version_ok = matches!(document.get("version"), Some(Json::Int(n)) if n == &1.into());
    if !matches!(document, Json::Object(_)) || !version_ok {
        return Err(format!(
            "provenance for {name} is not a version 1 provenance object"
        ));
    }
    let Some(bundles) = document
        .get("attestation_bundles")
        .and_then(Json::as_array)
        .filter(|b| !b.is_empty())
    else {
        return Err(format!(
            "provenance for {name} holds no attestation bundles"
        ));
    };
    let expected = format!(
        "{{'kind': 'GitHub', 'repository': {}, 'workflow': {}}}",
        py::repr_str(repository),
        py::repr_str(workflow)
    );
    for bundle in bundles {
        check_bundle_publisher(bundle, name, repository, workflow, &expected)?;
        check_bundle_attestations(bundle, name, commit)?;
    }
    Ok(())
}

fn check_names(
    project: &str,
    version: &str,
    repository: &str,
    commit: &str,
    workflow: &str,
) -> Result<()> {
    for (pattern, value, kind) in [
        (&*PROJECT_RE, project, "a valid project name"),
        (&*VERSION_RE, version, "a valid version"),
        (&*REPOSITORY_RE, repository, "an OWNER/REPO repository"),
        (&*FULL_SHA_RE, commit, "a full commit SHA"),
        (&*WORKFLOW_RE, workflow, "a workflow file name"),
    ] {
        if !pattern.is_match(value) {
            return Err(format!("'{value}' is not {kind}"));
        }
    }
    Ok(())
}

fn built_files(files: &[PyPath]) -> Result<BTreeMap<String, PathBuf>> {
    let mut built = BTreeMap::new();
    for path in files {
        let name = path.name();
        let fs = path.fs();
        if !FILENAME_RE.is_match(name) || !fs.is_file() || symlink(&fs) {
            return Err(format!("{path} is not a regular distribution file"));
        }
        if built.contains_key(name) {
            return Err(format!("{name} is named twice"));
        }
        built.insert(name.to_owned(), fs);
    }
    if built.is_empty() {
        return Err("no files to compare".into());
    }
    Ok(built)
}

fn compare_served(
    served: &BTreeMap<String, String>,
    digests: &BTreeMap<String, String>,
    label: &str,
) -> Result<()> {
    let extra: Vec<&str> = served
        .keys()
        .filter(|k| !digests.contains_key(k.as_str()))
        .map(String::as_str)
        .collect();
    if !extra.is_empty() {
        return Err(format!(
            "PyPI serves files for {label} that were not built: {}",
            repr_list(extra)
        ));
    }
    let missing: Vec<&str> = digests
        .keys()
        .filter(|k| !served.contains_key(k.as_str()))
        .map(String::as_str)
        .collect();
    if !missing.is_empty() {
        return Err(format!(
            "PyPI does not serve these files for {label}: {}",
            repr_list(missing)
        ));
    }
    for (name, built_digest) in digests {
        if &served[name] != built_digest {
            return Err(format!(
                "PyPI serves {name} with SHA-256 {}, built {built_digest}",
                served[name]
            ));
        }
    }
    Ok(())
}

pub type FetchFn<'a> = dyn FnMut(&str, &str) -> Result<Option<Vec<u8>>> + 'a;
pub type VerifyFn<'a> = dyn FnMut(&Path, &Path, &str) -> Result<()> + 'a;
pub type SleepFn<'a> = dyn FnMut(Duration) + 'a;

fn built_digests(built: &BTreeMap<String, PathBuf>) -> Result<BTreeMap<String, String>> {
    let mut digests = BTreeMap::new();
    for (name, path) in built {
        let bytes = read_bytes(path)?;
        let hex: String = Sha256::digest(&bytes)
            .iter()
            .map(|b| format!("{b:02x}"))
            .collect();
        digests.insert(name.clone(), hex);
    }
    Ok(digests)
}

fn poll_release_files(
    release_url: &str,
    label: &str,
    built_keys: &BTreeSet<&str>,
    config: &Config,
    get: &mut FetchFn<'_>,
    sleep: &mut SleepFn<'_>,
) -> Result<BTreeMap<String, String>> {
    let mut served = None;
    for attempt in 0..config.attempts {
        let raw = get(release_url, "application/json")?;
        served = match raw {
            None => None,
            Some(bytes) => Some(served_files(&bytes, label)?),
        };
        if let Some(ref map) = served {
            let served_keys: BTreeSet<&str> = map.keys().map(String::as_str).collect();
            if !served_keys.is_subset(built_keys) || built_keys.is_subset(&served_keys) {
                break;
            }
        }
        if attempt + 1 < config.attempts {
            sleep(config.delay);
        }
    }
    served.ok_or_else(|| format!("PyPI does not serve {label}"))
}

struct ProvenanceExpectation<'a> {
    base_url: &'a str,
    normalised: &'a str,
    version: &'a str,
    repository: &'a str,
    commit: &'a str,
    workflow: &'a str,
}

fn verify_all_provenances(
    built: &BTreeMap<String, PathBuf>,
    expect: &ProvenanceExpectation<'_>,
    get: &mut FetchFn<'_>,
    verify: &mut VerifyFn<'_>,
) -> Result<()> {
    let scratch = tempfile::tempdir().map_err(|e| e.to_string())?;
    for (name, file_path) in built {
        let url = format!(
            "{}/integrity/{}/{}/{}/provenance",
            expect.base_url, expect.normalised, expect.version, name
        );
        let Some(raw) = get(&url, INTEGRITY_ACCEPT)? else {
            return Err(format!("PyPI serves no provenance for {name}"));
        };
        check_provenance(
            &raw,
            name,
            expect.repository,
            expect.commit,
            expect.workflow,
        )?;
        let prov_path = scratch.path().join(format!("{name}.provenance"));
        write_bytes(&prov_path, &raw)?;
        verify(file_path, &prov_path, expect.repository)?;
    }
    Ok(())
}

#[allow(clippy::too_many_arguments)]
pub fn verify_publication_with(
    project: &str,
    version: &str,
    files: &[PyPath],
    repository: &str,
    commit: &str,
    workflow: &str,
    config: &Config,
    get: &mut FetchFn<'_>,
    verify: &mut VerifyFn<'_>,
    sleep: &mut SleepFn<'_>,
) -> Result<String> {
    check_names(project, version, repository, commit, workflow)?;
    let built = built_files(files)?;
    let digests = built_digests(&built)?;
    let normalised = NORMALISE_RE.replace_all(project, "-").to_ascii_lowercase();
    let label = format!("{normalised} {version}");
    let built_keys: BTreeSet<&str> = built.keys().map(String::as_str).collect();
    let release_url = format!("{}/pypi/{normalised}/{version}/json", config.base_url);
    let served_map = poll_release_files(&release_url, &label, &built_keys, config, get, sleep)?;
    compare_served(&served_map, &digests, &label)?;
    let expect = ProvenanceExpectation {
        base_url: &config.base_url,
        normalised: &normalised,
        version,
        repository,
        commit,
        workflow,
    };
    verify_all_provenances(&built, &expect, get, verify)?;
    let names_joined = built.keys().cloned().collect::<Vec<_>>().join(", ");
    Ok(format!(
        "verify_pypi: PyPI serves {label} as built, with provenance from {repository} at {commit}: {names_joined}"
    ))
}

pub fn verify_publication(
    project: &str,
    version: &str,
    files: &[PyPath],
    repository: &str,
    commit: &str,
    workflow: &str,
    config: &Config,
) -> Result<String> {
    verify_publication_with(
        project,
        version,
        files,
        repository,
        commit,
        workflow,
        config,
        &mut |url, accept| fetch_with_config(url, accept, config),
        &mut verify_attestations,
        &mut |d| std::thread::sleep(d),
    )
}
