"""Server-owned prices and limits for one-time 30 day access."""
from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType


@dataclass(frozen=True, slots=True)
class Plan:
    id: str
    name: str
    price_minor: int
    people_limit: int
    new_quotes: int
    catalog_items: int
    days: int = 30


PLANS: Mapping[str, Plan] = MappingProxyType({
    "start": Plan("start", "Старт", 29900, 1, 30, 500),
    "team": Plan("team", "Команда", 49900, 5, 100, 2000),
    "business": Plan("business", "Бизнес", 99000, 15, 300, 5000),
})
