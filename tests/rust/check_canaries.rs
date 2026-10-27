//! Release-family canary pins and live successful-run evidence.
//! Ported from tests/test_canaries.py and tests/test_canaries_refusals.py.
//! `test_ci_validates_manifest_and_runs_live_audit_only_on_schedule` reads
//! ci.yml rather than the checker, and stays with the Python tests.
mod support;

use chrono::{DateTime, TimeZone, Utc};
use release_policy::check_canaries::{
    self as cc, Canary, CheckResult, Failure, GitHubError, Live, LiveError,
};
use release_policy::pyjson::{self, Json};
use serde_json::{Value, json};
use support::{FakeGh, rule};

const SHA: &str = "1111111111111111111111111111111111111111";
const OLDER_SHA: &str = "2222222222222222222222222222222222222222";
const HEAD: &str = "3333333333333333333333333333333333333333";
const RECORDED: &str = "2026-08-27T00:00:00Z";

fn before_expiry() -> DateTime<Utc> {
    Utc.with_ymd_and_hms(2026, 11, 25, 0, 0, 0).unwrap()
}

fn after_expiry() -> DateTime<Utc> {
    Utc.with_ymd_and_hms(2026, 11, 27, 0, 0, 0).unwrap()
}

fn policy_workflow(family: &str) -> String {
    if family == "verify" {
        "verify-skills.yml".into()
    } else {
        format!("release-{family}.yml")
    }
}

fn pin(family: &str) -> String {
    format!(
        "uses: ryanduguid/release-policy/.github/workflows/{}@{SHA}\n",
        policy_workflow(family)
    )
}

fn manifest_with(
    current: &str,
    evidence: &str,
    family: &str,
    reference: &str,
    fields: Value,
) -> Value {
    let mut entry = json!({
        "family": family,
        "repository": "ryanduguid/example",
        "workflow_path": ".github/workflows/release.yml",
        "policy_workflow": format!(".github/workflows/{}", policy_workflow(family)),
        "current_policy_sha": current,
        "evidence": {"run_id": 123, "head_sha": HEAD, "ref": reference, "policy_sha": evidence, "started_at": RECORDED},
    });
    for (key, value) in fields.as_object().unwrap() {
        entry[key] = value.clone();
    }
    json!({"schema": 1, "families": [entry]})
}

fn manifest(family: &str, reference: &str, fields: Value) -> Value {
    manifest_with(SHA, SHA, family, reference, fields)
}

fn python() -> Value {
    manifest("python", "v1.2.3", json!({}))
}

fn run_payload(policy_sha: &str, run_id: i64, family: &str, reference: Value) -> Value {
    json!({
        "id": run_id,
        "conclusion": "success",
        "event": "push",
        "head_branch": reference,
        "head_sha": HEAD,
        "path": ".github/workflows/release.yml",
        "run_started_at": RECORDED,
        "referenced_workflows": [{
            "path": format!("ryanduguid/release-policy/.github/workflows/{}@{policy_sha}", policy_workflow(family)),
            "sha": policy_sha,
        }],
    })
}

fn run(reference: &str) -> Value {
    run_payload(SHA, 123, "python", json!(reference))
}

fn to_json(value: &Value) -> Json {
    pyjson::loads(&value.to_string()).unwrap()
}

fn parse(document: &Value) -> Result<Vec<Canary>, String> {
    cc::parse_manifest(&to_json(document))
}

/// A GitHub answer: a document, or a failed read with its HTTP status.
enum Reply {
    Body(Value),
    Fail(&'static str, Option<u16>),
}

fn body(value: Value) -> Reply {
    Reply::Body(value)
}

type Verifier = Box<dyn FnMut(&Canary) -> Result<(), String>>;

struct Fake<F: FnMut(&str) -> Reply> {
    json: F,
    pin: String,
    verifier: Option<Verifier>,
    requests: Vec<String>,
}

impl<F: FnMut(&str) -> Reply> Live for Fake<F> {
    fn fetch_json(&mut self, endpoint: &str) -> Result<Json, LiveError> {
        self.requests.push(endpoint.into());
        match (self.json)(endpoint) {
            Reply::Body(value) => Ok(to_json(&value)),
            Reply::Fail(message, status) => Err(LiveError::GitHub(GitHubError {
                message: message.into(),
                status,
            })),
        }
    }
    fn fetch_text(&mut self, endpoint: &str) -> Result<String, LiveError> {
        self.requests.push(endpoint.into());
        Ok(self.pin.clone())
    }
    fn verify_provenance(&mut self, canary: &Canary) -> Result<(), Failure> {
        (self.verifier.as_mut().unwrap())(canary).map_err(Failure::Runtime)
    }
}

fn audit_with(
    document: &Value,
    json: impl FnMut(&str) -> Reply,
    pin: &str,
    verifier: Option<Verifier>,
    now: DateTime<Utc>,
) -> CheckResult {
    let canaries = parse(document).unwrap();
    let release_evidence = verifier.is_some();
    let mut fake = Fake {
        json,
        pin: pin.into(),
        verifier,
        requests: Vec::new(),
    };
    cc::check_live(&canaries, &mut fake, release_evidence, now).unwrap()
}

/// `check_live` with no verifier, at the default clock of the earlier tests.
fn live(document: &Value, json: impl FnMut(&str) -> Reply, pin: &str) -> CheckResult {
    audit_with(document, json, pin, None, Utc::now())
}

fn accept() -> Option<Verifier> {
    Some(Box::new(|_| Ok(())))
}

fn has(errors: &[String], fragment: &str) -> bool {
    errors.iter().any(|e| e.contains(fragment))
}

/// A listing of successes, newest first as the tests give them.
fn listing(runs: Vec<Value>) -> Reply {
    body(json!({"workflow_runs": runs}))
}

/// The usual fetcher: compare `status`, the evidence run, then `runs`.
fn standard(status: &'static str, evidence: Value, runs: Vec<Value>) -> impl FnMut(&str) -> Reply {
    move |endpoint| {
        if endpoint.contains("/compare/") {
            body(json!({"status": status}))
        } else if endpoint.ends_with("/actions/runs/123") {
            body(evidence.clone())
        } else {
            listing(runs.clone())
        }
    }
}

// CanaryManifestTests

#[test]
fn root_tags_remain_valid_with_an_omitted_or_empty_prefix() {
    for family in ["archive", "python", "skills", "verify"] {
        let reference = if family == "verify" { "main" } else { "v1.2.3" };
        for fields in [json!({}), json!({"tag_prefix": ""})] {
            let parsed = parse(&manifest(family, reference, fields)).unwrap();
            assert_eq!(parsed[0].tag_prefix, "");
            assert_eq!(parsed[0].evidence.reference, reference);
        }
    }
}

#[test]
fn accepts_explicit_namespaces_for_archive_and_python() {
    for family in ["archive", "python"] {
        for reference in ["example-tool/v0.0.0", "example-tool/v1.2.3"] {
            let parsed = parse(&manifest(
                family,
                reference,
                json!({"tag_prefix": "example-tool"}),
            ))
            .unwrap();
            assert_eq!(parsed[0].tag_prefix, "example-tool");
            assert_eq!(parsed[0].evidence.reference, reference);
        }
    }
}

#[test]
fn rejects_malformed_prefixes_and_unsupported_families() {
    for prefix in [
        Value::Null,
        json!(false),
        json!(42),
        json!([]),
        json!({}),
        json!(" "),
        json!("../tool"),
        json!("./tool"),
        json!("foo/bar"),
        json!("foo\\bar"),
        json!("/foo"),
        json!("foo/"),
        json!("Foo"),
        json!("foo_bar"),
        json!("foo.bar"),
        json!("-foo"),
        json!("foo-"),
        json!("foo--bar"),
        json!("foo\n"),
    ] {
        let error = parse(&manifest(
            "python",
            "example-tool/v1.2.3",
            json!({"tag_prefix": prefix}),
        ))
        .unwrap_err();
        assert!(error.contains("tag_prefix"), "{prefix}: {error}");
    }
    for family in ["skills", "verify"] {
        let error = parse(&manifest(
            family,
            "main",
            json!({"tag_prefix": "example-tool"}),
        ))
        .unwrap_err();
        assert!(error.contains("tag_prefix"), "{family}: {error}");
    }
}

#[test]
fn rejects_tags_outside_the_opted_in_namespace() {
    for fields in [json!({}), json!({"tag_prefix": ""})] {
        let error = parse(&manifest("python", "example-tool/v1.2.3", fields)).unwrap_err();
        assert!(error.contains("canonical version tag"));
    }
    for reference in [
        "v1.2.3",
        "other-tool/v1.2.3",
        "example-tool-extra/v1.2.3",
        "example-tool//v1.2.3",
        "example-tool/../v1.2.3",
        "example-tool/nested/v1.2.3",
        "Example-tool/v1.2.3",
        "example-tool/1.2.3",
        "example-tool/v01.2.3",
        "example-tool/v1.02.3",
        "example-tool/v1.2.03",
        "example-tool/v1.2",
        "example-tool/v1.2.3-rc1",
        "example-tool/v1.2.3+build",
        "example-tool/v1.2.3\n",
        "refs/tags/example-tool/v1.2.3",
    ] {
        let error = parse(&manifest(
            "python",
            reference,
            json!({"tag_prefix": "example-tool"}),
        ))
        .unwrap_err();
        assert!(
            error.contains("canonical version tag"),
            "{reference:?}: {error}"
        );
    }
}

#[test]
fn optional_prefix_does_not_allow_unknown_or_missing_keys() {
    for document in [
        Value::Null,
        json!({"schema": 1}),
        manifest(
            "python",
            "v1.2.3",
            json!({"tag_prefix": "example-tool", "extra": true}),
        ),
        manifest("python", "v1.2.3", json!({"tag_prefixes": "example-tool"})),
        json!({"schema": 1, "families": [{"tag_prefix": "example-tool"}]}),
    ] {
        assert!(
            parse(&document).unwrap_err().contains("must contain"),
            "{document}"
        );
    }
}

#[test]
fn live_namespaced_audit_ignores_unrelated_or_invalid_successes() {
    for family in ["archive", "python"] {
        let recorded = run_payload(SHA, 123, family, json!("example-tool/v1.2.3"));
        let mut failed = run_payload(SHA, 123, family, json!("example-tool/v1.2.4"));
        failed["conclusion"] = json!("failure");
        let mut runs = vec![Value::Null, failed];
        for reference in [
            Value::Null,
            json!(123),
            json!("main"),
            json!("v9.9.9"),
            json!("other-tool/v9.9.9"),
            json!("example-tool/v01.2.3"),
            json!("example-tool/v1.2.3-rc1"),
        ] {
            runs.push(run_payload(SHA, 456, family, reference));
        }
        runs.push(recorded.clone());
        let document = manifest(
            family,
            "example-tool/v1.2.3",
            json!({"tag_prefix": "example-tool"}),
        );
        let result = live(&document, standard("behind", recorded, runs), &pin(family));
        assert_eq!(result.errors, Vec::<String>::new());
        assert_eq!(result.warnings, Vec::<String>::new());
    }
}

#[test]
fn live_namespaced_audit_rejects_stale_missing_or_wrong_evidence() {
    let recorded = run("example-tool/v1.2.3");
    let sibling = run_payload(SHA, 456, "python", json!("other-tool/v1.2.3"));
    let newer = run_payload(SHA, 789, "python", json!("example-tool/v1.2.4"));
    for (evidence, runs, message) in [
        (
            recorded.clone(),
            vec![newer, recorded.clone()],
            "latest successful run",
        ),
        (
            recorded.clone(),
            vec![sibling.clone()],
            "no relevant successful workflow run",
        ),
        (sibling, vec![recorded], "evidence run head_branch"),
    ] {
        let document = manifest(
            "python",
            "example-tool/v1.2.3",
            json!({"tag_prefix": "example-tool"}),
        );
        let result = live(
            &document,
            standard("behind", evidence, runs),
            &pin("python"),
        );
        assert!(
            has(&result.errors, message),
            "{message}: {:?}",
            result.errors
        );
        assert!(result.warnings.is_empty());
    }
}

#[test]
fn live_audit_orders_successes_by_start_time() {
    let recorded = run("v1.2.3");
    let mut older = run_payload(SHA, 100, "python", json!("v1.2.2"));
    older["run_started_at"] = json!("2026-08-01T00:00:00Z");
    let result = live(
        &python(),
        standard("behind", recorded.clone(), vec![older, recorded]),
        &pin("python"),
    );
    assert!(result.errors.is_empty(), "{:?}", result.errors);
    assert!(result.warnings.is_empty());
}

#[test]
fn live_legacy_release_and_verify_select_only_their_expected_refs() {
    for family in ["archive", "python", "skills", "verify"] {
        let reference = if family == "verify" { "main" } else { "v1.2.3" };
        let recorded = run_payload(SHA, 123, family, json!(reference));
        let runs = vec![
            run_payload(SHA, 456, family, json!("other/v9.9.9")),
            recorded.clone(),
        ];
        let result = live(
            &manifest(family, reference, json!({})),
            standard("identical", recorded, runs),
            &pin(family),
        );
        assert!(result.errors.is_empty(), "{family}: {:?}", result.errors);
        assert!(result.warnings.is_empty());
    }
}

#[test]
fn validates_shape_and_rejects_duplicates_or_bad_sha() {
    let parsed = parse(&python()).unwrap();
    assert_eq!(
        parsed.iter().map(|c| c.family.as_str()).collect::<Vec<_>>(),
        ["python"]
    );
    let mut duplicate = python();
    let entry = duplicate["families"][0].clone();
    duplicate["families"] = json!([entry.clone(), entry]);
    assert!(parse(&duplicate).unwrap_err().contains("duplicate"));
    let invalid = manifest_with("main", SHA, "python", "v1.2.3", json!({}));
    assert!(parse(&invalid).unwrap_err().contains("full lower-case SHA"));
}

#[test]
fn live_check_accepts_current_pin_and_latest_exact_success() {
    let canaries = parse(&python()).unwrap();
    let mut fake = Fake {
        json: |endpoint: &str| {
            if endpoint == format!("repos/ryanduguid/release-policy/compare/main...{SHA}") {
                body(json!({"status": "behind"}))
            } else if endpoint.ends_with("/actions/runs/123") {
                body(run("v1.2.3"))
            } else if endpoint.contains("/actions/workflows/") {
                listing(vec![run("v1.2.3")])
            } else {
                panic!("{endpoint}")
            }
        },
        pin: pin("python"),
        verifier: None,
        requests: Vec::new(),
    };
    let result = cc::check_live(&canaries, &mut fake, false, Utc::now()).unwrap();
    assert!(result.errors.is_empty() && result.warnings.is_empty());
    assert_eq!(fake.requests.len(), 4);
}

#[test]
fn detects_stale_current_pin_and_newer_success() {
    let older_pin = format!(
        "uses: ryanduguid/release-policy/.github/workflows/release-python.yml@{OLDER_SHA}\n"
    );
    let runs = vec![
        run_payload(SHA, 456, "python", json!("v1.2.3")),
        run("v1.2.3"),
    ];
    let result = live(
        &python(),
        standard("behind", run("v1.2.3"), runs),
        &older_pin,
    );
    assert!(has(&result.errors, "current pin"));
    assert!(has(&result.errors, "latest successful"));
}

#[test]
fn records_prior_pin_release_evidence_as_an_explicit_warning() {
    let document = manifest_with(SHA, OLDER_SHA, "python", "v1.2.3", json!({}));
    let evidence = run_payload(OLDER_SHA, 123, "python", json!("v1.2.3"));
    let result = live(
        &document,
        standard("behind", evidence.clone(), vec![evidence]),
        &pin("python"),
    );
    assert!(result.errors.is_empty(), "{:?}", result.errors);
    assert_eq!(result.warnings.len(), 1);
    assert!(result.warnings[0].contains("current pin lacks release evidence"));
}

#[test]
fn verify_accepts_a_newer_success_running_the_current_pin() {
    let recorded = run_payload(SHA, 123, "verify", json!("main"));
    let newer = run_payload(SHA, 456, "verify", json!("main"));
    let result = live(
        &manifest("verify", "main", json!({})),
        standard("behind", recorded.clone(), vec![newer, recorded]),
        &pin("verify"),
    );
    assert!(
        result.errors.is_empty() && result.warnings.is_empty(),
        "{:?}",
        result.errors
    );
}

#[test]
fn verify_rejects_a_latest_success_on_a_superseded_pin() {
    let recorded = run_payload(SHA, 123, "verify", json!("main"));
    let superseded = run_payload(OLDER_SHA, 456, "verify", json!("main"));
    let result = live(
        &manifest("verify", "main", json!({})),
        standard("behind", recorded.clone(), vec![superseded, recorded]),
        &pin("verify"),
    );
    assert_eq!(result.errors.len(), 1);
    assert!(result.errors[0].contains("used policy pin"));
    assert!(result.warnings.is_empty());
}

#[test]
fn verify_does_not_record_a_release_evidence_warning() {
    let recorded = run_payload(OLDER_SHA, 123, "verify", json!("main"));
    let newer = run_payload(SHA, 456, "verify", json!("main"));
    let document = manifest_with(SHA, OLDER_SHA, "verify", "main", json!({}));
    let result = live(
        &document,
        standard("behind", recorded.clone(), vec![newer, recorded]),
        &pin("verify"),
    );
    assert!(
        result.errors.is_empty() && result.warnings.is_empty(),
        "{:?}",
        result.errors
    );
}

#[test]
fn rejects_a_current_pin_unreachable_from_policy_main() {
    for (compare, message) in [
        (Some(json!({"status": "ahead"})), "compare status 'ahead'"),
        (
            Some(json!({"status": "diverged"})),
            "compare status 'diverged'",
        ),
        (Some(json!({"unexpected": true})), "compare status None"),
        (None, "No commit found"),
    ] {
        let result = live(
            &python(),
            move |endpoint: &str| {
                if endpoint.contains("/compare/") {
                    match &compare {
                        Some(value) => body(value.clone()),
                        None => Reply::Fail("HTTP 404: No commit found", None),
                    }
                } else if endpoint.ends_with("/actions/runs/123") {
                    body(run("v1.2.3"))
                } else {
                    listing(vec![run("v1.2.3")])
                }
            },
            &pin("python"),
        );
        assert_eq!(result.errors.len(), 1, "{:?}", result.errors);
        assert!(result.errors[0].contains("is not reachable from release-policy main"));
        assert!(result.errors[0].contains(message), "{:?}", result.errors);
        assert!(result.warnings.is_empty());
    }
}

#[test]
fn load_rejects_noncanonical_json() {
    let dir = tempfile::tempdir().unwrap();
    let path = dir.path().join("canaries.json");
    std::fs::write(&path, "{\"families\": [], \"schema\": 1}").unwrap();
    assert!(cc::load_manifest(&path).unwrap_err().contains("canonical"));
}

#[test]
fn live_finds_a_namespaced_run_hidden_behind_a_full_first_page() {
    let recorded = run("example-tool/v1.2.3");
    let irrelevant = (0..100)
        .map(|i| run_payload(SHA, 900 + i, "python", json!("other-tool/v1.0.0")))
        .collect::<Vec<_>>();
    let document = manifest(
        "python",
        "example-tool/v1.2.3",
        json!({"tag_prefix": "example-tool"}),
    );
    let result = live(
        &document,
        move |endpoint: &str| {
            if endpoint.contains("/compare/") {
                body(json!({"status": "behind"}))
            } else if endpoint.ends_with("/actions/runs/123") {
                body(recorded.clone())
            } else if endpoint.ends_with("page=1") {
                listing(irrelevant.clone())
            } else {
                listing(vec![recorded.clone()])
            }
        },
        &pin("python"),
    );
    assert!(
        result.errors.is_empty() && result.warnings.is_empty(),
        "{:?}",
        result.errors
    );
}

#[test]
fn live_reports_its_own_condition_when_every_page_is_searched() {
    let page = (0..100)
        .map(|i| run_payload(SHA, 900 + i, "python", json!("other-tool/v1.0.0")))
        .collect::<Vec<_>>();
    let document = manifest(
        "python",
        "example-tool/v1.2.3",
        json!({"tag_prefix": "example-tool"}),
    );
    let result = live(
        &document,
        move |endpoint: &str| {
            if endpoint.contains("/compare/") {
                body(json!({"status": "behind"}))
            } else {
                listing(page.clone())
            }
        },
        &pin("python"),
    );
    assert!(
        has(&result.errors, "pages of successful runs"),
        "{:?}",
        result.errors
    );
    assert!(!has(&result.errors, "no relevant successful workflow run"));
}

// CanaryRetentionTests

fn release(tag: &str, published_at: &str, draft: bool) -> Value {
    json!({"tag_name": tag, "published_at": published_at, "draft": draft})
}

#[derive(Clone)]
struct Expired {
    status: u16,
    releases: Option<Result<Value, &'static str>>,
    listing: Option<Value>,
    details: Vec<(i64, Result<Value, &'static str>)>,
}

impl Default for Expired {
    fn default() -> Self {
        Expired {
            status: 404,
            releases: None,
            listing: None,
            details: Vec::new(),
        }
    }
}

fn expired(spec: Expired) -> impl FnMut(&str) -> Reply {
    move |endpoint| {
        if endpoint.contains("/compare/") {
            return body(json!({"status": "behind"}));
        }
        if endpoint.ends_with("/actions/runs/123") {
            return Reply::Fail("gone", Some(spec.status));
        }
        if endpoint.contains("/releases?") {
            return match &spec.releases {
                None => body(json!([release("v1.2.3", "2026-08-27T00:05:00Z", false)])),
                Some(Ok(value)) => body(value.clone()),
                Some(Err(message)) => Reply::Fail(message, Some(429)),
            };
        }
        if endpoint.contains("/actions/workflows/") {
            return body(spec.listing.clone().unwrap_or(json!({"workflow_runs": []})));
        }
        for (run_id, detail) in &spec.details {
            if endpoint.ends_with(&format!("/actions/runs/{run_id}")) {
                return match detail {
                    Ok(value) => body(value.clone()),
                    Err(message) => Reply::Fail(message, Some(404)),
                };
            }
        }
        panic!("{endpoint}")
    }
}

fn verify_run(run_id: i64, started: &str, policy_sha: &str) -> Value {
    let mut value = run_payload(policy_sha, run_id, "verify", json!("main"));
    value["run_started_at"] = json!(started);
    value
}

fn verify_manifest() -> Value {
    manifest("verify", "main", json!({}))
}

#[test]
fn manifest_requires_a_utc_start_time() {
    let mut document = python();
    document["families"][0]["evidence"]["started_at"] = json!("2026-08-27 00:00");
    assert!(parse(&document).unwrap_err().contains("UTC timestamp"));
}

#[test]
fn a_404_before_retention_ends_is_a_missing_run() {
    let result = audit_with(
        &python(),
        expired(Expired::default()),
        &pin("python"),
        accept(),
        before_expiry(),
    );
    assert!(has(&result.errors, "missing before its retention"));
    assert!(result.notices.is_empty());
}

#[test]
fn other_failures_after_retention_are_not_expiry() {
    let spec = Expired {
        status: 500,
        ..Expired::default()
    };
    let result = audit_with(
        &python(),
        expired(spec),
        &pin("python"),
        accept(),
        after_expiry(),
    );
    assert!(has(&result.errors, "could not read evidence run"));
    assert!(result.notices.is_empty());
}

#[test]
fn expired_release_with_verified_provenance_passes_with_a_notice() {
    let checked = std::rc::Rc::new(std::cell::RefCell::new(Vec::new()));
    let seen = checked.clone();
    let verifier: Verifier = Box::new(move |c| {
        seen.borrow_mut().push(c.family.clone());
        Ok(())
    });
    let result = audit_with(
        &python(),
        expired(Expired::default()),
        &pin("python"),
        Some(verifier),
        after_expiry(),
    );
    assert!(
        result.errors.is_empty() && result.warnings.is_empty(),
        "{:?}",
        result.errors
    );
    assert_eq!(*checked.borrow(), ["python"]);
    assert!(result.notices[0].contains("signed provenance were verified instead"));
}

#[test]
fn expired_release_without_a_verifier_fails() {
    let result = audit_with(
        &python(),
        expired(Expired::default()),
        &pin("python"),
        None,
        after_expiry(),
    );
    assert!(has(&result.errors, "no provenance verifier"));
}

#[test]
fn failed_provenance_fails_with_or_without_expiry() {
    let reject =
        || -> Option<Verifier> { Some(Box::new(|_| Err("signature did not verify".into()))) };
    let result = audit_with(
        &python(),
        expired(Expired::default()),
        &pin("python"),
        reject(),
        after_expiry(),
    );
    assert!(has(&result.errors, "release provenance not verified"));
    assert!(result.notices.is_empty());
    let retained = |endpoint: &str| {
        if endpoint.contains("/compare/") {
            body(json!({"status": "behind"}))
        } else if endpoint.contains("/releases?") {
            body(json!([release("v1.2.3", "2026-08-27T00:05:00Z", false)]))
        } else if endpoint.ends_with("/actions/runs/123") {
            body(run("v1.2.3"))
        } else {
            listing(vec![run("v1.2.3")])
        }
    };
    let result = audit_with(
        &python(),
        retained,
        &pin("python"),
        reject(),
        after_expiry(),
    );
    assert!(has(&result.errors, "release provenance not verified"));
    assert!(result.notices.is_empty());
}

#[test]
fn live_probe_verifies_provenance_while_the_run_exists() {
    let checked = std::rc::Rc::new(std::cell::RefCell::new(Vec::new()));
    let seen = checked.clone();
    let verifier: Verifier = Box::new(move |c| {
        seen.borrow_mut().push(c.family.clone());
        Ok(())
    });
    let fetch = |endpoint: &str| {
        if endpoint.contains("/compare/") {
            body(json!({"status": "behind"}))
        } else if endpoint.contains("/releases?") {
            body(json!([release("v1.2.3", "2026-08-27T00:05:00Z", false)]))
        } else if endpoint.ends_with("/actions/runs/123") {
            body(run("v1.2.3"))
        } else {
            listing(vec![run("v1.2.3")])
        }
    };
    let result = audit_with(
        &python(),
        fetch,
        &pin("python"),
        Some(verifier),
        after_expiry(),
    );
    assert!(
        result.errors.is_empty() && result.notices.is_empty(),
        "{:?}",
        result.errors
    );
    assert_eq!(*checked.borrow(), ["python"]);
}

#[test]
fn a_newer_published_release_needs_review_even_with_a_lower_version() {
    let releases = json!([
        release("v1.2.3", "2026-08-27T00:05:00Z", false),
        release("v1.0.9", "2026-09-30T00:00:00Z", false)
    ]);
    let spec = Expired {
        releases: Some(Ok(releases)),
        ..Expired::default()
    };
    let result = audit_with(
        &python(),
        expired(spec),
        &pin("python"),
        accept(),
        after_expiry(),
    );
    assert!(
        result
            .errors
            .iter()
            .any(|e| e.contains("NEWER_RELEASE_REVIEW_REQUIRED") && e.contains("v1.0.9")),
        "{:?}",
        result.errors
    );
}

#[test]
fn drafts_and_sibling_namespaces_do_not_overtake_the_record() {
    let releases = json!([
        release("example-tool/v1.2.3", "2026-08-27T00:05:00Z", false),
        release("other-tool/v2.0.0", "2026-10-01T00:00:00Z", false),
        release("example-tool/v1.3.0", "2026-10-02T00:00:00Z", true),
    ]);
    let document = manifest(
        "python",
        "example-tool/v1.2.3",
        json!({"tag_prefix": "example-tool"}),
    );
    let spec = Expired {
        releases: Some(Ok(releases)),
        ..Expired::default()
    };
    let result = audit_with(
        &document,
        expired(spec),
        &pin("python"),
        accept(),
        after_expiry(),
    );
    assert!(result.errors.is_empty(), "{:?}", result.errors);
}

#[test]
fn release_listing_failures_fail_closed() {
    let full_page = Value::Array(
        (0..100)
            .map(|i| release(&format!("other/v{i}.0.0"), "2026-09-01T00:00:00Z", false))
            .collect(),
    );
    for (expected, releases) in [
        ("not a list", Ok(json!({"releases": []}))),
        ("could not read releases", Err("rate limited")),
        ("pages of releases", Ok(full_page)),
        (
            "no published release",
            Ok(json!([release("v1.2.3", "2026-08-27T00:05:00Z", true)])),
        ),
    ] {
        let spec = Expired {
            releases: Some(releases),
            ..Expired::default()
        };
        let result = audit_with(
            &python(),
            expired(spec),
            &pin("python"),
            accept(),
            after_expiry(),
        );
        assert!(
            has(&result.errors, expected),
            "{expected}: {:?}",
            result.errors
        );
    }
}

#[test]
fn a_retained_newer_release_run_names_the_review_action() {
    let runs = vec![
        run_payload(SHA, 456, "python", json!("v1.2.4")),
        run("v1.2.3"),
    ];
    let result = audit_with(
        &python(),
        standard("behind", run("v1.2.3"), runs),
        &pin("python"),
        None,
        after_expiry(),
    );
    assert!(
        result
            .errors
            .iter()
            .any(|e| e.contains("NEWER_RELEASE_REVIEW_REQUIRED") && e.contains("456")),
        "{:?}",
        result.errors
    );
}

#[test]
fn expired_verify_run_is_replaced_by_a_later_readable_success() {
    let later = verify_run(789, "2026-11-20T00:00:00Z", SHA);
    let spec = Expired {
        listing: Some(json!({"workflow_runs": [later.clone()]})),
        details: vec![(789, Ok(later))],
        ..Expired::default()
    };
    let result = audit_with(
        &verify_manifest(),
        expired(spec),
        &pin("verify"),
        accept(),
        after_expiry(),
    );
    assert!(result.errors.is_empty(), "{:?}", result.errors);
    assert!(result.notices[0].contains("current success 789"));
}

#[test]
fn expired_verify_run_without_a_good_later_success_fails() {
    let later = verify_run(789, "2026-11-20T00:00:00Z", SHA);
    let mut failed = later.clone();
    failed["conclusion"] = json!("failure");
    for (expected, latest, details) in [
        ("no later success", verify_run(123, RECORDED, SHA), vec![]),
        (
            "no later success",
            verify_run(789, "2026-08-26T00:00:00Z", SHA),
            vec![],
        ),
        (
            "could not read current success",
            later.clone(),
            vec![(789, Err("gone"))],
        ),
        (
            "response is not an object",
            later.clone(),
            vec![(789, Ok(json!([])))],
        ),
        ("conclusion", later, vec![(789, Ok(failed))]),
    ] {
        let spec = Expired {
            listing: Some(json!({"workflow_runs": [latest]})),
            details,
            ..Expired::default()
        };
        let result = audit_with(
            &verify_manifest(),
            expired(spec),
            &pin("verify"),
            None,
            after_expiry(),
        );
        assert!(
            has(&result.errors, expected),
            "{expected}: {:?}",
            result.errors
        );
        assert!(result.notices.is_empty());
    }
}

#[test]
fn expired_verify_run_on_a_superseded_pin_still_fails() {
    let later = verify_run(789, "2026-11-20T00:00:00Z", OLDER_SHA);
    let spec = Expired {
        listing: Some(json!({"workflow_runs": [later]})),
        ..Expired::default()
    };
    let result = audit_with(
        &verify_manifest(),
        expired(spec),
        &pin("verify"),
        None,
        after_expiry(),
    );
    assert!(has(&result.errors, "used policy pin"));
}

#[test]
fn a_non_object_run_and_an_unreadable_listing_are_reported_once_each() {
    let fetch = |endpoint: &str| {
        if endpoint.contains("/compare/") {
            body(json!({"status": "behind"}))
        } else if endpoint.ends_with("/actions/runs/123") {
            body(json!([]))
        } else {
            Reply::Fail("rate limited", Some(429))
        }
    };
    let result = audit_with(&python(), fetch, &pin("python"), None, after_expiry());
    let reported = result
        .errors
        .iter()
        .filter(|e| e.contains("not an object") || e.contains("latest successes"))
        .cloned()
        .collect::<Vec<_>>();
    assert_eq!(
        reported,
        [
            "python (ryanduguid/example): evidence run response is not an object",
            "python (ryanduguid/example): could not read latest successes: rate limited",
        ]
    );
}

#[test]
fn a_null_evidence_run_fails_in_every_mode_and_never_expires() {
    // Only a 404 can mark a run expired; a JSON null body is not a run record.
    for (document, family, latest) in [
        (python(), "python", run("v1.2.3")),
        (
            verify_manifest(),
            "verify",
            verify_run(123, "2026-08-27T00:00:00Z", SHA),
        ),
    ] {
        let fetch = |endpoint: &str| {
            if endpoint.contains("/compare/") {
                body(json!({"status": "identical"}))
            } else if endpoint.ends_with("/actions/runs/123") {
                body(Value::Null)
            } else if endpoint.contains("/releases?") {
                body(json!([release("v1.2.3", "2026-08-27T00:05:00Z", false)]))
            } else {
                listing(vec![latest.clone()])
            }
        };
        for release_evidence in [false, true] {
            for now in [before_expiry(), after_expiry()] {
                let verifier = if release_evidence { accept() } else { None };
                let result = audit_with(&document, fetch, &pin(family), verifier, now);
                assert_eq!(
                    (result.errors, result.warnings, result.notices),
                    (
                        vec![format!(
                            "{family} (ryanduguid/example): evidence run response is not an object"
                        )],
                        vec![],
                        vec![]
                    ),
                    "{family} release_evidence={release_evidence} now={now}"
                );
            }
        }
    }
}

#[test]
fn a_newer_release_without_a_successful_run_fails_while_the_run_exists() {
    let releases = std::rc::Rc::new(std::cell::RefCell::new(json!([
        release("v1.2.3", "2026-08-27T00:05:00Z", false),
        release("v1.2.4", "2026-09-30T00:00:00Z", false),
        release("other-tool/v9.0.0", "2026-10-01T00:00:00Z", false),
    ])));
    let fetch = |releases: std::rc::Rc<std::cell::RefCell<Value>>| {
        move |endpoint: &str| {
            if endpoint.contains("/compare/") {
                body(json!({"status": "behind"}))
            } else if endpoint.contains("/releases?") {
                body(releases.borrow().clone())
            } else if endpoint.ends_with("/actions/runs/123") {
                body(run("v1.2.3"))
            } else {
                listing(vec![run("v1.2.3")])
            }
        }
    };
    let result = audit_with(
        &python(),
        fetch(releases.clone()),
        &pin("python"),
        accept(),
        before_expiry(),
    );
    assert!(
        result
            .errors
            .iter()
            .any(|e| e.contains("NEWER_RELEASE_REVIEW_REQUIRED") && e.contains("v1.2.4")),
        "{:?}",
        result.errors
    );
    releases.borrow_mut().as_array_mut().unwrap().remove(1);
    let result = audit_with(
        &python(),
        fetch(releases),
        &pin("python"),
        accept(),
        before_expiry(),
    );
    assert!(result.errors.is_empty(), "{:?}", result.errors);
}

#[test]
fn expired_verify_run_trusts_only_the_replacement_run_record() {
    let later = verify_run(789, "2026-11-20T00:00:00Z", SHA);
    let older_pin = verify_run(789, "2026-11-20T00:00:00Z", OLDER_SHA);
    let mut no_pin = later.clone();
    no_pin
        .as_object_mut()
        .unwrap()
        .remove("referenced_workflows");
    let with = |field: &str, value: Value| {
        let mut changed = later.clone();
        changed[field] = value;
        changed
    };
    for (expected, detail) in [
        ("id 790 != 789", with("id", json!(790))),
        ("used policy pin", older_pin),
        ("used policy pin", no_pin),
        ("is not later", with("run_started_at", json!(RECORDED))),
        ("is not later", with("run_started_at", json!("soon"))),
    ] {
        let spec = Expired {
            listing: Some(json!({"workflow_runs": [later.clone()]})),
            details: vec![(789, Ok(detail))],
            ..Expired::default()
        };
        let result = audit_with(
            &verify_manifest(),
            expired(spec),
            &pin("verify"),
            None,
            after_expiry(),
        );
        assert!(
            has(&result.errors, expected),
            "{expected}: {:?}",
            result.errors
        );
        assert!(result.notices.is_empty());
    }
    let spec = Expired {
        listing: Some(json!({"workflow_runs": [with("id", json!("789"))]})),
        ..Expired::default()
    };
    let result = audit_with(
        &verify_manifest(),
        expired(spec),
        &pin("verify"),
        None,
        after_expiry(),
    );
    assert!(
        has(&result.errors, "no later success"),
        "{:?}",
        result.errors
    );
}

#[test]
fn main_prints_notices_without_failing_strict_mode() {
    let result = CheckResult {
        errors: vec![],
        warnings: vec![],
        notices: vec!["expired".into()],
    };
    let (text, code) = cc::render(&result, 4, true);
    assert_eq!(code, 0);
    assert!(text.starts_with("INFO expired\n"));
}

// GitHubReadTests: the gh helpers, run against the fake gh.

fn with_gh<T>(rules: Value, body: impl FnOnce() -> T) -> T {
    static LOCK: std::sync::Mutex<()> = std::sync::Mutex::new(());
    let _guard = LOCK.lock().unwrap_or_else(|e| e.into_inner());
    let fake = FakeGh::new(rules);
    let saved = fake
        .env()
        .into_iter()
        .map(|(key, value)| {
            let old = std::env::var_os(&key);
            // SAFETY: the lock serialises this file's environment changes.
            unsafe { std::env::set_var(&key, value) };
            (key, old)
        })
        .collect::<Vec<_>>();
    let result = body();
    for (key, old) in saved {
        // SAFETY: as above.
        unsafe {
            match old {
                Some(value) => std::env::set_var(&key, value),
                None => std::env::remove_var(&key),
            }
        }
    }
    result
}

#[test]
fn gh_json_returns_the_body_and_reads_the_status_on_failure() {
    let include = ["api", "--include", "repos/x"];
    let value = with_gh(
        json!([rule(&include, "HTTP/2.0 200 OK\nX: y\n\n{\"a\": 1}", "", 0)]),
        || cc::GhCli::json("repos/x"),
    );
    assert_eq!(value.unwrap(), to_json(&json!({"a": 1})));
    let error = with_gh(
        json!([rule(
            &include,
            "HTTP/2.0 404 Not Found\nX: y\n\n{}",
            "gh: Not Found",
            1
        )]),
        || cc::GhCli::json("repos/x"),
    );
    assert_eq!(
        error.unwrap_err(),
        LiveError::GitHub(GitHubError {
            message: "gh: Not Found".into(),
            status: Some(404)
        })
    );
    let error = with_gh(json!([rule(&include, "", "", 1)]), || {
        cc::GhCli::json("repos/x")
    });
    assert_eq!(
        error.unwrap_err(),
        LiveError::GitHub(GitHubError {
            message: "gh api failed for repos/x".into(),
            status: None
        })
    );
    let error = with_gh(
        json!([rule(&include, "HTTP/2.0 200 OK\n\nnot json", "", 0)]),
        || cc::GhCli::json("repos/x"),
    );
    assert!(
        matches!(error.unwrap_err(), LiveError::GitHub(e) if e.message.contains("invalid JSON"))
    );
}

#[test]
fn gh_text_returns_raw_content_or_raises() {
    let args = [
        "api",
        "-H",
        "Accept: application/vnd.github.raw+json",
        "repos/x/contents/f",
    ];
    let text = with_gh(json!([rule(&args, "uses: x@y", "", 0)]), || {
        cc::GhCli.fetch_text("repos/x/contents/f")
    });
    assert_eq!(text.unwrap(), "uses: x@y");
    let error = with_gh(json!([rule(&args, "", "", 1)]), || {
        cc::GhCli.fetch_text("repos/x/contents/f")
    });
    assert!(
        matches!(error.unwrap_err(), LiveError::GitHub(e) if e.message.contains("gh api failed"))
    );
}

#[test]
fn run_gh_returns_output_or_raises() {
    let args = ["release", "view"];
    assert_eq!(
        with_gh(json!([rule(&args, "ok", "", 0)]), || cc::GhCli::command(
            &args
        ))
        .unwrap(),
        "ok"
    );
    let error = with_gh(json!([rule(&args, "", "", 1)]), || {
        cc::GhCli::command(&args)
    })
    .unwrap_err();
    assert_eq!(error, Failure::Runtime("gh release failed".into()));
    let error = with_gh(json!([rule(&args, "", "denied", 1)]), || {
        cc::GhCli::command(&args)
    })
    .unwrap_err();
    assert_eq!(error, Failure::Runtime("denied".into()));
}

#[test]
fn tag_commit_dereferences_annotated_tags_and_refuses_the_rest() {
    let reply = |endpoint: &str, value: Value| {
        rule(
            &["api", "--include", endpoint],
            &format!("HTTP/2.0 200 OK\n\n{value}"),
            "",
            0,
        )
    };
    let rules = json!([
        reply(
            "repos/r/git/ref/tags/v1",
            json!({"object": {"type": "tag", "sha": "t1"}})
        ),
        reply(
            "repos/r/git/tags/t1",
            json!({"object": {"type": "commit", "sha": HEAD}})
        ),
        reply(
            "repos/r/git/ref/tags/light",
            json!({"object": {"type": "commit", "sha": HEAD}})
        ),
        reply(
            "repos/r/git/ref/tags/blob",
            json!({"object": {"type": "blob", "sha": "b"}})
        ),
        reply(
            "repos/r/git/ref/tags/odd",
            json!({"object": {"type": "commit", "sha": 7}})
        ),
        reply("repos/r/git/ref/tags/none", json!({})),
        reply(
            "repos/r/git/ref/tags/loop",
            json!({"object": {"type": "tag", "sha": "loop"}})
        ),
        reply(
            "repos/r/git/tags/loop",
            json!({"object": {"type": "tag", "sha": "loop"}})
        ),
    ]);
    with_gh(rules, || {
        assert_eq!(
            cc::GhCli::tag_commit("r", "v1").unwrap().as_deref(),
            Some(HEAD)
        );
        assert_eq!(
            cc::GhCli::tag_commit("r", "light").unwrap().as_deref(),
            Some(HEAD)
        );
        for tag in ["blob", "odd", "none", "loop"] {
            assert_eq!(cc::GhCli::tag_commit("r", tag).unwrap(), None, "{tag}");
        }
    });
}

#[test]
fn names_run_matches_the_exact_run_only() {
    let result = |uri: Value| json!({"verificationResult": {"signature": {"certificate": {"runInvocationURI": uri}}}});
    let base = "https://github.com/o/r/actions/runs/";
    let id = Json::Int(123.into());
    assert!(cc::names_run(
        &to_json(&json!([result(json!(format!("{base}123/attempts/2")))])),
        "o/r",
        &id
    ));
    for results in [
        json!([result(json!(format!("{base}1234/attempts/1")))]),
        json!([result(json!(format!("{base}123/attempts/")))]),
        json!([result(json!(format!("{base}123")))]),
        json!([result(json!(7)), {"verificationResult": null}, {}]),
        json!({"not": "a list"}),
    ] {
        assert!(!cc::names_run(&to_json(&results), "o/r", &id), "{results}");
    }
}

// ProvenanceVerifierTests

const ASSET: &[u8] = b"release bytes";

fn verifier_rules(
    release_doc: Option<Value>,
    tag_sha: &str,
    verified: Option<Value>,
    asset: &[u8],
) -> Value {
    let digest = format!("sha256:{}", release_policy::common::sha256(ASSET));
    let release_doc = release_doc.unwrap_or_else(|| {
        json!({"draft": false, "immutable": true, "assets": [
            {"name": "pkg-1.2.3.tar.gz", "digest": digest},
            {"name": "pkg-1.2.3.spdx.json", "digest": "sha256:0"},
        ]})
    });
    let verified = verified.unwrap_or_else(|| {
        json!([{"verificationResult": {"signature": {"certificate": {
            "runInvocationURI": "https://github.com/ryanduguid/example/actions/runs/123/attempts/1"
        }}}}])
    });
    let include = |endpoint: &str, value: &Value| {
        rule(
            &["api", "--include", endpoint],
            &format!("HTTP/2.0 200 OK\n\n{value}"),
            "",
            0,
        )
    };
    json!([
        include("repos/ryanduguid/example/releases/tags/v1.2.3", &release_doc),
        include("repos/ryanduguid/example/git/ref/tags/v1.2.3", &json!({"object": {"type": "commit", "sha": tag_sha}})),
        {"args": ["release", "download", "v1.2.3", "--repo", "ryanduguid/example", "--pattern", "pkg-1.2.3.tar.gz", "--dir", "<dir>"],
         "stdout": "", "stderr": "", "code": 0, "files": {"pkg-1.2.3.tar.gz": String::from_utf8(asset.to_vec()).unwrap()}},
        rule(&["attestation", "verify", "pkg-1.2.3.tar.gz", "--repo", "ryanduguid/example", "--signer-workflow",
               "ryanduguid/release-policy/.github/workflows/release-python.yml", "--signer-digest", SHA,
               "--source-ref", "refs/tags/v1.2.3", "--source-digest", HEAD, "--predicate-type",
               "https://slsa.dev/provenance/v1", "--format", "json"], &verified.to_string(), "", 0),
    ])
}

fn verify(rules: Value) -> Result<(), Failure> {
    let canary = parse(&python()).unwrap().remove(0);
    with_gh(rules, || cc::GhCli.verify_provenance(&canary))
}

#[test]
fn verifies_the_tarball_against_the_pinned_signer_source_and_run() {
    // Every flag is part of the rule the fake gh matches exactly.
    verify(verifier_rules(None, HEAD, None, ASSET)).unwrap();
}

#[test]
fn refuses_each_broken_binding() {
    let other_run = json!([{"verificationResult": {"signature": {"certificate": {
        "runInvocationURI": "https://github.com/ryanduguid/example/actions/runs/1234/attempts/1"
    }}}}]);
    for (expected, rules) in [
        (
            "published immutable",
            verifier_rules(
                Some(json!({"draft": false, "immutable": false})),
                HEAD,
                None,
                ASSET,
            ),
        ),
        ("points to", verifier_rules(None, OLDER_SHA, None, ASSET)),
        (
            "expected 1",
            verifier_rules(
                Some(json!({"draft": false, "immutable": true, "assets": []})),
                HEAD,
                None,
                ASSET,
            ),
        ),
        ("digest", verifier_rules(None, HEAD, None, b"other bytes")),
        (
            "names run",
            verifier_rules(None, HEAD, Some(other_run), ASSET),
        ),
    ] {
        match verify(rules) {
            Err(Failure::Runtime(message)) => {
                assert!(message.contains(expected), "{expected}: {message}")
            }
            other => panic!("{expected}: {other:?}"),
        }
    }
}

// ManifestRefusalTests

#[test]
fn parse_rejects_each_malformed_field() {
    let family = |key: &str, value: Value| {
        let mut document = python();
        document["families"][0][key] = value;
        document
    };
    let mut schema = python();
    schema["schema"] = json!(2);
    let mut families = python();
    families["families"] = json!({});
    let mut run_id = python();
    run_id["families"][0]["evidence"]["run_id"] = json!(0);
    for (document, message) in [
        (schema, "manifest schema must be 1"),
        (families, "manifest families must be a list"),
        (
            family("family", json!("")),
            "family[0].family must be a non-empty string",
        ),
        (
            family("family", json!("nightly")),
            "unsupported canary family: nightly",
        ),
        (
            family("repository", json!("example")),
            "python.repository must be owner/name",
        ),
        (
            family("workflow_path", json!("release.yml")),
            "python.workflow_path is not a workflow path",
        ),
        (
            family("policy_workflow", json!("release.yml")),
            "python.policy_workflow is not a workflow path",
        ),
        (run_id, "python.evidence.run_id must be positive"),
        (
            manifest("verify", "v1.2.3", json!({})),
            "verify evidence must use main",
        ),
    ] {
        let error = parse(&document).unwrap_err();
        assert!(error.contains(message), "{message}: {error}");
    }
}

#[test]
fn load_rejects_a_bom_invalid_text_and_missing_families() {
    let dir = tempfile::tempdir().unwrap();
    let path = dir.path().join("canaries.json");
    let canonical = format!(
        "{}\n",
        pyjson::dumps(
            &to_json(&python()),
            pyjson::Dump {
                indent: Some(2),
                sort_keys: true,
                ensure_ascii: true
            }
        )
    );
    for (raw, message) in [
        (
            [b"\xef\xbb\xbf".as_slice(), canonical.as_bytes()].concat(),
            "must not contain a UTF-8 BOM",
        ),
        (b"{\xff}".to_vec(), "must be valid UTF-8 JSON"),
        (canonical.into_bytes(), "must cover"),
    ] {
        std::fs::write(&path, raw).unwrap();
        assert!(
            cc::load_manifest(&path).unwrap_err().contains(message),
            "{message}"
        );
    }
}

// LiveAuditHelperTests

#[test]
fn a_workflow_without_one_literal_pin_is_an_audit_error() {
    let result = audit_with(
        &python(),
        expired(Expired::default()),
        "no pin here",
        accept(),
        before_expiry(),
    );
    assert!(has(&result.errors, "expected one literal"));
}

#[test]
fn referenced_workflows_ignore_malformed_and_unrelated_entries() {
    let canary = parse(&python()).unwrap().remove(0);
    let run =
        json!({"referenced_workflows": ["not a mapping", {"path": "other/repo@x", "sha": SHA}]});
    assert_eq!(cc::referenced_policy_sha(&to_json(&run), &canary), None);
}

#[test]
fn a_listing_without_workflow_runs_has_no_relevant_run() {
    let canary = parse(&python()).unwrap().remove(0);
    assert_eq!(
        cc::latest_relevant_run(&to_json(&json!({"total_count": 0})), &canary),
        None
    );
    assert_eq!(cc::latest_relevant_run(&to_json(&json!([])), &canary), None);
}

// MainTests

#[test]
fn offline_validation_of_the_recorded_manifest() {
    let out = support::cli(
        std::path::Path::new(env!("CARGO_MANIFEST_DIR")),
        &["check-canaries"],
        "",
    );
    assert_eq!(out.code, 0);
    assert_eq!(out.stdout, "canary manifest valid: 4 release families\n");
}

#[test]
fn live_audit_reports_and_fails_on_errors_or_required_warnings() {
    let failing = CheckResult {
        errors: vec!["pin drift".into()],
        warnings: vec![],
        notices: vec![],
    };
    assert_eq!(
        cc::render(&failing, 4, false),
        ("FAIL pin drift\n".into(), 1)
    );
    let stale = CheckResult {
        errors: vec![],
        warnings: vec!["stale run".into()],
        notices: vec![],
    };
    assert_eq!(cc::render(&stale, 4, true), ("WARN stale run\n".into(), 1));
}

struct UnexpectedLive;

impl Live for UnexpectedLive {
    fn fetch_json(&mut self, _endpoint: &str) -> Result<Json, LiveError> {
        Err(LiveError::Unexpected("boom: transport".into()))
    }
    fn fetch_text(&mut self, _endpoint: &str) -> Result<String, LiveError> {
        Err(LiveError::Unexpected("boom: transport".into()))
    }
    fn verify_provenance(&mut self, _canary: &Canary) -> Result<(), Failure> {
        Err(Failure::Runtime("not reached in this test".into()))
    }
}

#[test]
fn failure_conversions_preserve_live_error_messages() {
    assert_eq!(
        Failure::from(LiveError::GitHub(GitHubError {
            message: "Not Found".into(),
            status: Some(404),
        })),
        Failure::Runtime("Not Found".into())
    );
    assert_eq!(
        Failure::from(LiveError::Unexpected("boom".into())),
        Failure::Unexpected("boom".into())
    );
}

#[test]
fn unexpected_transport_errors_escape_the_audit() {
    let canaries = parse(&python()).unwrap();
    let error = cc::check_live(&canaries, &mut UnexpectedLive, false, Utc::now()).unwrap_err();
    assert_eq!(error, "boom: transport");
}

#[test]
fn malformed_manifest_fields_are_refused_with_field_names() {
    let cases = [
        (
            json!({"workflow_path": "workflow.yml"}),
            "python.workflow_path is not a workflow path",
        ),
        (
            json!({"policy_workflow": "nope.yml"}),
            "python.policy_workflow is not a workflow path",
        ),
        (
            json!({"current_policy_sha": "xyz"}),
            "python.current_policy_sha",
        ),
        (
            json!({"evidence": {
                "run_id": 0,
                "head_sha": HEAD,
                "ref": "v1.2.3",
                "policy_sha": SHA,
                "started_at": RECORDED,
            }}),
            "python.evidence.run_id",
        ),
        (
            json!({"evidence": {
                "run_id": 123,
                "head_sha": HEAD,
                "ref": "v1.2.3",
                "policy_sha": SHA,
                "started_at": "2026-08-27",
            }}),
            "python.evidence.started_at must be a UTC timestamp",
        ),
        (
            json!({"evidence": {
                "run_id": 123,
                "ref": "v1.2.3",
                "policy_sha": SHA,
                "started_at": RECORDED,
            }}),
            "python.evidence",
        ),
    ];
    for (fields, expected) in cases {
        let error = parse(&manifest("python", "v1.2.3", fields)).unwrap_err();
        assert!(error.contains(expected), "{error}");
    }
}

#[test]
fn unhashable_compare_statuses_raise_type_errors() {
    for (status, kind) in [(json!([]), "list"), (json!({}), "dict")] {
        let closure = move |endpoint: &str| {
            if endpoint.contains("/compare/") {
                body(json!({"status": status.clone()}))
            } else {
                listing(vec![])
            }
        };
        let error = cc::check_live(
            &parse(&python()).unwrap(),
            &mut Fake {
                json: closure,
                pin: SHA.into(),
                verifier: None,
                requests: Vec::new(),
            },
            false,
            Utc::now(),
        )
        .unwrap_err();
        assert!(error.contains("TypeError: unhashable type"), "{error}");
        assert!(error.contains(kind), "{error}");
    }
}

#[test]
fn non_object_evidence_run_responses_are_reported() {
    let result = live(
        &python(),
        |endpoint| {
            if endpoint.contains("/compare/") {
                body(json!({"status": "identical"}))
            } else if endpoint.ends_with("/actions/runs/123") {
                body(json!(5))
            } else {
                listing(vec![])
            }
        },
        &pin("python"),
    );
    assert!(has(
        &result.errors,
        "evidence run response is not an object"
    ));
}

#[test]
fn expired_evidence_runs_with_verified_provenance_record_a_notice() {
    let result = audit_with(
        &python(),
        |endpoint| {
            if endpoint.contains("/compare/") {
                body(json!({"status": "identical"}))
            } else if endpoint.ends_with("/actions/runs/123") {
                Reply::Fail("Not Found", Some(404))
            } else {
                listing(vec![])
            }
        },
        &pin("python"),
        accept(),
        after_expiry(),
    );
    assert!(
        result.notices.iter().any(|n| n.contains("has expired")),
        "{result:?}"
    );
}

#[test]
fn a_boolean_run_id_counts_as_python_int() {
    let mut document = python();
    document["families"][0]["evidence"]["run_id"] = json!(false);
    assert_eq!(
        parse(&document).unwrap_err(),
        "python.evidence.run_id must be positive"
    );
    document["families"][0]["evidence"]["run_id"] = json!(true);
    let canary = parse(&document).unwrap().remove(0);
    assert_eq!(canary.evidence.run_id, Json::Bool(true));
}

#[test]
fn manifest_text_and_sha_fields_are_refused_with_field_names() {
    let family = |key: &str, value: Value| {
        let mut document = python();
        document["families"][0][key] = value;
        document
    };
    let evidence = |key: &str, value: Value| {
        let mut document = python();
        document["families"][0]["evidence"][key] = value;
        document
    };
    for (document, message) in [
        (
            family("workflow_path", json!(5)),
            "python.workflow_path must be a non-empty string",
        ),
        (
            family("policy_workflow", Value::Null),
            "python.policy_workflow must be a non-empty string",
        ),
        (
            evidence("started_at", json!(20260827)),
            "python.evidence.started_at must be a non-empty string",
        ),
        (
            evidence("head_sha", json!("abc")),
            "python.evidence.head_sha must be a full lower-case SHA",
        ),
        (
            evidence("policy_sha", json!("A".repeat(40))),
            "python.evidence.policy_sha must be a full lower-case SHA",
        ),
    ] {
        assert_eq!(parse(&document).unwrap_err(), message);
    }
}

#[test]
fn runs_without_a_start_time_sort_after_dated_runs() {
    let canary = parse(&python()).unwrap().remove(0);
    // The dated run has the smallest id and is neither first nor last, so
    // neither id order nor listing order would pick it.
    let mut undated = run_payload(SHA, 9, "python", json!("v1.2.3"));
    undated.as_object_mut().unwrap().remove("run_started_at");
    let mut null_dated = run_payload(SHA, 8, "python", json!("v1.2.3"));
    null_dated["run_started_at"] = Value::Null;
    let dated = run_payload(SHA, 7, "python", json!("v1.2.3"));
    let payload = to_json(&json!({"workflow_runs": [undated, dated, null_dated]}));
    let latest = cc::latest_relevant_run(&payload, &canary).unwrap();
    assert_eq!(latest.get("id"), Some(&Json::Int(7.into())));
}

#[test]
fn a_replacement_run_needs_a_textual_start_time() {
    let later = verify_run(789, "2026-11-20T00:00:00Z", SHA);
    let mut detail = later.clone();
    detail["run_started_at"] = json!(20261120);
    let spec = Expired {
        listing: Some(json!({"workflow_runs": [later]})),
        details: vec![(789, Ok(detail))],
        ..Expired::default()
    };
    let result = audit_with(
        &verify_manifest(),
        expired(spec),
        &pin("verify"),
        None,
        after_expiry(),
    );
    assert!(
        has(&result.errors, "run_started_at 20261120 is not later"),
        "{:?}",
        result.errors
    );
}

#[test]
fn an_unparsable_recorded_start_escapes_when_the_run_is_gone() {
    // The manifest pattern admits month 13; strptime then refuses it.
    let mut document = python();
    document["families"][0]["evidence"]["started_at"] = json!("2026-13-27T00:00:00Z");
    let canaries = parse(&document).unwrap();
    let mut fake = Fake {
        json: |endpoint: &str| {
            if endpoint.contains("/compare/") {
                body(json!({"status": "identical"}))
            } else if endpoint.ends_with("/actions/runs/123") {
                Reply::Fail("Not Found", Some(404))
            } else {
                listing(vec![])
            }
        },
        pin: pin("python"),
        verifier: None,
        requests: Vec::new(),
    };
    let error = cc::check_live(&canaries, &mut fake, false, after_expiry()).unwrap_err();
    assert_eq!(
        error,
        "ValueError: time data '2026-13-27T00:00:00Z' does not match format '%Y-%m-%dT%H:%M:%SZ'"
    );
}

/// The usual fake, with a verifier that fails unexpectedly.
struct CrashingVerifier<F: FnMut(&str) -> Reply>(Fake<F>);

impl<F: FnMut(&str) -> Reply> Live for CrashingVerifier<F> {
    fn fetch_json(&mut self, endpoint: &str) -> Result<Json, LiveError> {
        self.0.fetch_json(endpoint)
    }
    fn fetch_text(&mut self, endpoint: &str) -> Result<String, LiveError> {
        self.0.fetch_text(endpoint)
    }
    fn verify_provenance(&mut self, _canary: &Canary) -> Result<(), Failure> {
        Err(Failure::Unexpected("OSError: disk full".into()))
    }
}

#[test]
fn unexpected_provenance_failures_escape_the_audit() {
    let mut live = CrashingVerifier(Fake {
        json: standard("identical", run("v1.2.3"), vec![run("v1.2.3")]),
        pin: pin("python"),
        verifier: None,
        requests: Vec::new(),
    });
    let error =
        cc::check_live(&parse(&python()).unwrap(), &mut live, true, before_expiry()).unwrap_err();
    assert_eq!(error, "OSError: disk full");
}

#[test]
fn gh_cli_reads_json_and_reports_raw_read_failures() {
    let include = ["api", "--include", "repos/x"];
    let value = with_gh(
        json!([rule(&include, "HTTP/2.0 200 OK\n\n{\"a\": 1}", "", 0)]),
        || cc::GhCli.fetch_json("repos/x"),
    );
    assert_eq!(value.unwrap(), to_json(&json!({"a": 1})));
    let raw = [
        "api",
        "-H",
        "Accept: application/vnd.github.raw+json",
        "repos/x/contents/f",
    ];
    let error = with_gh(json!([rule(&raw, "", "HTTP 404: Not Found", 1)]), || {
        cc::GhCli.fetch_text("repos/x/contents/f")
    });
    assert_eq!(
        error.unwrap_err(),
        LiveError::GitHub(GitHubError {
            message: "HTTP 404: Not Found".into(),
            status: None
        })
    );
}

#[test]
fn archive_provenance_is_signed_by_the_archive_publisher() {
    let canary = parse(&manifest("archive", "v1.2.3", json!({})))
        .unwrap()
        .remove(0);
    let mut rules = verifier_rules(None, HEAD, None, ASSET);
    // The Python signer does not vouch for an archive release.
    let python_signed = with_gh(rules.clone(), || cc::GhCli.verify_provenance(&canary));
    assert!(
        matches!(&python_signed, Err(Failure::Runtime(m)) if m.contains("no rule")),
        "{python_signed:?}"
    );
    rules[3]["args"][6] = json!("ryanduguid/release-policy/.github/workflows/publish-archives.yml");
    with_gh(rules, || cc::GhCli.verify_provenance(&canary)).unwrap();
}

#[test]
fn release_assets_must_be_a_list() {
    let release = |assets: Option<Value>| {
        let mut document = json!({"draft": false, "immutable": true});
        if let Some(assets) = assets {
            document["assets"] = assets;
        }
        verifier_rules(Some(document), HEAD, None, ASSET)
    };
    // An absent, empty, mapping or string listing holds no asset mapping.
    for assets in [
        None,
        Some(json!([])),
        Some(json!({"a": 1})),
        Some(json!("x")),
    ] {
        assert_eq!(
            verify(release(assets.clone())),
            Err(Failure::Runtime(
                "release v1.2.3 has 0 .tar.gz assets, expected 1".into()
            )),
            "{assets:?}"
        );
    }
    for (assets, kind) in [
        (json!(5), "int"),
        (json!(true), "bool"),
        (json!(1.5), "float"),
    ] {
        assert_eq!(
            verify(release(Some(assets))),
            Err(Failure::Unexpected(format!(
                "TypeError: '{kind}' object is not iterable"
            )))
        );
    }
}

#[test]
fn a_download_that_writes_no_asset_is_an_unexpected_failure() {
    let mut rules = verifier_rules(None, HEAD, None, ASSET);
    rules[2].as_object_mut().unwrap().remove("files");
    match verify(rules) {
        Err(Failure::Unexpected(message)) => {
            assert!(message.starts_with("[Errno 2] "), "{message}");
            assert!(message.ends_with("pkg-1.2.3.tar.gz'"), "{message}");
        }
        other => panic!("{other:?}"),
    }
}

#[test]
fn main_refuses_an_unreadable_manifest() {
    let dir = tempfile::tempdir().unwrap();
    let path = dir.path().join("canaries.json");
    std::fs::write(&path, b"\xff").unwrap();
    let out = support::cli(
        dir.path(),
        &["check-canaries", "--manifest", &path.to_string_lossy()],
        "",
    );
    assert_eq!(out.code, 1);
    assert!(out.stdout.is_empty());
    assert!(
        out.stderr.starts_with("check_canaries: ") && out.stderr.contains("must be valid UTF-8"),
        "{}",
        out.stderr
    );
}

#[test]
fn main_live_audit_fails_each_family_github_will_not_answer() {
    let fake = FakeGh::new(json!([]));
    let out = fake.run(
        std::path::Path::new(env!("CARGO_MANIFEST_DIR")),
        &["check-canaries", "--live"],
        "",
    );
    assert_eq!(out.code, 1, "{}", out.stderr);
    assert!(out.stderr.is_empty(), "{}", out.stderr);
    // The manifest itself may warn about a pin without release evidence;
    // every other line must be a failure.
    let lines = out.stdout.lines().collect::<Vec<_>>();
    assert!(
        lines
            .iter()
            .all(|l| l.starts_with("FAIL ") || l.starts_with("WARN ")),
        "{lines:?}"
    );
    for family in ["archive", "python", "skills", "verify"] {
        let prefix = format!("FAIL {family} (");
        assert!(
            lines.iter().any(|l| l.starts_with(&prefix)),
            "{family}: {lines:?}"
        );
    }
}

#[test]
fn main_live_audit_without_gh_is_an_unexpected_error() {
    let empty = tempfile::tempdir().unwrap();
    let out = support::cli_env(
        std::path::Path::new(env!("CARGO_MANIFEST_DIR")),
        &["check-canaries", "--live"],
        "",
        &[("PATH", &empty.path().to_string_lossy())],
    );
    assert_eq!(out.code, 1);
    assert!(out.stdout.is_empty());
    let expected = if cfg!(windows) {
        "check_canaries: unexpected error: program not found\n"
    } else {
        "check_canaries: unexpected error: [Errno 2] No such file or directory: 'gh'\n"
    };
    assert_eq!(out.stderr, expected);
}
