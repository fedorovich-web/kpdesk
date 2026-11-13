"""Versioned original KPDesk logo; no network requests or runtime image conversion."""
import base64
from functools import lru_cache
from pathlib import Path

from kpdesk.company.logo import CompanyLogo

_ASSET = Path(__file__).resolve().parents[1] / "assets/branding/kpdesk-default-v1.png"
_SHA256 = "b0acc098babdf330db44d727d2b998b56f7644bd2cbea4480ba3e53436c21b5c"


@lru_cache(maxsize=1)
def kpdesk_default_logo() -> CompanyLogo:
    return CompanyLogo(width=203, height=52, sha256=_SHA256,
                       data_base64=base64.b64encode(_ASSET.read_bytes()).decode("ascii"))
