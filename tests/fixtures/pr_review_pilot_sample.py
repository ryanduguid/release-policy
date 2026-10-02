"""Temporary synthetic input for the advisory review pilot."""


def reciprocal(value):
    """Return None for zero; otherwise return the reciprocal."""
    if value == 0:
        return None
    return 1 / value


if __name__ == "__main__":
    assert reciprocal(0) is None
    assert reciprocal(2) == 0.5
    assert reciprocal(-4) == -0.25
