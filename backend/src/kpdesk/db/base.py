"""Base mapping with no implicit table creation at application startup."""

from sqlalchemy.orm import DeclarativeBase


class Base(DeclarativeBase):
    pass
