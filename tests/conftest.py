"""Pytest policy: unit and fixture tests are offline by default."""

from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path

import pytest

from scripts.rki_pipeline import rights
from scripts.rki_pipeline.storage.base import RightsStorageAuthorizer


@dataclass(frozen=True)
class StorageRightsHarness:
    register_path: Path
    authorizer: RightsStorageAuthorizer

    def set_decisions(
        self,
        *decisions: tuple[str, str, str],
    ) -> dict[tuple[str, str], str]:
        entries = "".join(
            f'''  - source_id: "{source_id}"
    source_sha256: "{source_sha256}"
    state: "{state}"
    basis: "Reviewed RKI reuse terms"
    reviewed_by: "Legal Reviewer"
    reviewed_at: "2026-08-03T08:00:00Z"
'''
            for source_id, source_sha256, state in decisions
        )
        self.register_path.write_text(
            "schema_version: 1\ndecisions:\n" + (entries or "  []\n"),
            encoding="utf-8",
        )
        register = rights.load_rights_register(self.register_path)
        return {
            (decision.source_id, decision.source_sha256): decision.decision_sha256
            for decision in register.entries
            if decision.decision_sha256 is not None
        }


@pytest.fixture
def storage_rights(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> StorageRightsHarness:
    register_path = tmp_path / "storage-rights-register.yml"
    register_path.write_text(
        "schema_version: 1\n"
        "decisions:\n"
        '  - source_id: "rki:176904/12345.2"\n'
        f'    source_sha256: "{"b" * 64}"\n'
        '    state: "approved"\n'
        '    basis: "Reviewed RKI reuse terms"\n'
        '    reviewed_by: "Legal Reviewer"\n'
        '    reviewed_at: "2026-08-03T08:00:00Z"\n',
        encoding="utf-8",
    )
    monkeypatch.setattr(rights, "DEFAULT_REGISTER_PATH", register_path)
    return StorageRightsHarness(
        register_path=register_path,
        authorizer=RightsStorageAuthorizer(
            authority=rights.load_rights_authority(),
            policy=rights.load_rights_policy(),
        ),
    )


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    """Skip explicit network tests unless the integration opt-in is present."""

    del config
    if os.environ.get("DESINFECT_ALLOW_NETWORK_TESTS") == "1":
        return
    marker = pytest.mark.skip(reason="network test requires DESINFECT_ALLOW_NETWORK_TESTS=1")
    for item in items:
        if "network" in item.keywords:
            item.add_marker(marker)
