//! The closed skill-pack release mode. Ported from tests/test_verify_skills.py.
mod support;

use release_policy::verify_skills::*;
use support::{Repo, cli};

const PYTHON: &str = "fixture-python";

fn pack() -> Repo {
    let repo = Repo::new();
    repo.write("VERSION", "1.2.3\n");
    repo.write("requirements-test.txt", "");
    repo.write("scripts/validate_validation.py", "pass\n");
    repo.write("tests/verify_skills_cli.py", "pass\n");
    repo.write("tests/test_example.py", "pass\n");
    repo.git(&["add", "."]);
    repo
}

fn expected_commands() -> Vec<Vec<String>> {
    let pip = "-m pip install --isolated --disable-pip-version-check --no-input --no-deps \
               --only-binary :all: --requirement requirements-test.txt";
    [
        pip,
        "-B -m unittest discover -s tests -v",
        "scripts/validate_validation.py",
        "tests/verify_skills_cli.py",
    ]
    .iter()
    .map(|args| {
        std::iter::once(PYTHON.to_owned())
            .chain(args.split(' ').map(str::to_owned))
            .collect()
    })
    .collect()
}

#[test]
fn guard_accepts_supported_mode_and_rejects_empty_unknown_and_frozen_v010() {
    guard_skill_release(MODE, "v1.2.3").unwrap();
    for (mode, tag) in [("", "v1.2.3"), ("unknown", "v1.2.3"), (MODE, "v0.1.0")] {
        assert!(guard_skill_release(mode, tag).is_err(), "{mode} {tag}");
    }
}

#[test]
fn valid_fixture_runs_exact_argument_arrays_in_order_without_shell() {
    let repo = pack();
    let mut calls = Vec::new();
    verify_with(repo.root(), MODE, "VERSION", PYTHON, |args, cwd| {
        assert_eq!(cwd, repo.root());
        calls.push(args.to_vec());
        Ok(())
    })
    .unwrap();
    // Catches verification_commands mutations that reorder commands or change an argument.
    assert_eq!(calls, expected_commands());
}

#[test]
fn each_of_four_command_failures_stops_immediately() {
    let repo = pack();
    for failing in 0..4 {
        let mut calls = 0;
        let result = verify_with(repo.root(), MODE, "VERSION", PYTHON, |_, _| {
            calls += 1;
            if calls - 1 == failing {
                Err("failed".into())
            } else {
                Ok(())
            }
        });
        assert!(result.is_err());
        assert_eq!(calls, failing + 1);
    }
}

#[test]
fn required_files_reject_missing_untracked_symlink_gitlink_and_directory() {
    let repo = pack();
    let root = repo.root();
    assert!(require_tracked_regular_file(root, "missing", "required").is_err());
    repo.write("untracked", "x");
    assert!(require_tracked_regular_file(root, "untracked", "required").is_err());
    std::fs::create_dir(repo.path("directory")).unwrap();
    assert!(require_tracked_regular_file(root, "directory", "required").is_err());
    if support::symlink(&repo.path("VERSION"), &repo.path("link")) {
        repo.git(&["add", "link"]);
        assert!(require_tracked_regular_file(root, "link", "required").is_err());
    }
    // Symbolic-link and gitlink index entries are not regular files.
    for (name, mode) in [("symlink-mode", "120000"), ("gitlink", "160000")] {
        repo.write(name, "ordinary worktree file");
        repo.set_mode(name, mode);
        assert!(
            require_tracked_regular_file(root, name, "required").is_err(),
            "{mode}"
        );
    }
    for (name, mode) in [("executable", "100755"), ("ordinary", "100644")] {
        repo.set_mode(name, mode);
        repo.write(name, "x");
        let path = require_tracked_regular_file(root, name, "required").unwrap();
        assert_eq!(path, std::fs::canonicalize(root).unwrap().join(name));
    }
}

#[test]
fn path_rejects_absolute_backslash_traversal_parent_symlink_and_outside_root() {
    let repo = pack();
    for supplied in [
        "C:/outside",
        "/outside",
        "bad\\path",
        "../outside",
        "tests/../VERSION",
    ] {
        assert_eq!(
            resolve_safe_relative(repo.root(), supplied, "path").unwrap_err(),
            "path must be a safe relative POSIX path",
            "{supplied}"
        );
    }
    let outside = tempfile::tempdir().unwrap();
    std::fs::write(outside.path().join("file"), "x").unwrap();
    if support::symlink(outside.path(), &repo.path("parent")) {
        assert!(resolve_safe_relative(repo.root(), "parent/file", "path").is_err());
        let file = outside.path().join("outside");
        std::fs::write(&file, "x").unwrap();
        assert!(support::symlink(&file, &repo.path("outside-link")));
        assert!(resolve_safe_relative(repo.root(), "outside-link", "path").is_err());
    }
}

#[test]
fn tests_directory_requires_real_directory_and_tracked_regular_test() {
    let repo = pack();
    let found = require_tracked_test_files(repo.root()).unwrap();
    assert_eq!(found.len(), 1);
    assert!(found[0].ends_with("tests/test_example.py"));
    repo.git(&["rm", "-q", "--cached", "tests/test_example.py"]);
    assert!(require_tracked_test_files(repo.root()).is_err());
    std::fs::remove_file(repo.path("tests/test_example.py")).unwrap();
    repo.git(&["rm", "-q", "--cached", "tests/verify_skills_cli.py"]);
    std::fs::remove_file(repo.path("tests/verify_skills_cli.py")).unwrap();
    std::fs::remove_dir(repo.path("tests")).unwrap();
    repo.write("tests", "not a directory");
    repo.git(&["add", "tests"]);
    assert!(require_tracked_test_files(repo.root()).is_err());
}

#[test]
fn version_accepts_one_canonical_line_and_rejects_bom_encoding_and_bad_semver() {
    let repo = Repo::new();
    let version = repo.path("VERSION");
    for raw in [b"1.2.3".as_slice(), b"1.2.3\n", b"1.2.3\r\n"] {
        std::fs::write(&version, raw).unwrap();
        assert_eq!(read_canonical_version(&version).unwrap(), "1.2.3");
    }
    for raw in [
        b"\xef\xbb\xbf1.2.3\n".as_slice(),
        b"\xff",
        b"",
        b"1.2.3\n2.3.4\n",
        b"v1.2.3\n",
        b"01.2.3\n",
        b"1.2.3.4\n",
    ] {
        std::fs::write(&version, raw).unwrap();
        assert!(read_canonical_version(&version).is_err(), "{raw:?}");
    }
}

#[test]
fn cli_returns_nonzero_for_guard_and_verification_errors() {
    let repo = Repo::new();
    let out = cli(
        repo.root(),
        &[
            "verify-skills",
            "guard-release",
            "--mode",
            "unknown",
            "--tag",
            "v1.2.3",
        ],
        "",
    );
    assert_eq!(out.code, 1);
    assert_eq!(
        out.stderr,
        "verify_skills: unsupported skills verification mode: 'unknown'\n"
    );
    let out = cli(
        repo.root(),
        &["verify-skills", "verify", "--mode", MODE],
        "",
    );
    assert_eq!(out.code, 1);
    assert!(out.stderr.starts_with("verify_skills: "), "{}", out.stderr);
}
