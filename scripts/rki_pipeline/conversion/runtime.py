#!/usr/bin/env python3
"""Bounded, path-free runtime evidence for system PDF conversion tools."""
from __future__ import annotations

import hashlib
import os
from pathlib import Path
import platform
import re
import shutil
import stat
import tempfile

from scripts.rki_pipeline.conversion.base import NamedDigest, RuntimeEvidence
from scripts.rki_pipeline.pdf_validation import PdfLimits, ProcessRunner, ProcessRunnerError


class RuntimeEvidenceError(RuntimeError):
    """Installed conversion runtime cannot be attested deterministically."""


_EVIDENCE_LIMITS = PdfLimits(
    source_bytes=64 * 1024 * 1024,
    pages=1,
    raster_pixels=1,
    wall_seconds=30,
    cpu_seconds=30,
    address_space_bytes=1024 * 1024 * 1024,
    open_files=128,
    generated_file_bytes=8 * 1024 * 1024,
    stdout_bytes=8 * 1024 * 1024,
    stderr_bytes=1024 * 1024,
    total_output_bytes=8 * 1024 * 1024,
)
_LDD_PATH = re.compile(r"^\s*(?:(?P<name>\S+)\s+=>\s+)?(?P<path>/\S+)\s+\(")


def _sha256_regular(path: Path) -> str:
    try:
        resolved = path.resolve(strict=True)
        descriptor = os.open(
            resolved,
            os.O_RDONLY
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NOFOLLOW", 0),
        )
    except OSError as exc:
        raise RuntimeEvidenceError(f"Runtime-Datei ist nicht sicher lesbar: {path.name}") from exc
    try:
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            raise RuntimeEvidenceError(f"Runtime-Datei ist nicht regulär: {path.name}")
        digest = hashlib.sha256()
        while True:
            chunk = os.read(descriptor, 1024 * 1024)
            if not chunk:
                return digest.hexdigest()
            digest.update(chunk)
    except OSError as exc:
        raise RuntimeEvidenceError(f"Runtime-Datei konnte nicht gehasht werden: {path.name}") from exc
    finally:
        os.close(descriptor)


def _run(executable: str | Path, arguments: tuple[str, ...]) -> bytes:
    try:
        with tempfile.TemporaryDirectory(prefix="desinfect-runtime-") as raw_workdir:
            result = ProcessRunner().run(
                executable,
                arguments,
                cwd=Path(raw_workdir),
                limits=_EVIDENCE_LIMITS,
            )
    except ProcessRunnerError as exc:
        raise RuntimeEvidenceError(f"Runtime-Probe fehlgeschlagen: {Path(executable).name}") from exc
    if result.returncode != 0:
        raise RuntimeEvidenceError(
            f"Runtime-Probe endete mit Status {result.returncode}: {Path(executable).name}"
        )
    return result.stdout


def _tool_paths(tool_names: tuple[str, ...]) -> tuple[Path, ...]:
    paths: list[Path] = []
    for name in tool_names:
        found = shutil.which(name, path=os.defpath)
        if found is None:
            raise RuntimeEvidenceError(f"Konvertierungstool fehlt: {name}")
        paths.append(Path(found).resolve(strict=True))
    return tuple(paths)


def _libraries(tool_paths: tuple[Path, ...]) -> tuple[NamedDigest, ...]:
    values: dict[str, str] = {}
    for tool in tool_paths:
        try:
            lines = _run("ldd", (tool.as_posix(),)).decode("utf-8", errors="strict").splitlines()
        except UnicodeDecodeError as exc:
            raise RuntimeEvidenceError("ldd-Ausgabe ist nicht UTF-8") from exc
        if any("not found" in line for line in lines):
            raise RuntimeEvidenceError(f"Shared Library fehlt für {tool.name}")
        for line in lines:
            match = _LDD_PATH.match(line)
            if match is None:
                continue
            path = Path(match.group("path"))
            name = match.group("name") or path.name
            digest = _sha256_regular(path)
            previous = values.setdefault(name, digest)
            if previous != digest:
                raise RuntimeEvidenceError(f"Shared Library ist mehrdeutig: {name}")
    return tuple(NamedDigest(name, digest) for name, digest in sorted(values.items()))


def _fonts() -> tuple[NamedDigest, ...]:
    try:
        lines = _run("fc-list", ("--format=%{file}\n",)).decode(
            "utf-8",
            errors="strict",
        ).splitlines()
    except UnicodeDecodeError as exc:
        raise RuntimeEvidenceError("fontconfig-Ausgabe ist nicht UTF-8") from exc
    values: dict[str, str] = {}
    for raw in sorted(set(lines)):
        if not raw:
            continue
        path = Path(raw)
        digest = _sha256_regular(path)
        name = f"{path.name}-{digest[:12]}"
        values[name] = digest
    if not values:
        raise RuntimeEvidenceError("fontconfig meldet keine Fonts")
    return tuple(NamedDigest(name, digest) for name, digest in sorted(values.items()))


def collect_runtime_evidence(
    tool_names: tuple[str, ...] = ("pdfinfo", "pdftotext"),
) -> RuntimeEvidence:
    """Hash relevant shared libraries and all fontconfig-visible font bytes."""

    if type(tool_names) is not tuple or not tool_names:
        raise ValueError("tool_names muss ein nichtleeres Tupel sein")
    libc_name, libc_version = platform.libc_ver()
    if not libc_name or not libc_version:
        raise RuntimeEvidenceError("libc-Version konnte nicht ermittelt werden")
    system = platform.system().lower()
    machine = platform.machine().lower()
    if not system or not machine:
        raise RuntimeEvidenceError("Plattform konnte nicht ermittelt werden")
    tools = _tool_paths(tool_names)
    return RuntimeEvidence(
        platform=f"{system}-{machine}",
        libc=f"{libc_name}-{libc_version}",
        shared_libraries=_libraries(tools),
        fonts=_fonts(),
    )
