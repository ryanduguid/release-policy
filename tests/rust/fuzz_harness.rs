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

fn tag(full_tag: &str, version: &str, artifact_tag: &str) -> ReleaseTag {
    ReleaseTag {
        version: version.into(),
        version_tag: format!("v{version}"),
        full_tag: full_tag.into(),
        artifact_tag: artifact_tag.into(),
        tag_prefix: String::new(),
    }
}

#[test]
fn a_slash_in_an_artifact_tag_is_a_failure() {
    fn slashed(_: &str, _: &str) -> Result<ReleaseTag, String> {
        Ok(tag("v1.2.3", "1.2.3", "a/v1.2.3"))
    }
    let parsers = Parsers {
        derive: slashed,
        parse: slashed,
        ..Parsers::default()
    };
    assert_eq!(
        check_with(b"1.2.3", &parsers),
        Err("an artifact tag contains a slash".into())
    );
}

#[test]
fn a_parsed_tag_must_name_its_input_and_derive_back() {
    // "v1.2.3" is no version, so only the parse arm runs.
    fn elsewhere(_: &str, _: &str) -> Result<ReleaseTag, String> {
        Ok(tag("v9.9.9", "9.9.9", "v9.9.9"))
    }
    fn echo(value: &str, _: &str) -> Result<ReleaseTag, String> {
        Ok(tag(value, "9.9.9", value))
    }
    for (parse, expected) in [
        (
            elsewhere as fn(&str, &str) -> Result<ReleaseTag, String>,
            "a parsed tag does not name its input",
        ),
        (echo, "a parsed tag does not derive back to itself"),
    ] {
        let parsers = Parsers {
            parse,
            ..Parsers::default()
        };
        assert_eq!(check_with(b"v1.2.3", &parsers), Err(expected.into()));
    }
}

#[test]
fn required_check_lists_must_be_non_empty_unique_and_round_trip() {
    use release_policy::required_checks::RequiredCheck;
    type Checks = fn(&str) -> Result<Vec<RequiredCheck>, String>;
    fn check(workflow: &str) -> RequiredCheck {
        RequiredCheck {
            workflow: workflow.into(),
            job: "lint".into(),
        }
    }
    fn none(_: &str) -> Result<Vec<RequiredCheck>, String> {
        Ok(Vec::new())
    }
    fn twice(_: &str) -> Result<Vec<RequiredCheck>, String> {
        Ok(vec![check(".github/workflows/ci.yml"); 2])
    }
    // Each reading names a new workflow, so the labels never read back the same.
    fn drifting(text: &str) -> Result<Vec<RequiredCheck>, String> {
        Ok(vec![check(text)])
    }
    for (checks, expected) in [
        (none as Checks, "an empty required-check list was accepted"),
        (twice, "a required-check list repeats a check"),
        (
            drifting,
            "required checks do not round-trip through their labels",
        ),
    ] {
        let parsers = Parsers {
            checks,
            ..Parsers::default()
        };
        assert_eq!(check_with(b"anything", &parsers), Err(expected.into()));
    }
}

#[test]
fn safe_relative_paths_must_stay_relative_contained_and_stable() {
    type Relative = fn(&str) -> Result<String, String>;
    fn absolute(_: &str) -> Result<String, String> {
        Ok("/etc/passwd".into())
    }
    fn climbing(_: &str) -> Result<String, String> {
        Ok("a/../../b".into())
    }
    fn backslashed(_: &str) -> Result<String, String> {
        Ok(r"a\b".into())
    }
    fn growing(text: &str) -> Result<String, String> {
        Ok(format!("{text}x"))
    }
    for (relative, expected) in [
        (absolute as Relative, "a safe relative path is absolute"),
        (climbing, "a safe relative path climbs out"),
        (backslashed, "a safe relative path holds a backslash"),
        (growing, "a safe relative path is not stable"),
    ] {
        let parsers = Parsers {
            relative,
            ..Parsers::default()
        };
        assert_eq!(check_with(b"anything", &parsers), Err(expected.into()));
    }
}
