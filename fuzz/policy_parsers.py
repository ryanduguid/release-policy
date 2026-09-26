"""Fuzz pure release selectors without reading files or calling GitHub."""

from __future__ import annotations

import sys

from scripts.python_release import _safe_relative_path, derive_release_tag, parse_release_tag
from scripts.required_checks import parse_required_checks


def test_one_input(data: bytes) -> None:
    text = data.decode("utf-8", errors="replace")
    value, _, prefix = text.partition("\0")

    try:
        tag = derive_release_tag(value, prefix)
    except ValueError:
        pass
    else:
        assert parse_release_tag(tag.full_tag, prefix) == tag
        assert "/" not in tag.artifact_tag

    try:
        tag = parse_release_tag(value, prefix)
    except ValueError:
        pass
    else:
        assert tag.full_tag == value
        assert derive_release_tag(tag.version, prefix) == tag

    try:
        checks = parse_required_checks(text)
    except ValueError:
        pass
    else:
        assert checks
        assert len(set(checks)) == len(checks)
        assert parse_required_checks("\n".join(check.label for check in checks)) == checks
        duplicate = f"{checks[0].label}\n{checks[0].label}"
        try:
            parse_required_checks(duplicate)
        except ValueError:
            pass
        else:
            raise AssertionError("duplicate required checks were accepted")

    try:
        relative = _safe_relative_path(text)
    except ValueError:
        pass
    else:
        assert not relative.is_absolute()
        assert ".." not in relative.parts
        assert "\\" not in relative.as_posix()
        assert _safe_relative_path(relative.as_posix()) == relative


if __name__ == "__main__":
    import atheris

    atheris.instrument_all()
    atheris.Setup(sys.argv, test_one_input)
    atheris.Fuzz()
