"""Temporary synthetic control for advisory findings publication."""

import unittest


def reciprocal(value: float) -> float | None:
    """Return None for zero; otherwise return the reciprocal."""
    return 1 / value


class ReciprocalTests(unittest.TestCase):
    def test_nonzero_values(self):
        self.assertEqual(reciprocal(2), 0.5)
        self.assertEqual(reciprocal(-4), -0.25)
