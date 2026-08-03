#!/usr/bin/env python3
"""Materialize the reviewed synthetic P06 PDF conversion fixture."""
from __future__ import annotations

import argparse
from pathlib import Path
import tempfile

from scripts.rki_pipeline.conversion.runtime import (
    RuntimeEvidenceError,
    collect_runtime_evidence,
)
from scripts.rki_pipeline.conversion.service import (
    ConversionError,
    ConversionNeedsReview,
    materialize_conversion,
)
from scripts.rki_pipeline.io_utils import stable_json_dumps
from scripts.rki_pipeline.rights import (
    RightsPolicyError,
    load_rights_authority,
    load_rights_policy,
    resolve_rights,
)
from scripts.rki_pipeline.run_modes import EffectLedger, RunMode
from scripts.rki_pipeline.storage.base import (
    RightsStorageAuthorizer,
    StorageError,
    StorageIntent,
    hash_file,
)


ROOT = Path(__file__).resolve().parents[2]
TEXT_FIXTURE = ROOT / "tests" / "fixtures" / "pdf" / "text.pdf"
FIXTURE_SHA256 = "4665c3b8cfa6de8d9792a8defb977bfd200465b513575419e0a88541000f5b2a"
FIXTURE_BYTES = 979
SOURCE_ID = "rki:176904/900000001"
DOCUMENT_ID = "rki-176904-900000001-v1"
BITSTREAM_ID = f"rki-bitstream-{FIXTURE_SHA256}"
LOGICAL_KEY = (
    "Jahre/2026/Markdown/2026-08-03_gesamtausgabe_"
    f"{DOCUMENT_ID}_{BITSTREAM_ID}.md"
)
EXIT_FAILED = 3
EXIT_NEEDS_REVIEW = 4


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--fixture", type=Path, required=True)
    parser.add_argument("--mode", choices=(RunMode.MATERIALIZE.value,), required=True)
    parser.add_argument("--temp-root", type=Path)
    return parser


def _fixture_intent(
    fixture: Path,
) -> tuple[StorageIntent, RightsStorageAuthorizer]:
    try:
        resolved = fixture.resolve(strict=True)
    except OSError as exc:
        raise ValueError("P06-Konvertierungsfixture ist nicht lesbar") from exc
    if resolved != TEXT_FIXTURE.resolve(strict=True):
        raise ValueError("Nur registrierte P06-Textfixture ist zulässig")
    try:
        measured = hash_file(resolved)
    except StorageError as exc:
        raise ValueError("P06-Konvertierungsfixture ist nicht sicher lesbar") from exc
    if measured != (
        FIXTURE_BYTES,
        FIXTURE_SHA256,
    ):
        raise ValueError("P06-Konvertierungsfixture driftet vom Register")
    authority = load_rights_authority()
    policy = load_rights_policy()
    decision = resolve_rights(
        SOURCE_ID,
        FIXTURE_SHA256,
        authority=authority,
        policy=policy,
    )
    if decision.decision_sha256 is None:
        raise RightsPolicyError("Synthetische P06-Fixture besitzt keine Freigabe")
    intent = StorageIntent.from_path(
        resolved,
        artifact_id=BITSTREAM_ID,
        logical_key=LOGICAL_KEY,
        source_id=SOURCE_ID,
        source_sha256=FIXTURE_SHA256,
        decision_sha256=decision.decision_sha256,
        visibility="public",
        rights_state=decision.state.value,
        document_id=DOCUMENT_ID,
    )
    return intent, RightsStorageAuthorizer(authority, policy)


def _temp_root(value: Path | None) -> Path:
    if value is None:
        return Path(tempfile.mkdtemp(prefix="desinfect-p06-materialize-")).resolve()
    resolved = value.resolve()
    try:
        resolved.relative_to(ROOT)
    except ValueError:
        return resolved
    raise ValueError("temp_root darf nicht im Repository liegen")


def _result_payload(result, ledger: EffectLedger, temp_root: Path) -> dict[str, object]:
    return {
        "status": result.state,
        "quality": result.quality,
        "ocr_used": result.ocr_used,
        "conversion_id": result.conversion_id,
        "fingerprint_sha256": result.fingerprint_sha256,
        "output": result.output_path.as_posix(),
        "manifest": result.manifest_path.as_posix(),
        "temp_root": temp_root.as_posix(),
        "effects": [
            {
                "kind": event.kind.value,
                "target": event.target,
                "sha256": event.sha256,
                "size": event.size,
            }
            for event in ledger.events
        ],
    }


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    temp_root = (
        args.temp_root.absolute()
        if args.temp_root is not None
        else Path(tempfile.gettempdir()).resolve()
    )
    try:
        temp_root = _temp_root(args.temp_root)
        intent, authorizer = _fixture_intent(args.fixture)
        ledger = EffectLedger(RunMode.MATERIALIZE, temp_root=temp_root)
        result = materialize_conversion(
            intent,
            bitstream_id=BITSTREAM_ID,
            temp_root=temp_root,
            ledger=ledger,
            authorizer=authorizer,
            runtime=collect_runtime_evidence(),
        )
        print(stable_json_dumps(_result_payload(result, ledger, temp_root)), end="")
        return EXIT_NEEDS_REVIEW if result.state == "needs_review" else 0
    except ConversionNeedsReview as exc:
        print(
            stable_json_dumps(
                {"status": "needs_review", "error": str(exc), "temp_root": temp_root.as_posix()}
            ),
            end="",
        )
        return EXIT_NEEDS_REVIEW
    except (ConversionError, RuntimeEvidenceError, StorageError) as exc:
        print(
            stable_json_dumps(
                {"status": "failed", "error": str(exc), "temp_root": temp_root.as_posix()}
            ),
            end="",
        )
        return EXIT_FAILED
    except (OSError, ValueError, RightsPolicyError) as exc:
        print(
            stable_json_dumps(
                {"status": "invalid", "error": str(exc), "temp_root": temp_root.as_posix()}
            ),
            end="",
        )
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
