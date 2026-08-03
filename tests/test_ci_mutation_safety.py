"""Regression tests for Variant-B GitHub Actions mutation safety."""
from __future__ import annotations

from pathlib import Path

from scripts.validate_ci_mutation_safety import validate_repository


def workflow(root: Path, text: str) -> Path:
    path = root / ".github" / "workflows" / "writer.yml"
    path.parent.mkdir(parents=True)
    path.write_text(text, encoding="utf-8")
    return path


def safe_writer() -> str:
    return """name: writer
on: workflow_dispatch
jobs:
  write:
    runs-on: ubuntu-latest
    steps:
      - name: Commit and push safely
        shell: bash
        run: |
          set -Eeuo pipefail
          git status --short
          git diff --cached --name-status
          if git diff --cached --quiet; then
            exit 0
          fi
          git commit -m "safe"
          git push origin HEAD:main
"""


def test_safe_writer_passes() -> None:
    from tempfile import TemporaryDirectory

    with TemporaryDirectory() as directory:
        root = Path(directory)
        workflow(root, safe_writer())
        assert validate_repository(root) == []


def test_each_writer_step_requires_noop_and_diagnostics(tmp_path: Path) -> None:
    workflow(
        tmp_path,
        safe_writer().replace(
            "          git push origin HEAD:main\n",
            "      - name: Unsafe second writer\n        run: git push origin HEAD:main\n",
        ),
    )
    codes = {issue.code for issue in validate_repository(tmp_path)}
    assert {"CIW001", "CIW002", "CIW003"} <= codes


def test_multiline_audit_bypass_is_blocked(tmp_path: Path) -> None:
    workflow(
        tmp_path,
        """name: audit
on: workflow_dispatch
jobs:
  audit:
    runs-on: ubuntu-latest
    steps:
      - name: audit
        run: |
          npm audit \\
            || true
""",
    )
    assert "CIW008" in {issue.code for issue in validate_repository(tmp_path)}


def test_continue_on_error_cannot_soften_audit(tmp_path: Path) -> None:
    workflow(
        tmp_path,
        """name: audit
on: workflow_dispatch
jobs:
  audit:
    runs-on: ubuntu-latest
    steps:
      - name: npm audit
        continue-on-error: true
        run: npm audit
""",
    )
    assert "CIW009" in {issue.code for issue in validate_repository(tmp_path)}


def test_mutation_outside_analyzable_step_is_blocked(tmp_path: Path) -> None:
    workflow(
        tmp_path,
        """name: invalid
on: workflow_dispatch
x-note: git push origin HEAD:main
jobs: {}
""",
    )
    assert "CIW011" in {issue.code for issue in validate_repository(tmp_path)}
