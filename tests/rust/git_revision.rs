//! Commit identity is established before revisions reach other Git operations.
//! Ported from tests/test_git_revision.py.
mod support;

use release_policy::git_revision::{commit_from_output, resolve_commit};
use support::Repo;

#[test]
fn normal_revisions_peel_to_one_commit() {
    let repo = Repo::new();
    repo.git(&["commit", "--allow-empty", "-qm", "fixture"]);
    let sha = repo.git(&["rev-parse", "HEAD"]);
    repo.git(&["tag", "-am", "fixture", "fixture-tag"]);
    for revision in ["HEAD", "main", "fixture-tag", &sha[..12], &sha] {
        assert_eq!(
            resolve_commit(repo.root(), revision).unwrap(),
            sha,
            "{revision}"
        );
    }
    for revision in ["", "--help", "--format=%ct", "missing", "HEAD^{tree}"] {
        assert!(resolve_commit(repo.root(), revision).is_err(), "{revision}");
    }
}

#[test]
fn rejects_non_sha_output_even_from_a_successful_process() {
    let two = format!("{}\n{}\n", "a".repeat(40), "b".repeat(40));
    for output in ["", "help text\n", &two] {
        assert_eq!(
            commit_from_output(true, output.as_bytes(), "HEAD").unwrap_err(),
            "revision is not a commit in this checkout: HEAD",
            "{output:?}"
        );
    }
    let sha = format!("{}\n", "a".repeat(40));
    assert_eq!(
        commit_from_output(true, sha.as_bytes(), "HEAD").unwrap(),
        "a".repeat(40)
    );
    assert!(commit_from_output(false, sha.as_bytes(), "HEAD").is_err());
}

#[test]
fn a_checkout_git_cannot_enter_is_an_error() {
    let parent = tempfile::tempdir().unwrap();
    let error = resolve_commit(&parent.path().join("missing"), "HEAD").unwrap_err();
    #[cfg(unix)]
    assert_eq!(error, "[Errno 2] No such file or directory: 'git'");
    assert!(!error.is_empty());
}
