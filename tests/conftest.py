"""Pytest policy: unit and fixture tests are offline by default."""

from __future__ import annotations

import os

import pytest


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    """Skip explicit network tests unless the integration opt-in is present."""

    del config
    if os.environ.get("DESINFECT_ALLOW_NETWORK_TESTS") == "1":
        return
    marker = pytest.mark.skip(reason="network test requires DESINFECT_ALLOW_NETWORK_TESTS=1")
    for item in items:
        if "network" in item.keywords:
            item.add_marker(marker)
