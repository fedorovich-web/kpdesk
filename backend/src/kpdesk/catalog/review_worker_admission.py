"""Fail-closed, process-local admission for reviewed supplier publication.

An instance owns one immutable signing epoch. Draining is terminal: a worker
must be replaced with a newly verified instance, never hot-resumed using a
mutable credential. This is not a cross-host barrier or service discovery.
"""
from __future__ import annotations

import math
import time
from collections.abc import Iterator
from contextlib import contextmanager
from threading import Condition

from kpdesk.catalog.review_signing import ReviewSigningCredential


class ReviewWorkerNotReady(RuntimeError):
    """Worker is draining, lacks a signer, or has permanently stopped."""


class ReviewWorkerPublicationGate:
    """Count ongoing publications and reject new ones once drain begins."""

    def __init__(self, credential: ReviewSigningCredential) -> None:
        if not isinstance(credential, ReviewSigningCredential):
            raise TypeError("A verified signing credential is required.")
        self._condition = Condition()
        self._credential = credential
        self._draining = False
        self._active = 0

    @contextmanager
    def optional_permit(self) -> Iterator[ReviewSigningCredential | None]:
        """Reserve a slot atomically, or yield None for an unavailable worker.

        Returning None lets HTTP authentication run first: anonymous requests
        must remain 401 even if the signer is absent or draining.
        """
        with self._condition:
            credential = None if self._draining else self._credential
            if credential is not None:
                self._active += 1
        try:
            yield credential
        finally:
            if credential is not None:
                with self._condition:
                    self._active -= 1
                    self._condition.notify_all()

    @contextmanager
    def permit(self) -> Iterator[ReviewSigningCredential]:
        """Strict admission for internal callers; rejects any draining worker."""
        with self.optional_permit() as credential:
            if credential is None:
                raise ReviewWorkerNotReady("Review publication worker unavailable.")
            yield credential

    def begin_drain(self) -> None:
        """Reject all subsequent admissions, even after draining finishes."""
        with self._condition:
            self._draining = True
            self._condition.notify_all()

    def wait_drained(self, timeout_seconds: float) -> bool:
        """Wait for existing publications; on timeout remain fail-closed."""
        if (
            isinstance(timeout_seconds, bool)
            or not isinstance(timeout_seconds, (float, int))
            or not math.isfinite(timeout_seconds)
            or timeout_seconds < 0
        ):
            raise ValueError("Invalid worker drain timeout.")
        deadline = time.monotonic() + timeout_seconds
        with self._condition:
            if not self._draining:
                raise ReviewWorkerNotReady("Drain was not started.")
            while self._active != 0:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return False
                self._condition.wait(timeout=remaining)
            return True

    @property
    def signer_epoch(self) -> int:
        """Immutable signer epoch for this worker instance."""
        with self._condition:
            return self._credential.epoch

    @property
    def active_publications(self) -> int:
        with self._condition:
            return self._active

    @property
    def draining(self) -> bool:
        with self._condition:
            return self._draining
