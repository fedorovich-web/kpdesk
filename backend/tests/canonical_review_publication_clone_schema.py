"""Compatibility re-exports from the clone-locked provenance schema."""
from kpdesk.db.review_clone_schema.publication import (
    install_publication_provenance,
    rotate_review_signer,
)

__all__ = ["install_publication_provenance", "rotate_review_signer"]
