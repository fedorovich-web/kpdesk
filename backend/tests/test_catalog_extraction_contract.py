"""Saved NVIDIA facts were correct; property evidence must cite its own clause."""
import json
from pathlib import Path

import pytest

from kpdesk.llm.client import LLMError
from kpdesk.universal.extraction import _ground, _schema
from kpdesk.universal.schema import UniversalRequest


def captured() -> tuple[str,UniversalRequest]:
    data=json.loads((Path(__file__).parent/'fixtures/catalog_extraction_v1.json').read_text(encoding='utf-8'))
    return data['source'],UniversalRequest.model_validate(data['output'])


def test_saved_live_values_are_correct_but_broad_property_citations_rejected() -> None:
    source,request=captured()
    assert [str(line.quantity) for line in request.lines]==['100.5','2','1.25']
    assert request.lines[0].requirements[0].value=='медь'
    assert request.lines[1].requirements[0].value==40
    assert request.lines[1].requirements[0].operator=='min'
    with pytest.raises(LLMError,match='invalid_output'):_ground(source,request)


def test_only_property_spans_change_in_explicit_offline_counterfactual() -> None:
    source,request=captured()
    payload=request.model_dump(mode='json')
    payload['lines'][0]['requirements'][0]['source_quote']='материал медь'
    payload['lines'][1]['requirements'][0]['source_quote']='напор не менее 40 м'
    corrected=UniversalRequest.model_validate(payload)
    _ground(source,corrected)
    for original,line in zip(request.lines,corrected.lines,strict=True):
        assert original.model_dump(exclude={'requirements'})==line.model_dump(exclude={'requirements'})
        for before,after in zip(original.requirements,line.requirements,strict=True):
            assert before.model_dump(exclude={'source_quote'})==after.model_dump(exclude={'source_quote'})


def test_property_schema_explains_own_clause_instead_of_item_quote() -> None:
    schema=json.loads(json.dumps(_schema()))
    properties=schema['properties']['lines']['items']['properties']
    description=properties['requirements']['items']['properties']['source_quote'].get('description','')
    assert 'название товара' in description and 'количество покупки' in description
    assert 'материал медь' in description
