"""Core package for the desinfect RKI pipeline.

The package must remain safe to import: importing it performs no network access,
file writes, subprocess execution, or process termination.
"""

from __future__ import annotations

__all__ = ["__version__"]
__version__ = "0.1.0"
