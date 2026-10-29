use clap::{Args, Parser, Subcommand};
use release_policy::{Result, common::*, pin_families, release::*, verify_skills};
use std::path::PathBuf;
mod cli_checks;

#[derive(Parser)]
#[command(version, about = "Release and review policy tooling")]
struct Cli {
    #[command(subcommand)]
    command: Command,
}

#[derive(Subcommand)]
enum Command {
    /// Bind attribution statuses to one current PR snapshot and completed scan (scripts/attribution_status.py)
    #[command(name = "attribution-status", alias = "attribution_status")]
    AttributionStatus(cli_checks::AttributionArgs),
    /// Check a consumer's review index, verdict files and known-unknowns register (scripts/check_reviews.py)
    #[command(name = "check-reviews", alias = "check_reviews")]
    CheckReviews(cli_checks::ReviewsArgs),
    /// Validate release-family canary pins and live successful-run evidence (scripts/check_canaries.py)
    #[command(name = "check-canaries", alias = "check_canaries")]
    CheckCanaries(cli_checks::CanariesArgs),
    /// Check a publication candidate for content that must not leave private hands (scripts/check_egress.py)
    #[command(name = "check-egress", alias = "check_egress")]
    CheckEgress(cli_checks::EgressArgs),
    /// Require the consumer's mandatory checks to have succeeded for the exact release commit (scripts/required_checks.py)
    #[command(name = "required-checks", alias = "required_checks")]
    RequiredChecks(cli_checks::RequiredArgs),
    /// Build deterministic source-release archives from tracked Git content (scripts/build_release_archives.py)
    #[command(name = "build-release-archives", alias = "build_release_archives")]
    BuildReleaseArchives {
        #[arg(long)]
        commit: String,
        #[arg(long)]
        prefix: String,
        #[arg(long, default_value = ".")]
        source_directory: String,
        #[arg(long)]
        output_base: PathBuf,
    },
    /// Closed metadata parsing and exact candidate inventory for Python releases (scripts/python_release.py)
    #[command(name = "python-release", alias = "python_release")]
    PythonRelease {
        #[command(subcommand)]
        command: Release,
    },
    /// Guard and verify skill releases (scripts/verify_skills.py)
    #[command(name = "verify-skills", alias = "verify_skills")]
    VerifySkills {
        #[command(subcommand)]
        command: Skills,
    },
    /// Name the pin families a release-policy change moves, so consumers re-pin only those (scripts/pin_families.py)
    #[command(
        name = "pin-families",
        alias = "pin_families",
        disable_help_flag = true
    )]
    PinFamilies {
        #[arg(num_args=0.., allow_hyphen_values=true)]
        revisions: Vec<String>,
    },
}

#[derive(Subcommand)]
enum Release {
    Tag {
        #[arg(long, allow_hyphen_values = true)]
        tag: String,
        #[arg(long)]
        tag_prefix: String,
        #[arg(long, default_value="json", value_parser=["json","lines"])]
        format: String,
    },
    ValidateInputs {
        #[arg(long, default_value = ".")]
        root: PathBuf,
        #[arg(long)]
        source_directory: String,
        #[arg(long)]
        tag_prefix: String,
        #[arg(long, default_value="json", value_parser=["json","lines"])]
        format: String,
    },
    Metadata {
        #[arg(long, default_value = ".")]
        root: PathBuf,
        #[arg(long, value_parser=["pyproject","python-literal"])]
        version_parser: String,
        #[arg(long)]
        version_file: String,
        #[arg(long, default_value="json", value_parser=["json","lines"])]
        format: String,
    },
    CreateCandidate(CandidateArgs),
    VerifyCandidate(CandidateArgs),
}

#[derive(Args)]
struct CandidateArgs {
    #[arg(long)]
    directory: PathBuf,
    #[arg(long)]
    stem: String,
    #[arg(long)]
    version: String,
    #[arg(long)]
    tag: String,
    #[arg(long)]
    repository: String,
    #[arg(long)]
    commit: String,
    #[arg(long)]
    policy_sha: String,
    #[arg(long, value_parser = cli_checks::py_int, allow_hyphen_values = true)]
    run_id: num_bigint::BigInt,
    #[arg(long, value_parser = cli_checks::py_int, allow_hyphen_values = true)]
    run_attempt: num_bigint::BigInt,
}
impl CandidateArgs {
    fn context(&self) -> CandidateContext {
        CandidateContext {
            stem: self.stem.clone(),
            version: self.version.clone(),
            tag: self.tag.clone(),
            repository: self.repository.clone(),
            commit: self.commit.clone(),
            policy_sha: self.policy_sha.clone(),
            run_id: self.run_id.clone(),
            run_attempt: self.run_attempt.clone(),
        }
    }
}

#[derive(Subcommand)]
enum Skills {
    GuardRelease {
        #[arg(long)]
        mode: String,
        #[arg(long)]
        tag: String,
    },
    Verify {
        #[arg(long)]
        mode: String,
        #[arg(long, default_value = "VERSION")]
        version_file: String,
    },
}

fn output(value: &serde_json::Value, lines: &[&str], format: &str) {
    let text = if format == "lines" {
        lines.join("\n")
    } else {
        dumps(value, false, true, true, false)
    };
    print(&format!("{text}\n"), false);
}

fn execute(cli: Cli) -> Result<i32> {
    let cwd = std::env::current_dir().map_err(|e| e.to_string())?;
    match cli.command {
        Command::AttributionStatus(args) => return Ok(args.execute()),
        Command::CheckReviews(args) => return Ok(args.execute()),
        Command::CheckCanaries(args) => return Ok(args.execute()),
        Command::CheckEgress(args) => return Ok(args.execute()),
        Command::RequiredChecks(args) => return Ok(args.execute()),
        Command::BuildReleaseArchives {
            commit,
            prefix,
            source_directory,
            output_base,
        } => {
            release_policy::archives::build_release_archives(
                &cwd,
                &commit,
                &prefix,
                &output_base,
                &source_directory,
            )?;
        }
        Command::PythonRelease { command } => match command {
            Release::Tag {
                tag,
                tag_prefix,
                format,
            } => {
                let t = parse_release_tag(&tag, &tag_prefix)?;
                output(
                    &serde_json::json!({"artifact_tag":t.artifact_tag,"full_tag":t.full_tag,"tag_prefix":t.tag_prefix,"version":t.version,"version_tag":t.version_tag}),
                    &[&t.version, &t.version_tag, &t.full_tag, &t.artifact_tag],
                    &format,
                );
            }
            Release::ValidateInputs {
                root,
                source_directory,
                tag_prefix,
                format,
            } => {
                let directory = validate_source_directory(&root, &source_directory)?;
                validate_tag_prefix(&tag_prefix)?;
                output(
                    &serde_json::json!({"source_directory":directory,"tag_prefix":tag_prefix}),
                    &[&directory, &tag_prefix],
                    &format,
                );
            }
            Release::Metadata {
                root,
                version_parser,
                version_file,
                format,
            } => {
                let v = derive_metadata(&root, &version_parser, &version_file)?;
                output(
                    &v,
                    &[v["stem"].as_str().unwrap(), v["version"].as_str().unwrap()],
                    &format,
                );
            }
            Release::CreateCandidate(a) => {
                write_candidate_inventory(&a.directory, &a.context())?;
            }
            Release::VerifyCandidate(a) => {
                verify_candidate_inventory(&a.directory, &a.context())?;
            }
        },
        Command::VerifySkills { command } => {
            let result = match command {
                Skills::GuardRelease { mode, tag } => {
                    verify_skills::guard_skill_release(&mode, &tag)
                }
                Skills::Verify { mode, version_file } => verify_skills::verify_with(
                    &cwd,
                    &mode,
                    &version_file,
                    // The script ran its steps with its own interpreter; the
                    // binary uses the `python` the caller put first on PATH.
                    &verify_skills::python_on_path(),
                    |args, root| {
                        // Consumer Python test programs remain consumer-owned executables.
                        let status = std::process::Command::new(&args[0])
                            .args(&args[1..])
                            .current_dir(root)
                            .status()
                            .map_err(|e| e.to_string())?;
                        if !status.success() {
                            let command = args
                                .iter()
                                .map(|a| release_policy::py::repr_str(a))
                                .collect::<Vec<_>>()
                                .join(", ");
                            return Err(format!(
                                "Command '({command})' returned non-zero exit status {}.",
                                status.code().unwrap_or(1)
                            ));
                        }
                        Ok(())
                    },
                ),
            };
            if let Err(e) = result {
                print(&format!("verify_skills: {e}\n"), true);
                return Ok(1);
            }
        }
        Command::PinFamilies { revisions } => {
            if revisions.len() != 2 {
                print(&format!("{}\n", pin_families::USAGE), true);
                return Ok(2);
            }
            match pin_families::changed(&revisions[0], &revisions[1], &cwd) {
                Ok(families) => print(&pin_families::report(&families), false),
                Err(e) => {
                    print(&format!("pin_families: {e}\n"), true);
                    return Ok(1);
                }
            }
        }
    }
    Ok(0)
}

fn main() {
    // The compatibility readers can accept nested Python-sized data. Execute and
    // destroy it on the same explicitly sized stack on Windows and Linux.
    let result = std::thread::Builder::new()
        .stack_size(32 * 1024 * 1024)
        .spawn(|| match Cli::try_parse() {
            Ok(cli) => execute(cli),
            Err(usage) => Ok(print_clap(&usage)),
        })
        .unwrap()
        .join();
    let code = match result {
        Ok(Ok(code)) => code,
        Ok(Err(error)) => {
            print(&format!("release-policy: {error}\n"), true);
            1
        }
        Err(_) => 1,
    };
    std::process::exit(shutdown(code));
}

/// Help, version or a usage error, written as argparse wrote its messages:
/// through the buffered streams, ignoring write errors. A terminal keeps
/// clap's colours, and a write clap fails there stays pending as argparse's did.
fn print_clap(usage: &clap::Error) -> i32 {
    use std::io::IsTerminal;
    let terminal = if usage.use_stderr() {
        std::io::stderr().is_terminal()
    } else {
        std::io::stdout().is_terminal()
    };
    if terminal {
        if let Err(error) = usage.print() {
            usage_failed(error, usage.use_stderr());
        }
    } else {
        print_usage(&usage.render().to_string(), usage.use_stderr());
    }
    usage.exit_code()
}
