//! Resolve caller-supplied revisions to immutable Git commit identities.

use crate::{
    Result,
    common::{full, run},
    py,
};
use std::path::Path;

/// Peel one revision safely; subsequent Git operations use only the full SHA.
pub fn resolve_commit(root: &Path, revision: &str) -> Result<String> {
    let result = run(
        root,
        "git",
        &[
            "rev-parse",
            "--verify",
            "--end-of-options",
            &format!("{revision}^{{commit}}"),
        ],
    )?;
    commit_from_output(result.status.success(), &result.stdout, revision)
}

/// The commit `git rev-parse` reported, refusing anything but one full SHA.
pub fn commit_from_output(success: bool, stdout: &[u8], revision: &str) -> Result<String> {
    let text = py::decode_utf8(stdout).map_err(|e| e.to_string())?;
    let sha = py::strip(&text);
    if !success || !full("[0-9a-f]{40}|[0-9a-f]{64}", sha) {
        return Err(format!(
            "revision is not a commit in this checkout: {revision}"
        ));
    }
    Ok(sha.to_owned())
}
