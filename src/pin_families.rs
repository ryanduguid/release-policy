//! Name the pin families a release-policy change moves, so consumers re-pin only those.
//!
//! Consumers pin a reusable workflow by commit. A family is that workflow and every file
//! it executes, found by following references from it: the workflows it calls, the
//! composite action it uses, and the scripts it names, directly or through the scripts it
//! sources and runs. A family whose files are identical at both commits keeps its pin, so an
//! attribution-only change moves the attribution pins and leaves every release caller alone,
//! and a change outside these families moves nothing. Comment lines are skipped, so a workflow
//! that only mentions another does not tie their pins together; a name in other prose, such
//! as a docstring, still counts, which errs towards one re-pin too many rather than one missed.

use crate::{Result, common::git, git_revision::resolve_commit, py};
use std::collections::{BTreeMap, BTreeSet};
use std::path::Path;

pub const FAMILIES: [(&str, &str); 5] = [
    ("release-python", ".github/workflows/release-python.yml"),
    ("release-archive", ".github/workflows/release-archive.yml"),
    ("release-skills", ".github/workflows/release-skills.yml"),
    ("verify-skills", ".github/workflows/verify-skills.yml"),
    ("attribution", ".github/workflows/attribution-policy.yml"),
];

pub const USAGE: &str = "Usage: python scripts/pin_families.py OLD_COMMIT NEW_COMMIT";

/// The git command runner, injected so tests can observe every call.
pub type Git<'a> = dyn FnMut(&[&str]) -> Result<String> + 'a;

/// The references one file's text makes, found with the script's patterns and
/// Python's `\w`, `\s` and `\b`: workflow paths, action paths, script names
/// and imported module names.
pub fn scan(text: &str) -> [Vec<String>; 4] {
    static PATTERNS: std::sync::LazyLock<[fancy_regex::Regex; 4]> =
        std::sync::LazyLock::new(|| {
            let w = &py::W[1..py::W.len() - 1];
            let s = &*py::S;
            let b = py::word_boundary();
            [
                format!(r"\.github/workflows/[{w}.-]+\.yml"),
                format!(r"\.github/actions/[{w}.-]+"),
                format!(r"{b}[{w}-]+\.(?:py|sh){b}"),
                format!(r"(?m)^{s}*(?:from|import){s}+([{w}]+){b}"),
            ]
            .map(|p| fancy_regex::Regex::new(&p).unwrap())
        });
    let found = |pattern: &fancy_regex::Regex, group: usize| {
        pattern
            .captures_iter(text)
            .filter_map(|c| c.ok().map(|c| c[group].to_owned()))
            .collect::<Vec<_>>()
    };
    [
        found(&PATTERNS[0], 0),
        found(&PATTERNS[1], 0),
        found(&PATTERNS[2], 0),
        found(&PATTERNS[3], 1),
    ]
}

fn inputs_at(entry: &str, commit: &str, git: &mut Git) -> Result<BTreeSet<String>> {
    let tree: BTreeSet<_> = py::splitlines(&git(&["ls-tree", "-r", "--name-only", commit])?)
        .into_iter()
        .map(str::to_owned)
        .collect();
    let scripts: BTreeMap<_, _> = tree
        .iter()
        .filter(|p| p.starts_with("scripts/"))
        .map(|p| (p.rsplit('/').next().unwrap(), p))
        .collect();
    let mut found = BTreeSet::new();
    let mut queue = vec![entry.to_owned()];
    while let Some(path) = queue.pop() {
        if found.contains(&path) || !tree.contains(&path) {
            continue;
        }
        found.insert(path.clone());
        let raw = git(&["show", &format!("{commit}:{path}")])?;
        let text = py::splitlines(&raw)
            .into_iter()
            .filter(|line| !line.trim_start_matches(py::is_space).starts_with('#'))
            .collect::<Vec<_>>()
            .join("\n");
        let [workflows, actions, names, imports] = scan(&text);
        queue.extend(workflows);
        for action in actions {
            queue.extend(
                tree.iter()
                    .filter(|p| p.starts_with(&format!("{action}/")))
                    .cloned(),
            );
        }
        for name in names {
            if let Some(path) = scripts.get(name.as_str()) {
                queue.push((*path).clone());
            }
        }
        if path.ends_with(".py") {
            for module in imports {
                if let Some(path) = scripts.get(format!("{module}.py").as_str()) {
                    queue.push((*path).clone());
                }
            }
        }
    }
    Ok(found)
}

/// Every file the family rooted at `entry` executes at `commit`.
pub fn inputs(entry: &str, commit: &str, root: &Path) -> Result<BTreeSet<String>> {
    inputs_at(entry, &resolve_commit(root, commit)?, &mut |args| {
        git(root, args)
    })
}

/// For each family, the files it executes that differ between the 2 commits.
pub fn changed(old: &str, new: &str, root: &Path) -> Result<Vec<(String, Vec<String>)>> {
    changed_with(old, new, root, &mut |args| git(root, args))
}

/// `changed`, with the git runner supplied.
pub fn changed_with(
    old: &str,
    new: &str,
    root: &Path,
    git: &mut Git,
) -> Result<Vec<(String, Vec<String>)>> {
    let old = resolve_commit(root, old)?;
    let new = resolve_commit(root, new)?;
    let mut result = Vec::new();
    for (family, entry) in FAMILIES {
        let mut paths = inputs_at(entry, &old, git)?;
        paths.extend(inputs_at(entry, &new, git)?);
        let mut args = vec!["diff", "--name-only", &old, &new, "--"];
        args.extend(paths.iter().map(String::as_str));
        // With no paths, git diff would compare the whole tree: a family absent
        // from both moves nothing.
        let changed = if paths.is_empty() {
            Vec::new()
        } else {
            py::split_whitespace(&git(&args)?)
                .into_iter()
                .map(str::to_owned)
                .collect()
        };
        result.push((family.to_owned(), changed));
    }
    Ok(result)
}

/// One verdict line per family.
pub fn report(families: &[(String, Vec<String>)]) -> String {
    families
        .iter()
        .map(|(family, paths)| {
            if paths.is_empty() {
                format!("{family}: keep the pin\n")
            } else {
                format!("{family}: re-pin ({})\n", paths.join(", "))
            }
        })
        .collect()
}
