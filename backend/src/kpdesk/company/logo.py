"""Bounded raster logos embedded in immutable company-profile snapshots."""
from __future__ import annotations

import base64
import hashlib
import struct
import warnings
import zlib
from io import BytesIO
from typing import Literal

from PIL import Image, ImageOps, UnidentifiedImageError
from pydantic import BaseModel, ConfigDict, Field, model_validator

MAX_UPLOAD_BYTES = 1024 * 1024
MAX_PNG_BYTES = 65536
MAX_WIDTH, MAX_HEIGHT = 640, 256
_PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"


def _clean_png_chunks(raw: bytes) -> None:
    if not raw.startswith(_PNG_SIGNATURE):
        raise ValueError("logo_png_required")
    offset, seen_data = 8, False
    while offset + 12 <= len(raw):
        size = struct.unpack_from(">I", raw, offset)[0]
        kind = raw[offset + 4:offset + 8]
        end = offset + size + 12
        if (end > len(raw) or kind not in (b"IHDR", b"IDAT", b"IEND")
            or (offset == 8) != (kind == b"IHDR")):
            raise ValueError("logo_png_chunks_invalid")
        expected_crc = struct.unpack_from(">I", raw, end - 4)[0]
        if zlib.crc32(raw[offset + 4:end - 4]) != expected_crc:
            raise ValueError("logo_png_crc_invalid")
        if kind == b"IDAT":
            seen_data = True
        if kind == b"IEND":
            if size != 0 or end != len(raw) or not seen_data:
                raise ValueError("logo_png_end_invalid")
            return
        offset = end
    raise ValueError("logo_png_incomplete")


class CompanyLogo(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)
    mime_type: Literal["image/png"] = "image/png"
    width: int = Field(ge=1, le=MAX_WIDTH)
    height: int = Field(ge=1, le=MAX_HEIGHT)
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    data_base64: str = Field(min_length=1, max_length=87384)

    def png_bytes(self) -> bytes:
        return base64.b64decode(self.data_base64, validate=True)

    @model_validator(mode="after")
    def verified_png(self) -> CompanyLogo:
        raw = self.png_bytes()
        if (not 1 <= len(raw) <= MAX_PNG_BYTES
            or base64.b64encode(raw).decode("ascii") != self.data_base64
            or hashlib.sha256(raw).hexdigest() != self.sha256):
            raise ValueError("logo_snapshot_mismatch")
        _clean_png_chunks(raw)
        if (len(raw) < 33 or raw[8:16] != b"\x00\x00\x00\x0dIHDR"
            or struct.unpack_from(">II", raw, 16) != (self.width, self.height)
            or raw[24:29] != b"\x08\x06\x00\x00\x00"):
            raise ValueError("logo_dimensions_mismatch")
        try:
            with Image.open(BytesIO(raw), formats=("PNG",)) as image:
                if image.size != (self.width, self.height) or image.mode != "RGBA":
                    raise ValueError("logo_dimensions_mismatch")
                image.load()
                if image.getchannel("A").getbbox() is None:
                    raise ValueError("logo_is_empty")
        except (OSError, SyntaxError) as error:
            raise ValueError("logo_png_invalid") from error
        return self


def prepare_logo(raw: bytes) -> CompanyLogo:
    if not 1 <= len(raw) <= MAX_UPLOAD_BYTES:
        raise ValueError("Файл логотипа должен быть не больше 1 МиБ.")
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(BytesIO(raw), formats=("PNG", "JPEG", "WEBP")) as probe:
                if (max(probe.size) > 4096 or probe.width * probe.height > 4_194_304
                    or getattr(probe, "n_frames", 1) != 1):
                    raise ValueError("Требуется статичное изображение до 4096 пикселей по стороне и 4 мегапикселей.")
                probe.verify()
            with Image.open(BytesIO(raw), formats=("PNG", "JPEG", "WEBP")) as source:
                oriented = ImageOps.exif_transpose(source).convert("RGBA")
                bounds = oriented.getchannel("A").getbbox()
                if bounds is None:
                    raise ValueError("Логотип полностью прозрачен.")
                cropped = oriented.crop(bounds)
                cropped.thumbnail((MAX_WIDTH, MAX_HEIGHT), Image.Resampling.LANCZOS)
                # A new pixel-only object cannot carry source EXIF/ICC/text chunks.
                clean = Image.frombytes("RGBA", cropped.size, cropped.tobytes())
                stream = BytesIO()
                clean.save(stream, format="PNG", optimize=True)
    except (UnidentifiedImageError, OSError, SyntaxError, Image.DecompressionBombError,
            Image.DecompressionBombWarning) as error:
        raise ValueError("Не удалось прочитать логотип. Выберите корректный PNG, JPEG или WebP.") from error
    normalized = stream.getvalue()
    if len(normalized) > MAX_PNG_BYTES:
        raise ValueError("Логотип слишком сложный для КП. Выберите изображение с меньшим количеством деталей.")
    return CompanyLogo(width=clean.width, height=clean.height,
                       sha256=hashlib.sha256(normalized).hexdigest(),
                       data_base64=base64.b64encode(normalized).decode("ascii"))
