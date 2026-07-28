"""Modular RKI Epidemiologisches Bulletin source client."""

from scripts.rki_grabber.api import grab
from scripts.rki_grabber.models import GrabberRequest, GrabberResult, Scope

__all__ = ["GrabberRequest", "GrabberResult", "Scope", "grab"]
