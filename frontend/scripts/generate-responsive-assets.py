"""Generate responsive AVIF variants of approved KPDesk imagery.

This does not change original assets. Requires Pillow >= 13 with AVIF support.
Run from anywhere: python frontend/scripts/generate-responsive-assets.py
"""
from __future__ import annotations

from pathlib import Path

from PIL import Image, features


PUBLIC = Path(__file__).resolve().parents[1] / "public"
IMAGES = {
    "hero/01_hero_main.avif": (480, 800),
    "hero/12_hero_offer_stack.avif": (480, 800),
    "icons/01_document_add.avif": (96, 144),
    "icons/06_settings.avif": (96, 144),
    "icons/12_team.avif": (96, 144),
    "icons/22_success_check.avif": (96, 144),
    "icons/27_database.avif": (96, 144),
    "icons/31_lightning.avif": (96, 144),
    "icons/33_analytics.avif": (96, 144),
}


def main() -> None:
    if not features.check("avif"):
        raise SystemExit("Pillow needs AVIF support to generate responsive assets")
    saved = 0
    for relative, sizes in IMAGES.items():
        source = PUBLIC / "assets" / relative
        with Image.open(source) as image:
            image.load()
            for width in sizes:
                if width >= image.width:
                    raise ValueError(f"{relative}: variant width {width} exceeds source")
                target = source.with_name(f"{source.stem}-{width}w.avif")
                height = round(image.height * width / image.width)
                scaled = image.resize((width, height), Image.Resampling.LANCZOS)
                # AVIF supports transparency; preserve original RGB/RGBA mode.
                scaled.save(target, format="AVIF", quality=65, speed=6)
                print(f"{relative} -> {target.name} ({target.stat().st_size} bytes)")
                saved += 1
    print(f"Generated {saved} responsive AVIF variants.")


if __name__ == "__main__":
    main()
