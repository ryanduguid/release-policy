//! Bind attribution statuses to one current PR snapshot and completed scan.
//!
//! Only trusted invokers may choose paths or mutate snapshot/proof files. The
//! filesystem and executable search path must share the status token's trust;
//! the snapshot fingerprint does not authenticate an untrusted file writer.

use crate::common::{REPOSITORY, SHA, full, sha256};
use crate::py;
use crate::pyjson::{self, Dump, Json};
use std::path::Path;

fn text(value: Option<&Json>) -> Option<&str> {
    value.and_then(Json::as_str)
}

/// The fields of one PR read that later reads must reproduce exactly.
pub fn snapshot(
    payload: &Json,
    repository: &str,
    number: &num_bigint::BigInt,
) -> Result<Json, String> {
    let Json::Object(_) = payload else {
        return Err("unreadable PR identity".into());
    };
    match payload.get("number") {
        Some(Json::Int(found)) if found == number => {}
        _ => return Err("unreadable PR identity".into()),
    }
    let mut result = vec![("number".to_owned(), Json::Int(number.clone()))];
    for side in ["head", "base"] {
        let reference = payload.get(side);
        let repo = match reference {
            Some(r @ Json::Object(_)) => match r.get("repo") {
                Some(repo @ Json::Object(_)) => repo,
                _ => return Err("unreadable PR revision".into()),
            },
            _ => return Err("unreadable PR revision".into()),
        };
        let sha = text(reference.and_then(|r| r.get("sha")));
        let name = text(repo.get("full_name"));
        let Some(sha) = sha.filter(|s| full(SHA, s)) else {
            return Err("invalid PR revision".into());
        };
        let Some(name) = name.filter(|n| full(REPOSITORY, n)) else {
            return Err("invalid PR repository".into());
        };
        result.push((format!("{side}_sha"), Json::str_obj(sha)));
        result.push((format!("{side}_repository"), Json::Str(py::casefold(name))));
    }
    if result[4].1.as_str() != Some(py::casefold(repository).as_str()) {
        return Err("wrong base repository".into());
    }
    let base_ref = text(payload.get("base").and_then(|b| b.get("ref")));
    let title = text(payload.get("title"));
    let (Some(base_ref), Some(title)) = (base_ref.filter(|r| !r.is_empty()), title) else {
        return Err("unreadable PR metadata".into());
    };
    let body = match payload.get("body") {
        None | Some(Json::Null) => "",
        Some(Json::Str(body)) => body,
        Some(_) => return Err("unreadable PR body".into()),
    };
    result.push(("base_ref".into(), Json::str_obj(base_ref)));
    result.push(("title".into(), Json::str_obj(title)));
    result.push(("body".into(), Json::str_obj(body)));
    Ok(Json::Object(result))
}

pub fn fingerprint(value: &Json) -> String {
    let options = Dump {
        sort_keys: true,
        ..Dump::default()
    };
    sha256(pyjson::dumps(value, options).as_bytes())
}

/// Reads and status writes, injected so tests need no GitHub access.
pub trait GitHub {
    fn fetch(&mut self, endpoint: &str) -> Result<Json, String>;
    fn post(&mut self, head: &str, state: &str) -> Result<(), String>;
}

pub fn prepare(
    repository: &str,
    number: &num_bigint::BigInt,
    path: &Path,
    github: &mut dyn GitHub,
) -> Result<(), String> {
    let value = snapshot(
        &github.fetch(&format!("repos/{repository}/pulls/{number}"))?,
        repository,
        number,
    )?;
    // The runner owns this private file; PR files never supply it.
    let mut options = std::fs::OpenOptions::new();
    options.write(true).create_new(true);
    #[cfg(unix)]
    std::os::unix::fs::OpenOptionsExt::mode(&mut options, 0o600);
    let mut file = options.open(path).map_err(|e| e.to_string())?;
    std::io::Write::write_all(&mut file, pyjson::dumps(&value, Dump::default()).as_bytes())
        .map_err(|e| e.to_string())?;
    drop(file);
    github.post(
        value.get("head_sha").and_then(Json::as_str).unwrap(),
        "pending",
    )
}

fn read_json(path: &Path) -> Result<Json, String> {
    // Every refusal ends in the same status line, so the cause is not kept.
    let text = py::read_text(path).map_err(|_| "unreadable file".to_owned())?;
    pyjson::loads(&text).map_err(|e| e.0)
}

pub fn finish(
    repository: &str,
    number: &num_bigint::BigInt,
    path: &Path,
    result_path: &Path,
    job_status: &str,
    github: &mut dyn GitHub,
) -> Result<bool, String> {
    let value = read_json(path)?;
    let Some(head) = text(value.get("head_sha")).filter(|h| full(SHA, h)) else {
        return Err("unreadable owned snapshot".into());
    };
    let head = head.to_owned();
    // Close an owned pending status even when the scan or freshness read failed.
    let clean = (|| -> Result<bool, String> {
        let result = read_json(result_path)?;
        let current = snapshot(
            &github.fetch(&format!("repos/{repository}/pulls/{number}"))?,
            repository,
            number,
        )?;
        Ok(job_status == "success"
            && current.py_eq(&value)
            && matches!(result, Json::Object(_))
            && result.get("clean") == Some(&Json::Bool(true))
            && result
                .get("head_sha")
                .is_some_and(|h| h.py_eq(&Json::str_obj(&head)))
            && result
                .get("fingerprint")
                .is_some_and(|f| f.py_eq(&Json::Str(fingerprint(&value)))))
    })()
    .unwrap_or(false);
    github.post(&head, if clean { "success" } else { "failure" })?;
    Ok(clean)
}

/// The `gh` command line, which holds the status token.
pub struct GhCli {
    pub repository: String,
    pub run_url: String,
}

impl GitHub for GhCli {
    fn fetch(&mut self, endpoint: &str) -> Result<Json, String> {
        let out = std::process::Command::new("gh")
            .args(["api", endpoint])
            .output()
            .map_err(|e| e.to_string())?;
        if !out.status.success() {
            return Err("GitHub read failed".into());
        }
        pyjson::loads_bytes(&out.stdout).map_err(|_| "GitHub response was unreadable".into())
    }

    fn post(&mut self, head: &str, state: &str) -> Result<(), String> {
        let out = std::process::Command::new("gh")
            .args([
                "api",
                &format!("repos/{}/statuses/{head}", self.repository),
                "--method",
                "POST",
                "-f",
                &format!("state={state}"),
                "-f",
                "context=Attribution policy",
                "-f",
                "description=PR snapshot and attribution validation",
                "-f",
                &format!("target_url={}", self.run_url),
                "--silent",
            ])
            .output()
            .map_err(|e| e.to_string())?;
        if !out.status.success() {
            return Err("GitHub status write failed".into());
        }
        Ok(())
    }
}
