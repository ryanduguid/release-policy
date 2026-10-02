"""Temporary synthetic regression control for the advisory review pilot."""

import unittest


def reciprocal(value: float) -> float | None:
    """Return None for zero; otherwise return the reciprocal."""
    if value == 0:
        return None
    return 1 / value


class ReciprocalTests(unittest.TestCase):
    def test_zero_and_nonzero_values(self):
        self.assertIsNone(reciprocal(0))
        self.assertEqual(reciprocal(2), 0.5)
        self.assertEqual(reciprocal(-4), -0.25)
