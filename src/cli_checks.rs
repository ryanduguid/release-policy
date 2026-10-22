use clap::Args;
use release_policy::{Result, common::*, egress, py, required_checks as rc};
use std::io::Read;
use std::path::PathBuf;

#[derive(Args)]
pub struct EgressArgs {
    #[arg(required=true,num_args=1..)]
    paths: Vec<PathBuf>,
    #[arg(long)]
    terms: Option<PathBuf>,
    #[arg(long)]
    no_terms: bool,
    #[arg(long)]
    allow_structural: bool,
    #[arg(long)]
    allow_unscanned: bool,
    #[arg(long)]
    exclude: Vec<String>,
}
impl EgressArgs {
    pub fn execute(self) -> i32 {
        let result = (|| {
            if self.terms.is_some() && self.no_terms {
                return Err("--terms and --no-terms contradict each other".into());
            }
            if self.terms.is_none() && !self.no_terms {
                return Err("layer A needs a terms file. Pass --terms <file>, or --no-terms to publish with only the structural and reserved-namespace layers checked. Refusing to imply a clean result from an absent policy".into());
            }
            let terms = self
                .terms
                .as_ref()
                .map(|p| egress::load_terms(p))
                .transpose()?
                .unwrap_or_default();
            let label = self
                .terms
                .as_ref()
                .map(|p| p.to_string_lossy().into_owned())
                .unwrap_or("the terms file".into());
            egress::check_candidate(&self.paths, &terms, &self.exclude, &label)
        })();
        let checked = match result {
            Ok(c) => c,
            Err(e) => {
                print(&format!("check_egress: EGRESS CHECK ERROR: {e}\n"), true);
                print(
                    "check_egress: nothing was checked; this candidate is unchecked, not clean\n",
                    false,
                );
                return 2;
            }
        };
        for path in &checked.excluded {
            print(
                &format!(
                    "check_egress: excluded by --exclude, not checked: {}\n",
                    py::as_posix(path)
                ),
                false,
            );
        }
        for path in &checked.unscanned {
            print(
                &format!(
                    "check_egress: not scanned as text: {}\n",
                    py::as_posix(path)
                ),
                false,
            );
        }
        for finding in &checked.findings {
            print(&format!("check_egress: {}\n", finding.render()), false);
        }
        let blocking = checked
            .findings
            .iter()
            .filter(|f| f.layer != 'B' || !self.allow_structural)
            .count();
        if blocking > 0 {
            print(
                &format!("check_egress: RESULT: {blocking} finding(s); do not publish\n"),
                false,
            );
            return 1;
        }
        if !checked.unscanned.is_empty() && !self.allow_unscanned {
            print(
                &format!(
                    "check_egress: RESULT: {} file(s) could not be read as text; they are unchecked, so this candidate is not cleared. Review them and pass --allow-unscanned to publish anyway\n",
                    checked.unscanned.len()
                ),
                false,
            );
            return 1;
        }
        let mut waved = Vec::new();
        if !checked.findings.is_empty() {
            waved.push(format!(
                "{} structural finding(s) waved through by --allow-structural",
                checked.findings.len()
            ));
        }
        if !checked.unscanned.is_empty() {
            waved.push(format!(
                "{} unscanned file(s) waved through by --allow-unscanned",
                checked.unscanned.len()
            ));
        }
        print(
            &format!(
                "check_egress: RESULT: clean{}\n",
                if waved.is_empty() {
                    String::new()
                } else {
                    format!(", with {}", waved.join(", and "))
                }
            ),
            false,
        );
        0
    }
}

#[derive(Args)]
pub struct RequiredArgs {
    #[arg(long)]
    repository: String,
    #[arg(long)]
    commit: String,
    #[arg(long, default_value = "0", value_parser = py_int, allow_hyphen_values = true)]
    wait_seconds: num_bigint::BigInt,
    #[arg(long, default_value = "30", value_parser = py_int, allow_hyphen_values = true)]
    poll_seconds: num_bigint::BigInt,
}
impl RequiredArgs {
    pub fn execute(self) -> i32 {
        let start = std::time::Instant::now();
        let outcome = rc::command(
            &self.repository,
            &self.commit,
            &self.wait_seconds,
            &self.poll_seconds,
            || {
                let mut input = String::new();
                std::io::stdin()
                    .read_to_string(&mut input)
                    .map_err(|e| e.to_string())?;
                Ok(py::universal_newlines(&input))
            },
            &mut rc::gh_json,
            || start.elapsed().as_secs_f64(),
            |s| std::thread::sleep(std::time::Duration::from_secs_f64(s)),
        );
        print(&outcome.stdout, false);
        print(&outcome.stderr, true);
        outcome.code
    }
}

/// Python `int()` for an integer option, as argparse's `type=int` reads it.
pub fn py_int(value: &str) -> Result<num_bigint::BigInt> {
    py::parse_int(value, 10)
}

#[derive(Args)]
pub struct AttributionArgs {
    #[arg(value_parser = ["prepare", "finish"])]
    operation: String,
    #[arg(long)]
    repository: String,
    #[arg(long, value_parser = py_int, allow_hyphen_values = true)]
    number: num_bigint::BigInt,
    #[arg(long)]
    snapshot: PathBuf,
    #[arg(long)]
    result: PathBuf,
    #[arg(long, default_value = "failure", allow_hyphen_values = true)]
    job_status: String,
    #[arg(long, allow_hyphen_values = true)]
    run_url: String,
}
impl AttributionArgs {
    pub fn execute(self) -> i32 {
        use release_policy::attribution;
        if !full(REPOSITORY, &self.repository) || self.number < 1.into() {
            print("Invalid policy identity\n", true);
            return 1;
        }
        let mut github = attribution::GhCli {
            repository: self.repository.clone(),
            run_url: self.run_url.clone(),
        };
        let outcome = if self.operation == "prepare" {
            attribution::prepare(&self.repository, &self.number, &self.snapshot, &mut github)
                .map(|()| true)
        } else {
            attribution::finish(
                &self.repository,
                &self.number,
                &self.snapshot,
                &self.result,
                &self.job_status,
                &mut github,
            )
        };
        match outcome {
            Ok(true) => 0,
            Ok(false) => {
                print("Attribution scan or PR freshness validation failed\n", true);
                1
            }
            Err(_) => {
                print("Attribution status validation could not complete\n", true);
                1
            }
        }
    }
}

#[derive(Args)]
pub struct ReviewsArgs {
    #[arg(long, default_value = ".")]
    root: PathBuf,
    /// a revision whose ledgers must survive unchanged, such as origin/main
    #[arg(long, allow_hyphen_values = true)]
    base: Option<String>,
}
impl ReviewsArgs {
    pub fn execute(self) -> i32 {
        match release_policy::check_reviews::check_consumer(&self.root, self.base.as_deref()) {
            Ok(summary) => {
                print(&format!("{summary}\n"), false);
                0
            }
            Err(error) => {
                print(&format!("check_reviews: {error}\n"), true);
                1
            }
        }
    }
}

#[derive(Args)]
pub struct CanariesArgs {
    #[arg(long, default_value = "canaries.json")]
    manifest: PathBuf,
    #[arg(long)]
    live: bool,
    #[arg(long)]
    require_current_evidence: bool,
}
impl CanariesArgs {
    pub fn execute(self) -> i32 {
        use release_policy::check_canaries as cc;
        let canaries = match cc::load_manifest(&self.manifest) {
            Ok(canaries) => canaries,
            Err(error) => {
                print(&format!("check_canaries: {error}\n"), true);
                return 1;
            }
        };
        if !self.live {
            print(
                &format!(
                    "canary manifest valid: {} release families\n",
                    canaries.len()
                ),
                false,
            );
            return 0;
        }
        let result = match cc::check_live(&canaries, &mut cc::GhCli, true, chrono::Utc::now()) {
            Ok(result) => result,
            Err(error) => {
                print(
                    &format!("check_canaries: unexpected error: {error}\n"),
                    true,
                );
                return 1;
            }
        };
        let (text, code) = cc::render(&result, canaries.len(), self.require_current_evidence);
        print(&text, false);
        code
    }
}
