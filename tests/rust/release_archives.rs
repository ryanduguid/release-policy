//! Deterministic source-release archives. Ported from the builder tests in
//! tests/test_release_archives.py; its workflow-contract tests read the
//! reusable workflow YAML, which stays as it is, and remain in Python.
mod support;

use release_policy::archives::{
    self, build_release_archives, build_release_archives_with, preflight,
};
use std::collections::BTreeMap;
use std::io::Read;
use std::path::{Path, PathBuf};
use support::{Repo, cli};

fn checkout() -> &'static Path {
    Path::new(env!("CARGO_MANIFEST_DIR"))
}

fn zip_files(path: &Path) -> BTreeMap<String, Vec<u8>> {
    // Listed through git's own reader would need a repository; read the
    // central directory with the zip format's fixed layout instead.
    let raw = std::fs::read(path).unwrap();
    let mut files = BTreeMap::new();
    let mut at = 0;
    while at + 30 <= raw.len() && raw[at..at + 4] == [0x50, 0x4b, 0x03, 0x04] {
        let u16_at = |i: usize| u16::from_le_bytes([raw[i], raw[i + 1]]) as usize;
        let u32_at =
            |i: usize| u32::from_le_bytes([raw[i], raw[i + 1], raw[i + 2], raw[i + 3]]) as usize;
        let method = u16_at(at + 8);
        let size = u32_at(at + 18);
        let name_len = u16_at(at + 26);
        let extra_len = u16_at(at + 28);
        let name = String::from_utf8(raw[at + 30..at + 30 + name_len].to_vec()).unwrap();
        let start = at + 30 + name_len + extra_len;
        let data = &raw[start..start + size];
        let content = match method {
            0 => data.to_vec(),
            8 => inflate(data),
            other => panic!("unexpected zip method {other}"),
        };
        if !name.ends_with('/') {
            files.insert(name, content);
        }
        at = start + size;
    }
    files
}

/// Decompress raw deflate data through gzip -d, which every CI image has.
fn inflate(data: &[u8]) -> Vec<u8> {
    let mut gz = vec![0x1f, 0x8b, 8, 0, 0, 0, 0, 0, 0, 0xff];
    gz.extend_from_slice(data);
    gz.extend_from_slice(&[0; 8]);
    let mut child = std::process::Command::new("gzip")
        .args(["-dc"])
        .stdin(std::process::Stdio::piped())
        .stdout(std::process::Stdio::piped())
        .stderr(std::process::Stdio::null())
        .spawn()
        .unwrap();
    let mut stdin = child.stdin.take().unwrap();
    std::thread::spawn(move || std::io::Write::write_all(&mut stdin, &gz));
    let mut out = Vec::new();
    child.stdout.take().unwrap().read_to_end(&mut out).unwrap();
    let _ = child.wait();
    out
}

fn tar_files(path: &Path) -> BTreeMap<String, Vec<u8>> {
    let out = std::process::Command::new("gzip")
        .args(["-dc"])
        .arg(path)
        .output()
        .unwrap();
    let raw = out.stdout;
    let mut files = BTreeMap::new();
    let mut at = 0;
    while at + 512 <= raw.len() && raw[at] != 0 {
        let header = &raw[at..at + 512];
        let field = |a: usize, b: usize| {
            String::from_utf8(
                header[a..b]
                    .iter()
                    .take_while(|c| **c != 0)
                    .copied()
                    .collect(),
            )
            .unwrap()
        };
        let name = format!(
            "{}{}",
            if field(345, 500).is_empty() {
                String::new()
            } else {
                field(345, 500) + "/"
            },
            field(0, 100)
        );
        let size = usize::from_str_radix(field(124, 136).trim(), 8).unwrap_or(0);
        let kind = header[156];
        let data = raw[at + 512..at + 512 + size].to_vec();
        if kind == b'0' || kind == 0 {
            files.insert(name, data);
        }
        at += 512 + size.div_ceil(512) * 512;
    }
    files
}

fn sha(path: &Path) -> String {
    release_policy::common::sha256(&std::fs::read(path).unwrap())
}

fn refused<T: std::fmt::Debug>(result: Result<T, String>, fragment: &str) {
    let error = result.unwrap_err();
    assert!(error.contains(fragment), "{error:?} lacks {fragment:?}");
}

fn tree(root: &Path) -> Vec<PathBuf> {
    fn walk(path: &Path, out: &mut Vec<PathBuf>) {
        for entry in std::fs::read_dir(path).unwrap() {
            let path = entry.unwrap().path();
            out.push(path.clone());
            if path.is_dir() && !support_is_link(&path) {
                walk(&path, out);
            }
        }
    }
    let mut out = Vec::new();
    walk(root, &mut out);
    out.sort();
    out
}

fn support_is_link(path: &Path) -> bool {
    std::fs::symlink_metadata(path).is_ok_and(|m| m.file_type().is_symlink())
}

#[test]
fn parser_defaults_to_the_repository_root() {
    let out_dir = tempfile::tempdir().unwrap();
    let base = out_dir
        .path()
        .join("example")
        .to_string_lossy()
        .into_owned();
    let out = cli(
        checkout(),
        &[
            "build-release-archives",
            "--commit",
            "HEAD",
            "--prefix",
            "example/",
            "--output-base",
            &base,
        ],
        "",
    );
    assert_eq!(out.code, 0, "{}", out.stderr);
    assert!(zip_files(&out_dir.path().join("example.zip")).contains_key("example/README.md"));
}

#[test]
fn builder_pins_git_conversion_and_timezone() {
    let temp = tempfile::tempdir().unwrap();
    type Call = (Vec<String>, Vec<(String, String)>);
    let mut calls: Vec<Call> = Vec::new();
    let outputs = build_release_archives_with(
        checkout(),
        "HEAD",
        "example-1.2.3/",
        &temp.path().join("dist").join("example-1.2.3"),
        ".",
        &mut |args, env| {
            calls.push((
                args.to_vec(),
                env.iter()
                    .map(|(k, v)| ((*k).into(), (*v).into()))
                    .collect(),
            ));
            Ok((
                true,
                if args[0] == "show" {
                    "1234567890\n".into()
                } else {
                    String::new()
                },
            ))
        },
    )
    .unwrap();
    // resolve_commit's own rev-parse is not one of the builder's calls.
    assert_eq!(calls.len(), 3);
    let head = release_policy::git_revision::resolve_commit(checkout(), "HEAD").unwrap();
    assert_eq!(calls[0].0, ["show", "-s", "--format=%ct", &head]);
    let names = outputs
        .iter()
        .map(|p| p.file_name().unwrap().to_string_lossy().into_owned())
        .collect::<Vec<_>>();
    assert_eq!(names, ["example-1.2.3.zip", "example-1.2.3.tar.gz"]);
    for (args, env) in &calls[1..] {
        assert_eq!(
            args[..5],
            ["-c", "core.autocrlf=false", "-c", "core.eol=lf", "archive"]
        );
        assert_eq!(env, &[("TZ".to_owned(), "UTC".to_owned())]);
        assert!(args.contains(&"--mtime=@1234567890".to_owned()));
    }
}

#[test]
fn builder_rejects_a_non_numeric_commit_timestamp() {
    let temp = tempfile::tempdir().unwrap();
    let result = build_release_archives_with(
        checkout(),
        "HEAD",
        "example-1.2.3/",
        &temp.path().join("dist").join("example-1.2.3"),
        ".",
        &mut |_, _| Ok((true, "not-a-timestamp\n".into())),
    );
    refused(result, "timestamp");
}

#[test]
fn repeated_archives_are_identical_and_formats_agree() {
    let temp = tempfile::tempdir().unwrap();
    // The builder sets TZ itself, so the caller's zone cannot reach the archive.
    let first = build_release_archives(
        checkout(),
        "HEAD",
        "release-test/",
        &temp.path().join("first").join("release-test"),
        ".",
    )
    .unwrap();
    let second = build_release_archives(
        checkout(),
        "HEAD",
        "release-test/",
        &temp.path().join("second").join("release-test"),
        ".",
    )
    .unwrap();
    assert_eq!(
        first.iter().map(|p| sha(p)).collect::<Vec<_>>(),
        second.iter().map(|p| sha(p)).collect::<Vec<_>>()
    );
    let zip = zip_files(&first[0]);
    assert_eq!(zip, tar_files(&first[1]));
    assert!(zip.keys().all(|name| name.starts_with("release-test/")));
}

#[test]
fn component_archive_contains_only_the_selected_tracked_tree() {
    let repo = Repo::new();
    repo.write("root.txt", "root\n");
    repo.write("packages/example-tool/VERSION", "1.2.3\n");
    repo.write("packages/example-tool/source.txt", "component\n");
    repo.commit("fixture");
    let outputs = build_release_archives(
        repo.root(),
        "HEAD",
        "example-tool-1.2.3/",
        &repo.path("dist/example-tool-1.2.3"),
        "packages/example-tool",
    )
    .unwrap();
    let expected = BTreeMap::from([
        ("example-tool-1.2.3/VERSION".to_owned(), b"1.2.3\n".to_vec()),
        (
            "example-tool-1.2.3/source.txt".to_owned(),
            b"component\n".to_vec(),
        ),
    ]);
    assert_eq!(zip_files(&outputs[0]), expected);
    assert_eq!(tar_files(&outputs[1]), expected);
}

#[test]
fn component_archive_rejects_unsafe_untracked_file_and_symlink_paths() {
    let repo = Repo::new();
    repo.write("packages/tracked/source.txt", "tracked\n");
    repo.write("tracked-file.txt", "file\n");
    repo.commit("fixture");
    repo.write("packages/untracked/source.txt", "untracked\n");
    let resolved = std::fs::canonicalize(repo.root())
        .unwrap()
        .to_string_lossy()
        .into_owned();
    for (index, source) in [
        "",
        &resolved,
        "../escape",
        "packages\\tracked",
        "packages/untracked",
        "tracked-file.txt",
    ]
    .iter()
    .enumerate()
    {
        let base = repo.path(&format!("dist/unsafe-{index}"));
        assert!(
            build_release_archives(repo.root(), "HEAD", "safe/", &base, source).is_err(),
            "{source:?}"
        );
    }
    if support::symlink(
        &repo.path("packages/tracked"),
        &repo.path("packages/linked"),
    ) {
        assert!(
            build_release_archives(
                repo.root(),
                "HEAD",
                "safe/",
                &repo.path("dist/linked"),
                "packages/linked"
            )
            .is_err()
        );
    }
}

#[test]
fn builder_refuses_unsafe_prefixes_and_existing_outputs() {
    let temp = tempfile::tempdir().unwrap();
    for prefix in ["absolute", "/absolute/", "../escape/", "safe/../escape/"] {
        assert!(
            build_release_archives(
                checkout(),
                "HEAD",
                prefix,
                &temp.path().join("archive"),
                "."
            )
            .is_err(),
            "{prefix}"
        );
    }
    let existing = temp.path().join("existing.zip");
    std::fs::write(&existing, "keep").unwrap();
    refused(
        build_release_archives(
            checkout(),
            "HEAD",
            "safe/",
            &temp.path().join("existing"),
            ".",
        ),
        "refusing to overwrite existing archive",
    );
    assert_eq!(std::fs::read(&existing).unwrap(), b"keep");
}

#[test]
fn builder_rejects_an_output_directory_symlinked_outside_the_checkout() {
    let consumer = tempfile::tempdir_in(checkout()).unwrap();
    let outside = tempfile::tempdir().unwrap();
    let link = consumer.path().join("dist");
    if !support::symlink(outside.path(), &link) {
        return; // Symbolic links need a privilege on Windows.
    }
    assert!(build_release_archives(checkout(), "HEAD", "safe/", &link.join("safe"), ".").is_err());
    assert!(std::fs::read_dir(outside.path()).unwrap().next().is_none());
}

#[test]
fn invalid_revisions_and_relative_traversal_have_no_write_side_effects() {
    let parent = tempfile::tempdir_in(checkout()).unwrap();
    let outside = parent.path().join("escaped");
    for revision in ["--help", "--format=%ct", "missing", "HEAD^{tree}"] {
        assert!(
            build_release_archives(checkout(), revision, "safe/", &outside.join("archive"), ".")
                .is_err(),
            "{revision}"
        );
        assert!(tree(parent.path()).is_empty());
    }
    let name = parent
        .path()
        .file_name()
        .unwrap()
        .to_string_lossy()
        .into_owned();
    let relative = PathBuf::from(&name)
        .join("..")
        .join(&name)
        .join("escaped/archive");
    assert!(build_release_archives(checkout(), "HEAD", "safe/", &relative, ".").is_err());
    assert!(tree(parent.path()).is_empty());
}

#[test]
fn intermediate_and_dangling_parent_links_create_nothing() {
    let temp = tempfile::tempdir().unwrap();
    let root = temp.path();
    let outside = root.join("outside");
    std::fs::create_dir(&outside).unwrap();
    for target in [outside.clone(), root.join("missing")] {
        let link = root.join("linked");
        if !support::symlink(&target, &link) {
            return; // Symbolic links need a privilege on Windows.
        }
        let before = tree(root);
        assert!(
            build_release_archives(
                checkout(),
                "HEAD",
                "safe/",
                &link.join("nested/archive"),
                "."
            )
            .is_err()
        );
        assert_eq!(tree(root), before);
        assert!(std::fs::read_dir(&outside).unwrap().next().is_none());
        assert!(!root.join("missing").exists());
        let _ = std::fs::remove_file(&link).or_else(|_| std::fs::remove_dir(&link));
    }
}

#[test]
fn existing_and_dangling_archive_links_are_preserved() {
    let temp = tempfile::tempdir().unwrap();
    let target = temp.path().join("target");
    for suffix in [".zip", ".tar.gz"] {
        let link = temp.path().join(format!("archive{suffix}"));
        #[cfg(unix)]
        let made = std::os::unix::fs::symlink(&target, &link).is_ok();
        #[cfg(windows)]
        let made = std::os::windows::fs::symlink_file(&target, &link).is_ok();
        if !made {
            return; // Symbolic links need a privilege on Windows.
        }
        refused(
            build_release_archives(
                checkout(),
                "HEAD",
                "safe/",
                &temp.path().join("archive"),
                ".",
            ),
            "refusing to overwrite",
        );
        assert!(support_is_link(&link));
        assert!(!target.exists());
        std::fs::remove_file(&link).unwrap();
    }
}

#[test]
fn preflight_rejects_resolved_parent_aliases_without_writing() {
    // A parent whose resolved form differs from its spelling is refused even
    // when no component reports itself as a link, as a Windows junction does not.
    let temp = tempfile::tempdir().unwrap();
    let root = release_policy::py::resolve(temp.path());
    let outside = root.join("outside");
    std::fs::create_dir(&outside).unwrap();
    let link = root.join("linked");
    #[cfg(windows)]
    let made = std::process::Command::new("cmd")
        .args(["/c", "mklink", "/J"])
        .arg(&link)
        .arg(&outside)
        .output()
        .is_ok_and(|o| o.status.success());
    #[cfg(unix)]
    let made = std::os::unix::fs::symlink(&outside, &link).is_ok();
    if !made {
        return;
    }
    let before = tree(&root);
    refused(
        preflight(&link.join("archive"), &root, false),
        "symbolic links",
    );
    assert_eq!(tree(&root), before);
    assert!(std::fs::read_dir(&outside).unwrap().next().is_none());
}

#[test]
fn preflight_refuses_a_relative_destination_outside_the_repository() {
    let temp = tempfile::tempdir().unwrap();
    let root = release_policy::py::resolve(temp.path());
    let repository = root.join("checkout");
    std::fs::create_dir(&repository).unwrap();
    let before = tree(&root);
    refused(
        preflight(&root.join("outside").join("archive"), &repository, true),
        "inside the repository",
    );
    assert_eq!(tree(&root), before);
}

#[test]
fn component_must_be_a_tree_at_the_selected_historical_commit() {
    let temp = tempfile::tempdir().unwrap();
    let root = release_policy::py::resolve(temp.path());
    let repo = Repo::new_in(&root);
    repo.write("component", "formerly a file\n");
    let old = repo.commit("blob");
    std::fs::remove_file(repo.path("component")).unwrap();
    repo.write("component/VERSION", "1.2.3\n");
    repo.commit("tree");
    let before = tree(&root);
    refused(
        build_release_archives(
            repo.root(),
            &old,
            "safe/",
            &root.join("outside").join("archive"),
            "component",
        ),
        "tree at the selected commit",
    );
    assert_eq!(tree(&root), before);
}

#[test]
fn failed_archive_generation_removes_only_partial_outputs() {
    let temp = tempfile::tempdir().unwrap();
    let keep = temp.path().join("keep");
    std::fs::write(&keep, "keep").unwrap();
    let mut real = archives::system_git(checkout());
    let result = build_release_archives_with(
        checkout(),
        "HEAD",
        "safe/",
        &temp.path().join("archive"),
        ".",
        &mut |args, env| {
            if args.iter().any(|a| a == "archive") {
                let output = args
                    .iter()
                    .find_map(|a| a.strip_prefix("--output="))
                    .unwrap();
                std::fs::write(output, "partial").unwrap();
                return Ok((false, String::new()));
            }
            real(args, env)
        },
    );
    assert!(result.is_err());
    assert_eq!(tree(temp.path()), std::slice::from_ref(&keep));
    assert_eq!(std::fs::read(&keep).unwrap(), b"keep");
}
