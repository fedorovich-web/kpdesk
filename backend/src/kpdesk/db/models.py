"""Immutable catalog versions and an explicit active-version pointer."""

import uuid
from datetime import datetime
from decimal import Decimal

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    Integer,
    Numeric,
    String,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from kpdesk.db.base import Base
from kpdesk.matching.profiles import CATEGORY_ATTRIBUTES


class CatalogVersion(Base):
    __tablename__ = "catalog_versions"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    source_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    original_filename: Mapped[str] = mapped_column(String(512), nullable=False)
    rows_count: Mapped[int] = mapped_column(Integer, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    __table_args__ = (CheckConstraint("rows_count > 0", name="positive_catalog_rows"),)


class CatalogItem(Base):
    __tablename__ = "catalog_items"

    catalog_version_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("catalog_versions.id", ondelete="RESTRICT"),
        primary_key=True,
    )
    sku: Mapped[str] = mapped_column(String(100), primary_key=True)
    name: Mapped[str] = mapped_column(String(500), nullable=False)
    category: Mapped[str] = mapped_column(String(64), nullable=False)
    sale_unit: Mapped[str] = mapped_column(String(20), nullable=False)
    pieces_per_sale_unit: Mapped[int] = mapped_column(Integer, nullable=False)
    price_rub: Mapped[Decimal] = mapped_column(Numeric(14, 2), nullable=False)
    brand: Mapped[str | None] = mapped_column(String(200))
    paper_format: Mapped[str | None] = mapped_column(String(50))
    paper_gsm: Mapped[int | None] = mapped_column(Integer)
    pen_ink_color: Mapped[str | None] = mapped_column(String(100))
    pen_ink_type: Mapped[str | None] = mapped_column(String(100))
    attributes: Mapped[dict[str, str | int | list[str]]] = mapped_column(
        JSONB, nullable=False, default=dict, server_default=text("'{}'::jsonb")
    )

    __table_args__ = (
        CheckConstraint(
            "category IN (" + ", ".join(repr(key) for key in CATEGORY_ATTRIBUTES) + ")",
            name="valid_catalog_category"
        ),
        CheckConstraint("pieces_per_sale_unit > 0", name="positive_pack_size"),
        CheckConstraint("price_rub >= 0", name="nonnegative_price"),
    )


class CatalogActive(Base):
    __tablename__ = "catalog_active"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    catalog_version_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("catalog_versions.id", ondelete="RESTRICT"),
        nullable=True,
    )
    __table_args__ = (CheckConstraint("id = 1", name="singleton_active_catalog"),)


class IdempotencyRecord(Base):
    __tablename__ = "idempotency_records"

    namespace: Mapped[str] = mapped_column(String(60), primary_key=True)
    key: Mapped[str] = mapped_column(String(128), primary_key=True)
    request_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    result_version_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("catalog_versions.id", ondelete="RESTRICT"),
        nullable=False,
    )



class QuoteDocument(Base):
    """Mutable head pointer. Historical revisions are immutable."""
    __tablename__ = "quote_documents"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    current_revision: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    __table_args__ = (
        CheckConstraint("current_revision >= 1", name="positive_quote_revision"),
    )


class QuoteRevision(Base):
    """Price-locked historical draft; never UPDATE or DELETE from application."""
    __tablename__ = "quote_revisions"

    quote_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("quote_documents.id", ondelete="RESTRICT"),
        primary_key=True,
    )
    number: Mapped[int] = mapped_column(Integer, primary_key=True)
    catalog_version_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("catalog_versions.id", ondelete="RESTRICT"),
        nullable=False,
    )
    snapshot: Mapped[dict[str, object]] = mapped_column(JSONB, nullable=False)
    snapshot_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    is_complete: Mapped[bool] = mapped_column(nullable=False)
    total_rub: Mapped[Decimal | None] = mapped_column(Numeric(14, 2))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False,
    )
    __table_args__ = (
        CheckConstraint("number >= 1", name="positive_quote_revision_number"),
        CheckConstraint(
            "(is_complete AND total_rub IS NOT NULL) OR "
            "(NOT is_complete AND total_rub IS NULL)",
            name="quote_completion_total_consistent",
        ),
        CheckConstraint("total_rub IS NULL OR total_rub >= 0", name="quote_total_nonnegative"),
    )


class QuoteWriteRequest(Base):
    """Idempotency key is global to this single-user local workspace."""
    __tablename__ = "quote_write_requests"

    key: Mapped[str] = mapped_column(String(128), primary_key=True)
    request_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    quote_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("quote_documents.id", ondelete="RESTRICT"),
        nullable=False,
    )
    revision_number: Mapped[int] = mapped_column(Integer, nullable=False)
