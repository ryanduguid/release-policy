// Copyright 2026 Ryan Duguid
// SPDX-License-Identifier: LicenseRef-Duguid-Public-Attribution-1.0

//! `release-policy init`: each supported repository shape in tests/data/init
//! gets exactly its golden caller workflow and report, and init refuses what
//! it cannot write safely. tests/test_init_goldens.py runs the policy's own
//! validation on the same goldens, and CI lints them with actionlint.
mod support;

use std::path::{Path, PathBuf};
use support::{Repo, cli};

const CALLER: &str = ".github/workflows/release.yml";

fn shape(name: &str) -> PathBuf {
    Path::new(env!("CARGO_MANIFEST_DIR"))
        .join("tests/data/init")
        .join(name)
}

fn golden(path: &Path) -> String {
    std::fs::read_to_string(path).unwrap().replace("\r\n", "\n")
}

fn copy_tree(from: &Path, to: &Path) {
    for entry in std::fs::read_dir(from).unwrap() {
        let entry = entry.unwrap();
        let target = to.join(entry.file_name());
        if entry.file_type().unwrap().is_dir() {
            std::fs::create_dir_all(&target).unwrap();
            copy_tree(&entry.path(), &target);
        } else {
            // Read as text so a CRLF checkout still gives the fixture LF bytes.
            std::fs::write(&target, golden(&entry.path())).unwrap();
        }
    }
}

/// A committed checkout holding the shape's input tree.
fn checkout(name: &str) -> Repo {
    let repo = Repo::new();
    copy_tree(&shape(name).join("input"), repo.root());
    repo.commit("fixture");
    repo
}

fn args(name: &str) -> Vec<String> {
    golden(&shape(name).join("args"))
        .lines()
        .map(str::to_owned)
        .collect()
}

fn run(repo: &Repo, args: &[&str]) -> support::Output {
    cli(repo.root(), args, "")
}

#[test]
fn each_supported_shape_gets_its_golden_caller_and_report() {
    for name in ["python", "python-literal", "binary", "archive"] {
        let repo = checkout(name);
        let args = args(name);
        let out = run(&repo, &args.iter().map(String::as_str).collect::<Vec<_>>());
        assert_eq!(out.code, 0, "{name}: {}", out.stderr);
        assert_eq!(out.stderr, "", "{name}");
        assert_eq!(
            out.stdout,
            golden(&shape(name).join("stdout.txt")),
            "{name}"
        );
        // Byte for byte: the caller is LF on every platform.
        assert_eq!(
            std::fs::read(repo.path(CALLER)).unwrap(),
            golden(&shape(name).join("release.yml")).into_bytes(),
            "{name}"
        );
    }
}

#[test]
fn an_existing_caller_is_kept_unless_forced() {
    let repo = checkout("archive");
    repo.write(CALLER, "name: Mine\n");
    let args = args("archive");
    let mut args = args.iter().map(String::as_str).collect::<Vec<_>>();
    let out = run(&repo, &args);
    assert_eq!(out.code, 1);
    assert_eq!(out.stdout, "");
    assert_eq!(
        out.stderr,
        "release-policy: .github/workflows/release.yml already exists; pass --force to replace it\n"
    );
    assert_eq!(golden(&repo.path(CALLER)), "name: Mine\n");

    args.push("--force");
    let out = run(&repo, &args);
    assert_eq!(out.code, 0, "{}", out.stderr);
    assert_eq!(
        golden(&repo.path(CALLER)),
        golden(&shape("archive").join("release.yml"))
    );
}

#[cfg(unix)]
#[test]
fn a_symbolic_link_on_the_caller_path_is_refused_even_when_forced() {
    let repo = checkout("archive");
    let outside = support::tempdir();
    std::os::unix::fs::symlink(outside.path(), repo.path(".github/workflows/release.yml")).unwrap();
    let out = run(
        &repo,
        &[
            "init",
            "--artifact-stem",
            "example-archive",
            "--required-check",
            ".github/workflows/ci.yml: test",
            "--force",
        ],
    );
    assert_eq!(out.code, 1);
    assert_eq!(
        out.stderr,
        "release-policy: .github/workflows/release.yml must not be a symbolic link\n"
    );
    assert_eq!(std::fs::read_dir(outside.path()).unwrap().count(), 0);
}

fn refused(repo: &Repo, args: &[&str], message: &str) {
    let out = run(repo, args);
    assert_eq!(out.code, 1, "{args:?}: {}", out.stdout);
    assert_eq!(
        out.stderr,
        format!("release-policy: {message}\n"),
        "{args:?}"
    );
    assert!(!repo.path(CALLER).exists(), "{args:?}");
}

const CHECK: [&str; 2] = ["--required-check", ".github/workflows/ci.yml: test"];

#[test]
fn shapes_it_cannot_tell_apart_or_find_are_refused() {
    let both = checkout("python");
    both.write("Cargo.toml", "[package]\nname = \"example\"\n");
    refused(
        &both,
        &["init", CHECK[0], CHECK[1]],
        "found both pyproject.toml [project] and Cargo.toml [package]; pass --family",
    );
    let out = run(&both, &["init", "--family", "python", CHECK[0], CHECK[1]]);
    assert_eq!(out.code, 0, "{}", out.stderr);

    let none = Repo::new();
    none.write(".github/workflows/ci.yml", "name: CI\n");
    none.write("pyproject.toml", "[tool.ruff]\n");
    none.commit("fixture");
    refused(
        &none,
        &["init", CHECK[0], CHECK[1]],
        "found no pyproject.toml with [project], Cargo.toml with [package] or VERSION file; pass --family",
    );

    let broken = Repo::new();
    broken.write("Cargo.toml", "[package\n");
    refused(
        &broken,
        &["init", CHECK[0], CHECK[1]],
        "Cargo.toml is not valid TOML",
    );
}

#[test]
fn inputs_the_policy_would_refuse_are_refused_before_writing() {
    let archive = checkout("archive");
    for (args, message) in [
        (
            vec!["init", "--artifact-stem", "Example"],
            "artifact-stem must be lower-case and hyphenated: 'Example'",
        ),
        (
            vec![
                "init",
                "--artifact-stem",
                "example",
                "--version-file",
                "VERSION",
            ],
            "--version-file applies only to the python family",
        ),
        (
            vec!["init", "--family", "python", "--artifact-stem", "example"],
            "--artifact-stem does not apply to the python family, which takes its name from pyproject.toml",
        ),
    ] {
        let mut args = args;
        args.extend(CHECK);
        refused(&archive, &args, message);
    }
    refused(
        &archive,
        &[
            "init",
            "--artifact-stem",
            "example",
            "--required-check",
            ".github/workflows/missing.yml: test",
        ],
        "required check '.github/workflows/missing.yml: test' names a workflow file that is not here",
    );
    refused(
        &archive,
        &[
            "init",
            "--artifact-stem",
            "example",
            "--required-check",
            "ci.yml: test",
        ],
        "required check 'ci.yml: test' names a path outside .github/workflows/",
    );
    refused(
        &archive,
        &[
            "init",
            "--artifact-stem",
            "example",
            "--required-check",
            ".github/workflows/ci.yml: a\u{7}b",
        ],
        "required check '.github/workflows/ci.yml: a\\x07b' holds a control character",
    );

    let python = checkout("python-literal");
    refused(
        &python,
        &["init", CHECK[0], CHECK[1]],
        "pyproject parser requires [project].version; for a dynamic version pass --version-file",
    );
    refused(
        &python,
        &[
            "init",
            "--version-file",
            "example_pkg/v 1.py",
            CHECK[0],
            CHECK[1],
        ],
        "version-file must be a relative path of letters, digits, '.', '_', '-' and '/': 'example_pkg/v 1.py'",
    );

    refused(
        &archive,
        &["init", "--family", "binary", CHECK[0], CHECK[1]],
        "Cargo.toml is missing",
    );
    let binary = checkout("binary");
    binary.write("Cargo.toml", "[package]\nname = \"Example\"\n");
    refused(
        &binary,
        &["init", "--artifact-stem", "example", CHECK[0], CHECK[1]],
        "Cargo.toml [package] name is not a binary name the release accepts: 'Example'",
    );
    binary.write("Cargo.toml", "[package]\nname.workspace = true\n");
    refused(
        &binary,
        &["init", CHECK[0], CHECK[1]],
        "Cargo.toml must state a literal [package] name; workspace inheritance is not supported",
    );
    binary.write("Cargo.toml", "[package]\nname = \"Example\"\n");
    refused(
        &binary,
        &["init", CHECK[0], CHECK[1]],
        "artifact-stem must be lower-case and hyphenated: 'Example'",
    );
}

#[test]
fn init_runs_only_at_the_top_of_the_repository() {
    let repo = checkout("archive");
    std::fs::create_dir(repo.path("sub")).unwrap();
    let out = cli(&repo.path("sub"), &["init", CHECK[0], CHECK[1]], "");
    assert_eq!(out.code, 1);
    assert_eq!(
        out.stderr,
        "release-policy: run init at the repository's top level\n"
    );
}

#[test]
fn a_source_archive_is_named_after_its_directory_by_default() {
    let parent = support::tempdir();
    let root = parent.path().join("my-archive");
    std::fs::create_dir(&root).unwrap();
    let git = |args: &[&str]| {
        let status = std::process::Command::new("git")
            .args(args)
            .current_dir(&root)
            .status()
            .unwrap();
        assert!(status.success(), "git {args:?}");
    };
    git(&["init", "-q", "-b", "main"]);
    std::fs::create_dir_all(root.join(".github/workflows")).unwrap();
    std::fs::write(root.join(".github/workflows/ci.yml"), "name: CI\n").unwrap();
    std::fs::write(root.join("VERSION"), "1.0.0\n").unwrap();
    let out = cli(&root, &["init", CHECK[0], CHECK[1]], "");
    assert_eq!(out.code, 0, "{}", out.stderr);
    assert!(
        golden(&root.join(CALLER)).contains("      artifact-stem: my-archive\n"),
        "{}",
        golden(&root.join(CALLER))
    );
}

#[test]
fn a_package_name_that_is_already_a_stem_names_no_binary() {
    let repo = checkout("binary");
    repo.write(
        "Cargo.toml",
        "[package]\nname = \"example-tool\"\nversion = \"0.1.0\"\npublish = false\n",
    );
    let out = run(&repo, &["init", CHECK[0], CHECK[1]]);
    assert_eq!(out.code, 0, "{}", out.stderr);
    let caller = golden(&repo.path(CALLER));
    assert!(
        caller.contains("      artifact-stem: example-tool\n"),
        "{caller}"
    );
    assert!(!caller.contains("binary:"), "{caller}");
}
