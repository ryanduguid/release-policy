use crate::pyjson::Json;
use crate::{Result, common::*, py};
use num_bigint::BigInt;
use std::collections::{BTreeMap, BTreeSet};

const WORKFLOW: &str = r"\.github/workflows/[A-Za-z0-9_.-]+\.ya?ml";
pub const MAX_WAIT_SECONDS: u64 = 1800;

#[derive(Clone, Debug, PartialEq, Eq)]
pub struct RequiredCheck {
    pub workflow: String,
    pub job: String,
}
impl RequiredCheck {
    pub fn label(&self) -> String {
        format!("{}: {}", self.workflow, self.job)
    }
}
#[derive(Clone, Debug)]
pub struct Run {
    pub run_id: BigInt,
    pub status: String,
}
#[derive(Clone, Debug)]
pub struct Job {
    pub run_id: BigInt,
    pub job_id: BigInt,
    pub attempt: Json,
    pub status: String,
    pub conclusion: String,
}
impl Job {
    pub fn label(&self) -> String {
        let attempt = match &self.attempt {
            Json::Bool(true) => Some("True".to_owned()),
            v => integer(v)
                .filter(|i| i != &BigInt::from(0))
                .map(|i| i.to_string()),
        };
        if let Some(attempt) = attempt {
            format!("{} (attempt {attempt})", self.job_id)
        } else {
            self.job_id.to_string()
        }
    }
}
#[derive(Clone, Debug, Default, PartialEq)]
pub struct Verdict {
    pub passed: Vec<String>,
    pub pending: Vec<String>,
    pub failed: Vec<String>,
}
impl Verdict {
    pub fn ok(&self) -> bool {
        self.pending.is_empty() && self.failed.is_empty()
    }
}
pub type Runs = BTreeMap<String, Vec<Run>>;
pub type Jobs = BTreeMap<String, Vec<Job>>;

/// An exact `int`: `type(value) is int`, so never a bool.
fn integer(v: &Json) -> Option<BigInt> {
    match v {
        Json::Int(i) => Some(i.clone()),
        _ => None,
    }
}
fn typename(v: &Json) -> &'static str {
    match v {
        Json::Null => "NoneType",
        Json::Bool(_) => "bool",
        Json::Str(_) => "str",
        Json::Array(_) => "list",
        Json::Object(_) => "dict",
        Json::Int(_) => "int",
        Json::Float(_) => "float",
    }
}
/// `value.get(key)`, with an absent key or a non-mapping read as `None`.
fn at<'a>(v: &'a Json, key: &str) -> &'a Json {
    v.get(key).unwrap_or(&Json::Null)
}
fn text<'a>(v: &'a Json, field: &str, where_: &str) -> Result<&'a str> {
    if *v == Json::Null {
        return Ok("");
    }
    v.as_str().ok_or_else(||format!("{where_}: {field} is {}, not text; refusing to judge a listing that cannot be read in full",typename(v)))
}
fn full_name(v: &Json, field: &str, where_: &str) -> Result<String> {
    if *v == Json::Null {
        return Ok(String::new());
    }
    if !matches!(v, Json::Object(_)) {
        return Err(format!(
            "{where_}: {field} is {}, not a repository; refusing to judge a listing that cannot be read in full",
            typename(v)
        ));
    }
    Ok(py::casefold(text(
        at(v, "full_name"),
        &format!("{field}.full_name"),
        where_,
    )?))
}

pub fn parse_required_checks(input: &str) -> Result<Vec<RequiredCheck>> {
    let mut checks = Vec::new();
    for raw in py::splitlines(input) {
        let line = py::strip(raw);
        if line.is_empty() || line.starts_with('#') {
            continue;
        }
        let (workflow, job) = line.split_once(": ").ok_or_else(|| {
            format!(
                "required check {} is not '<workflow path>: <job name>'",
                py::repr_str(line)
            )
        })?;
        let (workflow, job) = (py::strip(workflow), py::strip(job));
        if job.is_empty() {
            return Err(format!(
                "required check {} is not '<workflow path>: <job name>'",
                py::repr_str(line)
            ));
        }
        if !full(WORKFLOW, workflow) {
            return Err(format!(
                "required check {} names a path outside .github/workflows/",
                py::repr_str(line)
            ));
        }
        let check = RequiredCheck {
            workflow: workflow.into(),
            job: job.into(),
        };
        if checks.contains(&check) {
            return Err(format!(
                "required check {} is listed twice",
                py::repr_str(&check.label())
            ));
        }
        checks.push(check);
    }
    if checks.is_empty() {
        return Err("no required checks were named; the gate fails closed".into());
    }
    Ok(checks)
}

fn read_pages(
    fetch: &mut impl FnMut(&str) -> Result<Json>,
    endpoint: &str,
    key: &str,
) -> Result<Option<Vec<Json>>> {
    let mut items = Vec::new();
    let mut seen = Vec::new();
    let mut total = Json::Null;
    let mut page = 1;
    loop {
        let joiner = if endpoint.contains('?') { '&' } else { '?' };
        let payload = fetch(&format!("{endpoint}{joiner}per_page=100&page={page}"))?;
        let Some(Json::Array(list)) = payload.get(key) else {
            return Err(format!(
                "GitHub returned no {} list for {endpoint}",
                py::repr_str(key)
            ));
        };
        if integer(at(&payload, "total_count")).is_none() {
            return Err(format!(
                "GitHub returned no numeric total_count for {endpoint}"
            ));
        }
        if page == 1 {
            total = at(&payload, "total_count").clone();
        } else if !at(&payload, "total_count").py_eq(&total) {
            return Ok(None);
        }
        for entry in list {
            let id=integer(at(entry, "id")).ok_or_else(||format!("{endpoint}: GitHub returned an entry with no numeric id; refusing to judge a listing that cannot be read in full"))?;
            seen.push(id);
            items.push(entry.clone());
        }
        if list.len() < 100 {
            break;
        }
        page += 1;
    }
    if seen.iter().collect::<BTreeSet<_>>().len() != seen.len()
        || integer(&total) != Some(BigInt::from(seen.len()))
    {
        return Ok(None);
    }
    Ok(Some(items))
}

pub fn paginate(
    fetch: &mut impl FnMut(&str) -> Result<Json>,
    endpoint: &str,
    key: &str,
) -> Result<Vec<Json>> {
    for _ in 0..3 {
        let Some(first) = read_pages(fetch, endpoint, key)? else {
            continue;
        };
        if let Some(second) = read_pages(fetch, endpoint, key)?
            && first.len() == second.len()
            && first
                .iter()
                .zip(&second)
                .all(|(a, b)| at(a, "id").py_eq(at(b, "id")))
        {
            return Ok(second);
        }
    }
    Err(format!(
        "{endpoint}: the listing changed while it was read, 3 times over"
    ))
}

pub fn trusted_runs(
    fetch: &mut impl FnMut(&str) -> Result<Json>,
    repository: &str,
    commit: &str,
) -> Result<Runs> {
    let runs = paginate(
        fetch,
        &format!("repos/{repository}/actions/runs?head_sha={commit}"),
        "workflow_runs",
    )?;
    let wanted = py::casefold(repository);
    let mut found = Runs::new();
    for run in runs {
        let where_ = format!("{repository}: a workflow run for {commit}");
        let Some(path) = at(&run, "path").as_str() else {
            return Err(format!(
                "{where_} has no readable path or id; refusing to judge a listing that cannot be read in full"
            ));
        };
        let id=integer(at(&run, "id")).ok_or_else(||format!("{where_} has no readable path or id; refusing to judge a listing that cannot be read in full"))?;
        if text(at(&run, "head_sha"), "head_sha", &where_)? != commit
            || !matches!(
                text(at(&run, "event"), "event", &where_)?,
                "push" | "workflow_dispatch"
            )
            || text(at(&run, "head_branch"), "head_branch", &where_)? != "main"
            || full_name(at(&run, "repository"), "repository", &where_)? != wanted
            || full_name(at(&run, "head_repository"), "head_repository", &where_)? != wanted
        {
            continue;
        }
        found.entry(path.into()).or_default().push(Run {
            run_id: id,
            status: text(at(&run, "status"), "status", &where_)?.into(),
        });
    }
    for runs in found.values_mut() {
        runs.sort_by(|a, b| b.run_id.cmp(&a.run_id));
    }
    Ok(found)
}

pub fn run_jobs(
    fetch: &mut impl FnMut(&str) -> Result<Json>,
    repository: &str,
    run_id: &BigInt,
    commit: &str,
) -> Result<Jobs> {
    let jobs = paginate(
        fetch,
        &format!("repos/{repository}/actions/runs/{run_id}/jobs?filter=latest"),
        "jobs",
    )?;
    let where_ = format!("{repository}: run {run_id}");
    let mut found = Jobs::new();
    for raw in jobs {
        let error = || {
            format!(
                "{where_} reported a job with no readable name or id; refusing to judge a listing that cannot be read in full"
            )
        };
        let name = at(&raw, "name").as_str().ok_or_else(error)?;
        let id = integer(at(&raw, "id")).ok_or_else(error)?;
        if text(at(&raw, "head_sha"), "head_sha", &where_)? != commit {
            continue;
        }
        // isinstance(attempt, int) admits a bool too.
        let attempt = match at(&raw, "run_attempt") {
            value @ (Json::Int(_) | Json::Bool(_)) => value.clone(),
            _ => Json::Int(0.into()),
        };
        found.entry(name.into()).or_default().push(Job {
            run_id: run_id.clone(),
            job_id: id,
            attempt,
            status: text(at(&raw, "status"), "status", &where_)?.into(),
            conclusion: text(at(&raw, "conclusion"), "conclusion", &where_)?.into(),
        });
    }
    for jobs in found.values_mut() {
        jobs.sort_by(|a, b| a.job_id.cmp(&b.job_id));
    }
    Ok(found)
}

pub fn evaluate(
    required: &[RequiredCheck],
    runs: &Runs,
    mut jobs: impl FnMut(&BigInt) -> Result<Jobs>,
) -> Result<Verdict> {
    let mut verdict = Verdict::default();
    let mut cache = BTreeMap::new();
    for check in required {
        let label = check.label();
        let Some(found_runs) = runs.get(&check.workflow).filter(|r| !r.is_empty()) else {
            verdict.failed.push(format!("{label}: no push or workflow_dispatch run of that workflow on main reported this commit"));
            continue;
        };
        let mut refused = Vec::new();
        let mut waiting = Vec::new();
        let mut witnesses = Vec::new();
        for run in found_runs {
            if !cache.contains_key(&run.run_id) {
                cache.insert(run.run_id.clone(), jobs(&run.run_id)?);
            }
            let matched = cache[&run.run_id]
                .get(&check.job)
                .map_or(&[][..], Vec::as_slice);
            if matched.len() > 1 {
                refused.push(format!("{label}: run {} has {} distinct jobs named {} (jobs {}); a required check must name exactly one job, so give each job a unique name",run.run_id,matched.len(),py::repr_str(&check.job),matched.iter().map(Job::label).collect::<Vec<_>>().join(", ")));
                continue;
            }
            match matched.first() {
                None if run.status != "completed" => waiting.push(format!(
                    "{label}: run {} is {} and has not reported this check yet",
                    run.run_id,
                    if run.status.is_empty() {
                        "in an unknown state"
                    } else {
                        &run.status
                    }
                )),
                None => refused.push(format!(
                    "{label}: run {} completed without reporting this check",
                    run.run_id
                )),
                Some(job) if job.status != "completed" => waiting.push(format!(
                    "{label}: run {} job {} is {}",
                    run.run_id,
                    job.label(),
                    job.status
                )),
                Some(job) if job.conclusion == "success" => {
                    witnesses.push(format!("run {} job {}", run.run_id, job.label()))
                }
                Some(job) => refused.push(format!(
                    "{label}: run {} job {} concluded {}, not 'success'",
                    run.run_id,
                    job.label(),
                    py::repr_str(&job.conclusion)
                )),
            }
        }
        if !refused.is_empty() {
            verdict.failed.extend(refused);
        } else if !waiting.is_empty() {
            verdict.pending.extend(waiting);
        } else {
            verdict
                .passed
                .push(format!("{label}: {}", witnesses.join(", ")));
        }
    }
    Ok(verdict)
}

#[derive(Clone, Copy)]
pub struct Polling {
    pub wait_seconds: u64,
    pub poll_seconds: u64,
}

pub fn check(
    required: &[RequiredCheck],
    repository: &str,
    commit: &str,
    fetch: &mut impl FnMut(&str) -> Result<Json>,
    polling: Polling,
    mut clock: impl FnMut() -> f64,
    mut sleep: impl FnMut(f64),
) -> Result<Verdict> {
    let deadline = clock() + polling.wait_seconds as f64;
    loop {
        let runs = trusted_runs(fetch, repository, commit)?;
        let verdict = evaluate(required, &runs, |id| {
            run_jobs(fetch, repository, id, commit)
        })?;
        if !verdict.failed.is_empty() || verdict.pending.is_empty() || clock() >= deadline {
            return Ok(verdict);
        }
        sleep((polling.poll_seconds as f64).min((deadline - clock()).max(0.0)));
    }
}

/// One `gh api` read, decoded as the script's text-mode subprocess read it.
pub fn gh_json(endpoint: &str) -> Result<Json> {
    let out = std::process::Command::new("gh")
        .args(["api", "-H", "X-GitHub-Api-Version: 2026-03-10", endpoint])
        .output()
        .map_err(|e| py::oserror_str(&e, "gh", py::OsStyle::Native))?;
    let decode = |raw: &[u8]| {
        py::decode_utf8(raw)
            .map(|t| py::universal_newlines(&t))
            .map_err(|e| format!("unexpected error: UnicodeDecodeError: {e}"))
    };
    let (stdout, stderr) = (decode(&out.stdout)?, decode(&out.stderr)?);
    if !out.status.success() {
        let message = py::strip(&stderr);
        return Err(if message.is_empty() {
            format!("gh api failed for {endpoint}")
        } else {
            message.into()
        });
    }
    crate::pyjson::loads(&stdout)
        .map_err(|_| format!("GitHub returned invalid JSON for {endpoint}"))
}

/// What the command printed and its exit status.
#[derive(Clone, Debug, PartialEq)]
pub struct Outcome {
    pub code: i32,
    pub stdout: String,
    pub stderr: String,
}

/// The command: validate the arguments before reading the list or GitHub,
/// then report every passed, pending and failed check.
#[allow(clippy::too_many_arguments)]
pub fn command(
    repository: &str,
    commit: &str,
    wait_seconds: &BigInt,
    poll_seconds: &BigInt,
    stdin: impl FnOnce() -> Result<String>,
    fetch: &mut impl FnMut(&str) -> Result<Json>,
    clock: impl FnMut() -> f64,
    sleep: impl FnMut(f64),
) -> Outcome {
    let fail = |message: String| Outcome {
        code: 1,
        stdout: String::new(),
        stderr: format!("FAIL {message}\n"),
    };
    if !full(REPOSITORY, repository) {
        return fail(format!(
            "repository {} is not owner/name",
            py::repr_str(repository)
        ));
    }
    if !full(SHA, commit) {
        return fail(format!(
            "commit {} is not a 40-hex commit",
            py::repr_str(commit)
        ));
    }
    let wait = u64::try_from(wait_seconds)
        .ok()
        .filter(|w| *w <= MAX_WAIT_SECONDS);
    if wait.is_none() || *poll_seconds < BigInt::from(1) {
        return fail(format!(
            "wait must be 0 to {MAX_WAIT_SECONDS} seconds and poll at least 1"
        ));
    }
    let polling = Polling {
        wait_seconds: wait.unwrap(),
        poll_seconds: u64::try_from(poll_seconds).unwrap_or(u64::MAX),
    };
    let verdict = match stdin()
        .and_then(|text| parse_required_checks(&text))
        .and_then(|checks| check(&checks, repository, commit, fetch, polling, clock, sleep))
    {
        Ok(verdict) => verdict,
        Err(error) => return fail(error),
    };
    let mut outcome = Outcome {
        code: 0,
        stdout: String::new(),
        stderr: String::new(),
    };
    for line in &verdict.passed {
        outcome.stdout.push_str(&format!("ok   {line}\n"));
    }
    for line in &verdict.pending {
        outcome
            .stderr
            .push_str(&format!("FAIL {line} after the {wait_seconds}s wait\n"));
    }
    for line in &verdict.failed {
        outcome.stderr.push_str(&format!("FAIL {line}\n"));
    }
    if !verdict.ok() {
        outcome.code = 1;
        return outcome;
    }
    outcome.stdout.push_str(&format!(
        "required checks passed: {} for {commit}\n",
        verdict.passed.len()
    ));
    outcome
}
