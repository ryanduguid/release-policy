//! Attribution statuses bound to one PR snapshot and completed scan.
//! Ported from tests/test_attribution_status.py.
mod support;

use num_bigint::BigInt;
use release_policy::attribution::{self, GitHub};
use release_policy::pyjson::{self, Json};
use serde_json::{Value, json};
use std::path::{Path, PathBuf};
use support::{FakeGh, rule};

const REPOSITORY: &str = "example/policy";
const RUN_URL: &str = "https://github.com/example/policy/actions/runs/1";

fn head() -> String {
    "a".repeat(40)
}

fn payload(changes: Value) -> Value {
    let mut value = json!({
        "number": 1,
        "title": "Clean title",
        "body": null,
        "head": {"sha": head(), "repo": {"full_name": "example/fork"}},
        "base": {"sha": "b".repeat(40), "ref": "main", "repo": {"full_name": REPOSITORY}},
    });
    for (key, changed) in changes.as_object().unwrap() {
        value[key] = changed.clone();
    }
    value
}

fn to_json(value: &Value) -> Json {
    pyjson::loads(&value.to_string()).unwrap()
}

/// Reads answer with `response`; status writes are recorded.
struct Fake {
    response: Option<Value>,
    posts: Vec<(String, String)>,
    reads: Vec<String>,
}

impl GitHub for Fake {
    fn fetch(&mut self, endpoint: &str) -> Result<Json, String> {
        self.reads.push(endpoint.into());
        self.response
            .as_ref()
            .map(to_json)
            .ok_or_else(|| "unavailable".into())
    }
    fn post(&mut self, head: &str, state: &str) -> Result<(), String> {
        self.posts.push((head.into(), state.into()));
        Ok(())
    }
}

struct Fixture {
    _dir: tempfile::TempDir,
    snapshot: PathBuf,
    result: PathBuf,
    github: Fake,
}

impl Fixture {
    fn new() -> Self {
        let dir = tempfile::tempdir().unwrap();
        let mut fixture = Fixture {
            snapshot: dir.path().join("snapshot.json"),
            result: dir.path().join("result.json"),
            _dir: dir,
            github: Fake {
                response: Some(payload(json!({}))),
                posts: Vec::new(),
                reads: Vec::new(),
            },
        };
        attribution::prepare(
            REPOSITORY,
            &BigInt::from(1),
            &fixture.snapshot,
            &mut fixture.github,
        )
        .unwrap();
        fixture
    }

    fn proof(&self, changes: Value) {
        let value = pyjson::loads(&std::fs::read_to_string(&self.snapshot).unwrap()).unwrap();
        let mut result = json!({"head_sha": head(), "clean": true, "fingerprint": attribution::fingerprint(&value)});
        for (key, changed) in changes.as_object().unwrap() {
            result[key] = changed.clone();
        }
        std::fs::write(&self.result, result.to_string()).unwrap();
    }

    fn finish(&mut self, status: &str) -> Result<bool, String> {
        attribution::finish(
            REPOSITORY,
            &BigInt::from(1),
            &self.snapshot,
            &self.result,
            status,
            &mut self.github,
        )
    }

    /// The statuses posted by the last finish.
    fn last_post(&self) -> (String, String) {
        self.github.posts.last().unwrap().clone()
    }

    fn cli_args(&self, operation: &str) -> Vec<String> {
        [
            "attribution-status",
            operation,
            "--repository",
            REPOSITORY,
            "--number",
            "1",
            "--snapshot",
            &self.snapshot.to_string_lossy(),
            "--result",
            &self.result.to_string_lossy(),
            "--job-status",
            "success",
            "--run-url",
            RUN_URL,
        ]
        .map(str::to_owned)
        .to_vec()
    }
}

fn snapshot_of(value: Value) -> Result<Json, String> {
    attribution::snapshot(&to_json(&value), REPOSITORY, &BigInt::from(1))
}

fn status_args(state: &str) -> Vec<String> {
    [
        "api".to_owned(),
        format!("repos/{REPOSITORY}/statuses/{}", head()),
        "--method".into(),
        "POST".into(),
        "-f".into(),
        format!("state={state}"),
        "-f".into(),
        "context=Attribution policy".into(),
        "-f".into(),
        "description=PR snapshot and attribution validation".into(),
        "-f".into(),
        format!("target_url={RUN_URL}"),
        "--silent".into(),
    ]
    .to_vec()
}

fn run(fake: &FakeGh, args: &[String]) -> support::Output {
    let args = args.iter().map(String::as_str).collect::<Vec<_>>();
    fake.run(Path::new("."), &args, "")
}

#[test]
fn completed_clean_scan_is_bound_to_the_current_snapshot() {
    let mut f = Fixture::new();
    f.proof(json!({}));
    assert!(f.finish("success").unwrap());
    assert_eq!(
        f.github.posts,
        [(head(), "pending".into()), (head(), "success".into())]
    );
}

#[test]
fn same_head_metadata_change_and_changed_revisions_fail() {
    let mut f = Fixture::new();
    f.proof(json!({}));
    for changed in [
        payload(json!({"title": "Changed title"})),
        payload(json!({"body": "Changed body"})),
        payload(json!({"head": {"sha": "c".repeat(40), "repo": {"full_name": "example/fork"}}})),
        payload(
            json!({"base": {"sha": "c".repeat(40), "ref": "main", "repo": {"full_name": REPOSITORY}}}),
        ),
        payload(
            json!({"base": {"sha": "b".repeat(40), "ref": "other", "repo": {"full_name": REPOSITORY}}}),
        ),
    ] {
        f.github.response = Some(changed);
        assert!(!f.finish("success").unwrap());
        assert_eq!(f.last_post(), (head(), "failure".into()));
    }
}

#[test]
fn missing_malformed_or_mismatched_proof_fails() {
    let mut f = Fixture::new();
    assert!(!f.finish("success").unwrap());
    assert_eq!(f.last_post(), (head(), "failure".into()));
    std::fs::write(&f.result, "not json").unwrap();
    assert!(!f.finish("success").unwrap());
    for changes in [
        json!({"head_sha": "b".repeat(40)}),
        json!({"clean": false}),
        json!({"clean": 1}),
        json!({"fingerprint": "different"}),
    ] {
        f.proof(changes);
        let before = f.github.posts.len();
        assert!(!f.finish("success").unwrap());
        assert_eq!(f.github.posts[before..], [(head(), "failure".into())]);
    }
}

#[test]
fn failed_or_cancelled_prerequisite_never_publishes_success() {
    let mut f = Fixture::new();
    f.proof(json!({}));
    for status in ["failure", "cancelled", "skipped"] {
        assert!(!f.finish(status).unwrap());
        assert_eq!(f.last_post(), (head(), "failure".into()));
    }
}

#[test]
fn api_read_failure_closes_owned_pending() {
    let mut f = Fixture::new();
    f.proof(json!({}));
    f.github.response = None;
    assert!(!f.finish("success").unwrap());
    assert_eq!(f.last_post(), (head(), "failure".into()));
}

#[test]
fn unreadable_identity_is_rejected_before_pending() {
    for bad in [
        Value::Null,
        json!({}),
        payload(json!({"number": 2})),
        payload(json!({"number": true})),
        payload(json!({"number": false})),
        payload(json!({"number": 1.0})),
        payload(json!({"body": 1})),
        payload(json!({"title": null})),
        payload(json!({"head": {"sha": "bad", "repo": {"full_name": "example/fork"}}})),
        payload(
            json!({"base": {"sha": "b".repeat(40), "ref": "main", "repo": {"full_name": "wrong/repo"}}}),
        ),
    ] {
        assert!(snapshot_of(bad.clone()).is_err(), "{bad}");
    }
}

#[test]
fn revision_and_metadata_shapes_fail_closed() {
    for side in ["head", "base"] {
        for bad in [
            Value::Null,
            json!({}),
            json!({"sha": head(), "repo": {}}),
            json!({"sha": head(), "repo": {"full_name": "invalid"}}),
        ] {
            assert!(
                snapshot_of(payload(json!({side: bad}))).is_err(),
                "{side} {bad}"
            );
        }
    }
    for base_ref in [Value::Null, json!("")] {
        let value = payload(
            json!({"base": {"sha": "b".repeat(40), "ref": base_ref, "repo": {"full_name": REPOSITORY}}}),
        );
        assert!(snapshot_of(value).is_err());
    }
}

#[test]
fn unreadable_owned_snapshot_cannot_write_an_arbitrary_status() {
    let mut f = Fixture::new();
    for raw in [
        None,
        Some(b"not json".as_slice()),
        Some(b"\xff"),
        Some(b"[]"),
        Some(b"{}"),
        Some(br#"{"head_sha":"invalid"}"#),
    ] {
        match raw {
            None => {
                let _ = std::fs::remove_file(&f.snapshot);
            }
            Some(raw) => std::fs::write(&f.snapshot, raw).unwrap(),
        }
        let count = f.github.posts.len();
        assert!(f.finish("success").is_err(), "{raw:?}");
        assert_eq!(f.github.posts.len(), count);
    }
}

#[test]
fn cli_reports_only_policy_outcome_and_closes_failed_scan() {
    let f = Fixture::new();
    let fetch = vec!["api".to_owned(), format!("repos/{REPOSITORY}/pulls/1")];
    let mut rules = vec![
        json!({"args": fetch, "stdout": payload(json!({})).to_string(), "stderr": "", "code": 0}),
    ];
    for state in ["pending", "success", "failure"] {
        rules.push(json!({"args": status_args(state), "stdout": "", "stderr": "", "code": 0}));
    }
    let fake = FakeGh::new(Value::Array(rules));
    f.proof(json!({}));
    let mut args = f.cli_args("finish");
    assert_eq!(run(&fake, &args).code, 0);
    std::fs::remove_file(&f.result).unwrap();
    let out = run(&fake, &args);
    assert_eq!(out.code, 1);
    assert_eq!(
        out.stderr,
        "Attribution scan or PR freshness validation failed\n"
    );
    std::fs::remove_file(&f.snapshot).unwrap();
    args[1] = "prepare".into();
    assert_eq!(run(&fake, &args).code, 0);
    let original = std::fs::read(&f.snapshot).unwrap();
    let calls = fake.calls().len();
    // An existing snapshot refuses the overwrite after one read.
    assert_eq!(run(&fake, &args).code, 1);
    assert_eq!(fake.calls()[calls..], [fetch]);
    assert_eq!(std::fs::read(&f.snapshot).unwrap(), original);
    let at = args.iter().position(|a| a == REPOSITORY).unwrap();
    args[at] = "invalid".into();
    let calls = fake.calls().len();
    let out = run(&fake, &args);
    assert_eq!(
        (out.code, out.stderr.as_str()),
        (1, "Invalid policy identity\n")
    );
    assert_eq!(fake.calls().len(), calls);
}

#[test]
fn failed_and_unreadable_api_responses_never_pass() {
    let endpoint = format!("repos/{REPOSITORY}/pulls/1");
    let mut gh = attribution::GhCli {
        repository: REPOSITORY.into(),
        run_url: RUN_URL.into(),
    };
    for (stdout, stderr, code, expected) in [
        ("", "private error", 1, "GitHub read failed"),
        ("not json", "", 0, "GitHub response was unreadable"),
    ] {
        let fake = FakeGh::new(json!([rule(&["api", &endpoint], stdout, stderr, code)]));
        let error = with_env(&fake, || gh.fetch(&endpoint)).unwrap_err();
        assert_eq!(error, expected);
    }
    let fake = FakeGh::new(json!([rule(
        &["api", "endpoint"],
        r#"{"number":1}"#,
        "",
        0
    )]));
    let value = with_env(&fake, || gh.fetch("endpoint")).unwrap();
    assert_eq!(value, to_json(&json!({"number": 1})));
}

/// Run `body` with this process's PATH pointing at the fake gh.
fn with_env<T>(fake: &FakeGh, body: impl FnOnce() -> T) -> T {
    static LOCK: std::sync::Mutex<()> = std::sync::Mutex::new(());
    let _guard = LOCK.lock().unwrap_or_else(|e| e.into_inner());
    let saved = fake
        .env()
        .into_iter()
        .map(|(key, value)| {
            let old = std::env::var_os(&key);
            // SAFETY: the lock serialises this test file's environment changes.
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
fn cli_redacts_failed_status_delivery() {
    let f = Fixture::new();
    f.proof(json!({}));
    let fetch = ["api".to_owned(), format!("repos/{REPOSITORY}/pulls/1")];
    let fake = FakeGh::new(json!([
        {"args": fetch, "stdout": payload(json!({})).to_string(), "stderr": "", "code": 0},
        {"args": status_args("success"), "stdout": "private status output", "stderr": "private status error", "code": 1},
    ]));
    let out = run(&fake, &f.cli_args("finish"));
    assert_eq!(out.code, 1);
    assert_eq!(fake.calls().last().unwrap(), &status_args("success"));
    assert_eq!(out.stdout, "");
    assert_eq!(
        out.stderr,
        "Attribution status validation could not complete\n"
    );
}

#[test]
fn cli_redacts_failed_or_malformed_api_reads() {
    let endpoint = format!("repos/{REPOSITORY}/pulls/1");
    for (stdout, stderr, code) in [
        ("private API output", "private API error", 1),
        ("private malformed API content", "", 0),
    ] {
        let f = Fixture::new();
        std::fs::remove_file(&f.snapshot).unwrap();
        let fake = FakeGh::new(json!([rule(&["api", &endpoint], stdout, stderr, code)]));
        let out = run(&fake, &f.cli_args("prepare"));
        assert_eq!(out.code, 1);
        assert_eq!(fake.calls(), [["api".to_owned(), endpoint.clone()]]);
        assert_eq!(out.stdout, "");
        assert_eq!(
            out.stderr,
            "Attribution status validation could not complete\n"
        );
    }
}

#[test]
fn cli_entry_point_finishes_a_clean_scan() {
    let f = Fixture::new();
    f.proof(json!({}));
    let fetch = ["api".to_owned(), format!("repos/{REPOSITORY}/pulls/1")];
    let fake = FakeGh::new(json!([
        {"args": fetch, "stdout": payload(json!({})).to_string(), "stderr": "", "code": 0},
        {"args": status_args("success"), "stdout": "", "stderr": "", "code": 0},
    ]));
    assert_eq!(run(&fake, &f.cli_args("finish")).code, 0);
}

#[cfg(unix)]
#[test]
fn snapshot_creation_is_private_and_refuses_final_symlinks() {
    use std::os::unix::fs::PermissionsExt;
    let mut f = Fixture::new();
    let private = f.snapshot.with_file_name("private.json");
    attribution::prepare(REPOSITORY, &BigInt::from(1), &private, &mut f.github).unwrap();
    assert_eq!(
        std::fs::metadata(&private).unwrap().permissions().mode() & 0o777,
        0o600
    );
    let original = std::fs::read(&f.snapshot).unwrap();
    let before = f.github.posts.clone();
    for target in [
        f.snapshot.clone(),
        f.snapshot.with_file_name("missing.json"),
    ] {
        let link = f.snapshot.with_file_name("link.json");
        std::os::unix::fs::symlink(&target, &link).unwrap();
        assert!(attribution::prepare(REPOSITORY, &BigInt::from(1), &link, &mut f.github).is_err());
        assert_eq!(f.github.posts, before);
        assert_eq!(std::fs::read(&f.snapshot).unwrap(), original);
        assert!(!f.snapshot.with_file_name("missing.json").exists());
        std::fs::remove_file(&link).unwrap();
    }
}

#[test]
fn surviving_old_event_reads_current_metadata() {
    let mut f = Fixture::new();
    std::fs::remove_file(&f.snapshot).unwrap();
    f.github.response = Some(payload(json!({"body": "Current metadata"})));
    attribution::prepare(REPOSITORY, &BigInt::from(1), &f.snapshot, &mut f.github).unwrap();
    let value = pyjson::loads(&std::fs::read_to_string(&f.snapshot).unwrap()).unwrap();
    assert_eq!(value.get("body"), Some(&Json::str_obj("Current metadata")));
    assert_eq!(
        f.github.reads.last().unwrap(),
        &format!("repos/{REPOSITORY}/pulls/1")
    );
}

#[test]
fn prepare_refuses_an_invalid_pr_before_writing_or_posting() {
    let dir = tempfile::tempdir().unwrap();
    let snapshot = dir.path().join("snapshot.json");
    let mut github = Fake {
        response: Some(payload(json!({"number": 2}))),
        posts: Vec::new(),
        reads: Vec::new(),
    };
    let error = attribution::prepare(REPOSITORY, &BigInt::from(1), &snapshot, &mut github);
    assert_eq!(error, Err("unreadable PR identity".into()));
    assert!(!snapshot.exists());
    assert!(github.posts.is_empty());
}

#[test]
fn an_invalid_current_pr_closes_owned_pending_as_failure() {
    let mut f = Fixture::new();
    f.proof(json!({}));
    f.github.response = Some(payload(json!({"title": null})));
    assert!(!f.finish("success").unwrap());
    assert_eq!(f.last_post(), (head(), "failure".into()));
}
