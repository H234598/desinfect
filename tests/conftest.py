"""Pytest policy: unit and fixture tests are offline by default."""

from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path

import pytest
import yaml

from scripts.rki_pipeline import rights
from scripts.rki_pipeline.storage.base import RightsStorageAuthorizer


def _write_rights_register(
    path: Path,
    *decisions: tuple[str, str, str],
) -> None:
    entries: list[dict[str, object]] = []
    for source_id, source_sha256, state in decisions:
        handle = source_id.removeprefix("rki:")
        canonical_url = f"https://edoc.rki.de/bitstream/handle/{handle}/source.pdf?sequence=1"
        approved = state == "approved"
        entries.append(
            {
                "source_id": source_id,
                "canonical_url": canonical_url,
                "version_or_bitstream": rights.bitstream_identity(canonical_url).bitstream_id,
                "source_sha256": source_sha256,
                "state": state,
                "mode": (
                    "materialized"
                    if approved
                    else "remove_all"
                    if state == "takedown"
                    else "origin_link"
                ),
                "allowed_actions": (
                    sorted(action.value for action in rights.RightsAction) if approved else []
                ),
                "components_state": (
                    "cleared" if approved else "blocked" if state == "takedown" else "unknown"
                ),
                "attribution": {
                    "creators": ["Synthetic Creator"],
                    "attribution_parties": ["Synthetic Rights Holder"],
                    "copyright_notice": "Synthetic copyright notice",
                    "license_notice": "CC BY 4.0",
                    "license_url": "https://creativecommons.org/licenses/by/4.0/",
                    "disclaimer_notice": "Synthetic fixture only",
                    "origin_url": f"https://edoc.rki.de/handle/{handle}",
                    "prior_change_history": [],
                    "current_change_notice": "Unchanged synthetic fixture",
                }
                if approved
                else None,
                "basis": "Synthetic fixture; no external publication rights claim",
                "reviewed_by": "Test Fixture",
                "reviewed_at": "2026-08-03T08:00:00Z",
            }
        )
    path.write_text(
        yaml.safe_dump(
            {
                "schema_version": 2,
                "decisions": entries,
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )


@dataclass(frozen=True)
class StorageRightsHarness:
    register_path: Path
    authorizer: RightsStorageAuthorizer

    def set_decisions(
        self,
        *decisions: tuple[str, str, str],
    ) -> dict[tuple[str, str], str]:
        _write_rights_register(self.register_path, *decisions)
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
    _write_rights_register(
        register_path,
        ("rki:176904/12345.2", "b" * 64, "approved"),
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
