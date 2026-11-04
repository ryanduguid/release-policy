// Copyright 2026 Ryan Duguid
// SPDX-License-Identifier: FSL-1.1-ALv2

//! Fabricated bytes to the release-tag, required-check and relative-path parsers.
#![no_main]

libfuzzer_sys::fuzz_target!(|data: &[u8]| {
    if let Err(problem) = release_policy::fuzz::check_input(data) {
        panic!("{problem}");
    }
});
