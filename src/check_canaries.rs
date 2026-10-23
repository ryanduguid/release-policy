//! Validate release-family canary pins and live successful-run evidence.

use crate::common::{REPOSITORY, SHA, full, repr_list, sha256};
use crate::py;
use crate::pyjson::{self, Dump, Json};
use chrono::{DateTime, NaiveDate, TimeZone, Utc};
use std::path::Path;
use std::sync::LazyLock;

const FAMILIES: [&str; 4] = ["archive", "python", "skills", "verify"];
// A release family publishes on a tag, so a success newer than the recorded
// evidence means that evidence has been overtaken and a person should look.
// The verification consumer runs on every push to its default branch, where a
// newer success is the ordinary state: requiring identity there made the
// manifest stale within a day of every consumer push and proved nothing the
// current-pin check does not prove directly.
const RELEASE_FAMILIES: [&str; 3] = ["archive", "python", "skills"];
// Successful runs are read page by page until the relevant namespaced run
// appears, because a repository with busy sibling namespaces can hide the
// recorded run behind newer successes from other namespaces. Ten pages of a
// hundred successes is far beyond anything the estate produces; a search that
// exhausts them is reported as its own condition rather than as a missing run.
pub const MAX_RUN_PAGES: u32 = 10;
// GitHub's compare API reports the pin relative to release-policy main. Only
// these 2 statuses mean the pin is an ancestor of main. GitHub refuses a
// reusable-workflow call at a commit no branch or tag reaches, before any job
// starts, so a pin that matches the consumer's workflow byte for byte can still
// be dead after a history rewrite of this repository.
const REACHABLE_STATUSES: [&str; 2] = ["identical", "behind"];
// From 1 October 2026 GitHub deletes workflow runs older than the repository's
// retention period, at most 90 days for a public repository. A recorded run
// that returns 404 only after that, plus a day's margin for long runs, has
// expired; an earlier 404 is a missing run and stays an error.
const RETENTION_DAYS: i64 = 90;
const EXPIRY_MARGIN_DAYS: i64 = 1;
// The release-policy workflow whose certificate signs each release family's
// provenance, read from verified attestations of the 4 October 2026 releases.
fn signer_workflow(family: &str) -> &'static str {
    match family {
        "python" => ".github/workflows/release-python.yml",
        _ => ".github/workflows/publish-archives.yml",
    }
}
pub const SLSA_PROVENANCE: &str = "https://slsa.dev/provenance/v1";

static WORKFLOW: LazyLock<regex::Regex> =
    LazyLock::new(|| regex::Regex::new(r"\A\.github/workflows/[A-Za-z0-9_.-]+\.ya?ml\z").unwrap());
static RELEASE_TAG: LazyLock<regex::Regex> = LazyLock::new(|| {
    regex::Regex::new(r"\Av(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\z").unwrap()
});
static TAG_PREFIX: LazyLock<regex::Regex> =
    LazyLock::new(|| regex::Regex::new(r"\A[a-z0-9]+(?:-[a-z0-9]+)*\z").unwrap());
static STARTED_AT: LazyLock<regex::Regex> = LazyLock::new(|| {
    let d = &*py::D;
    regex::Regex::new(&format!(
        r"\A{d}{{4}}-{d}{{2}}-{d}{{2}}T{d}{{2}}:{d}{{2}}:{d}{{2}}Z\z"
    ))
    .unwrap()
});
static STATUS_LINE: LazyLock<regex::Regex> =
    LazyLock::new(|| regex::Regex::new(r"\AHTTP/[0-9.]+ ([0-9]{3})").unwrap());

/// A failed GitHub read, with the HTTP status when GitHub returned one.
#[derive(Clone, Debug, PartialEq)]
pub struct GitHubError {
    pub message: String,
    pub status: Option<u16>,
}

impl GitHubError {
    pub fn new(message: impl Into<String>) -> Self {
        GitHubError {
            message: message.into(),
            status: None,
        }
    }
}

/// Why a step stopped: a handled failure, or an exception Python let escape.
#[derive(Clone, Debug, PartialEq)]
pub enum Failure {
    Runtime(String),
    Unexpected(String),
}

/// A failed live read: a GitHub error the audit reports, or an exception
/// Python let escape.
#[derive(Clone, Debug, PartialEq)]
pub enum LiveError {
    GitHub(GitHubError),
    Unexpected(String),
}

impl From<GitHubError> for LiveError {
    fn from(error: GitHubError) -> Self {
        LiveError::GitHub(error)
    }
}

impl From<LiveError> for Failure {
    fn from(error: LiveError) -> Self {
        match error {
            LiveError::GitHub(error) => Failure::Runtime(error.message),
            LiveError::Unexpected(message) => Failure::Unexpected(message),
        }
    }
}

/// Split a read into the reported outcome and an escaping exception.
fn handled<T>(result: Result<T, LiveError>) -> Result<Result<T, GitHubError>, String> {
    match result {
        Ok(value) => Ok(Ok(value)),
        Err(LiveError::GitHub(error)) => Ok(Err(error)),
        Err(LiveError::Unexpected(message)) => Err(message),
    }
}

#[derive(Clone, Debug)]
pub struct Evidence {
    /// A JSON integer or boolean: Python's `isinstance(value, int)` admits both.
    pub run_id: Json,
    pub head_sha: String,
    pub reference: String,
    pub policy_sha: String,
    pub started_at: String,
}

#[derive(Clone, Debug)]
pub struct Canary {
    pub family: String,
    pub repository: String,
    pub workflow_path: String,
    pub policy_workflow: String,
    pub current_policy_sha: String,
    pub evidence: Evidence,
    pub tag_prefix: String,
}

#[derive(Clone, Debug, Default, PartialEq)]
pub struct CheckResult {
    pub errors: Vec<String>,
    pub warnings: Vec<String>,
    // Verified conditions a reviewer should know about; never fail the audit.
    pub notices: Vec<String>,
}

/// The integer value of a JSON int or bool.
fn int_value(value: &Json) -> Option<num_bigint::BigInt> {
    match value {
        Json::Int(i) => Some(i.clone()),
        Json::Bool(b) => Some(u8::from(*b).into()),
        _ => None,
    }
}

fn repr(value: Option<&Json>) -> String {
    value.map_or("None".into(), Json::repr)
}

fn eq(value: Option<&Json>, expected: &Json) -> bool {
    value.is_some_and(|v| v.py_eq(expected))
}

fn mapping<'a>(
    value: &'a Json,
    label: &str,
    keys: &[&str],
    optional: &[&str],
) -> Result<&'a Json, String> {
    let exact = match value {
        Json::Object(pairs) => {
            let required = pairs
                .iter()
                .filter(|(k, _)| !optional.contains(&k.as_str()))
                .collect::<Vec<_>>();
            required.len() == keys.len() && required.iter().all(|(k, _)| keys.contains(&k.as_str()))
        }
        _ => false,
    };
    if !exact {
        let mut keys = keys.to_vec();
        keys.sort_unstable();
        let mut optional = optional.to_vec();
        optional.sort_unstable();
        return Err(format!(
            "{label} must contain exactly {} and only optional keys {}",
            repr_list(keys),
            repr_list(optional)
        ));
    }
    Ok(value)
}

fn field<'a>(value: &'a Json, key: &str) -> &'a Json {
    value.get(key).expect("a key the shape check required")
}

fn text<'a>(value: &'a Json, label: &str) -> Result<&'a str, String> {
    match value {
        Json::Str(s) if !s.is_empty() => Ok(s),
        _ => Err(format!("{label} must be a non-empty string")),
    }
}

fn sha<'a>(value: &'a Json, label: &str) -> Result<&'a str, String> {
    let result = text(value, label)?;
    if !full(SHA, result) {
        return Err(format!("{label} must be a full lower-case SHA"));
    }
    Ok(result)
}

fn matches_release_tag(reference: Option<&Json>, tag_prefix: &str) -> bool {
    let Some(mut reference) = reference.and_then(Json::as_str) else {
        return false;
    };
    if !tag_prefix.is_empty() {
        match reference.strip_prefix(&format!("{tag_prefix}/")) {
            Some(rest) => reference = rest,
            None => return false,
        }
    }
    RELEASE_TAG.is_match(reference)
}

pub fn parse_manifest(document: &Json) -> Result<Vec<Canary>, String> {
    let root = mapping(document, "manifest", &["families", "schema"], &[])?;
    if !field(root, "schema").py_eq(&Json::Int(1.into())) {
        return Err("manifest schema must be 1".into());
    }
    let Json::Array(entries) = field(root, "families") else {
        return Err("manifest families must be a list".into());
    };
    let mut canaries = Vec::new();
    let mut seen: Vec<&str> = Vec::new();
    for (index, raw_entry) in entries.iter().enumerate() {
        let entry = mapping(
            raw_entry,
            &format!("family[{index}]"),
            &[
                "current_policy_sha",
                "evidence",
                "family",
                "policy_workflow",
                "repository",
                "workflow_path",
            ],
            &["tag_prefix"],
        )?;
        let family = text(field(entry, "family"), &format!("family[{index}].family"))?;
        if !FAMILIES.contains(&family) {
            return Err(format!("unsupported canary family: {family}"));
        }
        if seen.contains(&family) {
            return Err(format!("duplicate canary family: {family}"));
        }
        seen.push(family);

        let empty = Json::Str(String::new());
        let tag_prefix = match entry.get("tag_prefix").unwrap_or(&empty) {
            Json::Str(prefix)
                if prefix.is_empty()
                    || (matches!(family, "archive" | "python") && TAG_PREFIX.is_match(prefix)) =>
            {
                prefix.clone()
            }
            _ => {
                return Err(format!(
                    "{family}.tag_prefix must be empty or a lower-case hyphenated archive/Python namespace"
                ));
            }
        };

        let repository = text(field(entry, "repository"), &format!("{family}.repository"))?;
        if !full(REPOSITORY, repository) {
            return Err(format!("{family}.repository must be owner/name"));
        }
        let workflow_path = text(
            field(entry, "workflow_path"),
            &format!("{family}.workflow_path"),
        )?;
        let policy_workflow = text(
            field(entry, "policy_workflow"),
            &format!("{family}.policy_workflow"),
        )?;
        if !WORKFLOW.is_match(workflow_path) {
            return Err(format!("{family}.workflow_path is not a workflow path"));
        }
        if !WORKFLOW.is_match(policy_workflow) {
            return Err(format!("{family}.policy_workflow is not a workflow path"));
        }

        let evidence = mapping(
            field(entry, "evidence"),
            &format!("{family}.evidence"),
            &["head_sha", "policy_sha", "ref", "run_id", "started_at"],
            &[],
        )?;
        let run_id = field(evidence, "run_id");
        if int_value(run_id).is_none_or(|id| id < 1.into()) {
            return Err(format!("{family}.evidence.run_id must be positive"));
        }
        let reference = text(field(evidence, "ref"), &format!("{family}.evidence.ref"))?;
        if family == "verify" {
            if reference != "main" {
                return Err("verify evidence must use main".into());
            }
        } else if !matches_release_tag(Some(field(evidence, "ref")), &tag_prefix) {
            return Err(format!(
                "{family} release evidence must use a canonical version tag"
            ));
        }
        let started_at = text(
            field(evidence, "started_at"),
            &format!("{family}.evidence.started_at"),
        )?;
        if !STARTED_AT.is_match(started_at) {
            return Err(format!(
                "{family}.evidence.started_at must be a UTC timestamp"
            ));
        }
        let current_policy_sha = sha(
            field(entry, "current_policy_sha"),
            &format!("{family}.current_policy_sha"),
        )?;
        let head_sha = sha(
            field(evidence, "head_sha"),
            &format!("{family}.evidence.head_sha"),
        )?;
        let policy_sha = sha(
            field(evidence, "policy_sha"),
            &format!("{family}.evidence.policy_sha"),
        )?;
        canaries.push(Canary {
            family: family.into(),
            repository: repository.into(),
            workflow_path: workflow_path.into(),
            policy_workflow: policy_workflow.into(),
            current_policy_sha: current_policy_sha.into(),
            evidence: Evidence {
                run_id: run_id.clone(),
                head_sha: head_sha.into(),
                reference: reference.into(),
                policy_sha: policy_sha.into(),
                started_at: started_at.into(),
            },
            tag_prefix,
        });
    }
    Ok(canaries)
}

fn canonical_bytes(document: &Json) -> Vec<u8> {
    let options = Dump {
        indent: Some(2),
        sort_keys: true,
        ensure_ascii: true,
    };
    format!("{}\n", pyjson::dumps(document, options)).into_bytes()
}

pub fn load_manifest(path: &Path) -> Result<Vec<Canary>, String> {
    let raw = crate::common::read_bytes(path)?;
    if raw.starts_with(b"\xef\xbb\xbf") {
        return Err("canary manifest must not contain a UTF-8 BOM".into());
    }
    let invalid = || "canary manifest must be valid UTF-8 JSON".to_owned();
    let text = std::str::from_utf8(&raw).map_err(|_| invalid())?;
    let document = pyjson::loads(text).map_err(|_| invalid())?;
    if raw != canonical_bytes(&document) {
        return Err("canary manifest must use canonical sorted JSON".into());
    }
    let canaries = parse_manifest(&document)?;
    let mut observed = canaries
        .iter()
        .map(|c| c.family.as_str())
        .collect::<Vec<_>>();
    observed.sort_unstable();
    if observed != FAMILIES {
        return Err(format!(
            "canary manifest must cover {}, found {}",
            repr_list(FAMILIES),
            repr_list(observed)
        ));
    }
    Ok(canaries)
}

fn extract_pin(workflow: &str, canary: &Canary) -> Result<String, String> {
    let literal = regex::escape(&format!(
        "ryanduguid/release-policy/{}@",
        canary.policy_workflow
    ));
    let pattern =
        fancy_regex::Regex::new(&format!("{literal}([0-9a-f]{{40}})(?![0-9a-f])")).unwrap();
    let matches = pattern
        .captures_iter(workflow)
        .filter_map(|c| c.ok().map(|c| c[1].to_owned()))
        .collect::<Vec<_>>();
    if matches.len() != 1 {
        return Err(format!(
            "{}: expected one literal {} pin, found {}",
            canary.family,
            canary.policy_workflow,
            matches.len()
        ));
    }
    Ok(matches.into_iter().next().unwrap())
}

pub fn referenced_policy_sha(run: &Json, canary: &Canary) -> Option<String> {
    let Some(Json::Array(referenced)) = run.get("referenced_workflows") else {
        return None;
    };
    let expected = format!("ryanduguid/release-policy/{}@", canary.policy_workflow);
    let matches = referenced
        .iter()
        .filter(|raw| matches!(raw, Json::Object(_)))
        .filter_map(|raw| {
            let path = raw.get("path").and_then(Json::as_str)?;
            let sha = raw.get("sha").and_then(Json::as_str)?;
            (path.starts_with(&expected) && full(SHA, sha)).then(|| sha.to_owned())
        })
        .collect::<Vec<_>>();
    (matches.len() == 1).then(|| matches[0].clone())
}

fn repr_pin(pin: &Option<String>) -> String {
    pin.as_deref().map_or("None".into(), py::repr_str)
}

/// `str(value or "")`.
fn str_or_empty(value: Option<&Json>) -> String {
    match value {
        Some(v) if v.truthy() => v.py_str(),
        _ => String::new(),
    }
}

pub fn latest_relevant_run(payload: &Json, canary: &Canary) -> Option<Json> {
    let Some(Json::Array(runs)) = payload.get("workflow_runs") else {
        return None;
    };
    // The listing has returned an older success ahead of newer ones on the same
    // page (30 September and 7 October 2026), so order by start time, newest
    // first, instead of trusting the response order.
    let mut runs = runs
        .iter()
        .filter(|r| matches!(r, Json::Object(_)))
        .map(|r| (str_or_empty(r.get("run_started_at")), r))
        .collect::<Vec<_>>();
    runs.sort_by(|a, b| b.0.cmp(&a.0));
    for (_, run) in runs {
        if !eq(run.get("conclusion"), &Json::str_obj("success")) {
            continue;
        }
        let branch = run.get("head_branch");
        let relevant = if canary.family == "verify" {
            eq(branch, &Json::str_obj("main"))
        } else {
            matches_release_tag(branch, &canary.tag_prefix)
        };
        if relevant {
            return Some(run.clone());
        }
    }
    None
}

/// `datetime.strptime(started_at, "%Y-%m-%dT%H:%M:%SZ")` in UTC, or the
/// `ValueError` text where Python's parser or `datetime` refuses the value.
fn started_at(canary: &Canary) -> Result<DateTime<Utc>, Failure> {
    parse_started_at(&canary.evidence.started_at)
}

/// The start time of a manifest that passed the UTC-timestamp check.
pub fn parse_started_at(value: &str) -> Result<DateTime<Utc>, Failure> {
    let refused = || {
        Failure::Unexpected(format!(
            "ValueError: time data {} does not match format '%Y-%m-%dT%H:%M:%SZ'",
            py::repr_str(value)
        ))
    };
    let chars = value.chars().collect::<Vec<_>>();
    let digit = |c: char| py::decimal_value(c);
    let number = |range: std::ops::Range<usize>| -> Option<u32> {
        chars[range]
            .iter()
            .try_fold(0, |n, c| Some(n * 10 + digit(*c)?))
    };
    // The format's regex: ASCII-led months, minutes and seconds; any decimal
    // digit where it writes \d.
    let ascii = |i: usize, set: &str| set.contains(chars[i]);
    let month_ok = (ascii(5, "1") && ascii(6, "012")) || (ascii(5, "0") && ascii(6, "123456789"));
    let day_ok = (ascii(8, "3") && ascii(9, "01"))
        || (ascii(8, "12") && digit(chars[9]).is_some())
        || (ascii(8, "0") && ascii(9, "123456789"));
    let hour_ok =
        (ascii(11, "2") && ascii(12, "0123")) || (ascii(11, "01") && digit(chars[12]).is_some());
    let minute_ok = ascii(14, "012345") && digit(chars[15]).is_some();
    let second_ok =
        (ascii(17, "6") && ascii(18, "01")) || (ascii(17, "012345") && digit(chars[18]).is_some());
    if !(month_ok && day_ok && hour_ok && minute_ok && second_ok) {
        return Err(refused());
    }
    let (year, month, day) = (
        number(0..4).unwrap(),
        number(5..7).unwrap(),
        number(8..10).unwrap(),
    );
    let (hour, minute, second) = (
        number(11..13).unwrap(),
        number(14..16).unwrap(),
        number(17..19).unwrap(),
    );
    if year == 0 {
        return Err(Failure::Unexpected(
            "ValueError: year must be in 1..9999, not 0".into(),
        ));
    }
    let date = NaiveDate::from_ymd_opt(year as i32, month, day).ok_or_else(|| {
        let last = (28..=31)
            .rev()
            .find(|d| NaiveDate::from_ymd_opt(year as i32, month, *d).is_some())
            .unwrap();
        Failure::Unexpected(format!(
            "ValueError: day {day} must be in range 1..{last} for month {month} in year {year}"
        ))
    })?;
    if second > 59 {
        return Err(Failure::Unexpected(format!(
            "ValueError: second must be in 0..59, not {second}"
        )));
    }
    let time = date.and_hms_opt(hour, minute, second).unwrap();
    Ok(Utc.from_utc_datetime(&time))
}

fn review_instruction(canary: &Canary, newer: &str) -> String {
    format!(
        "NEWER_RELEASE_REVIEW_REQUIRED: recorded run {} ({}) is overtaken by {newer}. Review the newer release's run, source SHA and policy pin, record it in canaries.json in canonical form, then rerun the live audit.",
        canary.evidence.run_id.py_str(),
        canary.evidence.reference
    )
}

/// The GitHub reads and the provenance check, injected for tests.
pub trait Live {
    fn fetch_json(&mut self, endpoint: &str) -> Result<Json, LiveError>;
    fn fetch_text(&mut self, endpoint: &str) -> Result<String, LiveError>;
    /// Ok unless the recorded release's signed provenance fails to verify.
    fn verify_provenance(&mut self, canary: &Canary) -> Result<(), Failure>;
}

/// Check the published releases once the recorded run has expired.
///
/// Runs disappear after retention, so an empty run listing cannot show that no
/// newer release happened. Published releases stay, and every release family
/// publishes one, so the newest release in the family's tag namespace by
/// publication time has to be the recorded one.
fn release_freshness_errors(
    canary: &Canary,
    live: &mut dyn Live,
    prefix: &str,
) -> Result<Vec<String>, String> {
    let mut releases = Vec::new();
    let mut complete = false;
    for page in 1..=MAX_RUN_PAGES {
        let payload = match handled(live.fetch_json(&format!(
            "repos/{}/releases?per_page=100&page={page}",
            canary.repository
        )))? {
            Ok(payload) => payload,
            Err(error) => {
                return Ok(vec![format!(
                    "{prefix}: could not read releases: {}",
                    error.message
                )]);
            }
        };
        let Json::Array(items) = payload else {
            return Ok(vec![format!("{prefix}: release listing is not a list")]);
        };
        let count = items.len();
        releases.extend(items.into_iter().filter(|i| matches!(i, Json::Object(_))));
        if count < 100 {
            complete = true;
            break;
        }
    }
    if !complete {
        return Ok(vec![format!(
            "{prefix}: more than {MAX_RUN_PAGES} pages of releases were read without reaching the end"
        )]);
    }
    let relevant = releases
        .iter()
        .filter(|r| {
            r.get("draft") == Some(&Json::Bool(false))
                && matches_release_tag(r.get("tag_name"), &canary.tag_prefix)
                && matches!(r.get("published_at"), Some(Json::Str(_)))
        })
        .collect::<Vec<_>>();
    let Some(mut newest) = relevant.first().copied() else {
        return Ok(vec![format!(
            "{prefix}: no published release in the family's tag namespace"
        )]);
    };
    let published = |r: &Json| {
        r.get("published_at")
            .and_then(Json::as_str)
            .unwrap()
            .to_owned()
    };
    for release in &relevant[1..] {
        // max() keeps the first of equal keys.
        if published(release) > published(newest) {
            newest = release;
        }
    }
    let newest_tag = newest.get("tag_name");
    if !eq(newest_tag, &Json::str_obj(&canary.evidence.reference)) {
        let newer = format!("release {}", repr(newest_tag));
        return Ok(vec![format!(
            "{prefix}: {}",
            review_instruction(canary, &newer)
        )]);
    }
    Ok(Vec::new())
}

/// Require a later, readable verification success once the recorded one expired.
///
/// The expired record stays a reviewed pointer; current verification is shown by
/// a different success that can still be read in full.
fn expired_verify_errors(
    canary: &Canary,
    latest: &Json,
    live: &mut dyn Live,
    prefix: &str,
) -> Result<Vec<String>, String> {
    let run_id = latest.get("id");
    let usable = run_id.and_then(int_value).is_some_and(|id| id >= 1.into())
        && !eq(run_id, &canary.evidence.run_id)
        && str_or_empty(latest.get("run_started_at")) > canary.evidence.started_at;
    if !usable {
        return Ok(vec![format!(
            "{prefix}: evidence run {} has expired and no later success replaces it; run the verification workflow on main and review it",
            canary.evidence.run_id.py_str()
        )]);
    }
    let run_id = run_id.unwrap();
    let shown = run_id.repr();
    let detail = match handled(live.fetch_json(&format!(
        "repos/{}/actions/runs/{}",
        canary.repository,
        run_id.py_str()
    )))? {
        Ok(detail) => detail,
        Err(error) => {
            return Ok(vec![format!(
                "{prefix}: could not read current success {shown}: {}",
                error.message
            )]);
        }
    };
    if !matches!(detail, Json::Object(_)) {
        return Ok(vec![format!(
            "{prefix}: current success {shown} response is not an object"
        )]);
    }
    // The listing only nominates the run; every property it is accepted for has
    // to come from the run's own record.
    let mut errors = Vec::new();
    for (name, value) in [
        ("id", run_id.clone()),
        ("conclusion", Json::str_obj("success")),
        ("head_branch", Json::str_obj("main")),
        ("path", Json::str_obj(&canary.workflow_path)),
    ] {
        if !eq(detail.get(name), &value) {
            errors.push(format!(
                "{prefix}: current success {shown} {name} {} != {}",
                repr(detail.get(name)),
                value.repr()
            ));
        }
    }
    let started = detail.get("run_started_at");
    let later = match started {
        Some(Json::Str(s)) => {
            STARTED_AT.is_match(s) && s.as_str() > canary.evidence.started_at.as_str()
        }
        _ => false,
    };
    if !later {
        errors.push(format!(
            "{prefix}: current success {shown} run_started_at {} is not later than the expired {}",
            repr(started),
            canary.evidence.started_at
        ));
    }
    let pin = referenced_policy_sha(&detail, canary);
    if pin.as_deref() != Some(canary.current_policy_sha.as_str()) {
        errors.push(format!(
            "{prefix}: current success {shown} used policy pin {} != current {}",
            repr_pin(&pin),
            canary.current_policy_sha
        ));
    }
    Ok(errors)
}

/// Audit each canary against GitHub.
///
/// `release_evidence` selects release evidence mode, which `main` always uses:
/// provenance and the published-release freshness check run for every release
/// family on every audit, so the path an expired run depends on is exercised
/// every week. Without it an expired release run is an error.
pub fn check_live(
    canaries: &[Canary],
    live: &mut dyn Live,
    release_evidence: bool,
    now: DateTime<Utc>,
) -> Result<CheckResult, String> {
    let mut result = CheckResult::default();
    let errors = &mut result.errors;
    for canary in canaries {
        let release_family = RELEASE_FAMILIES.contains(&canary.family.as_str());
        let prefix = format!("{} ({})", canary.family, canary.repository);
        let workflow_endpoint = format!(
            "repos/{}/contents/{}?ref=main",
            canary.repository, canary.workflow_path
        );
        let actual_pin = match handled(live.fetch_text(&workflow_endpoint))?
            .map_err(|e| e.message)
            .and_then(|text| extract_pin(&text, canary))
        {
            Ok(pin) => Some(pin),
            Err(error) => {
                errors.push(format!("{prefix}: {error}"));
                None
            }
        };
        if let Some(pin) = actual_pin.filter(|p| *p != canary.current_policy_sha) {
            errors.push(format!(
                "{prefix}: current pin {pin} != recorded {}",
                canary.current_policy_sha
            ));
        }

        let compare_endpoint = format!(
            "repos/ryanduguid/release-policy/compare/main...{}",
            canary.current_policy_sha
        );
        match handled(live.fetch_json(&compare_endpoint))? {
            Err(error) => errors.push(format!(
                "{prefix}: current pin {} is not reachable from release-policy main: {}",
                canary.current_policy_sha, error.message
            )),
            Ok(comparison) => {
                let status = comparison.get("status");
                if let Some(Json::Array(_) | Json::Object(_)) = status {
                    let kind = if matches!(status, Some(Json::Array(_))) {
                        "list"
                    } else {
                        "dict"
                    };
                    return Err(format!("TypeError: unhashable type: '{kind}'"));
                }
                if !REACHABLE_STATUSES
                    .iter()
                    .any(|s| eq(status, &Json::str_obj(s)))
                {
                    errors.push(format!(
                        "{prefix}: current pin {} is not reachable from release-policy main (compare status {})",
                        canary.current_policy_sha,
                        repr(status)
                    ));
                }
            }
        }

        let run_endpoint = format!(
            "repos/{}/actions/runs/{}",
            canary.repository,
            canary.evidence.run_id.py_str()
        );
        let mut run_expired = false;
        match handled(live.fetch_json(&run_endpoint))? {
            Err(error) => {
                if error.status != Some(404) {
                    errors.push(format!(
                        "{prefix}: could not read evidence run: {}",
                        error.message
                    ));
                } else {
                    let started = started_at(canary).map_err(|f| match f {
                        Failure::Runtime(m) | Failure::Unexpected(m) => m,
                    })?;
                    if now - started > chrono::Duration::days(RETENTION_DAYS + EXPIRY_MARGIN_DAYS) {
                        run_expired = true;
                    } else {
                        errors.push(format!(
                            "{prefix}: evidence run {} is missing before its retention period ended (started {})",
                            canary.evidence.run_id.py_str(),
                            canary.evidence.started_at
                        ));
                    }
                }
            }
            Ok(raw_run @ Json::Object(_)) => {
                for (name, value) in [
                    ("conclusion", Json::str_obj("success")),
                    ("head_branch", Json::str_obj(&canary.evidence.reference)),
                    ("head_sha", Json::str_obj(&canary.evidence.head_sha)),
                    ("path", Json::str_obj(&canary.workflow_path)),
                    ("run_started_at", Json::str_obj(&canary.evidence.started_at)),
                ] {
                    if !eq(raw_run.get(name), &value) {
                        errors.push(format!(
                            "{prefix}: evidence run {name} {} != {}",
                            repr(raw_run.get(name)),
                            value.repr()
                        ));
                    }
                }
                let observed = referenced_policy_sha(&raw_run, canary);
                if observed.as_deref() != Some(canary.evidence.policy_sha.as_str()) {
                    errors.push(format!(
                        "{prefix}: evidence policy pin {} != {}",
                        repr_pin(&observed),
                        canary.evidence.policy_sha
                    ));
                }
            }
            // Only a 404 can mark a run expired. Any other body, JSON null
            // included, is not a run record and fails.
            Ok(_) => errors.push(format!("{prefix}: evidence run response is not an object")),
        }

        if release_family && release_evidence {
            match live.verify_provenance(canary) {
                Err(Failure::Runtime(error)) => errors.push(format!(
                    "{prefix}: release provenance not verified: {error}"
                )),
                Err(Failure::Unexpected(error)) => return Err(error),
                Ok(()) if run_expired => result.notices.push(format!(
                    "{prefix}: evidence run {} has expired; release {} and its signed provenance were verified instead. The run's start time and conclusion can no longer be checked.",
                    canary.evidence.run_id.py_str(),
                    canary.evidence.reference
                )),
                Ok(()) => {}
            }
        }

        // A release can be published without a successful run to show for it,
        // so release evidence mode reads the published releases on every audit.
        if release_family && (run_expired || release_evidence) {
            let freshness = release_freshness_errors(canary, live, &prefix)?;
            errors.extend(freshness);
        }
        if run_expired && release_family {
            if !release_evidence {
                errors.push(format!(
                    "{prefix}: evidence run {} has expired and no provenance verifier was supplied",
                    canary.evidence.run_id.py_str()
                ));
            }
        } else {
            let workflow_name = canary.workflow_path.rsplit('/').next().unwrap();
            // Page through the successes until the relevant run appears or the
            // pages run out. Reading one page let a namespaced release run hide
            // behind newer successes from other namespaces.
            let mut latest = None;
            let mut partial_page_seen = false;
            let mut failed = false;
            for page in 1..=MAX_RUN_PAGES {
                let endpoint = format!(
                    "repos/{}/actions/workflows/{workflow_name}/runs?status=success&per_page=100&page={page}",
                    canary.repository
                );
                let payload = match handled(live.fetch_json(&endpoint))? {
                    Ok(payload) => payload,
                    Err(error) => {
                        errors.push(format!(
                            "{prefix}: could not read latest successes: {}",
                            error.message
                        ));
                        latest = None;
                        failed = true;
                        break;
                    }
                };
                latest = latest_relevant_run(&payload, canary);
                if latest.is_some() {
                    break;
                }
                match payload.get("workflow_runs") {
                    Some(Json::Array(runs)) if runs.len() >= 100 => {}
                    _ => {
                        partial_page_seen = true;
                        break;
                    }
                }
            }
            if latest.is_none() && !partial_page_seen && !failed {
                errors.push(format!(
                    "{prefix}: more than {MAX_RUN_PAGES} pages of successful runs were searched without finding the relevant one"
                ));
            }
            match latest {
                None if partial_page_seen => errors.push(format!(
                    "{prefix}: no relevant successful workflow run was returned"
                )),
                // The pagination-exceeded error above already names this condition;
                // nothing more can be compared without a run.
                None => {}
                Some(latest) if release_family => {
                    if !eq(latest.get("id"), &canary.evidence.run_id) {
                        let newer = format!(
                            "run {} ({})",
                            repr(latest.get("id")),
                            repr(latest.get("head_branch"))
                        );
                        errors.push(format!(
                            "{prefix}: latest successful run {} != recorded {}; {}",
                            repr(latest.get("id")),
                            canary.evidence.run_id.py_str(),
                            review_instruction(canary, &newer)
                        ));
                    }
                }
                Some(latest) => {
                    // The recorded run stays the reviewed example, checked above like any
                    // other. Freshness comes from the live listing instead: the newest
                    // success has to be running the pin the consumer declares today.
                    let observed = referenced_policy_sha(&latest, canary);
                    if observed.as_deref() != Some(canary.current_policy_sha.as_str()) {
                        errors.push(format!(
                            "{prefix}: latest successful run {} used policy pin {} != current {}",
                            repr(latest.get("id")),
                            repr_pin(&observed),
                            canary.current_policy_sha
                        ));
                    } else if run_expired {
                        let expiry = expired_verify_errors(canary, &latest, live, &prefix)?;
                        if expiry.is_empty() {
                            result.notices.push(format!(
                                "{prefix}: evidence run {} has expired; current success {} on the current pin was verified instead.",
                                canary.evidence.run_id.py_str(),
                                repr(latest.get("id"))
                            ));
                        }
                        errors.extend(expiry);
                    }
                }
            }
        }

        if release_family && canary.evidence.policy_sha != canary.current_policy_sha {
            result.warnings.push(format!(
                "{prefix}: current pin lacks release evidence; latest success used {}",
                canary.evidence.policy_sha
            ));
        }
    }
    Ok(result)
}

/// `gh` output as Python's text mode reads it.
fn text_output(raw: &[u8]) -> Result<String, LiveError> {
    py::decode_utf8(raw)
        .map(|t| py::universal_newlines(&t))
        .map_err(|e| LiveError::Unexpected(format!("UnicodeDecodeError: {e}")))
}

/// The `gh` command line.
pub struct GhCli;

impl GhCli {
    fn run(arguments: &[&str]) -> Result<(bool, String, String), LiveError> {
        let out = std::process::Command::new("gh")
            .args(arguments)
            .output()
            .map_err(|e| LiveError::Unexpected(py::oserror_str(&e, "gh", py::OsStyle::Native)))?;
        Ok((
            out.status.success(),
            text_output(&out.stdout)?,
            text_output(&out.stderr)?,
        ))
    }

    pub fn json(endpoint: &str) -> Result<Json, LiveError> {
        // --include puts the status line first, so a 404 is read from GitHub's
        // response rather than guessed from error text.
        let (ok, stdout, stderr) = Self::run(&["api", "--include", endpoint])?;
        let (head, body) = stdout.split_once("\n\n").unwrap_or((&stdout, ""));
        let status = STATUS_LINE
            .captures(head)
            .map(|c| c[1].parse::<u16>().unwrap());
        if !ok {
            let message = py::strip(&stderr);
            return Err(LiveError::GitHub(GitHubError {
                message: if message.is_empty() {
                    format!("gh api failed for {endpoint}")
                } else {
                    message.to_owned()
                },
                status,
            }));
        }
        pyjson::loads(body).map_err(|_| {
            LiveError::GitHub(GitHubError {
                message: format!("GitHub returned invalid JSON for {endpoint}"),
                status,
            })
        })
    }

    pub fn command(arguments: &[&str]) -> Result<String, Failure> {
        let (ok, stdout, stderr) = Self::run(arguments)?;
        if !ok {
            let message = py::strip(&stderr);
            return Err(Failure::Runtime(if message.is_empty() {
                format!("gh {} failed", arguments[0])
            } else {
                message.to_owned()
            }));
        }
        Ok(stdout)
    }

    pub fn tag_commit(repository: &str, tag: &str) -> Result<Option<String>, LiveError> {
        let mut target = Self::json(&format!("repos/{repository}/git/ref/tags/{tag}"))?;
        for _ in 0..5 {
            let Some(object @ Json::Object(_)) = target.get("object") else {
                return Ok(None);
            };
            if eq(object.get("type"), &Json::str_obj("commit")) {
                return Ok(object.get("sha").and_then(Json::as_str).map(str::to_owned));
            }
            if !eq(object.get("type"), &Json::str_obj("tag")) {
                return Ok(None);
            }
            let next = object.get("sha").map_or("None".into(), Json::py_str);
            target = Self::json(&format!("repos/{repository}/git/tags/{next}"))?;
        }
        Ok(None)
    }
}

/// True when a verified certificate's run invocation is exactly this run.
pub fn names_run(results: &Json, repository: &str, run_id: &Json) -> bool {
    let expected = regex::Regex::new(&format!(
        r"\A{}[1-9][0-9]*\z",
        regex::escape(&format!(
            "https://github.com/{repository}/actions/runs/{}/attempts/",
            run_id.py_str()
        ))
    ))
    .unwrap();
    let Json::Array(results) = results else {
        return false;
    };
    results.iter().any(|result| {
        let uri = [
            "verificationResult",
            "signature",
            "certificate",
            "runInvocationURI",
        ]
        .iter()
        .try_fold(result, |value, key| value.get(key));
        uri.and_then(Json::as_str)
            .is_some_and(|u| expected.is_match(u))
    })
}

impl Live for GhCli {
    fn fetch_json(&mut self, endpoint: &str) -> Result<Json, LiveError> {
        Self::json(endpoint)
    }

    fn fetch_text(&mut self, endpoint: &str) -> Result<String, LiveError> {
        let (ok, stdout, stderr) = Self::run(&[
            "api",
            "-H",
            "Accept: application/vnd.github.raw+json",
            endpoint,
        ])?;
        if !ok {
            let message = py::strip(&stderr);
            return Err(GitHubError::new(if message.is_empty() {
                format!("gh api failed for {endpoint}")
            } else {
                message.to_owned()
            })
            .into());
        }
        Ok(stdout)
    }

    /// Verify the recorded release's signed SLSA provenance.
    ///
    /// The certificate binds the signer workflow and its commit (the policy pin),
    /// the source tag and commit, and the run; gh checks the first four against the
    /// flags below, and the run is matched exactly here. The release and its tag
    /// are read live, so a later move or deletion fails.
    fn verify_provenance(&mut self, canary: &Canary) -> Result<(), Failure> {
        let (repository, reference) = (&canary.repository, &canary.evidence.reference);
        let release = Self::json(&format!("repos/{repository}/releases/tags/{reference}"))?;
        if !matches!(release, Json::Object(_))
            || release.get("draft") != Some(&Json::Bool(false))
            || release.get("immutable") != Some(&Json::Bool(true))
        {
            return Err(Failure::Runtime(format!(
                "release {reference} is not a published immutable release"
            )));
        }
        let tag_sha = Self::tag_commit(repository, reference)?;
        if tag_sha.as_deref() != Some(canary.evidence.head_sha.as_str()) {
            return Err(Failure::Runtime(format!(
                "tag {reference} points to {}, not {}",
                repr_pin(&tag_sha),
                canary.evidence.head_sha
            )));
        }
        let listed: Vec<&Json> = match release.get("assets") {
            Some(value) if !value.truthy() => Vec::new(),
            None => Vec::new(),
            Some(Json::Array(items)) => items.iter().collect(),
            // Iterating a dict or a str yields strings, never an asset mapping.
            Some(Json::Object(_) | Json::Str(_)) => Vec::new(),
            Some(other) => {
                let kind = match other {
                    Json::Bool(_) => "bool",
                    Json::Float(_) => "float",
                    _ => "int",
                };
                return Err(Failure::Unexpected(format!(
                    "TypeError: '{kind}' object is not iterable"
                )));
            }
        };
        let assets = listed
            .into_iter()
            .filter(|a| {
                matches!(a, Json::Object(_))
                    && a.get("name")
                        .map_or(String::new(), Json::py_str)
                        .ends_with(".tar.gz")
            })
            .collect::<Vec<_>>();
        if assets.len() != 1 {
            return Err(Failure::Runtime(format!(
                "release {reference} has {} .tar.gz assets, expected 1",
                assets.len()
            )));
        }
        let name = assets[0].get("name").unwrap().py_str();
        let directory = tempfile::tempdir().map_err(|e| Failure::Unexpected(e.to_string()))?;
        let outcome = (|| {
            let dir = directory.path().to_string_lossy().into_owned();
            Self::command(&[
                "release",
                "download",
                reference,
                "--repo",
                repository,
                "--pattern",
                &name,
                "--dir",
                &dir,
            ])?;
            let path = directory.path().join(&name);
            let raw = std::fs::read(&path).map_err(|e| {
                Failure::Unexpected(py::oserror_str(
                    &e,
                    &path.to_string_lossy(),
                    py::OsStyle::Errno,
                ))
            })?;
            let digest = format!("sha256:{}", sha256(&raw));
            if !eq(assets[0].get("digest"), &Json::str_obj(&digest)) {
                return Err(Failure::Runtime(format!(
                    "{name} digest {digest} != release {}",
                    repr(assets[0].get("digest"))
                )));
            }
            let signer = format!(
                "ryanduguid/release-policy/{}",
                signer_workflow(&canary.family)
            );
            let source_ref = format!("refs/tags/{reference}");
            Self::command(&[
                "attestation",
                "verify",
                &path.to_string_lossy(),
                "--repo",
                repository,
                "--signer-workflow",
                &signer,
                "--signer-digest",
                &canary.evidence.policy_sha,
                "--source-ref",
                &source_ref,
                "--source-digest",
                &canary.evidence.head_sha,
                "--predicate-type",
                SLSA_PROVENANCE,
                "--format",
                "json",
            ])
        })();
        drop(directory);
        let output = outcome?;
        let results = pyjson::loads(&output)
            .map_err(|e| Failure::Unexpected(format!("json.decoder.JSONDecodeError: {e}")))?;
        if !names_run(&results, repository, &canary.evidence.run_id) {
            return Err(Failure::Runtime(format!(
                "no verified attestation of {name} names run {}",
                canary.evidence.run_id.py_str()
            )));
        }
        Ok(())
    }
}

/// What the live audit prints and its exit status: notices, warnings and
/// errors, then the pass line when nothing failed.
pub fn render(
    result: &CheckResult,
    families: usize,
    require_current_evidence: bool,
) -> (String, i32) {
    let mut out = String::new();
    for notice in &result.notices {
        out.push_str(&format!("INFO {notice}\n"));
    }
    for warning in &result.warnings {
        out.push_str(&format!("WARN {warning}\n"));
    }
    for error in &result.errors {
        out.push_str(&format!("FAIL {error}\n"));
    }
    if !result.errors.is_empty() || (require_current_evidence && !result.warnings.is_empty()) {
        return (out, 1);
    }
    out.push_str(&format!("canary audit passed: {families} families\n"));
    (out, 0)
}
