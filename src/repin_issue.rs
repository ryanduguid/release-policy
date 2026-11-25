// Copyright 2026 Ryan Duguid
// SPDX-License-Identifier: Apache-2.0

//! File the attribution repin patch as an issue when the action pin is stale.

use crate::pyjson::{self, Dump, Json};
use crate::{Result, common::*, py};
use regex::{Regex, bytes::Regex as BytesRegex};
use std::path::Path;
use std::process::{Command, Stdio};
use std::sync::LazyLock;

pub const WORKFLOW: &str = ".github/workflows/attribution-policy.yml";
pub const ACTION: &str = ".github/actions/no-ai-attribution/action.yml";
pub const ACTION_DIRECTORY: &str = ".github/actions/no-ai-attribution";
pub const BASELINE: &str = "tests/test_repository_baseline.sh";
pub const TITLE: &str = "Repin the attribution action";
pub const BOT: &str = "github-actions[bot]";
pub const MAX_ISSUE_PAGES: usize = 10;

/// A git command runner: `(exit_code, stdout, stderr)`.
pub type Git<'a> = dyn FnMut(&[&str]) -> Result<(i32, String, String)> + 'a;
/// A gh command runner: `(arguments, optional_stdin) -> stdout`.
pub type Gh<'a> = dyn FnMut(&[&str], Option<&str>) -> Result<String> + 'a;

static PIN: LazyLock<Regex> = LazyLock::new(|| {
    Regex::new(
        r"(?m)^(?P<head>[ \t]*(?:- )?uses: ryanduguid/release-policy/\.github/actions/no-ai-attribution@)(?P<sha>[0-9a-f]{40})(?P<tail>[ \t]*)$",
    )
    .unwrap()
});

static DIGEST_LINE: LazyLock<Regex> = LazyLock::new(|| {
    Regex::new(
        r#"(?m)^(?P<head>[ \t]*"\.github/workflows/attribution-policy\.yml": ")(?P<digest>[0-9a-f]{64})(?P<tail>",)$"#,
    )
    .unwrap()
});

// Matches a third-party `uses:` pin line; `ryanduguid/` owners are excluded in code
// so the check runs directly on arbitrary byte slices without lookaround.
static THIRD_PARTY_PIN_LINE: LazyLock<BytesRegex> = LazyLock::new(|| {
    BytesRegex::new(
        r"(?-u)^(?P<head>[ \t]*(?:- )?uses: (?P<owner>[A-Za-z0-9][A-Za-z0-9_.-]*)/[A-Za-z0-9_./-]+)@[0-9a-f]{40} # (?P<major>v[0-9]+)(?:\.[0-9]+){0,2}$",
    )
    .unwrap()
});

fn lf(text: &str) -> String {
    text.replace("\r\n", "\n")
}

fn replace_crlf_bytes(raw: &[u8]) -> Vec<u8> {
    let mut out = Vec::with_capacity(raw.len());
    let mut i = 0;
    while i < raw.len() {
        if i + 1 < raw.len() && raw[i] == b'\r' && raw[i + 1] == b'\n' {
            out.push(b'\n');
            i += 2;
        } else {
            out.push(raw[i]);
            i += 1;
        }
    }
    out
}

/// Normalise CRLF and third-party action pins before hashing, matching `tests/test_repository_baseline.sh`.
pub fn canonical_digest(document: &[u8]) -> String {
    let canonical = replace_crlf_bytes(document);
    let mut normalised = Vec::with_capacity(canonical.len());
    for (index, line) in canonical.split(|&b| b == b'\n').enumerate() {
        if index > 0 {
            normalised.push(b'\n');
        }
        if let Some(caps) = THIRD_PARTY_PIN_LINE.captures(line)
            && &caps["owner"] != b"ryanduguid"
        {
            normalised.extend_from_slice(&caps["head"]);
            normalised.push(b'@');
            normalised.extend_from_slice(&[b'0'; 40]);
            normalised.extend_from_slice(b" # ");
            normalised.extend_from_slice(&caps["major"]);
            continue;
        }
        normalised.extend_from_slice(line);
    }
    sha256(&normalised)
}

pub fn system_git(root: &Path) -> impl FnMut(&[&str]) -> Result<(i32, String, String)> + '_ {
    move |arguments| {
        let out = Command::new("git")
            .arg("-C")
            .arg(root)
            .args(arguments)
            .output()
            .map_err(|e| py::oserror_str(&e, "git", py::OsStyle::Native))?;
        let stdout =
            py::universal_newlines(&py::decode_utf8(&out.stdout).map_err(|e| e.to_string())?);
        let stderr =
            py::universal_newlines(&py::decode_utf8(&out.stderr).map_err(|e| e.to_string())?);
        Ok((out.status.code().unwrap_or(1), stdout, stderr))
    }
}

pub fn system_gh(arguments: &[&str], stdin: Option<&str>) -> Result<String> {
    let mut command = Command::new("gh");
    command
        .args(arguments)
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
        .stdin(if stdin.is_some() {
            Stdio::piped()
        } else {
            Stdio::null()
        });
    let mut child = command
        .spawn()
        .map_err(|e| py::oserror_str(&e, "gh", py::OsStyle::Native))?;
    if let Some(input) = stdin
        && let Some(mut handle) = child.stdin.take()
    {
        use std::io::Write;
        handle
            .write_all(input.as_bytes())
            .map_err(|e| e.to_string())?;
    }
    let out = child.wait_with_output().map_err(|e| e.to_string())?;
    let stdout = py::universal_newlines(&py::decode_utf8(&out.stdout).map_err(|e| e.to_string())?);
    let stderr = py::universal_newlines(&py::decode_utf8(&out.stderr).map_err(|e| e.to_string())?);
    if !out.status.success() {
        let trimmed = py::strip(&stderr);
        if trimmed.is_empty() {
            return Err(format!("gh {} failed", arguments.join(" ")));
        }
        return Err(trimmed.to_owned());
    }
    Ok(stdout)
}

fn single_capture<'t>(pattern: &Regex, text: &'t str, label: &str) -> Result<regex::Captures<'t>> {
    let mut matches: Vec<_> = pattern.captures_iter(text).collect();
    if matches.len() != 1 {
        return Err(format!(
            "expected exactly one {label}, found {}",
            matches.len()
        ));
    }
    Ok(matches.pop().unwrap())
}

fn replace_group(caps: &regex::Captures<'_>, text: &str, group: &str, value: &str) -> String {
    let span = caps.name(group).expect("named capture group");
    format!("{}{value}{}", &text[..span.start()], &text[span.end()..])
}

fn tree(git: &mut Git<'_>, revision: &str, path: &str) -> Result<Option<String>> {
    let spec = format!("{revision}:{path}");
    let (code, stdout, _) = git(&["rev-parse", "--verify", "--quiet", &spec])?;
    Ok((code == 0).then(|| py::strip(&stdout).to_owned()))
}

fn ancestor(git: &mut Git<'_>, older: &str, newer: &str) -> Result<bool> {
    Ok(git(&["merge-base", "--is-ancestor", older, newer])?.0 == 0)
}

fn same(git: &mut Git<'_>, first: &str, second: &str, path: &str) -> Result<bool> {
    let Some(first_tree) = tree(git, first, path)? else {
        return Ok(false);
    };
    Ok(Some(first_tree) == tree(git, second, path)?)
}

pub fn pin_is_current_with(pin: &str, target: &str, git: &mut Git<'_>) -> Result<bool> {
    Ok(same(git, pin, target, ACTION_DIRECTORY)? && ancestor(git, pin, target)?)
}

pub fn pin_is_current(root: &Path, pin: &str, target: &str) -> Result<bool> {
    pin_is_current_with(pin, target, &mut system_git(root))
}

pub fn unchanged_since_with(target: &str, current: &str, git: &mut Git<'_>) -> Result<bool> {
    Ok(ancestor(git, target, current)?
        && same(git, target, current, ACTION_DIRECTORY)?
        && same(git, target, current, WORKFLOW)?
        && same(git, target, current, BASELINE)?)
}

pub fn unchanged_since(root: &Path, target: &str, current: &str) -> Result<bool> {
    unchanged_since_with(target, current, &mut system_git(root))
}

pub fn prepare_with(
    root: &Path,
    target: &str,
    patch: &Path,
    git: &mut Git<'_>,
) -> Result<Option<String>> {
    if !full("[0-9a-f]{40}", target) {
        return Err(format!("target is not a full commit SHA: {target}"));
    }
    let (_, head_out, _) = git(&["rev-parse", "HEAD"])?;
    if py::strip(&head_out) != target {
        return Err(format!("target {target} is not the checked-out commit"));
    }
    let workflow_path = root.join(WORKFLOW);
    let workflow_bytes = read_bytes(&workflow_path)?;
    let workflow = lf(&py::decode_utf8(&workflow_bytes).map_err(|e| e.to_string())?);
    let pin = single_capture(&PIN, &workflow, "live full-SHA action pin")?;
    let old_sha = pin.name("sha").unwrap().as_str().to_owned();
    if pin_is_current_with(&old_sha, target, git)? {
        return Ok(None);
    }
    let repinned = replace_group(&pin, &workflow, "sha", target);
    let baseline_path = root.join(BASELINE);
    let baseline_bytes = read_bytes(&baseline_path)?;
    let baseline = lf(&py::decode_utf8(&baseline_bytes).map_err(|e| e.to_string())?);
    let digest = single_capture(&DIGEST_LINE, &baseline, &format!("{WORKFLOW} digest"))?;
    let new_digest = canonical_digest(repinned.as_bytes());
    let rebaselined = replace_group(&digest, &baseline, "digest", &new_digest);
    write_bytes(&workflow_path, repinned.as_bytes())?;
    write_bytes(&baseline_path, rebaselined.as_bytes())?;
    let (diff_code, diff_stdout, diff_stderr) = git(&[
        "diff",
        "--no-color",
        "--no-ext-diff",
        "--",
        WORKFLOW,
        BASELINE,
    ])?;
    if diff_code != 0 {
        return Err(format!("git diff failed: {}", py::strip(&diff_stderr)));
    }
    write_bytes(patch, diff_stdout.as_bytes())?;
    Ok(Some(old_sha))
}

pub fn prepare(root: &Path, target: &str, patch: &Path) -> Result<Option<String>> {
    prepare_with(root, target, patch, &mut system_git(root))
}

pub fn issue_body(target: &str, patch_text: &str) -> Result<String> {
    if patch_text.contains("````") {
        return Err("the patch contains a four-backtick fence".into());
    }
    Ok(format!(
        "`{ACTION_DIRECTORY}` changed, and `{WORKFLOW}` no longer pins \
         a commit carrying it. Consumers run the pinned action, and push CI on main fails \
         `tests/test_attribution_pin.py` until the workflow is repinned to `{target}`.\n\n\
         The job token cannot push a workflow change, so the repin workflow files the patch \
         here. From a checkout of main that contains that commit:\n\n\
         1. Save the patch below as `repin.patch` and run `git apply repin.patch`.\n\
         2. Run `bash tests/test_repository_baseline.sh` and \
         `python -m unittest discover -s tests -p test_attribution_pin.py`; the workflow \
         ran both on the patched tree before filing this issue.\n\
         3. Open a pull request that closes this issue, then re-pin consumers as \
         `docs/re-pinning.md` describes.\n\n\
         ````diff\n{patch_text}````\n"
    ))
}

pub fn open_issues(repository: &str, gh: &mut Gh<'_>) -> Result<Vec<Json>> {
    let mut found = Vec::new();
    for page in 1..=MAX_ISSUE_PAGES {
        let endpoint = format!("repos/{repository}/issues?state=open&per_page=100&page={page}");
        let raw = gh(&["api", &endpoint], None)?;
        let parsed = pyjson::loads(&raw).map_err(|e| e.0)?;
        let Some(items) = parsed.as_array() else {
            return Err("GitHub returned a non-list issue page".into());
        };
        for item in items {
            let Json::Object(_) = item else {
                return Err("GitHub returned a non-object issue".into());
            };
            if item.get("title").and_then(Json::as_str) == Some(TITLE)
                && item.get("pull_request").is_none()
            {
                found.push(item.clone());
            }
        }
        if items.len() < 100 {
            return Ok(found);
        }
    }
    Err(format!(
        "no decision after {MAX_ISSUE_PAGES} pages of open issues"
    ))
}

fn opened_by_workflow(issue: &Json) -> bool {
    matches!(
        issue.get("user"),
        Some(user @ Json::Object(_)) if user.get("login").and_then(Json::as_str) == Some(BOT)
    )
}

fn issue_number(issue: &Json) -> Result<String> {
    issue
        .get("number")
        .map(Json::py_str)
        .ok_or_else(|| "GitHub issue is missing a number".to_owned())
}

pub fn publish(
    repository: &str,
    target: &str,
    patch_text: &str,
    gh: &mut Gh<'_>,
) -> Result<String> {
    if !full(REPOSITORY, repository) {
        return Err(format!("not an owner/name repository: {repository}"));
    }
    let body = issue_body(target, patch_text)?;
    let issues = open_issues(repository, gh)?;
    let ours: Vec<&Json> = issues
        .iter()
        .filter(|issue| opened_by_workflow(issue))
        .collect();
    if ours.is_empty() && !issues.is_empty() {
        return Ok(format!(
            "left #{}: a person opened it",
            issue_number(&issues[0])?
        ));
    }
    if ours.is_empty() {
        let payload = Json::Object(vec![
            ("title".into(), Json::str_obj(TITLE)),
            ("body".into(), Json::str_obj(&body)),
        ]);
        let endpoint = format!("repos/{repository}/issues");
        let created_raw = gh(
            &["api", "--method", "POST", &endpoint, "--input", "-"],
            Some(&pyjson::dumps(&payload, Dump::default())),
        )?;
        let created = pyjson::loads(&created_raw).map_err(|e| e.0)?;
        return Ok(format!("opened #{}", issue_number(&created)?));
    }
    let existing = ours[0];
    let number = issue_number(existing)?;
    if existing.get("body").and_then(Json::as_str) == Some(body.as_str()) {
        return Ok(format!("#{number} already carries this patch"));
    }
    let payload = Json::Object(vec![("body".into(), Json::str_obj(&body))]);
    let endpoint = format!("repos/{repository}/issues/{number}");
    gh(
        &["api", "--method", "PATCH", &endpoint, "--input", "-"],
        Some(&pyjson::dumps(&payload, Dump::default())),
    )?;
    Ok(format!("updated #{number}"))
}
