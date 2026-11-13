from __future__ import annotations

import argparse
import hashlib
import json
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from kpdesk.config import PROJECT_ROOT
from kpdesk.matching.category_classifier import (
    CategoryClassifier,
    load_category_model,
    stratified_category_groups,
    suggest_category_text,
)
from kpdesk.matching.cli import load_dataset
from kpdesk.matching.normalize import normalize_text
from kpdesk.matching.profiles import CATEGORY_ATTRIBUTES
from kpdesk.matching.runtime import CATEGORY_NAME
from kpdesk.matching.training import LabeledQuery

_CHALLENGES: tuple[tuple[str, str], ...] = (
    ("pencil", "Графитовый пишущий инструмент из дерева для набросков"),
    ("mechanical_pencil", "Чертёжный автокарандаш с подачей грифеля без заточки"),
    ("highlighter", "Клиновидный выделитель абзацев флуоресцентными чернилами"),
    ("eraser", "Стереть карандашную линию мягкой резинкой"),
    ("sharpener", "Приспособление для заточки деревянного карандаша"),
    ("notepad", "Блок для коротких рабочих заметок на спирали"),
    ("document_folder", "Пластиковый конверт на кнопке для хранения договоров"),
    ("binder", "Архивный регистратор с раскрывающимися кольцами"),
    ("plastic_sleeve", "Перфорированный прозрачный карман под документы"),
    ("label", "Наклейки на листе с разметкой для лазерной печати"),
    ("adhesive_tape", "Рулон прозрачной узкой клейкой ленты для стола"),
    ("correction_fluid", "Замазка белая жидкая для ошибочных записей"),
    ("staple", "Расходные металлические скобки под офисный сшиватель"),
    ("scissors", "Ручные ножницы с кольцами для нарезки бумаги"),
    ("keyboard", "Полноразмерная панель клавиш ввода для компьютера"),
    ("toner_cartridge", "Порошковый чёрный печатный расходник для лазерного аппарата"),
    ("ink_cartridge", "Кассета с жидкими чернилами в струйный принтер"),
    ("printer", "Лазерный печатающий аппарат без сканера и копира"),
    ("mfp", "Офисное многофункциональное устройство печати и сканирования"),
    ("copier", "Ксерокс для размножения бумажных оригиналов"),
    ("photo_paper", "Глянцевые листы с фотопокрытием для снимков"),
    ("stationery_other", "Органайзер для хранения канцелярии на рабочем столе"),
)
_UNKNOWN: tuple[str, ...] = (
    "Шредер для уничтожения конфиденциальных документов",
    "Ламинатор с нагревательными валами для плёнки",
    "Настольная светодиодная лампа с регулируемым светом",
    "Сейф металлический с кодовым замком для офиса",
    "Кофемашина автоматическая для кухни сотрудников",
    "Аккумуляторные батарейки типа АА для электронных приборов",
    "Проектор мультимедийный с HDMI для переговорной",
    "Беспроводной маршрутизатор Wi-Fi для офиса",
    "Офисное кресло с колёсами и подлокотниками",
    "Настенный кондиционер сплит-система для кабинета",
    "Персональный настольный компьютер системный блок",
    "Калькулятор инженерный с научными функциями",
)
_MODEL_ROOT = PROJECT_ROOT / "ml/models"


def category_quality_checks(
    *,
    trained_categories: set[str],
    accepted_precision: float | None,
    accepted_group_precision: float | None,
    coverage: float,
    group_coverage: float,
    unknown_false_suggestions: int,
) -> dict[str, bool]:
    return {
        "all_taxonomy_classes_trained": trained_categories == set(CATEGORY_ATTRIBUTES),
        "minimum_test_precision_0_95": (
            accepted_precision is not None and accepted_precision >= 0.95
        ),
        "minimum_group_precision_0_95": (
            accepted_group_precision is not None and accepted_group_precision >= 0.95
        ),
        "minimum_test_coverage_0_50": coverage >= 0.50,
        "minimum_group_coverage_0_50": group_coverage >= 0.50,
        "zero_unknown_false_suggestions": unknown_false_suggestions == 0,
    }


def evaluate_coverage_candidate(dataset_path: Path, model_path: Path) -> dict[str, Any]:
    data = load_dataset(dataset_path)
    if data.provenance_kind != "synthetic_example":
        raise ValueError("A46 coverage evaluation requires synthetic source.")
    if {row.request.category for row in data.queries} != set(CATEGORY_ATTRIBUTES):
        raise ValueError("Candidate training data lack taxonomy categories.")
    originals = {normalize_text(row.request.description) for row in data.queries}
    challenge_texts = [phrase for _, phrase in _CHALLENGES] + list(_UNKNOWN)
    if (
        len({normalize_text(phrase) for phrase in challenge_texts}) != len(challenge_texts)
        or originals & {normalize_text(phrase) for phrase in challenge_texts}
        or {category for category, _ in _CHALLENGES} != (
            set(CATEGORY_ATTRIBUTES) - {
                "envelope", "folder", "glue", "marker", "marker_refill",
                "mouse", "mouse_pad", "notebook", "notebook_cover", "paper",
                "paperclip", "pen", "pen_refill", "ruler", "staple_remover",
                "stapler", "usb_adapter", "usb_drive"
            }
        )
    ):
        raise ValueError("Independent challenge identities overlap or taxonomy changed.")
    candidate = load_category_model(model_path)
    if candidate.dataset_sha256 != data.source_sha256:
        raise ValueError("Candidate model does not match dataset.")
    active = load_category_model(_MODEL_ROOT / CATEGORY_NAME)

    def positive_metrics(model: CategoryClassifier) -> dict[str, Any]:
        results = [
            (category, suggest_category_text(model, description))
            for category, description in _CHALLENGES
        ]
        accepted = sum(item.predicted is not None for _, item in results)
        correct = sum(category == item.predicted for category, item in results)
        return {
            "total": len(results),
            "accepted": accepted,
            "correct_accepted": correct,
            "incorrect_accepted": accepted - correct,
            "abstained": len(results) - accepted,
            "accepted_precision": correct / accepted if accepted else None,
            "coverage": accepted / len(results),
            "correct_categories": [
                category for category, item in results if category == item.predicted
            ],
            "incorrect_predictions": [
                {"expected": category, "predicted": item.predicted}
                for category, item in results
                if item.predicted is not None and category != item.predicted
            ],
        }

    def test_metrics(model: CategoryClassifier, records: Sequence[LabeledQuery]) -> dict[str, Any]:
        accepted = correct = 0
        accepted_groups: dict[str, list[bool]] = {}
        for record in records:
            suggestion = suggest_category_text(model, record.request.description)
            if suggestion.predicted is None:
                continue
            accepted += 1
            is_correct = suggestion.predicted == record.request.category
            correct += is_correct
            accepted_groups.setdefault(record.query_group_id, []).append(is_correct)
        correct_groups = sum(all(outcomes) for outcomes in accepted_groups.values())
        return {
            "queries": len(records),
            "accepted": accepted,
            "correct_accepted": correct,
            "incorrect_accepted": accepted - correct,
            "accepted_precision": correct / accepted if accepted else None,
            "coverage": accepted / len(records),
            "total_groups": len({record.query_group_id for record in records}),
            "accepted_groups": len(accepted_groups),
            "group_coverage": len(accepted_groups) / len({record.query_group_id for record in records}),
            "correct_accepted_groups": correct_groups,
            "incorrect_accepted_groups": len(accepted_groups) - correct_groups,
            "accepted_group_precision": (
                correct_groups / len(accepted_groups) if accepted_groups else None
            ),
        }

    def unknown_metrics(model: CategoryClassifier) -> dict[str, Any]:
        suggestions = [suggest_category_text(model, text) for text in _UNKNOWN]
        return {
            "total": len(suggestions),
            "rejected": sum(item.abstained for item in suggestions),
            "false_suggestions": [
                {"description": text, "predicted": item.predicted}
                for text, item in zip(_UNKNOWN, suggestions, strict=True)
                if item.predicted is not None
            ],
        }

    split = stratified_category_groups(data.queries, candidate.split_seed)
    held_out = test_metrics(candidate, split.test)
    unknown = unknown_metrics(candidate)
    quality_checks = category_quality_checks(
        trained_categories=set(map(str, candidate.pipeline.named_steps["classifier"].classes_)),
        accepted_precision=held_out["accepted_precision"],
        accepted_group_precision=held_out["accepted_group_precision"],
        coverage=held_out["coverage"],
        group_coverage=held_out["group_coverage"],
        unknown_false_suggestions=len(unknown["false_suggestions"]),
    )
    return {
        "artifact_format": "kpdesk-a46-taxonomy-coverage-diagnostic-v1",
        "production_eligible": False,
        "synthetic_only": True,
        "separate_challenges_not_used_for_fit_or_calibration": True,
        "test_is_reused_iteration_diagnostic": True,
        "quality_checks": quality_checks,
        "quality_gate_passed": all(quality_checks.values()),
        "additional_independent_acceptance_required": True,
        "held_out_test": held_out,
        "dataset_sha256": data.source_sha256,
        "candidate_sha256": hashlib.sha256(model_path.read_bytes()).hexdigest(),
        "active_sha256": hashlib.sha256((_MODEL_ROOT / CATEGORY_NAME).read_bytes()).hexdigest(),
        "candidate": {
            "unseen_categories": positive_metrics(candidate),
            "unknown_products": unknown,
        },
        "active": {
            "unseen_categories": positive_metrics(active),
            "unknown_products": unknown_metrics(active),
        },
    }


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dataset", type=Path, required=True)
    parser.add_argument("--model", type=Path, required=True)
    parser.add_argument("--report", type=Path, required=True)
    args = parser.parse_args(argv)
    report_path = args.report.resolve()
    if report_path.parent != _MODEL_ROOT.resolve() or report_path.suffix != ".json":
        raise ValueError("Report must be created in trusted model directory.")
    report = evaluate_coverage_candidate(args.dataset, args.model)
    with args.report.open("x", encoding="utf-8") as output:
        json.dump(report, output, ensure_ascii=False, sort_keys=True, indent=2)
        output.write("\n")
    print(json.dumps(report, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
