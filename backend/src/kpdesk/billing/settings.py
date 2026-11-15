"""Explicit opt-in configuration. Secrets never appear in representations."""
import os
from collections.abc import Mapping
from dataclasses import dataclass, field
from types import MappingProxyType
from uuid import UUID

from kpdesk.billing.plans import PLANS


@dataclass(frozen=True, slots=True)
class Offer:
    product_id: UUID
    offer_id: UUID


@dataclass(frozen=True, slots=True)
class BillingSettings:
    enabled: bool = False
    api_key: str = field(default="", repr=False)
    webhook_secret: str = field(default="", repr=False)
    offers: Mapping[str, Offer] = field(default_factory=lambda: MappingProxyType({}))
    public_origin: str = "https://kpdesk.ru"

    def __post_init__(self) -> None:
        object.__setattr__(self, "offers", MappingProxyType(dict(self.offers)))
        if self.public_origin != "https://kpdesk.ru":
            raise ValueError("billing_public_origin_invalid")
        if not isinstance(self.enabled, bool):
            raise TypeError("billing_enabled_invalid")
        if not self.enabled:
            return
        if not self.api_key or any(not 33 <= ord(c) <= 126 for c in self.api_key):
            raise ValueError("billing_api_key_invalid")
        if (not 32 <= len(self.webhook_secret) <= 80
                or any(not 33 <= ord(c) <= 126 for c in self.webhook_secret)
                or self.webhook_secret == self.api_key):
            raise ValueError("billing_webhook_secret_invalid")
        if set(self.offers) != set(PLANS) or any(
            not isinstance(offer, Offer) or not isinstance(offer.product_id, UUID)
            or not isinstance(offer.offer_id, UUID) for offer in self.offers.values()
        ):
            raise ValueError("billing_offers_invalid")

    @classmethod
    def from_env(cls, environ: Mapping[str, str] | None = None) -> BillingSettings:
        env = os.environ if environ is None else environ
        flag = env.get("KP_DESK_LAVA_ENABLED")
        if flag not in (None, "true", "false"):
            raise ValueError("billing_enabled_invalid")
        if flag != "true":
            return cls()
        try:
            offers = {
                plan: Offer(UUID(env[f"KP_DESK_LAVA_{plan.upper()}_PRODUCT_ID"]),
                            UUID(env[f"KP_DESK_LAVA_{plan.upper()}_OFFER_ID"]))
                for plan in PLANS
            }
        except (KeyError, ValueError, TypeError, AttributeError):
            raise ValueError("billing_offers_invalid") from None
        return cls(enabled=True, api_key=env.get("KP_DESK_LAVA_API_KEY", ""),
                   webhook_secret=env.get("KP_DESK_LAVA_WEBHOOK_SECRET", ""), offers=offers)
