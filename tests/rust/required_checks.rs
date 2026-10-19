//! The consumer's mandatory checks for the exact release commit.
//! Ported from tests/test_required_checks.py. The tests that read the
//! adapter workflows' gate timeouts and the consumer guides (GateTimeoutTests'
//! two workflow readers and ConsumerGuideTests) check files that stay as they
//! are, and remain in Python; the documented-wait test reads its wait from the
//! workflow here as well.
mod support;

use num_bigint::BigInt;
use release_policy::pyjson::{self, Json};
use release_policy::required_checks::{self as rc, Job, Polling, Run, Verdict};
use serde_json::{Value, json};
use std::collections::BTreeMap;
use std::path::Path;

const REPO: &str = "ryanduguid/example";
const SHA: &str = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa";
const OTHER_SHA: &str = "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb";
const CI: &str = ".github/workflows/ci.yml";
const BOUNDARIES: &str = ".github/workflows/boundaries.yml";

fn list() -> String {
    format!("{CI}: lint\n{CI}: payday-super-checker / test (3.12)\n{BOUNDARIES}: boundaries\n")
}

fn with(mut base: Value, overrides: Value) -> Value {
    for (key, value) in overrides.as_object().unwrap() {
        base[key] = value.clone();
    }
    base
}

fn run(id: i64, overrides: Value) -> Value {
    with(
        json!({"id": id, "path": CI, "head_sha": SHA, "event": "push", "head_branch": "main", "status": "completed",
               "repository": {"full_name": REPO}, "head_repository": {"full_name": REPO}}),
        overrides,
    )
}

fn job(id: i64, name: &str, conclusion: &str, overrides: Value) -> Value {
    with(
        json!({"id": id, "name": name, "head_sha": SHA, "run_attempt": 1, "status": "completed", "conclusion": conclusion}),
        overrides,
    )
}

fn ok_job(id: i64, name: &str) -> Value {
    job(id, name, "success", json!({}))
}

fn to_json(value: &Value) -> Json {
    pyjson::loads(&value.to_string()).unwrap()
}

/// Serves fabricated runs and jobs and records every endpoint requested.
struct FakeGitHub {
    runs: Vec<Value>,
    jobs: BTreeMap<i64, Vec<Value>>,
    requests: Vec<String>,
    // A page number at which a run appears mid-pagination, and whether that settles.
    insert_before_page: Option<usize>,
    settle: bool,
}

impl FakeGitHub {
    fn new(runs: Vec<Value>, jobs: Vec<(i64, Vec<Value>)>) -> Self {
        FakeGitHub {
            runs,
            jobs: jobs.into_iter().collect(),
            requests: Vec::new(),
            insert_before_page: None,
            settle: true,
        }
    }

    fn call(&mut self, endpoint: &str) -> Result<Json, String> {
        self.requests.push(endpoint.into());
        let page: usize = endpoint.rsplit("page=").next().unwrap().parse().unwrap();
        let start = (page - 1) * 100;
        let slice = |items: &[Value]| {
            items
                .iter()
                .skip(start)
                .take(100)
                .cloned()
                .collect::<Vec<_>>()
        };
        if endpoint.contains("/actions/runs?") {
            let mut runs = self.runs.clone();
            if self.insert_before_page.is_some_and(|p| page >= p) {
                // A run started while the pages were read, shifting the rest down.
                runs.insert(0, run(9999, json!({})));
                if self.settle {
                    self.insert_before_page = None;
                }
            }
            return Ok(to_json(
                &json!({"workflow_runs": slice(&runs), "total_count": runs.len()}),
            ));
        }
        let run_id: i64 = endpoint
            .split("/actions/runs/")
            .nth(1)
            .unwrap()
            .split('/')
            .next()
            .unwrap()
            .parse()
            .unwrap();
        let found = &self.jobs[&run_id];
        Ok(to_json(
            &json!({"jobs": slice(found), "total_count": found.len()}),
        ))
    }
}

fn complete_success() -> FakeGitHub {
    FakeGitHub::new(
        vec![run(10, json!({})), run(11, json!({"path": BOUNDARIES}))],
        vec![
            (
                10,
                vec![
                    ok_job(1, "lint"),
                    ok_job(2, "payday-super-checker / test (3.12)"),
                ],
            ),
            (11, vec![ok_job(3, "boundaries")]),
        ],
    )
}

fn checks(text: &str) -> Vec<rc::RequiredCheck> {
    rc::parse_required_checks(text).unwrap()
}

fn trusted(github: &mut FakeGitHub) -> Result<rc::Runs, String> {
    rc::trusted_runs(&mut |e| github.call(e), REPO, SHA)
}

fn check(github: &mut FakeGitHub, text: &str) -> Result<Verdict, String> {
    check_with(github, text, 0, 30, || 0.0, |_| {})
}

fn check_with(
    github: &mut FakeGitHub,
    text: &str,
    wait: u64,
    poll: u64,
    clock: impl FnMut() -> f64,
    sleep: impl FnMut(f64),
) -> Result<Verdict, String> {
    rc::check(
        &checks(text),
        REPO,
        SHA,
        &mut |e| github.call(e),
        Polling {
            wait_seconds: wait,
            poll_seconds: poll,
        },
        clock,
        sleep,
    )
}

fn runs(entries: &[(i64, &str)]) -> Vec<Run> {
    entries
        .iter()
        .map(|(id, status)| Run {
            run_id: BigInt::from(*id),
            status: (*status).into(),
        })
        .collect()
}

fn refusal(result: Result<impl std::fmt::Debug, String>, fragment: &str) {
    let error = result.unwrap_err();
    assert!(error.contains(fragment), "{error:?} lacks {fragment:?}");
}

// ParseTests

#[test]
fn parses_paths_and_job_names_and_ignores_comments() {
    let labels = checks(&format!("# comment\n\n{}", list()))
        .iter()
        .map(|c| c.label())
        .collect::<Vec<_>>();
    assert_eq!(
        labels,
        [
            format!("{CI}: lint"),
            format!("{CI}: payday-super-checker / test (3.12)"),
            format!("{BOUNDARIES}: boundaries")
        ]
    );
}

#[test]
fn rejects_malformed_lines() {
    for text in [
        "lint".to_owned(),
        format!("{CI}:lint"),
        format!("{CI}: "),
        "docs/ci.yml: lint".into(),
        format!("{CI}: lint\n{CI}: lint\n"),
        String::new(),
        "# only a comment\n".into(),
    ] {
        assert!(rc::parse_required_checks(&text).is_err(), "{text:?}");
    }
}

// TrustedRunTests

#[test]
fn keeps_only_push_and_dispatch_runs_of_this_repository_at_the_exact_commit() {
    let mut github = FakeGitHub::new(
        vec![
            run(1, json!({})),
            run(2, json!({"event": "workflow_dispatch"})),
            run(3, json!({"head_sha": OTHER_SHA})),
            run(4, json!({"event": "pull_request"})),
            run(5, json!({"head_branch": "feature"})),
            run(6, json!({"repository": {"full_name": "someone/else"}})),
            run(7, json!({"head_repository": {"full_name": "fork/example"}})),
        ],
        vec![],
    );
    let found = trusted(&mut github).unwrap();
    assert_eq!(found.len(), 1);
    assert_eq!(
        fmt_runs(&found[CI]),
        fmt_runs(&runs(&[(2, "completed"), (1, "completed")]))
    );
}

fn fmt_runs(runs: &[Run]) -> Vec<String> {
    runs.iter()
        .map(|r| format!("{}:{}", r.run_id, r.status))
        .collect()
}

#[test]
fn a_run_that_cannot_be_read_refuses_the_listing() {
    for (broken, expected) in [
        (json!("not a run"), "no numeric id"),
        (json!({"id": null}), "no numeric id"),
        (run(9, json!({"id": "9"})), "no numeric id"),
        (run(8, json!({"path": null})), "no readable path or id"),
        (run(8, json!({"path": 123})), "no readable path or id"),
    ] {
        let mut github = FakeGitHub::new(vec![run(1, json!({})), broken], vec![]);
        refusal(trusted(&mut github), expected);
    }
}

#[test]
fn a_trust_field_of_the_wrong_shape_refuses_the_listing() {
    for (overrides, expected) in [
        (json!({"head_sha": 42}), "head_sha is int, not text"),
        (json!({"event": ["push"]}), "event is list, not text"),
        (
            json!({"head_branch": {"name": "main"}}),
            "head_branch is dict, not text",
        ),
        (
            json!({"repository": "ryanduguid/example"}),
            "repository is str, not a repository",
        ),
        (
            json!({"repository": {"full_name": 7}}),
            "repository.full_name is int, not text",
        ),
        (
            json!({"head_repository": 42}),
            "head_repository is int, not a repository",
        ),
        (json!({"status": 3}), "status is int, not text"),
    ] {
        let mut github = FakeGitHub::new(vec![run(2, json!({})), run(1, overrides)], vec![]);
        refusal(trusted(&mut github), expected);
    }
}

#[test]
fn a_malformed_failed_run_cannot_hide_behind_a_valid_success() {
    let mut github = FakeGitHub::new(
        vec![run(1, json!({})), run(2, json!({"head_repository": 42}))],
        vec![(1, vec![ok_job(1, "lint")])],
    );
    assert!(check(&mut github, &format!("{CI}: lint\n")).is_err());
}

#[test]
fn an_absent_trust_field_excludes_the_run_without_refusing() {
    for overrides in [
        json!({"head_sha": null}),
        json!({"event": null}),
        json!({"head_branch": null}),
        json!({"repository": null}),
        json!({"head_repository": null}),
        json!({"repository": {"full_name": null}}),
    ] {
        let mut github = FakeGitHub::new(vec![run(1, overrides)], vec![]);
        assert!(trusted(&mut github).unwrap().is_empty());
    }
}

#[test]
fn repository_names_compare_case_insensitively() {
    let mut github = FakeGitHub::new(
        vec![run(
            1,
            json!({"repository": {"full_name": "RyanDuguid/Example"}}),
        )],
        vec![],
    );
    assert_eq!(
        fmt_runs(&trusted(&mut github).unwrap()[CI]),
        ["1:completed"]
    );
}

#[test]
fn every_run_of_a_workflow_is_kept_and_ordered_for_readable_messages() {
    for order in [
        vec![run(9, json!({})), run(3, json!({}))],
        vec![run(3, json!({})), run(9, json!({}))],
    ] {
        let mut github = FakeGitHub::new(order, vec![]);
        assert_eq!(
            fmt_runs(&trusted(&mut github).unwrap()[CI]),
            ["9:completed", "3:completed"]
        );
    }
}

#[test]
fn pagination_follows_full_pages_and_stops_on_a_short_one() {
    let mut github = FakeGitHub::new((1..202).map(|i| run(i, json!({}))).collect(), vec![]);
    let found = trusted(&mut github).unwrap()[CI].clone();
    assert_eq!(found.len(), 201);
    assert_eq!(found[0].run_id, BigInt::from(201));
    let pages = github
        .requests
        .iter()
        .map(|r| r.rsplit("page=").next().unwrap().to_owned())
        .collect::<Vec<_>>();
    assert_eq!(pages, ["1", "2", "3", "1", "2", "3"]);
}

#[test]
fn a_listing_that_shifts_mid_read_is_read_again() {
    let mut github = FakeGitHub::new((1..201).map(|i| run(i, json!({}))).collect(), vec![]);
    github.insert_before_page = Some(2);
    let found = trusted(&mut github).unwrap()[CI].clone();
    assert_eq!(found.len(), 200);
    let ids = found
        .iter()
        .map(|r| r.run_id.clone())
        .collect::<std::collections::BTreeSet<_>>();
    assert_eq!(ids, (1..201).map(BigInt::from).collect());
}

#[test]
fn a_listing_that_keeps_shifting_fails_closed() {
    let mut github = FakeGitHub::new((1..201).map(|i| run(i, json!({}))).collect(), vec![]);
    github.insert_before_page = Some(2);
    github.settle = false;
    refusal(
        trusted(&mut github),
        "the listing changed while it was read",
    );
}

#[test]
fn a_repeated_entry_is_detected_even_within_one_page() {
    let mut github = FakeGitHub::new(vec![run(1, json!({})), run(1, json!({}))], vec![]);
    refusal(
        trusted(&mut github),
        "the listing changed while it was read",
    );
}

#[test]
fn a_count_that_disagrees_with_the_entries_fails_closed() {
    let mut short = |_: &str| {
        Ok(to_json(
            &json!({"workflow_runs": [run(1, json!({}))], "total_count": 2}),
        ))
    };
    refusal(
        rc::trusted_runs(&mut short, REPO, SHA),
        "the listing changed while it was read",
    );
}

#[test]
fn a_malformed_listing_is_an_error_not_an_empty_result() {
    for payload in [json!({"workflow_runs": "x"}), json!([]), json!({})] {
        let mut fetch = |_: &str| Ok(to_json(&payload));
        assert!(
            rc::trusted_runs(&mut fetch, REPO, SHA).is_err(),
            "{payload}"
        );
    }
}

// ListingStabilityTests

#[test]
fn balanced_movement_cannot_authorise_release_preflight() {
    for key in ["workflow_runs", "jobs"] {
        let mut github = if key == "workflow_runs" {
            FakeGitHub::new(
                (1..201).rev().map(|v| run(v, json!({}))).collect(),
                (1..202)
                    .map(|v| {
                        (
                            v,
                            vec![job(
                                v,
                                "lint",
                                if v == 201 { "failure" } else { "success" },
                                json!({}),
                            )],
                        )
                    })
                    .collect(),
            )
        } else {
            FakeGitHub::new(
                vec![run(10, json!({}))],
                vec![(
                    10,
                    (1..201)
                        .rev()
                        .map(|v| {
                            ok_job(
                                v,
                                &if v == 150 {
                                    "lint".into()
                                } else {
                                    format!("other-{v}")
                                },
                            )
                        })
                        .collect(),
                )],
            )
        };
        let mut moved = false;
        let mut fetch = |endpoint: &str| {
            let listing = if key == "workflow_runs" {
                endpoint.contains("/actions/runs?")
            } else {
                endpoint.contains("/jobs?")
            };
            if listing && endpoint.ends_with("page=2") && !moved {
                if key == "workflow_runs" {
                    github.runs.retain(|r| r["id"] != 150);
                    github.runs.insert(0, run(201, json!({})));
                } else {
                    let jobs = github.jobs.get_mut(&10).unwrap();
                    jobs.retain(|j| j["id"] != 150);
                    jobs.insert(0, job(201, "lint", "failure", json!({})));
                }
                moved = true;
            }
            github.call(endpoint)
        };
        let verdict = rc::check(
            &checks(&format!("{CI}: lint")),
            REPO,
            SHA,
            &mut fetch,
            Polling {
                wait_seconds: 0,
                poll_seconds: 30,
            },
            || 0.0,
            |_| {},
        )
        .unwrap();
        assert!(!verdict.ok(), "{key}");
        assert!(
            verdict.failed[0].contains("201"),
            "{key}: {:?}",
            verdict.failed
        );
    }
}

#[test]
fn balanced_movement_cannot_hide_a_failed_run_or_job() {
    for key in ["workflow_runs", "jobs"] {
        let make = |v: i64| {
            if key == "workflow_runs" {
                run(v, json!({}))
            } else {
                ok_job(v, &format!("job-{v}"))
            }
        };
        let mut records: Vec<Value> = (1..201).rev().map(make).collect();
        let mut moved = false;
        let mut fetch = |endpoint: &str| {
            let page: usize = endpoint.rsplit("page=").next().unwrap().parse().unwrap();
            if page == 2 && !moved {
                records.retain(|r| r["id"] != 150);
                records.insert(
                    0,
                    if key == "workflow_runs" {
                        run(201, json!({}))
                    } else {
                        job(201, "required", "failure", json!({}))
                    },
                );
                moved = true;
            }
            let slice = records
                .iter()
                .skip((page - 1) * 100)
                .take(100)
                .cloned()
                .collect::<Vec<_>>();
            Ok(to_json(&json!({key: slice, "total_count": 200})))
        };
        let found = rc::paginate(&mut fetch, "endpoint", key).unwrap();
        let ids = found
            .iter()
            .map(|r| r.get("id").unwrap().repr())
            .collect::<Vec<_>>();
        assert!(ids.contains(&"201".to_owned()));
        assert!(!ids.contains(&"150".to_owned()));
    }
}

#[test]
fn both_listing_families_require_a_numeric_count() {
    for key in ["workflow_runs", "jobs"] {
        for count in [Value::Null, json!("1"), json!(true)] {
            let payload = json!({key: [{"id": 1}], "total_count": count});
            refusal(
                rc::paginate(&mut |_| Ok(to_json(&payload)), "endpoint", key),
                "no numeric total_count",
            );
        }
        let payload = json!({key: [{"id": 1}]});
        refusal(
            rc::paginate(&mut |_| Ok(to_json(&payload)), "endpoint", key),
            "no numeric total_count",
        );
    }
}

fn sequence(values: Vec<Value>) -> impl FnMut(&str) -> Result<Json, String> {
    let mut values = values.into_iter();
    move |_| Ok(to_json(&values.next().expect("no more responses")))
}

#[test]
fn changed_second_page_count_retries_and_boolean_ids_refuse() {
    let valid = json!({"jobs": [{"id": 1}], "total_count": 1});
    let broken = json!({"jobs": [{"id": true}], "total_count": 1});
    refusal(
        rc::paginate(&mut |_| Ok(to_json(&broken)), "endpoint", "jobs"),
        "no numeric id",
    );
    let mut fetch = sequence(vec![
        valid.clone(),
        json!({"jobs": [], "total_count": 1}),
        valid.clone(),
        valid,
    ]);
    let found = rc::paginate(&mut fetch, "endpoint", "jobs").unwrap();
    assert_eq!(found, [to_json(&json!({"id": 1}))]);
}

#[test]
fn fresh_state_is_used_and_perpetual_balanced_movement_refuses() {
    let success = json!({"jobs": [ok_job(1, "required")], "total_count": 1});
    let failed = json!({"jobs": [job(1, "required", "failure", json!({}))], "total_count": 1});
    let found = rc::paginate(&mut sequence(vec![success, failed]), "endpoint", "jobs").unwrap();
    assert_eq!(
        found[0].get("conclusion").unwrap().as_str(),
        Some("failure")
    );
    let moving = (0..6)
        .map(|v| json!({"jobs": [{"id": v}], "total_count": 1}))
        .collect();
    refusal(
        rc::paginate(&mut sequence(moving), "endpoint", "jobs"),
        "listing changed",
    );
}

// RunJobTests

fn jobs_of(github: &mut FakeGitHub) -> Result<rc::Jobs, String> {
    rc::run_jobs(&mut |e| github.call(e), REPO, &BigInt::from(10), SHA)
}

fn fmt_job(job: &Job) -> String {
    format!(
        "{} {} {} {} {}",
        job.run_id,
        job.job_id,
        job.attempt.repr(),
        job.status,
        job.conclusion
    )
}

#[test]
fn groups_every_job_of_the_exact_commit_under_its_name() {
    let mut github = FakeGitHub::new(
        vec![],
        vec![(
            10,
            vec![
                job(1, "lint", "failure", json!({})),
                ok_job(2, "lint"),
                job(3, "other", "success", json!({"head_sha": OTHER_SHA})),
                job(
                    6,
                    "quiet",
                    "success",
                    json!({"status": null, "conclusion": null}),
                ),
                job(0, "lint", "cancelled", json!({})),
            ],
        )],
    );
    let jobs = jobs_of(&mut github).unwrap();
    assert_eq!(jobs.keys().collect::<Vec<_>>(), ["lint", "quiet"]);
    assert_eq!(
        jobs["lint"].iter().map(fmt_job).collect::<Vec<_>>(),
        [
            "10 0 1 completed cancelled",
            "10 1 1 completed failure",
            "10 2 1 completed success"
        ]
    );
    assert_eq!(
        jobs["quiet"].iter().map(fmt_job).collect::<Vec<_>>(),
        ["10 6 1  "]
    );
}

#[test]
fn an_unreported_attempt_reads_as_unknown_and_stays_out_of_messages() {
    let mut github = FakeGitHub::new(
        vec![],
        vec![(
            10,
            vec![
                job(1, "lint", "success", json!({"run_attempt": null})),
                ok_job(2, "other"),
            ],
        )],
    );
    let jobs = jobs_of(&mut github).unwrap();
    assert_eq!(jobs["lint"][0].attempt, Json::Int(0.into()));
    assert_eq!(jobs["lint"][0].label(), "1");
    assert_eq!(jobs["other"][0].label(), "2 (attempt 1)");
}

#[test]
fn an_attempt_of_the_wrong_shape_is_unknown_rather_than_a_refusal() {
    let mut github = FakeGitHub::new(
        vec![],
        vec![(
            10,
            vec![job(1, "lint", "success", json!({"run_attempt": "second"}))],
        )],
    );
    assert_eq!(
        jobs_of(&mut github).unwrap()["lint"][0].attempt,
        Json::Int(0.into())
    );
}

#[test]
fn a_job_field_of_the_wrong_shape_refuses_the_listing() {
    for (overrides, expected) in [
        (json!({"head_sha": 7}), "head_sha is int, not text"),
        (json!({"status": ["queued"]}), "status is list, not text"),
        (
            json!({"conclusion": {"ok": true}}),
            "conclusion is dict, not text",
        ),
    ] {
        let mut github = FakeGitHub::new(
            vec![],
            vec![(10, vec![job(1, "lint", "success", overrides)])],
        );
        refusal(jobs_of(&mut github), expected);
    }
}

#[test]
fn a_job_that_cannot_be_read_refuses_the_listing() {
    for (broken, expected) in [
        (json!({"id": 5}), "no readable name or id"),
        (
            job(4, "no id", "success", json!({"id": null})),
            "no numeric id",
        ),
        (
            with(ok_job(4, "x"), json!({"name": 7})),
            "no readable name or id",
        ),
    ] {
        let mut github = FakeGitHub::new(vec![], vec![(10, vec![ok_job(1, "lint"), broken])]);
        refusal(jobs_of(&mut github), expected);
    }
}

#[test]
fn the_gh_wrapper_returns_json_and_fails_on_errors() {
    let args = |e: &str| {
        vec!["api", "-H", "X-GitHub-Api-Version: 2026-03-10", e]
            .into_iter()
            .map(String::from)
            .collect::<Vec<_>>()
    };
    for (stdout, stderr, code, expected) in [
        ("{\"jobs\": []}", "", 0, Ok(json!({"jobs": []}))),
        ("", "HTTP 404", 1, Err("HTTP 404")),
        ("", "", 1, Err("gh api failed")),
        ("not json", "", 0, Err("invalid JSON")),
    ] {
        let fake = support::FakeGh::new(
            json!([{"args": args("x"), "stdout": stdout, "stderr": stderr, "code": code}]),
        );
        let result = with_env(&fake, || rc::gh_json("x"));
        match expected {
            Ok(value) => assert_eq!(result.unwrap(), to_json(&value)),
            Err(fragment) => refusal(result, fragment),
        }
    }
}

fn with_env<T>(fake: &support::FakeGh, body: impl FnOnce() -> T) -> T {
    static LOCK: std::sync::Mutex<()> = std::sync::Mutex::new(());
    let _guard = LOCK.lock().unwrap_or_else(|e| e.into_inner());
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

// EvaluateTests

fn queued(id: i64, name: &str) -> Value {
    job(
        id,
        name,
        "success",
        json!({"conclusion": null, "status": "queued"}),
    )
}

#[test]
fn complete_success_for_the_exact_commit_passes() {
    let verdict = check(&mut complete_success(), &list()).unwrap();
    assert!(verdict.ok());
    assert_eq!(verdict.passed.len(), 3);
    assert!(
        verdict
            .passed
            .contains(&format!("{CI}: lint: run 10 job 1 (attempt 1)"))
    );
}

#[test]
fn a_missing_check_fails() {
    let mut github = FakeGitHub::new(
        vec![run(10, json!({}))],
        vec![(10, vec![ok_job(1, "lint")])],
    );
    let verdict = check(&mut github, &list()).unwrap();
    assert!(!verdict.ok());
    assert_eq!(verdict.failed.len(), 2);
    assert!(verdict.failed[0].contains("completed without reporting this check"));
    assert!(verdict.failed[1].contains("no push or workflow_dispatch run"));
}

#[test]
fn a_pending_check_fails_closed_when_the_wait_ends() {
    let mut github = complete_success();
    github.jobs.get_mut(&10).unwrap()[0] = job(
        1,
        "lint",
        "success",
        json!({"conclusion": null, "status": "in_progress"}),
    );
    let verdict = check(&mut github, &list()).unwrap();
    assert_eq!(verdict.pending.len(), 1);
    assert!(verdict.pending[0].contains("is in_progress"));
    assert!(!verdict.ok());
}

#[test]
fn failed_cancelled_and_skipped_conclusions_are_not_success() {
    for conclusion in ["failure", "cancelled", "skipped", "neutral", "timed_out"] {
        let mut github = complete_success();
        github.jobs.get_mut(&10).unwrap()[1] = job(
            2,
            "payday-super-checker / test (3.12)",
            conclusion,
            json!({}),
        );
        let verdict = check(&mut github, &list()).unwrap();
        assert_eq!(verdict.failed.len(), 1);
        assert!(verdict.failed[0].contains(&format!("concluded '{conclusion}'")));
    }
}

#[test]
fn a_success_for_the_wrong_commit_does_not_count() {
    let mut github = complete_success();
    github.runs[0] = run(10, json!({"head_sha": OTHER_SHA}));
    let verdict = check(&mut github, &list()).unwrap();
    assert_eq!(verdict.failed.len(), 2);
    assert!(verdict.failed[0].contains("no push or workflow_dispatch run"));
    let mut github = complete_success();
    github.jobs.get_mut(&10).unwrap()[0] =
        job(1, "lint", "success", json!({"head_sha": OTHER_SHA}));
    assert_eq!(check(&mut github, &list()).unwrap().failed.len(), 1);
}

#[test]
fn a_success_under_the_wrong_workflow_identity_does_not_count() {
    let mut github = complete_success();
    github.runs[1] = run(11, json!({"path": ".github/workflows/impostor.yml"}));
    let verdict = check(&mut github, &list()).unwrap();
    assert_eq!(verdict.failed.len(), 1);
    assert!(verdict.failed[0].contains(&format!("{BOUNDARIES}: boundaries")));
    let mut github = complete_success();
    github.runs[1] = run(11, json!({"path": BOUNDARIES, "event": "pull_request"}));
    assert_eq!(check(&mut github, &list()).unwrap().failed.len(), 1);
}

#[test]
fn a_failing_run_refuses_even_though_another_run_succeeded() {
    let mut github = complete_success();
    github
        .runs
        .push(run(12, json!({"event": "workflow_dispatch"})));
    github
        .jobs
        .insert(12, vec![job(9, "lint", "cancelled", json!({}))]);
    let verdict = check(&mut github, &list()).unwrap();
    assert_eq!(verdict.failed.len(), 2);
    assert!(verdict.failed[0].contains("run 12 job 9 (attempt 1) concluded 'cancelled'"));
    assert!(verdict.failed[1].contains("run 12 completed without reporting this check"));
}

#[test]
fn a_failing_rerun_of_an_older_run_refuses() {
    let mut github = complete_success();
    github
        .runs
        .push(run(12, json!({"event": "workflow_dispatch"})));
    github.jobs.insert(
        12,
        vec![
            ok_job(20, "lint"),
            ok_job(21, "payday-super-checker / test (3.12)"),
        ],
    );
    github.jobs.insert(
        10,
        vec![
            job(1, "lint", "failure", json!({})),
            ok_job(2, "payday-super-checker / test (3.12)"),
        ],
    );
    let verdict = check(&mut github, &list()).unwrap();
    assert_eq!(verdict.failed.len(), 1);
    assert!(verdict.failed[0].contains("run 10 job 1 (attempt 1) concluded 'failure'"));
    assert!(!verdict.ok());
}

#[test]
fn a_newer_run_that_never_reported_the_check_refuses_an_older_success() {
    let mut github = complete_success();
    github
        .runs
        .push(run(12, json!({"conclusion": "cancelled"})));
    github
        .jobs
        .insert(12, vec![ok_job(9, "payday-super-checker / test (3.12)")]);
    let verdict = check(&mut github, &list()).unwrap();
    assert_eq!(verdict.failed.len(), 1);
    assert!(verdict.failed[0].contains("run 12 completed without reporting this check"));
    assert!(verdict.failed[0].contains(&format!("{CI}: lint")));
}

#[test]
fn a_newer_run_still_in_progress_is_pending_not_missing() {
    let mut github = complete_success();
    github.runs.push(run(12, json!({"status": "in_progress"})));
    github.jobs.insert(12, vec![]);
    let verdict = check(&mut github, &list()).unwrap();
    assert!(verdict.failed.is_empty());
    assert_eq!(verdict.pending.len(), 2);
    assert!(
        verdict.pending[0].contains("run 12 is in_progress and has not reported this check yet")
    );
}

#[test]
fn a_pending_run_holds_the_check_even_though_another_run_passed() {
    let mut github = complete_success();
    github.runs.push(run(12, json!({"status": "queued"})));
    github.jobs.insert(12, vec![]);
    let verdict = check(&mut github, &list()).unwrap();
    assert!(verdict.failed.is_empty());
    assert_eq!(verdict.pending.len(), 2);
    assert_eq!(verdict.passed.len(), 1);
}

#[test]
fn a_workflow_with_no_trusted_run_at_all_fails() {
    let mut github = FakeGitHub::new(
        vec![run(10, json!({}))],
        vec![(10, vec![ok_job(1, "lint")])],
    );
    let verdict = check(&mut github, &format!("{BOUNDARIES}: boundaries\n")).unwrap();
    assert_eq!(verdict.failed.len(), 1);
    assert!(verdict.failed[0].contains("no push or workflow_dispatch run"));
}

#[test]
fn only_the_named_skipped_checks_matter() {
    let mut github = complete_success();
    github.jobs.get_mut(&10).unwrap().push(job(
        7,
        "the-wip-tally / test (3.12)",
        "skipped",
        json!({}),
    ));
    assert!(check(&mut github, &list()).unwrap().ok());
}

#[test]
fn a_bounded_wait_polls_then_accepts_a_late_success() {
    let github = std::cell::RefCell::new(complete_success());
    github.borrow_mut().jobs.get_mut(&10).unwrap()[0] = queued(1, "lint");
    let mut ticks = [0.0, 0.0, 30.0, 30.0, 60.0].into_iter();
    let mut sleeps = Vec::new();
    let verdict = rc::check(
        &checks(&list()),
        REPO,
        SHA,
        &mut |e| github.borrow_mut().call(e),
        Polling {
            wait_seconds: 120,
            poll_seconds: 30,
        },
        || ticks.next().unwrap(),
        |s| {
            sleeps.push(s);
            if sleeps.len() == 2 {
                github.borrow_mut().jobs.get_mut(&10).unwrap()[0] = ok_job(1, "lint");
            }
        },
    )
    .unwrap();
    assert!(verdict.ok());
    assert_eq!(sleeps, [30.0, 30.0]);
}

#[test]
fn a_bounded_wait_times_out_and_fails_closed() {
    let mut github = complete_success();
    github.jobs.get_mut(&10).unwrap()[0] = queued(1, "lint");
    let mut ticks = [0.0, 0.0, 0.0, 50.0, 50.0, 70.0].into_iter();
    let mut sleeps = Vec::new();
    let verdict = check_with(
        &mut github,
        &list(),
        60,
        30,
        || ticks.next().unwrap(),
        |s| sleeps.push(s),
    )
    .unwrap();
    assert!(!verdict.ok());
    assert_eq!(sleeps, [30.0, 10.0]);
    assert_eq!(verdict.pending.len(), 1);
}

#[test]
fn an_api_error_propagates_instead_of_passing() {
    let mut broken = |_: &str| Err("HTTP 500".to_owned());
    let result = rc::check(
        &checks(&list()),
        REPO,
        SHA,
        &mut broken,
        Polling {
            wait_seconds: 0,
            poll_seconds: 30,
        },
        || 0.0,
        |_| {},
    );
    refusal(result, "HTTP 500");
}

// AmbiguousJobTests

fn collision(other: Value, reverse: bool) -> FakeGitHub {
    let mut rows = vec![other, ok_job(102, "lint")];
    if reverse {
        rows.reverse();
    }
    FakeGitHub::new(vec![run(10, json!({}))], vec![(10, rows)])
}

fn lint() -> String {
    format!("{CI}: lint\n")
}

#[test]
fn a_passing_namesake_never_covers_a_job_that_did_not_pass() {
    for other in [
        job(101, "lint", "failure", json!({})),
        job(101, "lint", "cancelled", json!({})),
        job(101, "lint", "skipped", json!({})),
        job(
            101,
            "lint",
            "success",
            json!({"conclusion": null, "status": "in_progress"}),
        ),
        job(103, "lint", "failure", json!({})),
        queued(103, "lint"),
    ] {
        for reverse in [false, true] {
            let verdict = check(&mut collision(other.clone(), reverse), &lint()).unwrap();
            assert!(!verdict.ok(), "an ambiguous check authorised release");
            assert!(verdict.pending.is_empty());
            assert!(verdict.failed[0].contains("2 distinct jobs named 'lint'"));
        }
    }
}

#[test]
fn two_successful_namesakes_are_still_ambiguous() {
    let verdict = check(&mut collision(ok_job(101, "lint"), false), &lint()).unwrap();
    assert!(!verdict.ok());
    assert!(verdict.failed[0].contains("jobs 101 (attempt 1), 102 (attempt 1)"));
    assert!(verdict.failed[0].contains("give each job a unique name"));
}

#[test]
fn a_collision_split_across_pages_is_detected() {
    let mut rows = vec![job(101, "lint", "failure", json!({}))];
    rows.extend((0..99).map(|i| ok_job(200 + i, &format!("filler-{i}"))));
    rows.push(ok_job(102, "lint"));
    let mut github = FakeGitHub::new(vec![run(10, json!({}))], vec![(10, rows)]);
    let verdict = check(&mut github, &lint()).unwrap();
    assert!(!verdict.ok());
    assert!(verdict.failed[0].contains("2 distinct jobs named 'lint'"));
}

#[test]
fn a_duplicate_name_nobody_selected_does_not_block_the_release() {
    let mut github = complete_success();
    github
        .jobs
        .get_mut(&10)
        .unwrap()
        .extend([job(8, "docs", "failure", json!({})), ok_job(9, "docs")]);
    assert!(check(&mut github, &list()).unwrap().ok());
}

#[test]
fn unique_matrix_jobs_sharing_a_run_still_pass() {
    let mut github = FakeGitHub::new(
        vec![run(10, json!({}))],
        vec![(
            10,
            vec![
                ok_job(1, "test (3.11)"),
                ok_job(2, "test (3.12)"),
                ok_job(3, "test (3.13)"),
            ],
        )],
    );
    let verdict = check(
        &mut github,
        &format!("{CI}: test (3.11)\n{CI}: test (3.12)\n{CI}: test (3.13)\n"),
    )
    .unwrap();
    assert!(verdict.ok());
    assert_eq!(verdict.passed.len(), 3);
}

#[test]
fn a_re_run_job_reports_once_and_is_not_a_collision() {
    let mut github = FakeGitHub::new(
        vec![run(10, json!({}))],
        vec![(
            10,
            vec![job(1, "lint", "success", json!({"run_attempt": 2}))],
        )],
    );
    let verdict = check(&mut github, &lint()).unwrap();
    assert!(verdict.ok());
    assert!(verdict.passed[0].contains("job 1 (attempt 2)"));
}

fn command(github: &mut FakeGitHub, args: [&str; 4], text: &str) -> rc::Outcome {
    rc::command(
        args[0],
        args[1],
        &BigInt::from(args[2].parse::<i64>().unwrap()),
        &BigInt::from(args[3].parse::<i64>().unwrap()),
        || Ok(text.to_owned()),
        &mut |e| github.call(e),
        || 0.0,
        |_| {},
    )
}

#[test]
fn the_command_refuses_an_ambiguous_check_with_an_actionable_message() {
    let mut github = collision(job(101, "lint", "failure", json!({})), false);
    let outcome = command(&mut github, [REPO, SHA, "0", "30"], &lint());
    assert_eq!(
        outcome.code, 1,
        "an ambiguous required selector authorised release"
    );
    assert!(outcome.stderr.contains("2 distinct jobs named 'lint'"));
    assert!(outcome.stderr.contains("run 10"));
    assert!(!outcome.stderr.contains("Traceback"));
}

// GateTimeoutTests

#[test]
fn a_failed_or_missing_check_returns_without_waiting_for_other_pending_checks() {
    for missing in [false, true] {
        let mut github = complete_success();
        github.jobs.get_mut(&10).unwrap()[0] = queued(1, "lint");
        if missing {
            github.runs = vec![run(10, json!({}))];
        } else {
            github.jobs.get_mut(&11).unwrap()[0] = job(3, "boundaries", "failure", json!({}));
        }
        let verdict = check_with(
            &mut github,
            &list(),
            600,
            30,
            || 0.0,
            |_| panic!("Unexpected wait after refusal"),
        )
        .unwrap();
        assert!(!verdict.failed.is_empty());
        assert!(!verdict.pending.is_empty());
    }
}

#[test]
fn the_documented_wait_succeeds_late_and_fails_when_it_expires() {
    let workflow = std::fs::read_to_string(
        Path::new(env!("CARGO_MANIFEST_DIR")).join(".github/workflows/release-skills.yml"),
    )
    .unwrap();
    let wait: u64 = regex::Regex::new(r"--wait-seconds (\d+)")
        .unwrap()
        .captures(&workflow)
        .unwrap()[1]
        .parse()
        .unwrap();
    for (elapsed, expected) in [(360.0, true), (wait as f64 + 30.0, false)] {
        let github = std::cell::RefCell::new(complete_success());
        github.borrow_mut().jobs.get_mut(&10).unwrap()[0] = queued(1, "lint");
        let now = std::cell::Cell::new(0.0);
        let verdict = rc::check(
            &checks(&list()),
            REPO,
            SHA,
            &mut |e| github.borrow_mut().call(e),
            Polling {
                wait_seconds: wait,
                poll_seconds: 30,
            },
            || now.get(),
            |s| {
                now.set(now.get() + s);
                if expected && now.get() >= elapsed {
                    github.borrow_mut().jobs.get_mut(&10).unwrap()[0] = ok_job(1, "lint");
                }
            },
        )
        .unwrap();
        assert_eq!(verdict.ok(), expected);
        assert!(now.get() <= wait as f64);
    }
}

// MainTests

#[test]
fn passes_and_reports_every_check() {
    let outcome = command(&mut complete_success(), [REPO, SHA, "0", "30"], &list());
    assert_eq!((outcome.code, outcome.stderr.as_str()), (0, ""));
    assert_eq!(outcome.stdout.matches("ok   ").count(), 3);
    assert!(outcome.stdout.contains("required checks passed: 3"));
}

#[test]
fn rejects_bad_arguments_before_any_request() {
    let mut github = complete_success();
    for args in [
        ["bad", SHA, "0", "30"],
        [REPO, "abc", "0", "30"],
        [REPO, SHA, "9999", "30"],
        [REPO, SHA, "0", "0"],
    ] {
        let outcome = command(&mut github, args, &list());
        assert_eq!(outcome.code, 1);
        assert!(outcome.stderr.contains("FAIL"));
    }
    assert!(github.requests.is_empty());
}

#[test]
fn reports_parse_and_api_errors_as_failures() {
    let outcome = command(&mut complete_success(), [REPO, SHA, "0", "30"], "");
    assert_eq!(outcome.code, 1);
    assert!(outcome.stderr.contains("fails closed"));
    let outcome = rc::command(
        REPO,
        SHA,
        &BigInt::from(0),
        &BigInt::from(30),
        || Ok(list()),
        &mut |_| Err("HTTP 500".to_owned()),
        || 0.0,
        |_| {},
    );
    assert_eq!(
        (outcome.code, outcome.stderr.as_str()),
        (1, "FAIL HTTP 500\n")
    );
}

#[test]
fn reports_pending_and_failed_checks() {
    let mut github = complete_success();
    github.jobs.get_mut(&10).unwrap()[0] = queued(1, "lint");
    github.jobs.get_mut(&11).unwrap()[0] = job(3, "boundaries", "failure", json!({}));
    let outcome = command(&mut github, [REPO, SHA, "0", "30"], &list());
    assert_eq!(outcome.code, 1);
    assert_eq!(outcome.stdout.matches("ok   ").count(), 1);
    assert!(outcome.stderr.contains("is queued after the 0s wait"));
    assert!(outcome.stderr.contains("concluded 'failure'"));
}

#[test]
fn runs_as_a_script_and_exits_with_its_verdict() {
    let out = support::cli(
        Path::new("."),
        &["required-checks", "--repository", "bad", "--commit", SHA],
        "",
    );
    assert_eq!(out.code, 1);
}

#[test]
fn reads_the_list_from_stdin_by_default() {
    let mut github = complete_success();
    let mut rules = Vec::new();
    // The fake gh serves the same pages FakeGitHub would.
    for endpoint in [
        format!("repos/{REPO}/actions/runs?head_sha={SHA}&per_page=100&page=1"),
        format!("repos/{REPO}/actions/runs/10/jobs?filter=latest&per_page=100&page=1"),
        format!("repos/{REPO}/actions/runs/11/jobs?filter=latest&per_page=100&page=1"),
    ] {
        let body = github.call(&endpoint).unwrap();
        rules.push(json!({"args": ["api", "-H", "X-GitHub-Api-Version: 2026-03-10", endpoint],
                          "stdout": pyjson::dumps(&body, pyjson::Dump::default()), "stderr": "", "code": 0}));
    }
    let fake = support::FakeGh::new(Value::Array(rules));
    let out = fake.run(
        Path::new("."),
        &["required-checks", "--repository", REPO, "--commit", SHA],
        &list(),
    );
    assert_eq!(out.code, 0, "{}", out.stderr);
    assert!(out.stdout.contains("required checks passed"));
}
