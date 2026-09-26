from __future__ import annotations

import unittest
from pathlib import Path
from unittest import mock

from fuzz import policy_parsers

CORPUS = Path(__file__).resolve().parents[1] / "fuzz" / "corpus"


class FuzzHarnessTests(unittest.TestCase):
    def test_seed_corpus(self) -> None:
        seeds = sorted(CORPUS.iterdir())
        self.assertGreater(len(seeds), 0)
        for seed in seeds:
            with self.subTest(seed=seed.name):
                policy_parsers.test_one_input(seed.read_bytes())

    def test_unexpected_parser_errors_escape(self) -> None:
        with mock.patch.object(policy_parsers, "derive_release_tag", side_effect=RuntimeError("bug")):
            with self.assertRaisesRegex(RuntimeError, "bug"):
                policy_parsers.test_one_input(b"1.2.3")

    def test_inconsistent_tag_round_trip_is_a_failure(self) -> None:
        with mock.patch.object(policy_parsers, "parse_release_tag", return_value=None):
            with self.assertRaises(AssertionError):
                policy_parsers.test_one_input(b"1.2.3")

    def test_duplicate_check_acceptance_is_a_failure(self) -> None:
        parser = policy_parsers.parse_required_checks

        def accept_duplicate(text: str):
            return parser(text.splitlines()[0])

        with mock.patch.object(policy_parsers, "parse_required_checks", side_effect=accept_duplicate):
            with self.assertRaisesRegex(AssertionError, "duplicate required checks"):
                policy_parsers.test_one_input(b".github/workflows/ci.yml: lint")
