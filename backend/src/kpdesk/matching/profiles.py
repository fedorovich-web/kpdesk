"""Versioned product taxonomy and deterministic attribute rules.

Adding a product category changes this registry, not matching business logic.
Supplies with device compatibility have additional explicit hard constraints.
"""

from dataclasses import dataclass
from typing import Literal

TAXONOMY_VERSION = "kpdesk-taxonomy-v4"
type AttributeRule = Literal["exact", "minimum"]


@dataclass(frozen=True, slots=True)
class ProductAttributeRule:
    name: str
    mode: AttributeRule
    value_kind: type[str | int | list[str]]


def e(name: str) -> ProductAttributeRule:
    return ProductAttributeRule(name, "exact", str)


def n(name: str) -> ProductAttributeRule:
    return ProductAttributeRule(name, "exact", int)


def min_n(name: str) -> ProductAttributeRule:
    return ProductAttributeRule(name, "minimum", int)


_CARTRIDGE = (
    e("cartridge_code"), e("printer_model"),
    ProductAttributeRule("compatible_models", "exact", list),
    e("color"), e("oem_status"),
)
_PRINTER = (e("print_technology"), e("color_mode"), e("max_format"))

CATEGORY_ATTRIBUTES: dict[str, tuple[ProductAttributeRule, ...]] = {
    "paper": (),
    "pen": (e("pen_type"), n("tip_mm_x10")),
    "pencil": (e("lead_grade"), e("pencil_type")),
    "mechanical_pencil": (e("lead_grade"), n("lead_diameter_mm_x10")),
    "marker": (e("ink_color"), e("marker_type"), n("tip_mm_x10")),
    "highlighter": (e("ink_color"), e("tip_shape")),
    "eraser": (e("eraser_type"),),
    "sharpener": (e("sharpener_type"), n("holes")),
    "notebook": (e("format"), n("sheets"), e("ruling")),
    "notepad": (e("format"), n("sheets"), e("ruling")),
    "document_folder": (e("folder_kind"), e("format"), e("material")),
    "binder": (e("format"), n("rings"), n("spine_mm")),
    "plastic_sleeve": (e("format"), n("thickness_microns")),
    "envelope": (e("format"), e("color")),
    "label": (e("format"), n("labels_per_sheet"), e("printer_technology")),
    "adhesive_tape": (n("width_mm"), n("length_m"), e("tape_type")),
    "glue": (e("glue_type"), n("volume_ml")),
    "correction_fluid": (e("correction_type"),),
    "staple": (e("staple_size"), n("pack_count")),
    "scissors": (n("length_mm"),),
    "ruler": (n("length_mm"),),
    "folder": (n("spine_mm"), e("format")),
    "stapler": (min_n("capacity_sheets"),),
    "paperclip": (n("size_mm"), n("pack_count")),
    "keyboard": (e("connection"), n("keys")),
    "mouse": (e("connection"), min_n("dpi")),
    "mouse_pad": (e("surface_material"), n("width_mm"), n("height_mm")),
    "pen_refill": (e("ink_color"), e("refill_type")),
    "usb_adapter": (e("connector_from"), e("connector_to")),
    "notebook_cover": (e("format"), e("material")),
    "staple_remover": (e("remover_type"),),
    "marker_refill": (e("refill_type"), e("ink_color")),
    "usb_drive": (n("capacity_gb"), e("usb_standard")),
    "toner_cartridge": _CARTRIDGE,
    "ink_cartridge": _CARTRIDGE,
    "printer": _PRINTER,
    "mfp": _PRINTER,
    "copier": _PRINTER,
    "photo_paper": (e("format"), n("gsm"), e("finish")),
    "stationery_other": (e("product_kind"),),
}

REQUIRED_PRODUCT_ATTRIBUTES: dict[str, frozenset[str]] = {
    "paper": frozenset(),
    "pen": frozenset(),
    "pencil": frozenset({"pencil_type"}),
    "mechanical_pencil": frozenset({"lead_diameter_mm_x10"}),
    "marker": frozenset({"ink_color", "marker_type"}),
    "highlighter": frozenset({"ink_color"}),
    "eraser": frozenset({"eraser_type"}),
    "sharpener": frozenset({"sharpener_type"}),
    "notebook": frozenset({"format", "sheets"}),
    "notepad": frozenset({"format", "sheets"}),
    "document_folder": frozenset({"folder_kind", "format"}),
    "binder": frozenset({"format", "rings"}),
    "plastic_sleeve": frozenset({"format"}),
    "envelope": frozenset({"format"}),
    "label": frozenset({"format", "labels_per_sheet"}),
    "adhesive_tape": frozenset({"width_mm", "length_m"}),
    "glue": frozenset({"glue_type"}),
    "correction_fluid": frozenset({"correction_type"}),
    "staple": frozenset({"staple_size"}),
    "scissors": frozenset({"length_mm"}),
    "ruler": frozenset({"length_mm"}),
    "folder": frozenset({"spine_mm"}),
    "stapler": frozenset({"capacity_sheets"}),
    "paperclip": frozenset({"size_mm", "pack_count"}),
    "keyboard": frozenset({"connection", "keys"}),
    "mouse": frozenset({"connection", "dpi"}),
    "mouse_pad": frozenset(),
    "pen_refill": frozenset(),
    "usb_adapter": frozenset(),
    "notebook_cover": frozenset(),
    "staple_remover": frozenset(),
    "marker_refill": frozenset(),
    "usb_drive": frozenset({"capacity_gb", "usb_standard"}),
    "toner_cartridge": frozenset({"cartridge_code", "compatible_models", "color", "oem_status"}),
    "ink_cartridge": frozenset({"cartridge_code", "compatible_models", "color", "oem_status"}),
    "printer": frozenset({"print_technology", "color_mode", "max_format"}),
    "mfp": frozenset({"print_technology", "color_mode", "max_format"}),
    "copier": frozenset({"print_technology", "color_mode", "max_format"}),
    "photo_paper": frozenset({"format", "gsm", "finish"}),
    "stationery_other": frozenset({"product_kind"}),
}
if CATEGORY_ATTRIBUTES.keys() != REQUIRED_PRODUCT_ATTRIBUTES.keys():
    raise RuntimeError("Category profile and required-attribute registries differ.")
