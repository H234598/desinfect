#!/usr/bin/env python3
"""Validate one offline P06 manifest catalog."""

from __future__ import annotations

import argparse
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.rki_pipeline.manifests import load_manifest_catalog  # noqa: E402


def validate(root: Path) -> None:
    """Fail unless ``root`` is one complete canonical manifest snapshot."""

    load_manifest_catalog(root)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    loaded = load_manifest_catalog(args.root)
    print(
        "manifests: ok; "
        f"sources={len(loaded.graph.sources)}; "
        f"documents={len(loaded.graph.documents)}; "
        f"conversions={len(loaded.graph.conversions)}; "
        f"storage={len(loaded.graph.storage_references)}; "
        f"catalog_sha256={loaded.rendered.catalog_sha256}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
