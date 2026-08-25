#!/usr/bin/env python3
"""Strict, atomic MkDocs site builds."""

from __future__ import annotations

import argparse
from collections.abc import Iterator, Mapping, Sequence
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass
import os
from pathlib import Path, PurePosixPath
import re
import stat
import subprocess
import sys
import tempfile
from urllib.parse import urlsplit

import yaml

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from scripts.rki_pipeline.io_utils import (
    UnsafePathError,
    normalize_posix_path,
    sha256_file,
    source_date_epoch,
)
from scripts.rki_pipeline.staging import StagingError, preview_directory, staged_directory
from scripts.web.build_docs import (
    BuildDocsError,
    DocsBuildResult,
    _regular_files,
    build_docs,
    docs_preview_session,
    load_publication_config,
    snapshot_sources,
)


class SiteBuildError(RuntimeError):
    """A strict site build could not complete safely."""


@dataclass(frozen=True, slots=True)
class SiteBuildResult:
    """Result of one strict site transaction."""

    docs: DocsBuildResult
    site_hashes: Mapping[str, str]
    published: bool


_FD_DIRECTORY_PATH = re.compile(r"^(?:/proc/self/fd|/dev/fd)/(0|[1-9][0-9]*)$")


def _repo_root(repo_root: Path) -> Path:
    try:
        return repo_root.resolve(strict=True)
    except OSError as exc:
        raise SiteBuildError(f"invalid repository root: {repo_root}") from exc


def _validated_site_url(site_url: str | None) -> str | None:
    if site_url is None:
        return None
    try:
        parsed = urlsplit(site_url)
        hostname = parsed.hostname
        parsed.port
    except ValueError as exc:
        raise SiteBuildError("site_url is not a valid HTTPS URL") from exc
    if (
        parsed.scheme.lower() != "https"
        or not hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or any(character.isspace() for character in site_url)
    ):
        raise SiteBuildError(
            "site_url must be an HTTPS URL without credentials, query, or fragment"
        )
    return site_url


def _validated_only(only: tuple[PurePosixPath, ...]) -> tuple[PurePosixPath, ...]:
    selected: list[PurePosixPath] = []
    for value in only:
        if not isinstance(value, PurePosixPath):
            raise SiteBuildError(f"--only must be a relative Markdown path: {value!r}")
        raw = value.as_posix()
        try:
            normalized = normalize_posix_path(raw)
        except UnsafePathError as exc:
            raise SiteBuildError(f"unsafe --only path: {raw!r}") from exc
        if normalized != raw or not raw.endswith(".md"):
            raise SiteBuildError(f"--only must be canonical Markdown: {raw!r}")
        if value in selected:
            raise SiteBuildError(f"duplicate --only path: {raw}")
        selected.append(value)
    return tuple(selected)


def _regular_site_hashes(stage: Path) -> Mapping[str, str]:
    try:
        hashes: dict[str, str] = {}
        for relative, path in _regular_files(stage, required=True, allow_root_symlink=True):
            hashes[relative.as_posix()] = sha256_file(path)
        return dict(sorted(hashes.items()))
    except (BuildDocsError, OSError, ValueError) as exc:
        raise SiteBuildError(f"cannot validate generated site: {stage}") from exc


def _require_regular_file(path: Path) -> None:
    try:
        metadata = path.lstat()
    except OSError as exc:
        raise SiteBuildError(f"required site output is missing: {path}") from exc
    if not stat.S_ISREG(metadata.st_mode):
        raise SiteBuildError(f"required site output is not a regular file: {path}")


def _validate_site_tree(stage: Path) -> Mapping[str, str]:
    _require_regular_file(stage / "index.html")
    _require_regular_file(stage / "404.html")
    return _regular_site_hashes(stage)


def write_temp_mkdocs_config(
    *,
    repo_root: Path,
    docs_dir: Path,
    site_dir: Path,
    site_url: str | None,
    source_date_epoch: int,
) -> Path:
    """Write one absolute-path MkDocs config beneath the repository build root."""

    del source_date_epoch
    source = repo_root / "mkdocs.yml"
    try:
        if source.is_symlink() or not source.is_file():
            raise SiteBuildError(f"mkdocs source must be a regular file: {source}")
        parsed = yaml.safe_load(source.read_text(encoding="utf-8"))
        if not isinstance(parsed, dict):
            raise SiteBuildError("mkdocs configuration must be a mapping")
        if not docs_dir.is_absolute() or not site_dir.is_absolute():
            raise SiteBuildError("mkdocs docs_dir and site_dir must be absolute directories")
        absolute_docs = (
            docs_dir
            if _FD_DIRECTORY_PATH.fullmatch(docs_dir.as_posix())
            else docs_dir.resolve(strict=True)
        )
        absolute_site = (
            site_dir
            if _FD_DIRECTORY_PATH.fullmatch(site_dir.as_posix())
            else site_dir.resolve(strict=True)
        )
        if not absolute_docs.is_dir() or not absolute_site.is_dir():
            raise SiteBuildError("mkdocs docs_dir and site_dir must be existing directories")
        parsed["docs_dir"] = str(absolute_docs)
        parsed["site_dir"] = str(absolute_site)
        if site_url is not None:
            parsed["site_url"] = site_url
        build_root = repo_root / "build"
        build_root.mkdir(parents=True, exist_ok=True)
        descriptor, raw_path = tempfile.mkstemp(
            prefix=".mkdocs-build-", suffix=".yml", dir=build_root
        )
        path = Path(raw_path)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                descriptor = -1
                yaml.safe_dump(parsed, handle, allow_unicode=True, sort_keys=False)
        except BaseException:
            if descriptor != -1:
                os.close(descriptor)
            path.unlink(missing_ok=True)
            raise
        return path
    except SiteBuildError:
        raise
    except (OSError, UnicodeDecodeError, ValueError, yaml.YAMLError) as exc:
        raise SiteBuildError(f"cannot create temporary MkDocs config: {source}") from exc


def _fd_descriptor(path: Path) -> int | None:
    match = _FD_DIRECTORY_PATH.fullmatch(path.as_posix())
    if match is None:
        return None
    try:
        return int(match.group(1))
    except ValueError as exc:
        raise SiteBuildError(f"invalid FD-backed stage path: {path}") from exc


def run_mkdocs_build(
    repo_root: Path,
    config_path: Path,
    *,
    strict: bool,
    epoch: int,
    pass_fds: tuple[int, ...] = (),
) -> int:
    """Run MkDocs through the current interpreter without shell parsing."""

    command = [
        sys.executable,
        "-m",
        "mkdocs",
        "build",
        "--config-file",
        str(config_path),
    ]
    if strict:
        command.append("--strict")
    environment = os.environ.copy()
    environment["SOURCE_DATE_EPOCH"] = str(epoch)
    options: dict[str, object] = {
        "cwd": repo_root,
        "check": False,
        "capture_output": True,
        "text": True,
        "env": environment,
    }
    if pass_fds:
        options["pass_fds"] = pass_fds
    completed = subprocess.run(command, **options)
    return completed.returncode


def _run_site_build(
    *,
    repo_root: Path,
    docs_dir: Path,
    site_dir: Path,
    site_url: str | None,
    epoch: int,
) -> Mapping[str, str]:
    config_path: Path | None = None
    try:
        config_path = write_temp_mkdocs_config(
            repo_root=repo_root,
            docs_dir=docs_dir,
            site_dir=site_dir,
            site_url=site_url,
            source_date_epoch=epoch,
        )
        descriptors = tuple(
            sorted(
                {
                    descriptor
                    for descriptor in (_fd_descriptor(docs_dir), _fd_descriptor(site_dir))
                    if descriptor is not None
                }
            )
        )
        if (
            run_mkdocs_build(
                repo_root,
                config_path,
                strict=True,
                epoch=epoch,
                pass_fds=descriptors,
            )
            != 0
        ):
            raise SiteBuildError("MkDocs strict build failed")
        result = _validate_site_tree(site_dir)
    except BaseException as primary:
        if config_path is not None:
            try:
                config_path.unlink(missing_ok=True)
            except BaseException as cleanup_error:
                primary.add_note(f"Temporary MkDocs config cleanup failed: {cleanup_error}")
        raise
    if config_path is not None:
        try:
            config_path.unlink(missing_ok=True)
        except BaseException as cleanup_error:
            raise SiteBuildError("Temporary MkDocs config cleanup failed") from cleanup_error
    return result


@contextmanager
def site_preview_session(repo_root: Path) -> Iterator[Path]:
    """Yield a disposable FD-anchored site directory without publishing it."""

    root = _repo_root(repo_root)
    try:
        config = load_publication_config(root)
    except BuildDocsError as exc:
        raise SiteBuildError(str(exc)) from exc
    stack = ExitStack()
    try:
        try:
            stage = stack.enter_context(preview_directory(config.site_dir, allowed_root=root))
        except BuildDocsError:
            raise
        except (OSError, StagingError, UnsafePathError, ValueError) as exc:
            raise SiteBuildError(str(exc)) from exc
        yield stage
    except BaseException:
        active = sys.exc_info()
        try:
            stack.__exit__(*active)
        except BaseException as teardown_error:
            if active[1] is not None:
                active[1].add_note(f"Additional site preview teardown error: {teardown_error}")
            else:
                raise
        raise
    else:
        try:
            stack.close()
        except SiteBuildError:
            raise
        except (OSError, StagingError, UnsafePathError, ValueError) as exc:
            raise SiteBuildError(str(exc)) from exc


def build_site(
    repo_root: Path,
    *,
    check: bool,
    dry_run: bool,
    strict: bool,
    force: bool,
    site_url: str | None,
    only: tuple[PurePosixPath, ...] = (),
) -> SiteBuildResult:
    """Build and atomically publish a strict site, or run disposable previews."""

    if only and not dry_run:
        raise SiteBuildError("--only requires --dry-run")
    if force and dry_run:
        raise SiteBuildError("--force cannot be combined with --dry-run")
    if not strict:
        raise SiteBuildError("--strict is required for site builds")
    selected = _validated_only(only)
    validated_url = _validated_site_url(site_url)
    root = _repo_root(repo_root)
    epoch = source_date_epoch()
    try:
        before = snapshot_sources(root)
        config = load_publication_config(root)
    except (BuildDocsError, OSError, UnsafePathError, ValueError) as exc:
        raise SiteBuildError(str(exc)) from exc
    del check

    if dry_run:
        try:
            with docs_preview_session(root, only=selected) as docs:
                with site_preview_session(root) as site_stage:
                    site_hashes = _run_site_build(
                        repo_root=root,
                        docs_dir=docs.docs_dir,
                        site_dir=site_stage,
                        site_url=validated_url,
                        epoch=epoch,
                    )
                    if snapshot_sources(root) != before:
                        raise SiteBuildError("source-hash drift during site build")
        except SiteBuildError:
            raise
        except (BuildDocsError, OSError, StagingError, UnsafePathError, ValueError) as exc:
            raise SiteBuildError(str(exc)) from exc
        return SiteBuildResult(docs=docs.result, site_hashes=site_hashes, published=False)

    try:
        docs = build_docs(root, force=force)
        with staged_directory(config.site_dir, allowed_root=root, force=force) as site_stage:
            site_hashes = _run_site_build(
                repo_root=root,
                docs_dir=(root / "build/docs").resolve(strict=True),
                site_dir=site_stage,
                site_url=validated_url,
                epoch=epoch,
            )
            if snapshot_sources(root) != before:
                raise SiteBuildError("source-hash drift during site build")
    except SiteBuildError:
        raise
    except (BuildDocsError, OSError, StagingError, UnsafePathError, ValueError) as exc:
        raise SiteBuildError(str(exc)) from exc
    return SiteBuildResult(docs=docs, site_hashes=site_hashes, published=True)


def _parse_only(values: Sequence[str]) -> tuple[PurePosixPath, ...]:
    parsed: list[PurePosixPath] = []
    for value in values:
        try:
            normalized = normalize_posix_path(value)
        except UnsafePathError as exc:
            raise SiteBuildError(f"unsafe --only path: {value!r}") from exc
        if normalized != value:
            raise SiteBuildError(f"--only must use canonical Markdown syntax: {value!r}")
        parsed.append(PurePosixPath(value))
    return tuple(parsed)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Build strict MkDocs site")
    parser.add_argument("--repo-root", type=Path, default=Path.cwd())
    parser.add_argument("--check", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--strict", action="store_true")
    parser.add_argument("--force", action="store_true")
    parser.add_argument("--site-url")
    parser.add_argument("--only", action="append", default=[])
    args = parser.parse_args(argv)
    try:
        build_site(
            args.repo_root,
            check=args.check,
            dry_run=args.dry_run,
            strict=args.strict,
            force=args.force,
            site_url=args.site_url,
            only=_parse_only(args.only),
        )
    except (SiteBuildError, ValueError) as exc:
        print(f"site build failed: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
