"""Explicit short-lived credential for clone-only reviewed-catalog publication.

The key is a secret: never log, serialize, expose via API, or embed in an
HTTP request. Rotation requires coordinated replacement across workers.
"""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True, slots=True)
class ReviewSigningCredential:
    epoch: int
    key: bytes = field(repr=False)

    def __post_init__(self) -> None:
        if type(self.epoch) is not int or self.epoch < 1 or self.epoch > 2**63 - 1:
            raise ValueError("Invalid signing key epoch.")
        if type(self.key) is not bytes or len(self.key) != 32:
            raise ValueError("Review signing key must contain exactly 32 bytes.")
