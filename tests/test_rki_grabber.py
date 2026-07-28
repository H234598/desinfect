"""Aggregate P03 validator test."""

from scripts.validate_p03_grabber import validate


def test_p03_grabber_contracts() -> None:
    """Keep the documented P03 validator green under pytest."""

    validate()
