"""Downloaded labels -> strict dataset -> real local fit, with no automatic activation."""
import json
from dataclasses import replace
from pathlib import Path
from uuid import uuid4

import pytest
from test_document_alternatives import saved

from kpdesk.matching.cli import load_dataset, main
from kpdesk.matching.feedback_bundle import (
    FeedbackDecision,
    FeedbackExportRequest,
    build_feedback_bundle,
)


def feedback_files(tmp_path: Path) -> list[Path]:
    quote, products = saved()
    assert quote.source_request is not None
    org, actor = uuid4(), uuid4()
    paths = []
    for i in range(6):
        line = quote.source_request.lines[0].model_copy(update={'description': f'Карандаш HB запрос {i}'})
        current = replace(quote, quote_id=uuid4(), source_request=quote.source_request.model_copy(update={'lines': [line]}))
        request = FeedbackExportRequest(expected_revision=1, source_snapshot_sha256=current.snapshot_sha256,
            reviewed=True, training_consent=True, decisions=(FeedbackDecision(
                line_number=1, relevant_sku='FIRST', rejected_skus=('SECOND',), no_match=False),))
        bundle = build_feedback_bundle(current, products, request, organization_id=org, reviewer_id=actor)
        path = tmp_path / f'feedback-{i}.json'
        path.write_text(bundle.model_dump_json(), encoding='utf-8')
        paths.append(path)
    return paths


def test_feedback_roundtrip_fit_and_single_test_evaluation(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    paths = feedback_files(tmp_path)
    output = tmp_path / 'dataset.json'
    args = ['feedback-dataset', '--feedback', *(str(p) for p in paths), '--output', str(output)]
    assert main(args) == 0
    capsys.readouterr()
    dataset = load_dataset(output)
    assert dataset.provenance_kind == 'manager_reviewed'
    assert not dataset.declared_independent_labels and not dataset.independent_labels
    assert dataset.requires_reviewed_negatives
    assert len(dataset.queries) == 6 and all(p.price_rub == 0 for p in dataset.products.values())
    root = tmp_path / 'models'
    model = root / 'candidate.joblib'
    assert main(['train', '--dataset', str(output), '--model', str(model), '--trusted-root', str(root)]) == 0
    capsys.readouterr()
    manifest = json.loads(model.with_suffix('.joblib.manifest.json').read_text(encoding='utf-8'))
    assert manifest['training_data_kind'] == 'manager_reviewed' and not manifest['production_eligible']
    assert main(['evaluate', '--dataset', str(output), '--model', str(model), '--trusted-root', str(root)]) == 0
    capsys.readouterr()
    with pytest.raises((ValueError, FileExistsError)):
        main(args)
    with pytest.raises(ValueError, match='already evaluated'):
        main(['evaluate', '--dataset', str(output), '--model', str(model), '--trusted-root', str(root)])


def test_manager_dataset_cannot_treat_unreviewed_candidates_as_negative(tmp_path: Path) -> None:
    paths = feedback_files(tmp_path)
    output = tmp_path / 'dataset.json'
    main(['feedback-dataset', '--feedback', *(str(p) for p in paths), '--output', str(output)])
    body = json.loads(output.read_text(encoding='utf-8'))
    body['queries'][0]['reviewed_negative_skus'] = []
    output.write_text(json.dumps(body), encoding='utf-8')
    with pytest.raises(ValueError, match='nonempty'):
        load_dataset(output)
