"""Smoke test: the package imports and exposes a version string."""

from __future__ import annotations

import re

import agri.core


def test_version_is_semver_like() -> None:
    assert isinstance(agri.core.__version__, str)
    assert re.match(r"^\d+\.\d+\.\d+", agri.core.__version__)
