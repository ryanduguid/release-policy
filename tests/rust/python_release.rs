//! Closed metadata parsing and exact candidate inventory for Python releases.
//! Ported from tests/test_python_release.py and tests/test_python_release_refusals.py.
//!
//! Two Python cases replaced `derive_release_tag` with a mock returning a tag
//! it can never derive, to reach the defensive re-check in `parse_release_tag`
//! and in the candidate context. The Rust `parse_release_tag` has no such
//! re-check, since the tag it derives is its input by construction; the
//! candidate context's re-check is reached by a version that names another tag.
mod support;

use num_bigint::BigInt;
use release_policy::pyjson::{self, Json};
use release_policy::release::*;
use std::path::{Path, PathBuf};
use support::{Repo, cli};

fn project(repo: &Repo, text: &str) {
    repo.write("pyproject.toml", text);
}

fn derive(repo: &Repo, parser: &str, file: &str) -> Result<serde_json::Value, String> {
    derive_metadata(repo.root(), parser, file)
}

fn refused(result: Result<impl std::fmt::Debug, String>, fragment: &str) {
    let error = result.unwrap_err();
    assert!(error.contains(fragment), "{error:?} lacks {fragment:?}");
}

const DEMO: &str = "[project]\nname = \"demo-package\"\nversion = \"1.2.3\"\n";

// MetadataTests

#[test]
fn reads_static_pyproject_without_executing_consumer_code() {
    let repo = Repo::new();
    project(&repo, DEMO);
    repo.write("consumer.py", "raise RuntimeError('must never execute')\n");
    repo.commit("fixture");
    let metadata = derive(&repo, "pyproject", "pyproject.toml").unwrap();
    assert_eq!(
        metadata,
        serde_json::json!({"name": "demo-package", "stem": "demo_package", "version": "1.2.3"})
    );
}

#[test]
fn normalises_dotted_and_mixed_case_names_to_the_built_wheel_stem() {
    let repo = Repo::new();
    project(
        &repo,
        "[project]\nname = \"Demo.Pkg\"\nversion = \"1.2.3\"\n",
    );
    repo.commit("fixture");
    let metadata = derive(&repo, "pyproject", "pyproject.toml").unwrap();
    assert_eq!(metadata["name"], "Demo.Pkg");
    assert_eq!(metadata["stem"], "demo_pkg");
    // The stem must name the file the build backend actually writes.
    let dist = tempfile::tempdir().unwrap();
    for name in [
        "demo_pkg-1.2.3-py3-none-any.whl",
        "demo_pkg-1.2.3.tar.gz",
        "demo_pkg-1.2.3.spdx.json",
    ] {
        std::fs::write(dist.path().join(name), "x").unwrap();
    }
    let ctx = CandidateContext {
        stem: "demo_pkg".into(),
        ..context("v1.2.3")
    };
    write_candidate_inventory(dist.path(), &ctx).unwrap();
}

#[test]
fn reads_one_literal_version_assignment_without_importing_module() {
    let repo = Repo::new();
    project(
        &repo,
        "[project]\nname = \"dynamic-demo\"\ndynamic = [\"version\"]\n",
    );
    repo.write(
        "src/dynamic_demo/version.py",
        "raise RuntimeError('must never execute')\n__version__: str = '2.3.4'\n",
    );
    repo.commit("fixture");
    let metadata = derive(&repo, "python-literal", "src/dynamic_demo/version.py").unwrap();
    assert_eq!(
        metadata,
        serde_json::json!({"name": "dynamic-demo", "stem": "dynamic_demo", "version": "2.3.4"})
    );
}

#[test]
fn rejects_nonliteral_duplicate_and_noncanonical_versions() {
    let repo = Repo::new();
    project(
        &repo,
        "[project]\nname = \"dynamic-demo\"\ndynamic = [\"version\"]\n",
    );
    let path = repo.write("version.py", "__version__ = make_version()\n");
    repo.commit("fixture");
    refused(
        derive(&repo, "python-literal", "version.py"),
        "literal string",
    );
    std::fs::write(&path, "__version__ = '1.2.3'\n__version__ = '1.2.3'\n").unwrap();
    refused(derive(&repo, "python-literal", "version.py"), "exactly one");
    std::fs::write(&path, "__version__ = '01.2.3'\n").unwrap();
    refused(derive(&repo, "python-literal", "version.py"), "canonical");
}

#[test]
fn rejects_unknown_parser_untracked_path_traversal_and_shell_payload() {
    let repo = Repo::new();
    project(&repo, DEMO);
    repo.commit("fixture");
    repo.write("untracked.py", "__version__ = '1.2.3'\n");
    for (parser, file) in [
        ("shell", "pyproject.toml"),
        ("python-literal", "../version.py"),
        ("python-literal", "untracked.py"),
        ("python-literal", "version.py; touch executed"),
    ] {
        assert!(derive(&repo, parser, file).is_err(), "{parser} {file}");
    }
    assert!(!repo.path("executed").exists());
}

#[test]
fn rejects_symlinked_version_file() {
    let repo = Repo::new();
    project(
        &repo,
        "[project]\nname = \"dynamic-demo\"\ndynamic = [\"version\"]\n",
    );
    let target = repo.write("real-version.py", "__version__ = '1.2.3'\n");
    if !support::symlink(&target, &repo.path("version.py")) {
        return; // Symbolic links need a privilege on Windows.
    }
    repo.commit("fixture");
    refused(
        derive(&repo, "python-literal", "version.py"),
        "symbolic link",
    );
}

// ReleaseInputTests

fn inputs_repo() -> Repo {
    let repo = Repo::new();
    repo.write("packages/example/source.txt", "tracked\n");
    repo.write("tracked-file.txt", "tracked\n");
    repo.commit("fixture");
    repo
}

#[test]
fn validates_component_and_derives_all_tag_forms() {
    let repo = inputs_repo();
    assert_eq!(
        validate_source_directory(repo.root(), "packages/example").unwrap(),
        "packages/example"
    );
    validate_tag_prefix("example").unwrap();
    assert_eq!(validate_source_directory(repo.root(), ".").unwrap(), ".");
    assert_eq!(
        parse_release_tag("example/v1.2.3", "example").unwrap(),
        ReleaseTag {
            version: "1.2.3".into(),
            version_tag: "v1.2.3".into(),
            full_tag: "example/v1.2.3".into(),
            artifact_tag: "example-v1.2.3".into(),
            tag_prefix: "example".into(),
        }
    );
    assert_eq!(derive_release_tag("1.2.3", "").unwrap().full_tag, "v1.2.3");
}

#[test]
fn cli_emits_json_and_line_protocols() {
    let repo = inputs_repo();
    let root = repo.root().to_string_lossy().into_owned();
    let run = |args: &[&str]| {
        let mut all = vec!["python-release"];
        all.extend(args);
        let out = cli(repo.root(), &all, "");
        assert_eq!(out.code, 0, "{}", out.stderr);
        out.stdout
    };
    let inputs = [
        "validate-inputs",
        "--root",
        &root,
        "--source-directory",
        "packages/example",
        "--tag-prefix",
        "example",
    ];
    assert_eq!(
        run(&inputs),
        "{\"source_directory\": \"packages/example\", \"tag_prefix\": \"example\"}\n"
    );
    assert_eq!(
        run(&[&inputs[..], &["--format", "lines"]].concat()),
        "packages/example\nexample\n"
    );
    let tag = run(&["tag", "--tag", "example/v1.2.3", "--tag-prefix", "example"]);
    assert!(tag.contains("\"artifact_tag\": \"example-v1.2.3\""));
    assert_eq!(
        run(&[
            "tag",
            "--tag",
            "example/v1.2.3",
            "--tag-prefix",
            "example",
            "--format",
            "lines"
        ]),
        "1.2.3\nv1.2.3\nexample/v1.2.3\nexample-v1.2.3\n"
    );
}

#[test]
fn rejects_unsafe_component_paths_and_tag_forms() {
    let repo = inputs_repo();
    std::fs::create_dir(repo.path("packages/untracked")).unwrap();
    for supplied in [
        "",
        "packages\\example",
        "C:/packages/example",
        "packages/control\x1f",
        "/packages/example",
        "packages//example",
        "packages/./example",
        "packages/../example",
        "packages/[example]",
        "packages/missing",
        "packages/untracked",
        "tracked-file.txt",
    ] {
        assert!(
            validate_source_directory(repo.root(), supplied).is_err(),
            "{supplied:?}"
        );
    }
    // A gitlink on the way to the component is refused.
    repo.set_mode("vendor", "160000");
    repo.write("vendor/inner/file.txt", "x");
    refused(
        validate_source_directory(repo.root(), "vendor/inner"),
        "gitlink",
    );
    // Index reads that fail or come back malformed are refused.
    let outside = tempfile::tempdir().unwrap();
    refused(
        validate_source_directory(outside.path(), "packages"),
        "Git index",
    );
    refused(parse_index_entries(true, b"malformed\0"), "malformed");
    for prefix in ["Example", "example--tool", "example/tool"] {
        assert!(validate_tag_prefix(prefix).is_err(), "{prefix}");
    }
    refused(derive_release_tag("01.2.3", ""), "canonical");
    refused(
        parse_release_tag("other/v1.2.3", "example"),
        "does not match",
    );
    refused(parse_release_tag("example/v1.2", "example"), "canonical");
}

#[test]
fn symlinked_component_is_refused() {
    let repo = inputs_repo();
    let target = repo.path("packages/example");
    if !support::symlink(&target, &repo.path("linked")) {
        return; // Symbolic links need a privilege on Windows.
    }
    refused(
        validate_source_directory(repo.root(), "linked"),
        "symbolic link",
    );
}

// CandidateInventoryTests

const STEM: &str = "demo_pkg";
const VERSION: &str = "1.2.3";

fn context(tag: &str) -> CandidateContext {
    CandidateContext {
        stem: STEM.into(),
        version: VERSION.into(),
        tag: tag.into(),
        repository: "ryanduguid/demo-package".into(),
        commit: "1".repeat(40),
        policy_sha: "2".repeat(40),
        run_id: BigInt::from(12345),
        run_attempt: BigInt::from(2),
    }
}

fn dist() -> (tempfile::TempDir, PathBuf) {
    let dir = tempfile::tempdir().unwrap();
    let path = dir.path().to_path_buf();
    for (name, payload) in [
        (
            format!("{STEM}-{VERSION}-py3-none-any.whl"),
            b"wheel\n".as_slice(),
        ),
        (format!("{STEM}-{VERSION}.tar.gz"), b"sdist\n"),
        (
            format!("{STEM}-{VERSION}.spdx.json"),
            b"{\"spdxVersion\":\"SPDX-2.3\"}\n",
        ),
    ] {
        std::fs::write(path.join(name), payload).unwrap();
    }
    (dir, path)
}

fn manifest(dist: &Path) -> Json {
    pyjson::loads(&std::fs::read_to_string(dist.join("release-manifest.json")).unwrap()).unwrap()
}

fn write_manifest(dist: &Path, document: &Json, canonical: bool) {
    let options = pyjson::Dump {
        indent: canonical.then_some(2),
        sort_keys: true,
        ensure_ascii: true,
    };
    std::fs::write(
        dist.join("release-manifest.json"),
        format!("{}\n", pyjson::dumps(document, options)),
    )
    .unwrap();
}

fn set(document: &mut Json, path: &[&str], value: Option<Json>) {
    let Json::Object(pairs) = document else {
        panic!()
    };
    let index = pairs.iter().position(|(k, _)| k == path[0]);
    if path.len() == 1 {
        match (index, value) {
            (Some(i), Some(v)) => pairs[i].1 = v,
            (Some(i), None) => {
                pairs.remove(i);
            }
            (None, Some(v)) => pairs.push((path[0].into(), v)),
            (None, None) => {}
        }
    } else {
        set(&mut pairs[index.unwrap()].1, &path[1..], value);
    }
}

#[test]
fn round_trips_exact_candidate_and_checksum_inventory() {
    let (_dir, dist) = dist();
    let ctx = context("v1.2.3");
    let created = write_candidate_inventory(&dist, &ctx).unwrap();
    assert_eq!(verify_candidate_inventory(&dist, &ctx).unwrap(), created);
    let mut names = std::fs::read_dir(&dist)
        .unwrap()
        .map(|e| e.unwrap().file_name().into_string().unwrap())
        .collect::<Vec<_>>();
    names.sort();
    assert_eq!(
        names,
        [
            "SHA256SUMS".to_owned(),
            format!("{STEM}-{VERSION}-py3-none-any.whl"),
            format!("{STEM}-{VERSION}.spdx.json"),
            format!("{STEM}-{VERSION}.tar.gz"),
            "release-manifest.json".into(),
        ]
    );
    let document = manifest(&dist);
    let source = document.get("source").unwrap();
    assert_eq!(
        source.get("repository").unwrap().as_str(),
        Some("ryanduguid/demo-package")
    );
    assert_eq!(
        source.get("ref").unwrap().as_str(),
        Some("refs/tags/v1.2.3")
    );
    assert_eq!(
        document.get("policy").unwrap().get("sha").unwrap().as_str(),
        Some("2".repeat(40).as_str())
    );
    assert_eq!(
        document.get("run").unwrap().repr(),
        "{'attempt': 2, 'id': 12345}"
    );
    let Some(Json::Array(assets)) = document.get("assets") else {
        panic!()
    };
    assert_eq!(assets.len(), 3);
    assert_eq!(
        std::fs::read_to_string(dist.join("SHA256SUMS"))
            .unwrap()
            .lines()
            .count(),
        4
    );
}

#[test]
fn round_trips_namespaced_source_tag_without_slash_bearing_assets() {
    let (_dir, dist) = dist();
    let ctx = context("payday-super-checker/v1.2.3");
    let created = write_candidate_inventory(&dist, &ctx).unwrap();
    assert_eq!(verify_candidate_inventory(&dist, &ctx).unwrap(), created);
    let document = manifest(&dist);
    assert_eq!(
        document.get("source").unwrap().get("ref").unwrap().as_str(),
        Some("refs/tags/payday-super-checker/v1.2.3")
    );
}

#[test]
fn rejects_noncanonical_context_and_non_tag_manifest_ref() {
    let ctx = CandidateContext {
        version: "01.2.3".into(),
        ..context("v01.2.3")
    };
    refused(ctx.validate(), "canonical version");
    let (_dir, dist) = dist();
    write_candidate_inventory(&dist, &context("v1.2.3")).unwrap();
    let mut document = manifest(&dist);
    set(
        &mut document,
        &["source", "ref"],
        Some(Json::str_obj("refs/heads/main")),
    );
    refused(inventory_from_manifest(&document), "source ref");
}

fn rewrite_checksums(dist: &Path) {
    let mut names = std::fs::read_dir(dist)
        .unwrap()
        .map(|e| e.unwrap().file_name().into_string().unwrap())
        .filter(|n| n != "SHA256SUMS")
        .collect::<Vec<_>>();
    names.sort();
    let text: String = names
        .iter()
        .map(|n| {
            format!(
                "{}  {n}\n",
                release_policy::common::sha256(&std::fs::read(dist.join(n)).unwrap())
            )
        })
        .collect();
    std::fs::write(dist.join("SHA256SUMS"), text).unwrap();
}

#[test]
fn rejects_a_boolean_size_that_equals_a_one_byte_asset() {
    let (_dir, dist) = dist();
    let wheel = format!("{STEM}-{VERSION}-py3-none-any.whl");
    std::fs::write(dist.join(&wheel), "w").unwrap();
    let ctx = context("v1.2.3");
    write_candidate_inventory(&dist, &ctx).unwrap();
    verify_candidate_inventory(&dist, &ctx).unwrap(); // the honest integer 1 stays acceptable
    let mut document = manifest(&dist);
    let Some(Json::Array(assets)) = document.get("assets").cloned() else {
        panic!()
    };
    let assets = assets
        .into_iter()
        .map(|mut asset| {
            if asset.get("name").unwrap().as_str() == Some(&wheel) {
                assert_eq!(asset.get("size"), Some(&Json::Int(1.into())));
                set(&mut asset, &["size"], Some(Json::Bool(true)));
            }
            asset
        })
        .collect();
    set(&mut document, &["assets"], Some(Json::Array(assets)));
    write_manifest(&dist, &document, true);
    rewrite_checksums(&dist);
    refused(verify_candidate_inventory(&dist, &ctx), "size is invalid");
}

#[test]
fn rejects_tamper_extra_file_and_context_mismatch() {
    let (_dir, dist) = dist();
    let ctx = context("v1.2.3");
    write_candidate_inventory(&dist, &ctx).unwrap();
    let wheel = dist.join(format!("{STEM}-{VERSION}-py3-none-any.whl"));
    std::fs::write(&wheel, "bogus\n").unwrap();
    refused(verify_candidate_inventory(&dist, &ctx), "digest");
    std::fs::write(&wheel, "wheel\n").unwrap();
    std::fs::write(dist.join("extra.txt"), "extra").unwrap();
    refused(verify_candidate_inventory(&dist, &ctx), "inventory");
    std::fs::remove_file(dist.join("extra.txt")).unwrap();
    let other = CandidateContext {
        commit: "3".repeat(40),
        ..ctx
    };
    refused(verify_candidate_inventory(&dist, &other), "source commit");
}

#[test]
fn refuses_to_overwrite_control_files_or_accept_symlinks() {
    let (_dir, dist) = dist();
    let ctx = context("v1.2.3");
    std::fs::write(dist.join("release-manifest.json"), "{}").unwrap();
    refused(
        write_candidate_inventory(&dist, &ctx),
        "refusing to overwrite existing control file",
    );
    std::fs::remove_file(dist.join("release-manifest.json")).unwrap();
    let wheel = dist.join(format!("{STEM}-{VERSION}-py3-none-any.whl"));
    let target = dist.join("real.whl");
    std::fs::copy(&wheel, &target).unwrap();
    std::fs::remove_file(&wheel).unwrap();
    if support::symlink(&target, &wheel) {
        refused(write_candidate_inventory(&dist, &ctx), "regular file");
    }
}

#[test]
fn rejects_a_symlinked_candidate_directory() {
    let (_dir, dist) = dist();
    let root = tempfile::tempdir().unwrap();
    let link = root.path().join("dist");
    if support::symlink(&dist, &link) {
        refused(
            write_candidate_inventory(&link, &context("v1.2.3")),
            "regular directory",
        );
    }
}

// MetadataRefusalTests

#[test]
fn rejects_a_backslash_version_file_path() {
    let repo = Repo::new();
    project(&repo, DEMO);
    repo.commit("fixture");
    refused(
        derive(&repo, "pyproject", "pkg\\version.py"),
        "safe relative POSIX path",
    );
}

#[test]
fn rejects_a_version_file_that_is_a_directory() {
    let repo = Repo::new();
    project(&repo, DEMO);
    repo.write("pkg/__init__.py", "");
    repo.commit("fixture");
    refused(
        derive(&repo, "pyproject", "pkg"),
        "regular file inside the checkout",
    );
}

#[test]
fn rejects_a_version_file_over_the_metadata_limit() {
    let repo = Repo::new();
    let padding = format!("# {}\n", "x".repeat(1024 * 1024));
    project(&repo, &format!("{DEMO}{padding}"));
    repo.commit("fixture");
    refused(
        derive(&repo, "pyproject", "pyproject.toml"),
        "exceeds the 1 MiB metadata limit",
    );
}

#[test]
fn rejects_a_bom_or_non_utf8_pyproject() {
    let repo = Repo::new();
    for (raw, message) in [
        (
            b"\xef\xbb\xbf[project]\nname = 'x'\n".as_slice(),
            "must not contain a UTF-8 BOM",
        ),
        (b"[project]\nname = '\xff'\n", "must be UTF-8"),
    ] {
        repo.write("pyproject.toml", raw);
        repo.commit("fixture");
        refused(derive(&repo, "pyproject", "pyproject.toml"), message);
    }
}

#[test]
fn rejects_a_missing_or_blank_project_name() {
    let repo = Repo::new();
    for (text, message) in [
        ("[tool.other]\nx = 1\n", "must declare [project].name"),
        (
            "[project]\nname = \"  \"\nversion = \"1.2.3\"\n",
            "must be a non-empty string",
        ),
    ] {
        project(&repo, text);
        repo.commit("fixture");
        refused(derive(&repo, "pyproject", "pyproject.toml"), message);
    }
}

#[test]
fn rejects_a_missing_or_non_string_pyproject_version() {
    let repo = Repo::new();
    for (text, message) in [
        ("[project]\nname = \"demo\"\n", "requires [project].version"),
        (
            "[project]\nname = \"demo\"\nversion = 1\n",
            "must be a string",
        ),
    ] {
        project(&repo, text);
        repo.commit("fixture");
        refused(derive(&repo, "pyproject", "pyproject.toml"), message);
    }
}

#[test]
fn python_literal_reads_one_direct_string_assignment() {
    let repo = Repo::new();
    project(&repo, DEMO);
    repo.write("pkg/version.py", "other = 1\n__version__ = \"1.2.3\"\n");
    repo.commit("fixture");
    assert_eq!(
        derive(&repo, "python-literal", "pkg/version.py").unwrap()["version"],
        "1.2.3"
    );
}

#[test]
fn python_literal_refusals() {
    let repo = Repo::new();
    for (source, message) in [
        (
            "__version__ = other = \"1.2.3\"\n",
            "one direct literal string assignment",
        ),
        ("__version__: str\n", "one direct literal string assignment"),
        ("__version__ = 1\n", "one direct literal string assignment"),
        ("def broken(:\n", "valid Python source"),
    ] {
        project(&repo, DEMO);
        repo.write("pkg/version.py", source);
        repo.commit("fixture");
        refused(derive(&repo, "python-literal", "pkg/version.py"), message);
    }
}

#[test]
fn rejects_a_name_that_makes_no_safe_wheel_stem() {
    let repo = Repo::new();
    project(
        &repo,
        "[project]\nname = \"demo package\"\nversion = \"1.2.3\"\n",
    );
    repo.commit("fixture");
    refused(
        derive(&repo, "pyproject", "pyproject.toml"),
        "safe wheel stem",
    );
}

#[test]
fn metadata_command_prints_lines_or_json() {
    let repo = Repo::new();
    project(&repo, DEMO);
    repo.commit("fixture");
    let root = repo.root().to_string_lossy().into_owned();
    for (format, expected) in [
        ("lines", "demo_package\n1.2.3\n"),
        (
            "json",
            "{\"name\": \"demo-package\", \"stem\": \"demo_package\", \"version\": \"1.2.3\"}\n",
        ),
    ] {
        let out = cli(
            repo.root(),
            &[
                "python-release",
                "metadata",
                "--root",
                &root,
                "--version-parser",
                "pyproject",
                "--version-file",
                "pyproject.toml",
                "--format",
                format,
            ],
            "",
        );
        assert_eq!((out.code, out.stdout.as_str()), (0, expected));
    }
}

// CandidateRefusalTests

#[test]
fn rejects_an_unsafe_workflow_context() {
    let (_dir, dist) = dist();
    let base = context("v1.2.3");
    for (ctx, message) in [
        (
            CandidateContext {
                stem: "Bad Stem".into(),
                ..base.clone()
            },
            "stem is not safe",
        ),
        (
            CandidateContext {
                repository: "no-owner".into(),
                ..base.clone()
            },
            "repository must be owner/name",
        ),
        (
            CandidateContext {
                commit: "abc".into(),
                ..base.clone()
            },
            "source commit must be a full lower-case SHA",
        ),
        (
            CandidateContext {
                policy_sha: "A".repeat(40),
                ..base.clone()
            },
            "policy SHA must be a full lower-case SHA",
        ),
        (
            CandidateContext {
                run_id: BigInt::from(0),
                ..base.clone()
            },
            "run id and attempt must be positive",
        ),
    ] {
        refused(write_candidate_inventory(&dist, &ctx), message);
    }
}

#[test]
fn rejects_an_empty_asset_and_a_stray_file() {
    let (_dir, dist) = dist();
    let ctx = context("v1.2.3");
    let wheel = dist.join(format!("{STEM}-{VERSION}-py3-none-any.whl"));
    std::fs::write(&wheel, "").unwrap();
    refused(write_candidate_inventory(&dist, &ctx), "invalid size");
    std::fs::write(&wheel, "wheel\n").unwrap();
    std::fs::write(dist.join("notes.txt"), "stray\n").unwrap();
    refused(
        write_candidate_inventory(&dist, &ctx),
        "candidate payload inventory mismatch",
    );
}

#[test]
fn regular_asset_rejects_a_directory() {
    let (_dir, dist) = dist();
    let folder = dist.join("folder");
    std::fs::create_dir(&folder).unwrap();
    refused(regular_asset(&folder), "must be a regular file: folder");
}

#[test]
fn rejects_malformed_manifest_documents() {
    let changes: [(&[&str], Option<Json>, &str); 6] = [
        (&["package"], None, "root has an invalid shape"),
        (
            &["schema"],
            Some(Json::Int(2.into())),
            "schema is unsupported",
        ),
        (
            &["policy", "workflow"],
            Some(Json::str_obj(".github/workflows/other.yml")),
            "policy signer is not release-python.yml",
        ),
        (
            &["assets"],
            Some(Json::Object(vec![])),
            "assets must be a list",
        ),
        (
            &["run", "id"],
            Some(Json::str_obj("not a number")),
            "scalar fields are invalid",
        ),
        (&["assets"], None, "root has an invalid shape"),
    ];
    let ctx = context("v1.2.3");
    for (path, value, message) in changes {
        let (_dir, dist) = dist();
        write_candidate_inventory(&dist, &ctx).unwrap();
        let mut document = manifest(&dist);
        set(&mut document, path, value);
        write_manifest(&dist, &document, true);
        refused(verify_candidate_inventory(&dist, &ctx), message);
    }
    // A numeric asset name.
    let (_dir, dist) = dist();
    write_candidate_inventory(&dist, &ctx).unwrap();
    let mut document = manifest(&dist);
    let Some(Json::Array(mut assets)) = document.get("assets").cloned() else {
        panic!()
    };
    set(&mut assets[0], &["name"], Some(Json::Int(1.into())));
    set(&mut document, &["assets"], Some(Json::Array(assets)));
    write_manifest(&dist, &document, true);
    refused(
        verify_candidate_inventory(&dist, &ctx),
        "asset text fields are invalid",
    );
}

#[test]
fn rejects_an_oversized_unparsable_or_non_canonical_manifest() {
    let ctx = context("v1.2.3");
    let (_dir, dist) = dist();
    write_candidate_inventory(&dist, &ctx).unwrap();
    let document = manifest(&dist);
    std::fs::write(
        dist.join("release-manifest.json"),
        " ".repeat(1024 * 1024 + 1),
    )
    .unwrap();
    refused(
        verify_candidate_inventory(&dist, &ctx),
        "exceeds the 1 MiB limit",
    );
    write_manifest(&dist, &document, false);
    refused(
        verify_candidate_inventory(&dist, &ctx),
        "not canonical JSON",
    );
    std::fs::write(dist.join("release-manifest.json"), "{not json").unwrap();
    refused(verify_candidate_inventory(&dist, &ctx), "not valid JSON");
}

#[test]
fn rejects_asset_records_that_disagree_with_the_files() {
    let ctx = context("v1.2.3");
    type Change = fn(&mut Vec<CandidateAsset>);
    let changes: [(Change, &str); 4] = [
        (
            |a| a[0].name = "other.whl".into(),
            "payload inventory is invalid",
        ),
        (|a| a.push(a[0].clone()), "contains duplicate assets"),
        (
            |a| a[0].media_type = "text/plain".into(),
            "media type mismatch",
        ),
        (|a| a[0].size += 1, "size mismatch"),
    ];
    for (change, message) in changes {
        let (_dir, dist) = dist();
        let mut tampered = write_candidate_inventory(&dist, &ctx).unwrap();
        change(&mut tampered.assets);
        std::fs::write(
            dist.join("release-manifest.json"),
            manifest_bytes(&tampered),
        )
        .unwrap();
        refused(verify_candidate_inventory(&dist, &ctx), message);
    }
}

#[test]
fn rejects_an_altered_checksum_inventory() {
    let ctx = context("v1.2.3");
    let (_dir, dist) = dist();
    write_candidate_inventory(&dist, &ctx).unwrap();
    let sums = dist.join("SHA256SUMS");
    let text = std::fs::read_to_string(&sums).unwrap();
    std::fs::write(&sums, format!("{text}\n")).unwrap();
    refused(
        verify_candidate_inventory(&dist, &ctx),
        "checksum inventory or digest mismatch",
    );
}

#[test]
fn create_and_verify_commands_round_trip() {
    let (_dir, dist) = dist();
    let directory = dist.to_string_lossy().into_owned();
    let args = |command: &'static str| {
        vec![
            "python-release".to_owned(),
            command.into(),
            "--directory".into(),
            directory.clone(),
            "--stem".into(),
            STEM.into(),
            "--version".into(),
            VERSION.into(),
            "--tag".into(),
            "v1.2.3".into(),
            "--repository".into(),
            "ryanduguid/demo-package".into(),
            "--commit".into(),
            "1".repeat(40),
            "--policy-sha".into(),
            "2".repeat(40),
            "--run-id".into(),
            "12345".into(),
            "--run-attempt".into(),
            "2".into(),
        ]
    };
    for command in ["create-candidate", "verify-candidate"] {
        let args = args(command);
        let args = args.iter().map(String::as_str).collect::<Vec<_>>();
        let out = cli(&dist, &args, "");
        assert_eq!(out.code, 0, "{}", out.stderr);
    }
    assert!(dist.join("SHA256SUMS").is_file());
}

#[test]
fn python_literal_keeps_cpythons_literal_rules() {
    // A set holding an unhashable tuple raises TypeError at once; a hashable
    // one waits for the assignment count.
    refused(
        python_literal_version("__version__ = {([],)}\n__version__ = \"1.2.3\"\n"),
        "one direct literal string assignment",
    );
    refused(
        python_literal_version("__version__ = {(1,)}\n__version__ = \"1.2.3\"\n"),
        "exactly one __version__ assignment",
    );
    // A leading zero alone does not make a based literal.
    assert_eq!(
        python_literal_version("zero = 00\n__version__ = \"1.2.3\"\n").unwrap(),
        "1.2.3"
    );
    // The 4300-digit limit covers decimal literals only.
    let hexadecimal = format!("big = 0x{}\n__version__ = \"1.2.3\"\n", "1".repeat(5000));
    assert_eq!(python_literal_version(&hexadecimal).unwrap(), "1.2.3");
    let decimal = format!("big = {}\n__version__ = \"1.2.3\"\n", "1".repeat(4301));
    refused(python_literal_version(&decimal), "valid Python source");
    let largest = format!("big = {}\n__version__ = \"1.2.3\"\n", "1".repeat(4300));
    assert_eq!(python_literal_version(&largest).unwrap(), "1.2.3");
}

#[test]
fn metadata_refuses_a_checkout_whose_git_cannot_run() {
    let repo = Repo::new();
    project(&repo, DEMO);
    repo.commit("fixture");
    let empty = tempfile::tempdir().unwrap();
    let out = support::cli_env(
        repo.root(),
        &[
            "python-release",
            "metadata",
            "--version-parser",
            "pyproject",
            "--version-file",
            "pyproject.toml",
        ],
        "",
        &[("PATH", &empty.path().to_string_lossy())],
    );
    assert_eq!(out.code, 1);
    assert!(out.stdout.is_empty());
    // Windows reports the failed program search without naming the program.
    let expected = if cfg!(windows) {
        "release-policy: program not found\n"
    } else {
        "release-policy: [Errno 2] No such file or directory: 'git'\n"
    };
    assert_eq!(out.stderr, expected);
}

#[test]
fn a_candidate_version_must_name_its_tag() {
    let (_dir, dist) = dist();
    let ctx = CandidateContext {
        version: "1.2.4".into(),
        ..context("v1.2.3")
    };
    refused(
        write_candidate_inventory(&dist, &ctx),
        "candidate tag and canonical version do not match",
    );
}

#[test]
fn manifest_policy_and_source_must_be_exact_mappings() {
    let ctx = context("v1.2.3");
    let (_dir, dist) = dist();
    write_candidate_inventory(&dist, &ctx).unwrap();
    for (key, value, message) in [
        ("policy", Json::Int(1.into()), "policy has an invalid shape"),
        ("source", Json::Array(vec![]), "source has an invalid shape"),
        ("run", Json::Null, "run has an invalid shape"),
    ] {
        let mut document = manifest(&dist);
        set(&mut document, &[key], Some(value));
        refused(inventory_from_manifest(&document), message);
    }
}

#[test]
fn manifest_run_numbers_convert_as_python_int_does() {
    let ctx = context("v1.2.3");
    let (_dir, dist) = dist();
    write_candidate_inventory(&dist, &ctx).unwrap();
    let read = |id: Json| {
        let mut document = manifest(&dist);
        set(&mut document, &["run", "id"], Some(id));
        inventory_from_manifest(&document).map(|i| i.context.run_id)
    };
    // bool is an int, and int() truncates a float towards zero.
    assert_eq!(read(Json::Bool(true)).unwrap(), BigInt::from(1));
    assert_eq!(read(Json::Float(12345.9)).unwrap(), BigInt::from(12345));
    assert_eq!(read(Json::str_obj("12345")).unwrap(), BigInt::from(12345));
    for id in [Json::Bool(false), Json::Float(0.5)] {
        refused(read(id), "run id and attempt must be positive");
    }
    for id in [Json::Float(f64::NAN), Json::Null, Json::Array(vec![])] {
        refused(read(id), "scalar fields are invalid");
    }
    for id in [Json::Float(f64::INFINITY), Json::Float(f64::NEG_INFINITY)] {
        assert_eq!(
            read(id).unwrap_err(),
            "unexpected error: OverflowError: cannot convert float infinity to integer"
        );
    }
}

#[test]
fn verification_compares_the_run_id_and_attempt() {
    let ctx = context("v1.2.3");
    let (_dir, dist) = dist();
    write_candidate_inventory(&dist, &ctx).unwrap();
    for (other, message) in [
        (
            CandidateContext {
                run_id: BigInt::from(999),
                ..ctx.clone()
            },
            "candidate run id mismatch: 12345 != 999",
        ),
        (
            CandidateContext {
                run_attempt: BigInt::from(3),
                ..ctx.clone()
            },
            "candidate run attempt mismatch: 2 != 3",
        ),
    ] {
        assert_eq!(
            verify_candidate_inventory(&dist, &other).unwrap_err(),
            message
        );
    }
}
