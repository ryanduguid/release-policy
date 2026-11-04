//! Replays how the base Python tooling treated library-dependent input:
//! CPython's ast and literal_eval, tomllib, json, re, datetime and its Unicode
//! tables. tests/rust/data/python_goldens.json comes from
//! artifacts/rust-port-2026-10-11/release-policy/gen_goldens.py run against
//! the base revision.

use release_policy::check_canaries::{self, Failure};
use release_policy::egress::{self, Term};
use release_policy::pyjson::{self, Dump};
use release_policy::{check_reviews, pin_families, py, release, required_checks};
use serde_json::{Value, json};
use std::path::Path;

fn tag(t: release::ReleaseTag) -> Value {
    json!({"version": t.version, "version_tag": t.version_tag, "full_tag": t.full_tag,
           "artifact_tag": t.artifact_tag, "tag_prefix": t.tag_prefix})
}

fn outcome<T: Into<Value>>(result: Result<T, String>) -> Value {
    match result {
        Ok(value) => json!({"ok": value.into()}),
        Err(error) => json!({"error": error}),
    }
}

fn probe(case: &Value) -> Value {
    let s = |key: &str| case[key].as_str().unwrap();
    match s("kind") {
        "literal" => outcome(release::python_literal_version(s("text"))),
        "project-name" => outcome(release::project_name(s("text"))),
        "pyproject-version" => outcome(release::pyproject_version(s("text"))),
        "wheel-stem" => outcome(release::wheel_stem(s("name"))),
        "derive-tag" => outcome(release::derive_release_tag(s("version"), s("prefix")).map(tag)),
        "parse-tag" => outcome(release::parse_release_tag(s("tag"), s("prefix")).map(tag)),
        "safe-path" => outcome(release::safe_relative_path(s("path"))),
        "checks" => outcome(
            required_checks::parse_required_checks(s("text")).map(|checks| {
                Value::from(
                    checks
                        .into_iter()
                        .map(|c| json!([c.workflow, c.job]))
                        .collect::<Vec<_>>(),
                )
            }),
        ),
        "isodate" => outcome(check_reviews::date(s("value"), "x")),
        "started-at" => outcome(
            check_canaries::parse_started_at(s("value"))
                .map(|t| t.format("%Y-%m-%dT%H:%M:%S+00:00").to_string())
                .map_err(|f| match f {
                    Failure::Runtime(m) | Failure::Unexpected(m) => m,
                }),
        ),
        "egress" => {
            let terms = case["terms"]
                .as_array()
                .unwrap()
                .iter()
                .enumerate()
                .map(|(i, t)| Term {
                    line: i + 1,
                    text: t.as_str().unwrap().into(),
                })
                .collect::<Vec<_>>();
            outcome(
                egress::scan_text(
                    s("text"),
                    "fixture.md",
                    &terms,
                    "terms.txt",
                    &egress::STRUCTURAL_PATTERNS,
                )
                .map(|found| {
                    Value::from(
                        found
                            .iter()
                            .map(egress::Finding::render)
                            .collect::<Vec<_>>(),
                    )
                }),
            )
        }
        "json" => outcome(pyjson::loads(s("text")).map_err(|e| e.0).map(|value| {
            let indented = Dump {
                indent: Some(2),
                sort_keys: true,
                ensure_ascii: true,
            };
            format!(
                "{}|{}",
                pyjson::dumps(&value, indented),
                pyjson::dumps(&value, Dump::default())
            )
        })),
        "pin-scan" => json!({"ok": pin_families::scan(s("text"))}),
        "unicode" => {
            let c = s("char").chars().next().unwrap();
            let lower: String = c.to_lowercase().collect();
            let padded = format!(" {c}x{c} ");
            json!({"ok": [py::is_space(c), py::is_decimal(c), lower, py::casefold(&c.to_string()),
                          py::repr_str(&c.to_string()), py::strip(&padded)]})
        }
        other => panic!("unknown kind {other}"),
    }
}

/// The documented divergence, where there is one: a pyproject.toml number
/// outside the 64-bit range, or a float that overflows to infinity, makes the
/// TOML unreadable here, while tomllib held it as a Python int or inf.
fn documented(case: &Value) -> Option<Value> {
    let text = case["text"].as_str()?;
    let out_of_range = text.contains("99999999999999999999") || text.contains("1e1000");
    match case["kind"].as_str()? {
        "project-name" if out_of_range => {
            Some(json!({"error": "pyproject.toml must declare [project].name"}))
        }
        "pyproject-version" if out_of_range => {
            Some(json!({"error": "pyproject parser requires [project].version"}))
        }
        _ => None,
    }
}

#[test]
fn library_dependent_behaviour_matches_the_base_python() {
    let path = Path::new(env!("CARGO_MANIFEST_DIR")).join("tests/rust/data/python_goldens.json");
    let cases: Vec<Value> = serde_json::from_slice(&std::fs::read(path).unwrap()).unwrap();
    let failures = cases
        .iter()
        .filter(|case| probe(case) != documented(case).unwrap_or_else(|| case["expected"].clone()))
        .map(|case| format!("{case}\n  rust: {}", probe(case)))
        .collect::<Vec<_>>();
    assert!(
        failures.is_empty(),
        "{} of {} differ:\n{}",
        failures.len(),
        cases.len(),
        failures[..failures.len().min(25)].join("\n")
    );
}
