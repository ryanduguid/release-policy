//! The fuzz target's invariants on its seed corpus, and proof that each one
//! fails when a parser misbehaves. Ported from tests/test_fuzz_harness.py.

use release_policy::fuzz::{Parsers, check_input, check_with};
use release_policy::release::ReleaseTag;
use std::path::Path;

#[test]
fn seed_corpus() {
    let corpus = Path::new(env!("CARGO_MANIFEST_DIR")).join("fuzz/corpus");
    let mut seeds = std::fs::read_dir(&corpus)
        .unwrap()
        .map(|e| e.unwrap().path())
        .filter(|p| p.is_file())
        .collect::<Vec<_>>();
    seeds.sort();
    assert!(!seeds.is_empty());
    for seed in seeds {
        check_input(&std::fs::read(&seed).unwrap())
            .unwrap_or_else(|e| panic!("{}: {e}", seed.display()));
    }
}

#[test]
fn unexpected_parser_errors_escape() {
    // A parser that panics is not caught: the fuzzer reports the crash.
    fn broken(_: &str, _: &str) -> Result<ReleaseTag, String> {
        panic!("bug")
    }
    let parsers = Parsers {
        derive: broken,
        ..Parsers::default()
    };
    let outcome = std::panic::catch_unwind(|| check_with(b"1.2.3", &parsers));
    assert!(outcome.is_err());
}

#[test]
fn inconsistent_tag_round_trip_is_a_failure() {
    fn nothing(_: &str, _: &str) -> Result<ReleaseTag, String> {
        Ok(ReleaseTag {
            version: String::new(),
            version_tag: String::new(),
            full_tag: String::new(),
            artifact_tag: String::new(),
            tag_prefix: String::new(),
        })
    }
    let parsers = Parsers {
        parse: nothing,
        ..Parsers::default()
    };
    assert!(check_with(b"1.2.3", &parsers).is_err());
}

#[test]
fn duplicate_check_acceptance_is_a_failure() {
    fn first_line(
        text: &str,
    ) -> Result<Vec<release_policy::required_checks::RequiredCheck>, String> {
        release_policy::required_checks::parse_required_checks(text.lines().next().unwrap_or(""))
    }
    let parsers = Parsers {
        checks: first_line,
        ..Parsers::default()
    };
    let error = check_with(b".github/workflows/ci.yml: lint", &parsers).unwrap_err();
    assert!(error.contains("duplicate required checks"), "{error}");
}
