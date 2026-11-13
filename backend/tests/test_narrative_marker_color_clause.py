"""A saved false rejection: independently colored marker subjects share a sentence."""
import json
from pathlib import Path
from typing import Any

import httpx
import pytest
from test_narrative_review import response, review

from kpdesk.llm.settings import LLMSettings
from kpdesk.quotes.document import ProposalNarrative
from kpdesk.quotes.narrative_review import _context_issues, review_proposal_narrative


def captured() -> tuple[dict[str, Any], ProposalNarrative]:
    fixture=json.loads((Path(__file__).parent/'fixtures/narrative_marker_color_clause.json').read_text(encoding='utf-8'))
    return fixture['facts'], ProposalNarrative.model_validate(fixture['draft'])


def neutral_draft(solution: str) -> ProposalNarrative:
    return ProposalNarrative(headline='Офисные товары',opening='Предлагаем выбранные позиции.',
        solution=solution,closing='Подтвердите состав.')


def test_saved_solution_binds_board_use_to_blue_marker_not_black_subject() -> None:
    facts, draft=captured()
    assert _context_issues(json.dumps(facts),neutral_draft(draft.solution))==[]


def test_ambiguous_saved_plural_headline_still_requires_revision() -> None:
    facts,draft=captured()
    issues=_context_issues(json.dumps(facts),draft)
    assert issues and {issue['section'] for issue in issues}=={'headline'}


@pytest.mark.parametrize('claim',[
    'Чёрный маркер предназначен для записей на доске, синий перманентный — для маркировки.',
    'Синий маркер предназначен для записей на доске, чёрный перманентный — для доски.',
    'Оба маркера предназначены для досок, чёрный перманентный — для маркировки.',
    'Синий и чёрный маркеры предназначены для досок.',
    'Серый маркер предназначен для досок, чёрный перманентный — для маркировки.',
    'Чёрный маркер, синий маркер предназначены для досок.',
    'Синий маркер, чёрный маркер предназначены для досок.',
    'Чёрный маркер (используется для маркировки), синий маркер предназначены для досок.',
    'Чёрный маркер, который используется для маркировки, синий маркер предназначены для досок.',
    'Чёрный маркер используется для маркировки, синий — предназначены для досок.',
])
def test_other_or_unresolved_subject_cannot_borrow_blue_board_evidence(claim: str) -> None:
    facts,_=captured()
    assert any(issue['reason']=='unsupported_surface'
               for issue in _context_issues(json.dumps(facts),neutral_draft(claim)))


def test_two_same_color_markers_remain_ambiguous() -> None:
    facts,draft=captured()
    facts['products'][2]['name']='Маркер перманентный синий'
    facts['products'][2]['verified_specs']['ink_color']='синий'
    assert _context_issues(json.dumps(facts),neutral_draft(draft.solution))


@pytest.mark.asyncio
async def test_unambiguous_saved_solution_still_needs_separate_model_review() -> None:
    facts,draft=captured()
    calls=[]
    def provider(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return response(review())
    async with httpx.AsyncClient(transport=httpx.MockTransport(provider)) as client:
        await review_proposal_narrative(json.dumps(facts),neutral_draft(draft.solution),
            LLMSettings(api_key='offline',test_project_approved=True),client)
    assert len(calls)==1


pytestmark=pytest.mark.usefixtures('mock_llm_operation')
