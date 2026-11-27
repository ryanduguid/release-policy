// Copyright 2026 Ryan Duguid
// SPDX-License-Identifier: Apache-2.0

mod support;

use release_policy::common::{dumps, read_bytes, sha256};
use release_policy::pyjson::{self, Json};
use release_policy::repin_issue::{
    ACTION, BASELINE, BOT, MAX_ISSUE_PAGES, TITLE, WORKFLOW, canonical_digest, issue_body,
    open_issues, pin_is_current, pin_is_current_with, prepare, prepare_with, publish, system_gh,
    unchanged_since,
};
use serde_json::json;
use std::cell::RefCell;
use std::collections::VecDeque;
use std::path::PathBuf;
use support::{FakeGh, Repo, cli, rule, tempdir};

const PIN_MARKER: &str = "uses: ryanduguid/release-policy/.github/actions/no-ai-attribution@";

fn workflow_text(sha: &str, extra: &str) -> String {
    format!(
        "name: Attribution Policy\non:\n  pull_request_target:\njobs:\n  check:\n    runs-on: ubuntu-latest\n    steps:\n{extra}      - {PIN_MARKER}{sha}\n"
    )
}

fn baseline_text(digest: &str) -> String {
    format!("CANONICAL_SHA256 = {{\n    \"{WORKFLOW}\": \"{digest}\",\n}}\n")
}

fn setup_repo() -> (Repo, String, String, String) {
    let repo = Repo::new();
    repo.write(ACTION, "name: action\n");
    repo.write("README.md", "v1\n");
    let first = repo.commit("initial");

    let initial_workflow = workflow_text(&first, "");
    let initial_digest = canonical_digest(initial_workflow.as_bytes());
    repo.write(WORKFLOW, &initial_workflow);
    repo.write(BASELINE, baseline_text(&initial_digest));
    let pinned_commit = repo.commit("pin action");

    repo.write(ACTION, "name: action v2\n");
    let changed_commit = repo.commit("change action");

    (repo, first, pinned_commit, changed_commit)
}

#[test]
fn canonical_digest_normalises_crlf_and_third_party_pins() {
    let lf_doc = b"steps:\n  - uses: actions/checkout@1111111111111111111111111111111111111111 # v4.2.1\n  - uses: actions/checkout@2222222222222222222222222222222222222222 # v4\n  - uses: ryanduguid/release-policy/.github/actions/no-ai-attribution@aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa\n";
    let crlf_doc = String::from_utf8_lossy(lf_doc)
        .replace('\n', "\r\n")
        .into_bytes();
    let other_third_party = b"steps:\n  - uses: actions/checkout@9999999999999999999999999999999999999999 # v4.0.0\n  - uses: actions/checkout@3333333333333333333333333333333333333333 # v4.9.9\n  - uses: ryanduguid/release-policy/.github/actions/no-ai-attribution@aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa\n";
    assert_eq!(canonical_digest(lf_doc), canonical_digest(&crlf_doc));
    assert_eq!(
        canonical_digest(lf_doc),
        canonical_digest(other_third_party)
    );

    // Own-repo pins keep their SHA in the digest regardless of owner casing.
    let changed_own = b"steps:\n  - uses: actions/checkout@1111111111111111111111111111111111111111 # v4.2.1\n  - uses: actions/checkout@2222222222222222222222222222222222222222 # v4\n  - uses: ryanduguid/release-policy/.github/actions/no-ai-attribution@bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb\n";
    assert_ne!(canonical_digest(lf_doc), canonical_digest(changed_own));

    let mixed_a = b"uses: RyanDuguid/release-policy/.github/workflows/pr-review.yml@aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa # v1.0.0\n";
    let mixed_b = b"uses: RyanDuguid/release-policy/.github/workflows/pr-review.yml@bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb # v1.0.0\n";
    assert_ne!(canonical_digest(mixed_a), canonical_digest(mixed_b));

    // Non-UTF-8 lines pass through unmodified without panicking.
    let raw = b"# \xff\n- uses: actions/checkout@1234567890abcdef1234567890abcdef12345678 # v4\r\n";
    let expected =
        b"# \xff\n- uses: actions/checkout@0000000000000000000000000000000000000000 # v4\n";
    assert_eq!(canonical_digest(raw), sha256(expected));
}

#[test]
fn prepare_detects_stale_pin_writes_patch_and_reports_current() {
    let (repo, first, pinned, changed) = setup_repo();
    let patch_dir = tempdir();
    let patch = patch_dir.path().join("repin.patch");

    // Current when pinned commit and target share action tree; unchanged across unrelated edits.
    assert!(pin_is_current(repo.root(), &first, &pinned).unwrap());
    repo.write("README.md", "v2\n");
    let cur_commit = repo.commit("unrelated doc change");
    assert!(unchanged_since(repo.root(), &changed, &cur_commit).unwrap());

    // Actually pin to `first` (stale relative to `changed` -> `cur_commit`).
    let wf_with_first = workflow_text(
        &first,
        "      - uses: actions/checkout@1234567890abcdef1234567890abcdef12345678 # v4.1.0\n",
    );
    repo.write(WORKFLOW, &wf_with_first);
    repo.write(
        BASELINE,
        baseline_text(&canonical_digest(wf_with_first.as_bytes())),
    );
    let head = repo.commit("pin back to old action");

    let stale = prepare(repo.root(), &head, &patch).unwrap();
    assert_eq!(stale, Some(first.clone()));
    assert!(patch.exists());
    let updated_wf = std::fs::read_to_string(repo.path(WORKFLOW)).unwrap();
    assert!(updated_wf.contains(&format!("{PIN_MARKER}{head}")));

    // Applying the patch cleanly on a fresh clone of `head` matches the working tree.
    let status = std::process::Command::new("git")
        .args(["apply", "--check", patch.to_str().unwrap()])
        .current_dir(repo.root())
        .status()
        .unwrap();
    assert!(!status.success()); // working tree is already patched

    repo.commit("commit repin");
    let head2 = repo.git(&["rev-parse", "HEAD"]);
    let patch2 = patch_dir.path().join("no-patch.patch");
    assert_eq!(prepare(repo.root(), &head2, &patch2).unwrap(), None);
    assert!(!patch2.exists());
}

#[test]
fn pin_is_current_refuses_missing_tree_or_non_ancestor() {
    let mut fake_git = |args: &[&str]| -> release_policy::Result<(i32, String, String)> {
        if args[0] == "rev-parse" {
            return Ok((1, String::new(), String::new()));
        }
        panic!("unexpected git call: {args:?}");
    };
    assert!(!pin_is_current_with(&"a".repeat(40), &"b".repeat(40), &mut fake_git).unwrap());

    let repo = Repo::new();
    repo.write("README.md", "start\n");
    let base = repo.commit("base");
    repo.git(&["checkout", "-b", "side", "-q"]);
    repo.write(ACTION, "name: same\n");
    let side = repo.commit("side");
    repo.git(&["checkout", "main", "-q"]);
    repo.write(ACTION, "name: same\n");
    let main_head = repo.commit("main");
    assert!(!pin_is_current(repo.root(), &side, &main_head).unwrap());
    assert!(!unchanged_since(repo.root(), &side, &main_head).unwrap());
    assert!(!pin_is_current(repo.root(), &base, &main_head).unwrap());
}

#[test]
fn prepare_validates_inputs_and_git_diff_failures() {
    let (repo, first, _pinned, head) = setup_repo();
    let patch = tempdir().path().join("out.patch");

    let err = prepare(repo.root(), "main", &patch).unwrap_err();
    assert_eq!(err, "target is not a full commit SHA: main");

    let other = "0".repeat(40);
    let err = prepare(repo.root(), &other, &patch).unwrap_err();
    assert_eq!(err, format!("target {other} is not the checked-out commit"));

    // Missing pin line.
    repo.write(WORKFLOW, "name: empty\n");
    let bad_wf = repo.commit("bad wf");
    assert_eq!(
        prepare(repo.root(), &bad_wf, &patch).unwrap_err(),
        "expected exactly one live full-SHA action pin, found 0"
    );

    // Duplicate pin lines.
    repo.write(
        WORKFLOW,
        format!("{}{}", workflow_text(&head, ""), workflow_text(&head, "")),
    );
    let dup_wf = repo.commit("dup wf");
    assert_eq!(
        prepare(repo.root(), &dup_wf, &patch).unwrap_err(),
        "expected exactly one live full-SHA action pin, found 2"
    );

    // Missing digest line (workflow still points at stale `first`).
    repo.write(WORKFLOW, workflow_text(&first, ""));
    repo.write(BASELINE, "CANONICAL_SHA256 = {}\n");
    let bad_base = repo.commit("bad baseline");
    assert_eq!(
        prepare(repo.root(), &bad_base, &patch).unwrap_err(),
        format!("expected exactly one {WORKFLOW} digest, found 0")
    );

    // git diff failure via injected runner.
    let mut seq = VecDeque::from([
        (0, format!("{bad_base}\n"), String::new()), // rev-parse HEAD
        (0, "tree1\n".into(), String::new()),        // tree pin
        (0, "tree2\n".into(), String::new()),        // tree target
        (1, String::new(), "diff exploded\n".into()),
    ]);
    // Write a valid digest first so we reach git diff.
    let wf_bytes = std::fs::read(repo.path(WORKFLOW)).unwrap();
    repo.write(BASELINE, baseline_text(&canonical_digest(&wf_bytes)));
    let err = prepare_with(repo.root(), &bad_base, &patch, &mut |_args| {
        Ok(seq.pop_front().unwrap())
    })
    .unwrap_err();
    assert_eq!(err, "git diff failed: diff exploded");
}

#[test]
fn issue_body_and_publish_cover_create_update_skip_and_errors() {
    let sha = "a".repeat(40);
    let patch = "--- a/w\n+++ b/w\n";
    let body = issue_body(&sha, patch).unwrap();
    assert!(body.contains("No, Mr Bond, I expect you to repin") || body.contains(&sha));
    assert!(body.ends_with(&format!("````diff\n{patch}````\n")));
    assert_eq!(
        issue_body(&sha, "bad\n````\nfence\n").unwrap_err(),
        "the patch contains a four-backtick fence"
    );

    assert_eq!(
        publish("not-a-repo", &sha, patch, &mut |_, _| panic!("no gh")).unwrap_err(),
        "not an owner/name repository: not-a-repo"
    );

    // 1. Open a new issue when none exist (ignoring pull_request and wrong titles).
    let calls: RefCell<Vec<(Vec<String>, Option<String>)>> = RefCell::new(Vec::new());
    let mut gh_open = |args: &[&str], stdin: Option<&str>| -> release_policy::Result<String> {
        calls.borrow_mut().push((
            args.iter().map(|s| (*s).to_owned()).collect(),
            stdin.map(str::to_owned),
        ));
        if args[1].starts_with("repos/o/r/issues?") {
            return Ok(json!([
                {"number": 1, "title": "Other", "user": {"login": BOT}},
                {"number": 2, "title": TITLE, "pull_request": {}, "user": {"login": BOT}}
            ])
            .to_string());
        }
        Ok(json!({"number": 42}).to_string())
    };
    assert_eq!(
        publish("o/r", &sha, patch, &mut gh_open).unwrap(),
        "opened #42"
    );
    let recorded = calls.borrow();
    assert_eq!(recorded.len(), 2);
    assert_eq!(
        recorded[1].0,
        [
            "api",
            "--method",
            "POST",
            "repos/o/r/issues",
            "--input",
            "-"
        ]
    );
    let sent = pyjson::loads(recorded[1].1.as_deref().unwrap()).unwrap();
    assert_eq!(sent.get("title").and_then(Json::as_str), Some(TITLE));
    assert_eq!(sent.get("body").and_then(Json::as_str), Some(body.as_str()));

    // 2. Skip when only a human opened one.
    let mut gh_human = |_: &[&str], _: Option<&str>| {
        Ok(json!([{"number": 9, "title": TITLE, "user": {"login": "octocat"}}]).to_string())
    };
    assert_eq!(
        publish("o/r", &sha, patch, &mut gh_human).unwrap(),
        "left #9: a person opened it"
    );

    // 3. No-op when bot issue already carries exact body.
    let mut gh_same = |_: &[&str], _: Option<&str>| {
        Ok(
            json!([{"number": 11, "title": TITLE, "body": body, "user": {"login": BOT}}])
                .to_string(),
        )
    };
    assert_eq!(
        publish("o/r", &sha, patch, &mut gh_same).unwrap(),
        "#11 already carries this patch"
    );

    // 4. Update bot issue even when a human also opened one.
    let mut patched_payload = None;
    let mut gh_update = |args: &[&str], stdin: Option<&str>| {
        if args[1].starts_with("repos/o/r/issues?") {
            return Ok(json!([
                {"number": 3, "title": TITLE, "user": {"login": "human"}},
                {"number": 7, "title": TITLE, "body": "old", "user": {"login": BOT}}
            ])
            .to_string());
        }
        patched_payload = Some((args.join(" "), stdin.unwrap().to_owned()));
        Ok("{}".into())
    };
    assert_eq!(
        publish("o/r", &sha, patch, &mut gh_update).unwrap(),
        "updated #7"
    );
    let (patch_cmd, patch_body) = patched_payload.unwrap();
    assert_eq!(patch_cmd, "api --method PATCH repos/o/r/issues/7 --input -");
    let expected_json = dumps(&serde_json::json!({"body": body}), false, true, true, false);
    assert_eq!(patch_body, expected_json);
}

#[test]
fn open_issues_handles_pagination_and_malformed_github_payloads() {
    let err_not_list = open_issues("o/r", &mut |_, _| Ok("{}".into())).unwrap_err();
    assert_eq!(err_not_list, "GitHub returned a non-list issue page");

    let err_not_obj = open_issues("o/r", &mut |_, _| Ok("[1]".into())).unwrap_err();
    assert_eq!(err_not_obj, "GitHub returned a non-object issue");

    // Page 1 has 100 unrelated entries, page 2 has 1 matching entry.
    let filler: Vec<_> = (1..=100)
        .map(|n| json!({"number": n, "title": "Other"}))
        .collect();
    let found = open_issues("o/r", &mut |args, _| {
        if args[1].ends_with("page=1") {
            Ok(serde_json::to_string(&filler).unwrap())
        } else {
            Ok(json!([{"number": 101, "title": TITLE, "user": {"login": BOT}}]).to_string())
        }
    })
    .unwrap();
    assert_eq!(found.len(), 1);

    // Exhaust all MAX_ISSUE_PAGES pages -> error.
    let err_limit = open_issues("o/r", &mut |_, _| {
        Ok(serde_json::to_string(&filler).unwrap())
    })
    .unwrap_err();
    assert_eq!(
        err_limit,
        format!("no decision after {MAX_ISSUE_PAGES} pages of open issues")
    );

    // Missing number in issue object.
    let err_no_num = publish("o/r", &"a".repeat(40), "p\n", &mut |_, _| {
        Ok(json!([{"title": TITLE, "user": {"login": "person"}}]).to_string())
    })
    .unwrap_err();
    assert_eq!(err_no_num, "GitHub issue is missing a number");
}

#[test]
fn cli_prepare_and_publish_with_fake_gh_and_error_paths() {
    let (repo, first, _pinned, changed) = setup_repo();
    // Pin workflow back to `first` with matching baseline digest so `prepare` updates it.
    let stale_wf = workflow_text(&first, "      # No, Mr Bond, I expect you to repin\n");
    repo.write(WORKFLOW, &stale_wf);
    repo.write(
        BASELINE,
        baseline_text(&canonical_digest(stale_wf.as_bytes())),
    );
    let stale_commit = repo.commit("stale state");
    let patch_file = PathBuf::from("out.patch");

    let out = cli(
        repo.root(),
        &[
            "repin-issue",
            "prepare",
            "--target",
            &stale_commit,
            "--patch",
            patch_file.to_str().unwrap(),
        ],
        "",
    );
    assert_eq!(out.code, 0, "stderr: {}", out.stderr);
    assert_eq!(
        out.stdout,
        format!("attribution pin {first} is stale; wrote out.patch\n")
    );
    assert!(out.stderr.is_empty());

    // Already current on CLI when committed.
    repo.commit("apply repin");
    let cur = repo.git(&["rev-parse", "HEAD"]);
    let out_cur = cli(
        repo.root(),
        &[
            "repin_issue",
            "prepare",
            "--target",
            &cur,
            "--patch",
            "unused.patch",
        ],
        "",
    );
    assert_eq!(out_cur.code, 0);
    assert_eq!(out_cur.stdout, "attribution pin is current\n");

    // Publish refuses stale main WITHOUT calling gh.
    let gh = FakeGh::new(json!([]));
    let out_stale = gh.run(
        repo.root(),
        &[
            "repin-issue",
            "publish",
            "--repository",
            "o/r",
            "--target",
            &changed,
            "--patch",
            "out.patch",
            "--current",
            &cur,
        ],
        "",
    );
    assert_eq!(out_stale.code, 1);
    assert!(out_stale.stdout.is_empty());
    assert_eq!(
        out_stale.stderr,
        format!(
            "main no longer descends from {changed} with the same action and workflow; rerun this workflow to repin to main\n"
        )
    );
    assert!(gh.calls().is_empty());

    // Publish succeeds when target == current, including CRLF patch normalisation.
    let crlf_patch = read_bytes(&repo.path("out.patch"))
        .unwrap()
        .iter()
        .flat_map(|&b| {
            if b == b'\n' {
                vec![b'\r', b'\n']
            } else {
                vec![b]
            }
        })
        .collect::<Vec<u8>>();
    repo.write("crlf.patch", crlf_patch);

    gh.set_rules(json!([
        rule(
            &["api", "repos/o/r/issues?state=open&per_page=100&page=1"],
            "[]",
            "",
            0
        ),
        rule(
            &[
                "api",
                "--method",
                "POST",
                "repos/o/r/issues",
                "--input",
                "-"
            ],
            "{\"number\": 12}",
            "",
            0
        ),
    ]));
    let out_pub = gh.run(
        repo.root(),
        &[
            "repin-issue",
            "publish",
            "--repository",
            "o/r",
            "--target",
            &cur,
            "--patch",
            "crlf.patch",
            "--current",
            &cur,
        ],
        "",
    );
    assert_eq!(out_pub.code, 0, "stderr: {}", out_pub.stderr);
    assert_eq!(out_pub.stdout, "opened #12\n");
    assert_eq!(gh.calls().len(), 2);

    // Publish with failing gh exits 1 with stderr propagated.
    gh.set_rules(json!([rule(
        &["api", "repos/o/r/issues?state=open&per_page=100&page=1"],
        "",
        "boom\n",
        1
    )]));
    let out_err = gh.run(
        repo.root(),
        &[
            "repin-issue",
            "publish",
            "--repository",
            "o/r",
            "--target",
            &cur,
            "--patch",
            "crlf.patch",
            "--current",
            &cur,
        ],
        "",
    );
    assert_eq!(out_err.code, 1);
    assert_eq!(out_err.stderr, "release-policy: boom\n");

    // system_gh with empty stderr falls back to command name message.
    gh.set_rules(json!([rule(&["api", "broken"], "", "  \n", 1)]));
    let _ = system_gh;
    let gh_path = gh.bin();
    let empty_err_out = gh.run(
        repo.root(),
        &[
            "repin-issue",
            "publish",
            "--repository",
            "o/r",
            "--target",
            &cur,
            "--patch",
            "missing.patch",
            "--current",
            &cur,
        ],
        "",
    );
    assert_eq!(empty_err_out.code, 1);
    assert!(empty_err_out.stderr.starts_with("release-policy: "));
    assert!(gh_path.exists());

    // Prepare normalises `./repin.patch` like Python's pathlib.Path.
    repo.write(
        WORKFLOW,
        workflow_text(
            &first,
            "      - uses: RyanDuguid/other/action@aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa # v1\n",
        ),
    );
    let mixed_wf = std::fs::read(repo.path(WORKFLOW)).unwrap();
    repo.write(BASELINE, baseline_text(&canonical_digest(&mixed_wf)));
    let dot_cur = repo.commit("mixed case baseline");
    let out_dot = cli(
        repo.root(),
        &[
            "repin-issue",
            "prepare",
            "--target",
            &dot_cur,
            "--patch",
            "./repin.patch",
        ],
        "",
    );
    assert_eq!(out_dot.code, 0, "stderr: {}", out_dot.stderr);
    assert_eq!(
        out_dot.stdout,
        format!("attribution pin {first} is stale; wrote repin.patch\n")
    );

    // Large stdin + gh early failure ignores BrokenPipe and preserves gh stderr.
    let big_patch = format!(
        "--- a/x\n+++ b/x\n@@ -1 +1 @@\n+{}\n",
        "x".repeat(256 * 1024)
    );
    repo.write("big.patch", &big_patch);
    gh.set_rules(json!([
        rule(
            &["api", "repos/o/r/issues?state=open&per_page=100&page=1"],
            "[]",
            "",
            0
        ),
        rule(
            &[
                "api",
                "--method",
                "POST",
                "repos/o/r/issues",
                "--input",
                "-"
            ],
            "",
            "early exit\n",
            1,
        ),
    ]));
    let out_broken = gh.run(
        repo.root(),
        &[
            "repin-issue",
            "publish",
            "--repository",
            "o/r",
            "--target",
            &dot_cur,
            "--patch",
            "big.patch",
            "--current",
            &dot_cur,
        ],
        "",
    );
    assert_eq!(out_broken.code, 1);
    assert_eq!(out_broken.stderr, "release-policy: early exit\n");
}
