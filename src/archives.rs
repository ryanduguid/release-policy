//! Build deterministic source-release archives from tracked Git content.

use crate::{Result, common::*, git_revision::resolve_commit, py, release::validate_source_syntax};
use std::path::{Path, PathBuf};
use std::process::Command;

/// One git call the builder makes: its arguments and extra environment.
/// The answer is the exit status and the text it printed.
pub type Git<'a> = dyn FnMut(&[String], &[(&str, &str)]) -> GitAnswer + 'a;

/// A git call's exit status and the text it printed.
pub type GitAnswer = Result<(bool, String)>;

/// Run git in `root` with output captured, as the script's `subprocess.run` did.
pub fn system_git(root: &Path) -> impl FnMut(&[String], &[(&str, &str)]) -> GitAnswer + '_ {
    move |args, env| {
        // The script let `git archive` write to the terminal and captured the rest.
        let archive = args.iter().any(|a| a == "archive");
        let out = Command::new("git")
            .args(args)
            .current_dir(root)
            .envs(env.iter().copied())
            .stderr(if archive {
                std::process::Stdio::inherit()
            } else {
                std::process::Stdio::piped()
            })
            .output()
            .map_err(|e| py::oserror_str(&e, "git", py::OsStyle::Native))?;
        let text = py::decode_utf8(&out.stdout).map_err(|e| e.to_string())?;
        Ok((out.status.success(), py::universal_newlines(&text)))
    }
}

/// Refuse an output that would escape the checkout or replace an archive.
///
/// The caller owns the filesystem. These checks prevent validation side effects;
/// they do not provide atomic protection against concurrent directory replacement.
pub fn preflight(base: &Path, root: &Path, relative: bool) -> Result<Vec<PathBuf>> {
    if base
        .components()
        .any(|c| c == std::path::Component::ParentDir)
    {
        return Err("output directory must not contain parent traversal".into());
    }
    let parent = base.parent().unwrap();
    if parent.ancestors().any(symlink) || absolute(parent)? != parent {
        return Err("output directory must not traverse symbolic links".into());
    }
    let resolved = absolute(parent)?;
    if relative && resolved != root && !resolved.starts_with(root) {
        return Err("relative output directory must remain inside the repository".into());
    }
    let outputs = [".zip", ".tar.gz"]
        .iter()
        .map(|s| PathBuf::from(format!("{}{s}", base.display())))
        .collect::<Vec<_>>();
    for output in &outputs {
        if symlink(output) || output.exists() {
            return Err(format!(
                "refusing to overwrite existing archive: {}",
                output.display()
            ));
        }
    }
    Ok(outputs)
}

fn owned(args: &[&str]) -> Vec<String> {
    args.iter().map(|a| (*a).to_owned()).collect()
}

/// Require `source` to be a tree at `commit`, reached only through trees. The
/// archive is read from the commit, so the checkout, which may have moved or
/// removed the component since, is not consulted.
fn source_tree(commit: &str, source: &str, git: &mut Git) -> Result<()> {
    let parts = source.split('/').collect::<Vec<_>>();
    for depth in 1..=parts.len() {
        let component = parts[..depth].join("/");
        let args = owned(&["ls-tree", "--full-tree", "-z", commit, "--", &component]);
        let (ok, listed) = git(&args, &[])?;
        if !ok {
            return Err(format!(
                "unexpected error: Command 'git ls-tree --full-tree -z {commit} -- {component}' returned non-zero exit status 1."
            ));
        }
        let Some((meta, _)) = listed
            .split('\0')
            .filter_map(|record| record.split_once('\t'))
            .find(|(_, path)| *path == component)
        else {
            return Err(format!(
                "source-directory is missing at the selected commit: {source}"
            ));
        };
        let mut fields = meta.split(' ');
        if matches!(fields.next(), Some("120000" | "160000")) {
            return Err(format!(
                "source-directory must not traverse a symbolic link or gitlink: {source}"
            ));
        }
        if fields.next() != Some("tree") {
            return Err("source-directory must be a tree at the selected commit".into());
        }
    }
    let prefix = format!("{source}/");
    let args = owned(&[
        "ls-tree",
        "-r",
        "-z",
        "--name-only",
        "--full-tree",
        commit,
        "--",
        &prefix,
    ]);
    let (ok, tracked) = git(&args, &[])?;
    if !ok {
        return Err(format!(
            "unexpected error: Command 'git ls-tree -r -z --name-only --full-tree {commit} -- {prefix}' returned non-zero exit status 1."
        ));
    }
    if tracked.is_empty() {
        return Err(format!(
            "source-directory must contain tracked Git files: {source}"
        ));
    }
    Ok(())
}

/// Build ZIP and tar.gz archives with stable text and time metadata.
pub fn build_release_archives(
    root: &Path,
    revision: &str,
    prefix: &str,
    supplied_base: &Path,
    source: &str,
) -> Result<Vec<PathBuf>> {
    let root = absolute(root)?;
    build_release_archives_with(
        &root,
        revision,
        prefix,
        supplied_base,
        source,
        &mut system_git(&root),
    )
}

/// `build_release_archives`, with the archive-specific git calls supplied.
pub fn build_release_archives_with(
    root: &Path,
    revision: &str,
    prefix: &str,
    supplied_base: &Path,
    source: &str,
    git: &mut Git,
) -> Result<Vec<PathBuf>> {
    if !full(r"[A-Za-z0-9._-]+(?:/[A-Za-z0-9._-]+)*/", prefix)
        || prefix[..prefix.len().saturating_sub(1)]
            .split('/')
            .any(|p| matches!(p, "" | "." | ".."))
    {
        return Err("prefix must be a safe relative POSIX path ending in '/'".into());
    }
    let root = absolute(root)?;
    let commit = resolve_commit(&root, revision)?;
    let source = validate_source_syntax(source)?;
    if source != "." {
        source_tree(&commit, source, git)?;
    }
    let treeish = if source == "." {
        commit.clone()
    } else {
        format!("{commit}:{source}")
    };
    // Do not resolve the supplied output: traversal and symlink checks must see it.
    // Path() normalises separators and '.' parts, and the messages show that form.
    let supplied = py::PyPath::new(&supplied_base.to_string_lossy());
    let base_path = if supplied.is_absolute() {
        supplied.clone()
    } else {
        py::PyPath::new(&root.to_string_lossy()).join(&supplied_base.to_string_lossy())
    };
    let base = base_path.fs();
    let outputs = preflight(&base, &root, !supplied.is_absolute())?;
    let utc = [("TZ", "UTC")];
    let (ok, timestamp) = git(&owned(&["show", "-s", "--format=%ct", &commit]), &utc)?;
    if !ok {
        return Err("unexpected error: Command 'git show' returned non-zero exit status 1.".into());
    }
    let timestamp = py::strip(&timestamp).to_owned();
    if !full("[0-9]+", &timestamp) {
        return Err("commit timestamp must be a non-negative integer".into());
    }
    py::mkdir_parents(&base_path.parent())?;
    preflight(&base, &root, !supplied.is_absolute())?;
    let result = (|| {
        for (format, output) in ["zip", "tar.gz"].into_iter().zip(&outputs) {
            let args = owned(&["-c", "core.autocrlf=false", "-c", "core.eol=lf", "archive"])
                .into_iter()
                .chain([
                    format!("--format={format}"),
                    format!("--prefix={prefix}"),
                    format!("--mtime=@{timestamp}"),
                    format!("--output={}", absolute(output)?.display()),
                    treeish.clone(),
                ])
                .collect::<Vec<_>>();
            let (ok, _) = git(&args, &utc)?;
            if !ok {
                return Err(format!(
                    "unexpected error: git archive --format={format} returned non-zero exit status 1."
                ));
            }
        }
        Ok(())
    })();
    if result.is_err() {
        for output in &outputs {
            let _ = std::fs::remove_file(output);
        }
    }
    result?;
    Ok(outputs)
}
