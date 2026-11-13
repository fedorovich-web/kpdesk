"""Bounded, self-contained company logo snapshots; synthetic pixels only."""
import base64
import hashlib
import random
import struct
import zlib
from io import BytesIO

import pytest
from PIL import Image, PngImagePlugin
from pydantic import ValidationError

from kpdesk.company.logo import CompanyLogo, prepare_logo
from kpdesk.company.schema import CompanyProfileData, profile_digest


def source_logo(size: tuple[int, int] = (400, 120), *, color: str = "#008b50", fmt: str = "PNG") -> bytes:
    image = Image.new("RGBA" if fmt != "JPEG" else "RGB", size, color)
    out = BytesIO()
    image.save(out, format=fmt)
    return out.getvalue()


@pytest.mark.parametrize("fmt", ["PNG", "JPEG", "WEBP"])
def test_prepared_logo_is_bounded_png_with_unchanged_aspect_and_verified_digest(fmt: str) -> None:
    logo = prepare_logo(source_logo((1600, 400), fmt=fmt))
    assert logo.width <= 640 and logo.height <= 256
    assert logo.width / logo.height == 4
    raw = logo.png_bytes()
    assert len(raw) <= 65536 and hashlib.sha256(raw).hexdigest() == logo.sha256
    assert CompanyLogo.model_validate_json(logo.model_dump_json()) == logo
    with Image.open(BytesIO(raw)) as image:
        assert image.format == "PNG" and image.mode == "RGBA" and not image.info


def test_metadata_is_removed_but_visible_pixels_and_transparency_are_preserved() -> None:
    image = Image.new("RGBA", (60, 60), (0, 0, 0, 0))
    image.paste((0, 139, 80, 128), (10, 20, 50, 40))
    info = PngImagePlugin.PngInfo()
    info.add_text("private", "do not retain this metadata")
    out = BytesIO(); image.save(out, format="PNG", pnginfo=info)
    logo = prepare_logo(out.getvalue())
    assert (logo.width, logo.height) == (40, 20)
    assert b"do not retain" not in logo.png_bytes()
    with Image.open(BytesIO(logo.png_bytes())) as normalized:
        assert normalized.getpixel((0, 0)) == (0, 139, 80, 128)


@pytest.mark.parametrize("raw", [b"", b"<svg><script>bad()</script></svg>", b"x" * 1048577, b"\x89PNG\r\n\x1a\ntruncated"],
                         ids=["empty", "svg", "oversized", "truncated-png"])
def test_bad_or_oversized_input_is_rejected(raw: bytes) -> None:
    with pytest.raises(ValueError):
        prepare_logo(raw)


def test_animation_oversized_pixels_and_empty_alpha_are_rejected() -> None:
    frames = [Image.new("RGBA", (10, 10), color) for color in ("red", "blue")]
    animated = BytesIO(); frames[0].save(animated, format="PNG", save_all=True, append_images=frames[1:])
    for raw in (animated.getvalue(), source_logo((3000, 2000)), source_logo(color="#00000000")):
        with pytest.raises(ValueError):
            prepare_logo(raw)


def test_wire_snapshot_rejects_forged_digest_size_metadata_and_extra_fields() -> None:
    logo = prepare_logo(source_logo())
    for changes in ({"sha256": "0" * 64}, {"width": 3}, {"height": True},
                    {"data_base64": "https://example.invalid/image.png"}, {"url": "file:///secret"}):
        with pytest.raises(ValidationError):
            CompanyLogo.model_validate({**logo.model_dump(), **changes})
    dirty = Image.new("RGBA", (40, 20), "green")
    info = PngImagePlugin.PngInfo(); info.add_text("private", "not allowed")
    stream = BytesIO(); dirty.save(stream, format="PNG", pnginfo=info)
    raw = stream.getvalue()
    with pytest.raises(ValidationError):
        CompanyLogo.model_validate({**logo.model_dump(), "width": 40, "height": 20,
                                   "data_base64": base64.b64encode(raw).decode(), "sha256": hashlib.sha256(raw).hexdigest()})


def test_logo_is_optional_and_its_bytes_are_covered_by_profile_digest() -> None:
    old = CompanyProfileData(display_name="Учебная компания")
    assert "logo" not in old.model_dump(mode="json")
    first = CompanyProfileData(display_name="Учебная компания", logo=prepare_logo(source_logo()))
    second = CompanyProfileData(display_name="Учебная компания", logo=prepare_logo(source_logo(color="blue")))
    assert len({profile_digest(old), profile_digest(first), profile_digest(second)}) == 3


def test_forged_oversized_png_header_is_rejected_before_pillow_decode() -> None:
    logo = prepare_logo(source_logo())
    raw = bytearray(logo.png_bytes())
    raw[16:24] = struct.pack(">II", 1_000_000, 1_000_000)
    raw[29:33] = struct.pack(">I", zlib.crc32(raw[12:29]))
    with pytest.raises(ValidationError, match="logo_dimensions_mismatch"):
        CompanyLogo.model_validate({**logo.model_dump(), "data_base64": base64.b64encode(raw).decode(),
                                   "sha256": hashlib.sha256(raw).hexdigest()})


def test_exif_orientation_is_applied_and_not_retained() -> None:
    image = Image.new("RGB", (40, 20), "green")
    exif = Image.Exif(); exif[274] = 6
    source = BytesIO(); image.save(source, format="JPEG", exif=exif)
    logo = prepare_logo(source.getvalue())
    assert (logo.width, logo.height) == (20, 40)
    with Image.open(BytesIO(logo.png_bytes())) as result:
        assert not result.info and not result.getexif()


def test_oversized_normalized_png_is_rejected_without_hidden_quality_reduction() -> None:
    image = Image.frombytes("RGBA", (512, 256), random.Random(0).randbytes(512 * 256 * 4))
    source = BytesIO(); image.save(source, format="PNG")
    assert len(source.getvalue()) < 1048576
    with pytest.raises(ValueError, match="слишком сложный"):
        prepare_logo(source.getvalue())
