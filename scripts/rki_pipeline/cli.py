#!/usr/bin/env python3
"""Thin domain router for reviewed RKI pipeline CLIs."""
from __future__ import annotations

import sys

from scripts.rki_pipeline import aggregation, archive, conversion_cli, reconciliation


def main(argv: list[str] | None = None) -> int:
    arguments = list(sys.argv[1:] if argv is None else argv)
    if arguments and arguments[0] == "convert":
        return conversion_cli.main(arguments[1:])
    if arguments and arguments[0] == "build-archive":
        return archive.main(arguments[1:])
    if arguments and arguments[0] == "aggregate":
        return aggregation.main(arguments[1:])
    if arguments and arguments[0] == "reconcile":
        return reconciliation.main(arguments[1:])
    print(
        "usage: python -m scripts.rki_pipeline.cli (convert|build-archive|aggregate|reconcile) ...",
        file=sys.stderr,
    )
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
