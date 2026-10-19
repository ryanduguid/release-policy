//! Command entry points the module tests do not reach. Ported from
//! tests/test_entry_points.py; its PR-review case stays with the Python
//! PR-review tooling.
mod support;

use support::{Repo, cli};

#[test]
fn archive_builder_command_passes_its_arguments_through() {
    let repo = Repo::new();
    repo.write("pkg/a.txt", "a\n");
    repo.write("other.txt", "b\n");
    repo.commit("fixture");
    let out = cli(
        repo.root(),
        &[
            "build-release-archives",
            "--commit",
            "HEAD",
            "--prefix",
            "example/",
            "--source-directory",
            "pkg",
            "--output-base",
            "dist/example",
        ],
        "",
    );
    assert_eq!(out.code, 0, "{}", out.stderr);
    for name in ["dist/example.zip", "dist/example.tar.gz"] {
        assert!(repo.path(name).is_file(), "{name}");
    }
}

#[test]
fn skill_release_guard_command_accepts_a_supported_mode() {
    let repo = Repo::new();
    let out = cli(
        repo.root(),
        &[
            "verify-skills",
            "guard-release",
            "--mode",
            "subcontractor-accounting-v1",
            "--tag",
            "v1.2.3",
        ],
        "",
    );
    assert_eq!((out.code, out.stderr.as_str()), (0, ""));
}
