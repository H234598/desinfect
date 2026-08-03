"""Stable identities for RKI documents and PDF bitstreams."""
from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
import re
from urllib.parse import parse_qsl, quote, urlencode, unquote, urlsplit, urlunsplit


_HANDLE_RE = re.compile(
    r"^(?P<prefix>176904)/(?P<number>[0-9]+)"
    r"(?:\.(?P<version>[2-9]|[1-9][0-9]+))?$"
)
_PDF_PATH_RE = re.compile(
    r"^/bitstream/handle/(?P<handle>176904/[0-9]+"
    r"(?:\.(?:[2-9]|[1-9][0-9]+))?)/.+\.pdf$",
    re.IGNORECASE,
)


class DocumentIdentityError(ValueError):
    """RKI identity input is malformed or ambiguous."""


@dataclass(frozen=True, slots=True)
class DocumentIdentity:
    handle: str
    source_id: str
    document_id: str
    version: int
    supersedes: str | None


@dataclass(frozen=True, slots=True)
class BitstreamIdentity:
    canonical_url: str
    bitstream_id: str
    version: int | None


def _handle_parts(handle: str) -> tuple[str, str, int | None]:
    """Validate one numeric RKI handle and split its stable components."""

    if type(handle) is not str:
        raise DocumentIdentityError("RKI-Handle muss eine Zeichenkette sein")
    match = _HANDLE_RE.fullmatch(handle)
    if match is None:
        raise DocumentIdentityError(f"Ungültiger RKI-Handle: {handle}")
    version_raw = match.group("version")
    return match.group("prefix"), match.group("number"), (
        int(version_raw) if version_raw is not None else None
    )


def document_identity(handle: str) -> DocumentIdentity:
    """Return stable document identity derived only from its RKI handle."""

    prefix, number, handle_version = _handle_parts(handle)
    version = 1 if handle_version is None else handle_version
    document_id = f"rki-{prefix}-{number}-v{version}"
    return DocumentIdentity(
        handle=handle,
        source_id=f"rki:{handle}",
        document_id=document_id,
        version=version,
        supersedes=(f"rki-{prefix}-{number}-v{version - 1}" if version > 1 else None),
    )


def bitstream_identity(url: str) -> BitstreamIdentity:
    """Return canonical, sequence-aware identity for one RKI PDF bitstream."""

    if type(url) is not str:
        raise DocumentIdentityError("Bitstream-URL muss eine Zeichenkette sein")
    parsed = urlsplit(url)
    if (
        parsed.scheme.lower() != "https"
        or parsed.netloc.lower() != "edoc.rki.de"
        or parsed.username is not None
        or parsed.password is not None
        or parsed.fragment
    ):
        raise DocumentIdentityError("Ungültige RKI-Bitstream-URL")

    normalized_path = quote(unquote(parsed.path), safe="/-._~")
    path_match = _PDF_PATH_RE.fullmatch(normalized_path)
    if path_match is None:
        raise DocumentIdentityError("Bitstream-URL muss auf ein RKI-PDF zeigen")
    _handle_parts(path_match.group("handle"))

    query = parse_qsl(parsed.query, keep_blank_values=True)
    values: dict[str, str] = {}
    for key, value in query:
        if key not in {"sequence", "isAllowed"} or key in values:
            raise DocumentIdentityError("Bitstream-URL enthält mehrdeutige Parameter")
        values[key] = value
    if "isAllowed" in values and values["isAllowed"] != "y":
        raise DocumentIdentityError("isAllowed muss y sein")
    if "sequence" in values and re.fullmatch(r"[1-9][0-9]*", values["sequence"]) is None:
        raise DocumentIdentityError("sequence muss eine positive Ganzzahl sein")

    canonical_query = urlencode(
        sorted((key, value) for key, value in values.items() if key == "sequence")
    )
    canonical_url = urlunsplit(("https", "edoc.rki.de", normalized_path, canonical_query, ""))
    return BitstreamIdentity(
        canonical_url=canonical_url,
        bitstream_id=f"rki-bitstream-{sha256(canonical_url.encode()).hexdigest()}",
        version=int(values["sequence"]) if "sequence" in values else None,
    )
