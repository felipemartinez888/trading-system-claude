"""Local pytest configuration for the research statistics tests."""

from __future__ import annotations

import pytest


def pytest_configure(config: pytest.Config) -> None:
    # registered here so ``--strict-markers`` accepts it; slow tests still run by default (CI)
    config.addinivalue_line(
        "markers", "slow: long-running acceptance simulations (run in CI; -m 'not slow' locally)"
    )
