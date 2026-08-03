"""Immutable, machine-independent conversion evidence and identities."""

from __future__ import annotations

from dataclasses import dataclass
import re
from typing import Any

from scripts.rki_pipeline.io_utils import sha256_bytes, stable_json_dumps

_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_DOCUMENT_ID = re.compile(r"^rki-176904-[0-9]+-v[1-9][0-9]*$")
_BITSTREAM_ID = re.compile(r"^rki-bitstream-[0-9a-f]{64}$")
_LANGUAGE = re.compile(r"^[a-z]{3}$")
_HTTPS_URL = re.compile(r"https://[^\s\"'<>]+")
_FIXED_ENVIRONMENT = {
    "LANG": "C.UTF-8",
    "LC_ALL": "C.UTF-8",
    "TZ": "UTC",
}


class EvidenceError(ValueError):
    """Conversion evidence is incomplete, mutable, or machine-specific."""


def _text(value: object, field: str) -> str:
    if type(value) is not str or not value.strip():
        raise EvidenceError(f"{field} muss ein nichtleerer String sein")
    return value


def _sha256(value: object, field: str) -> str:
    if type(value) is not str or _SHA256.fullmatch(value) is None:
        raise EvidenceError(f"{field} muss ein kleingeschriebener SHA-256 sein")
    return value


def _without_machine_path(value: str, field: str) -> None:
    without_https_urls = _HTTPS_URL.sub("", value)
    if "/" in without_https_urls or "\\" in without_https_urls:
        raise EvidenceError(f"{field} enthält einen Maschinenpfad")


def _tuple(value: object, field: str) -> tuple[Any, ...]:
    if type(value) is not tuple:
        raise EvidenceError(f"{field} muss ein unveränderliches Tupel sein")
    return value


def _sorted_unique(values: tuple[Any, ...], field: str, key) -> None:
    keys = tuple(key(item) for item in values)
    if keys != tuple(sorted(keys)) or len(keys) != len(set(keys)):
        raise EvidenceError(f"{field} muss eindeutig und sortiert sein")


@dataclass(frozen=True, slots=True)
class NamedDigest:
    name: str
    sha256: str

    def __post_init__(self) -> None:
        _text(self.name, "name")
        _without_machine_path(self.name, "name")
        if "/" in self.name:
            raise EvidenceError("name enthält einen Maschinenpfad")
        _sha256(self.sha256, "sha256")

    def to_dict(self) -> dict[str, str]:
        return {"name": self.name, "sha256": self.sha256}

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> NamedDigest:
        return cls(name=payload["name"], sha256=payload["sha256"])


@dataclass(frozen=True, slots=True)
class EnvironmentVariable:
    name: str
    value: str

    def __post_init__(self) -> None:
        _text(self.value, f"environment.{self.name}")
        if type(self.name) is not str or _FIXED_ENVIRONMENT.get(self.name) != self.value:
            raise EvidenceError("Umgebungsvariable ist nicht Teil der festen Laufzeitumgebung")

    def to_dict(self) -> dict[str, str]:
        return {"name": self.name, "value": self.value}

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> EnvironmentVariable:
        return cls(name=payload["name"], value=payload["value"])


@dataclass(frozen=True, slots=True)
class OcrSettings:
    dpi: int
    color_mode: str
    psm: int
    oem: int
    languages: tuple[str, ...]
    tessdata: tuple[NamedDigest, ...]

    def __post_init__(self) -> None:
        if type(self.dpi) is not int or not 72 <= self.dpi <= 1200:
            raise EvidenceError("dpi muss zwischen 72 und 1200 liegen")
        if self.color_mode not in {"mono", "gray", "rgb"}:
            raise EvidenceError("color_mode ist ungültig")
        if type(self.psm) is not int or not 0 <= self.psm <= 13:
            raise EvidenceError("psm ist ungültig")
        if type(self.oem) is not int or not 0 <= self.oem <= 3:
            raise EvidenceError("oem ist ungültig")
        languages = _tuple(self.languages, "languages")
        if not languages or any(type(item) is not str or _LANGUAGE.fullmatch(item) is None for item in languages):
            raise EvidenceError("languages muss ISO-639-2-Codes enthalten")
        _sorted_unique(languages, "languages", lambda item: item)
        tessdata = _tuple(self.tessdata, "tessdata")
        if not tessdata or not all(isinstance(item, NamedDigest) for item in tessdata):
            raise EvidenceError("tessdata muss NamedDigest-Werte enthalten")
        _sorted_unique(tessdata, "tessdata", lambda item: item.name)
        if tuple(item.name for item in tessdata) != languages:
            raise EvidenceError("tessdata muss exakt die Sprachmenge abdecken")

    def to_dict(self) -> dict[str, object]:
        return {
            "dpi": self.dpi,
            "color_mode": self.color_mode,
            "psm": self.psm,
            "oem": self.oem,
            "languages": list(self.languages),
            "tessdata": [item.to_dict() for item in self.tessdata],
        }

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> OcrSettings:
        return cls(
            dpi=payload["dpi"],
            color_mode=payload["color_mode"],
            psm=payload["psm"],
            oem=payload["oem"],
            languages=tuple(payload["languages"]),
            tessdata=tuple(NamedDigest.from_dict(item) for item in payload["tessdata"]),
        )


@dataclass(frozen=True, slots=True)
class ToolEvidence:
    name: str
    version_output: str
    executable_sha256: str
    argv: tuple[str, ...]
    environment: tuple[EnvironmentVariable, ...]
    ocr_settings: OcrSettings | None

    def __post_init__(self) -> None:
        _text(self.name, "tool.name")
        _without_machine_path(self.name, "tool.name")
        _text(self.version_output, "tool.version_output")
        _without_machine_path(self.version_output, "tool.version_output")
        _sha256(self.executable_sha256, "tool.executable_sha256")
        argv = _tuple(self.argv, "tool.argv")
        if not argv or any(type(item) is not str or not item for item in argv):
            raise EvidenceError("tool.argv muss nichtleere Strings enthalten")
        for item in argv:
            _without_machine_path(item, "tool.argv")
        environment = _tuple(self.environment, "tool.environment")
        if not all(isinstance(item, EnvironmentVariable) for item in environment):
            raise EvidenceError("tool.environment enthält ungültige Werte")
        _sorted_unique(environment, "tool.environment", lambda item: item.name)
        if self.ocr_settings is not None and not isinstance(self.ocr_settings, OcrSettings):
            raise EvidenceError("tool.ocr_settings ist ungültig")

    def to_dict(self) -> dict[str, object]:
        return {
            "name": self.name,
            "version_output": self.version_output,
            "executable_sha256": self.executable_sha256,
            "argv": list(self.argv),
            "environment": [item.to_dict() for item in self.environment],
            "ocr_settings": None if self.ocr_settings is None else self.ocr_settings.to_dict(),
        }

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> ToolEvidence:
        raw_ocr = payload["ocr_settings"]
        return cls(
            name=payload["name"],
            version_output=payload["version_output"],
            executable_sha256=payload["executable_sha256"],
            argv=tuple(payload["argv"]),
            environment=tuple(EnvironmentVariable.from_dict(item) for item in payload["environment"]),
            ocr_settings=None if raw_ocr is None else OcrSettings.from_dict(raw_ocr),
        )


@dataclass(frozen=True, slots=True)
class RuntimeEvidence:
    platform: str
    libc: str
    shared_libraries: tuple[NamedDigest, ...]
    fonts: tuple[NamedDigest, ...]

    def __post_init__(self) -> None:
        _text(self.platform, "runtime.platform")
        _without_machine_path(self.platform, "runtime.platform")
        _text(self.libc, "runtime.libc")
        _without_machine_path(self.libc, "runtime.libc")
        for field, values in (
            ("runtime.shared_libraries", self.shared_libraries),
            ("runtime.fonts", self.fonts),
        ):
            items = _tuple(values, field)
            if not all(isinstance(item, NamedDigest) for item in items):
                raise EvidenceError(f"{field} enthält ungültige Werte")
            _sorted_unique(items, field, lambda item: item.name)

    def to_dict(self) -> dict[str, object]:
        return {
            "platform": self.platform,
            "libc": self.libc,
            "shared_libraries": [item.to_dict() for item in self.shared_libraries],
            "fonts": [item.to_dict() for item in self.fonts],
        }

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> RuntimeEvidence:
        return cls(
            platform=payload["platform"],
            libc=payload["libc"],
            shared_libraries=tuple(
                NamedDigest.from_dict(item) for item in payload["shared_libraries"]
            ),
            fonts=tuple(NamedDigest.from_dict(item) for item in payload["fonts"]),
        )


def conversion_fingerprint(
    *,
    source_sha256: str,
    converter: str,
    converter_version: str,
    options_sha256: str,
    toolchain: tuple[ToolEvidence, ...],
    runtime: RuntimeEvidence,
) -> str:
    """Hash every input that can change deterministic conversion output."""

    _sha256(source_sha256, "source_sha256")
    _text(converter, "converter")
    _without_machine_path(converter, "converter")
    _text(converter_version, "converter_version")
    _without_machine_path(converter_version, "converter_version")
    _sha256(options_sha256, "options_sha256")
    tools = _tuple(toolchain, "toolchain")
    if not tools or not all(isinstance(item, ToolEvidence) for item in tools):
        raise EvidenceError("toolchain muss ToolEvidence-Werte enthalten")
    if not isinstance(runtime, RuntimeEvidence):
        raise EvidenceError("runtime muss RuntimeEvidence sein")
    payload = {
        "source_sha256": source_sha256,
        "converter": converter,
        "converter_version": converter_version,
        "options_sha256": options_sha256,
        "toolchain": [item.to_dict() for item in tools],
        "runtime": runtime.to_dict(),
    }
    return sha256_bytes(stable_json_dumps(payload).encode("utf-8"))


def conversion_id(document_id: str, bitstream_id: str, fingerprint_sha256: str) -> str:
    """Bind one conversion identity to exact document, bitstream, and evidence."""

    if type(document_id) is not str or _DOCUMENT_ID.fullmatch(document_id) is None:
        raise EvidenceError("document_id ist nicht kanonisch")
    if type(bitstream_id) is not str or _BITSTREAM_ID.fullmatch(bitstream_id) is None:
        raise EvidenceError("bitstream_id ist nicht kanonisch")
    _sha256(fingerprint_sha256, "fingerprint_sha256")
    digest = sha256_bytes(
        f"{document_id}\0{bitstream_id}\0{fingerprint_sha256}".encode("utf-8")
    )
    return f"conv-{digest}"
