//! The fuzz target's checks, shared by the cargo-fuzz target and the tests
//! that replay its seed corpus.
//!
//! Fabricated bytes go to the pure release selectors: release-tag identity,
//! required-check selectors and relative-path parsing. Each must refuse the
//! input or meet its round-trip, duplicate and containment invariants; the
//! error says which invariant failed.

use crate::release::{ReleaseTag, derive_release_tag, parse_release_tag, safe_relative_path};
use crate::required_checks::{RequiredCheck, parse_required_checks};

/// The parsers under test, replaceable so the tests can prove each check bites.
pub struct Parsers {
    pub derive: fn(&str, &str) -> Result<ReleaseTag, String>,
    pub parse: fn(&str, &str) -> Result<ReleaseTag, String>,
    pub checks: fn(&str) -> Result<Vec<RequiredCheck>, String>,
    pub relative: fn(&str) -> Result<String, String>,
}

impl Default for Parsers {
    fn default() -> Self {
        Parsers {
            derive: derive_release_tag,
            parse: parse_release_tag,
            checks: parse_required_checks,
            relative: safe_relative_path,
        }
    }
}

/// Check one input against the real parsers.
pub fn check_input(data: &[u8]) -> Result<(), String> {
    check_with(data, &Parsers::default())
}

pub fn check_with(data: &[u8], parsers: &Parsers) -> Result<(), String> {
    let text = String::from_utf8_lossy(data);
    let (value, prefix) = text.split_once('\0').unwrap_or((&text, ""));

    if let Ok(tag) = (parsers.derive)(value, prefix) {
        if (parsers.parse)(&tag.full_tag, prefix).as_ref() != Ok(&tag) {
            return Err("a derived tag does not parse back to itself".into());
        }
        if tag.artifact_tag.contains('/') {
            return Err("an artifact tag contains a slash".into());
        }
    }

    if let Ok(tag) = (parsers.parse)(value, prefix) {
        if tag.full_tag != value {
            return Err("a parsed tag does not name its input".into());
        }
        if (parsers.derive)(&tag.version, prefix).as_ref() != Ok(&tag) {
            return Err("a parsed tag does not derive back to itself".into());
        }
    }

    if let Ok(checks) = (parsers.checks)(&text) {
        if checks.is_empty() {
            return Err("an empty required-check list was accepted".into());
        }
        if checks
            .iter()
            .enumerate()
            .any(|(i, c)| checks[..i].contains(c))
        {
            return Err("a required-check list repeats a check".into());
        }
        let labels = checks.iter().map(RequiredCheck::label).collect::<Vec<_>>();
        if (parsers.checks)(&labels.join("\n")).as_ref() != Ok(&checks) {
            return Err("required checks do not round-trip through their labels".into());
        }
        let duplicate = format!("{}\n{}", labels[0], labels[0]);
        if (parsers.checks)(&duplicate).is_ok() {
            return Err("duplicate required checks were accepted".into());
        }
    }

    if let Ok(relative) = (parsers.relative)(&text) {
        if relative.starts_with('/') {
            return Err("a safe relative path is absolute".into());
        }
        if relative.split('/').any(|part| part == "..") {
            return Err("a safe relative path climbs out".into());
        }
        if relative.contains('\\') {
            return Err("a safe relative path holds a backslash".into());
        }
        if (parsers.relative)(&relative).as_ref() != Ok(&relative) {
            return Err("a safe relative path is not stable".into());
        }
    }
    Ok(())
}
