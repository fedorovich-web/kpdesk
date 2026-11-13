"""Separate model review of proposal claims against the submitted facts.

This is a fallible semantic check, not proof of truth or approval to send.
Unsupported, uncertain and incomplete reviews fail closed.
"""
from __future__ import annotations

import json
import re
from collections.abc import Awaitable, Callable
from typing import Literal

import httpx
from pydantic import BaseModel, ConfigDict, Field

from kpdesk.llm.client import (
    LLMError,
    structured_inference,
)
from kpdesk.llm.client import unique_object as _unique_json_object
from kpdesk.llm.settings import LLMSettings
from kpdesk.matching.attribute_values import normalize_attribute_value
from kpdesk.quotes.commercial_policy import REVIEW_PROMPT, has_company_facts
from kpdesk.quotes.document import ProposalNarrative

_REASONS = ('unsupported_property', 'invented_condition', 'quantity_claim',
            'source_attribution', 'contradiction', 'other_unsupported_claim')
_BOARD_WORD = re.compile(r'\b(?:доск(?:а|и|у|е|ой|ам|ами|ах)|досок)\b', re.IGNORECASE)
_MARKER_NOUN = re.compile(r'\bмаркер(?:ы|а|ов|ом|ами|у|ам|ах|е)?\b', re.IGNORECASE)
_MARKER_COLOR = r'(?:синий|ч[её]рный|красный|зел[её]ный|ж[её]лтый|белый|голубой|пурпурный)'
_COLOR_SUBJECT_BOUNDARY = (
    rf',\s*(?={_MARKER_COLOR}\s+(?:перманентный\s*)?[—–-])'
)
_BOARD_IDENTITY = re.compile(r'\bмаркер(?:ы)?\s+для\s+(?:маркерн\w*\s+)?досок\b', re.IGNORECASE)
_MARKER_PURPOSE = re.compile(
    r'\bмаркер(?:ы|а|ов|ом|ами)?\b'
    r'(?P<prefix>[^.!?;\n]{0,160}?(?:\.\s*Предназначен\w*\s*)?)'
    r'\b(?:для\s+(?:(?:стойкой|постоянной)\s+)?маркировки|маркировать)\s+(?P<media>[^.!?,;\n]{1,120})',
    re.IGNORECASE,
)
_MARKER_MEDIA = tuple(re.compile(pattern, re.IGNORECASE) for pattern in (
    r'\bдокумент\w*\b', r'\bповерхност\w*\b', r'\bофисн\w*\s+материал\w*\b',
))
_VARIANT_REFERENCE = re.compile(
    r'\b(?:вариант(?:а|е|ом)?\s*№?\s*(?P<number>[1-3])'
    r'|(?P<ordinal>перв(?:ый|ом|ого)|втор(?:ой|ом|ого)|трет(?:ий|ьем|ьего))\s+вариант(?:а|е|ом)?)\b',
    re.IGNORECASE,
)
_PROMPT = REVIEW_PROMPT


class _Issue(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    quote: str = Field(min_length=1, max_length=4000)
    reason: Literal['unsupported_property', 'invented_condition', 'quantity_claim',
                    'source_attribution', 'contradiction', 'other_unsupported_claim']


class _Section(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    status: Literal['supported', 'unsupported', 'uncertain']
    issues: list[_Issue] = Field(max_length=8)


class _Review(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    headline: _Section
    opening: _Section
    solution: _Section
    closing: _Section


class _WireIssue(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    passage_id: str = Field(min_length=1, max_length=40)
    reason: Literal['unsupported_property', 'invented_condition', 'quantity_claim',
                    'source_attribution', 'contradiction', 'other_unsupported_claim']


class _WireSection(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    status: Literal['supported', 'unsupported', 'uncertain']
    issues: list[_WireIssue] = Field(max_length=8)


class _WireReview(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    headline: _WireSection
    opening: _WireSection
    solution: _WireSection
    closing: _WireSection


def _marker_evidence(data: dict[str, object]) -> list[str]:
    """Only selected markers' names/types supply marker-specific evidence."""
    products = data.get('products')
    if not isinstance(products, list):
        return []
    result = []
    for product in products:
        if not isinstance(product, dict) or not isinstance(product.get('name'), str):
            continue
        name = product['name']
        specs = product.get('verified_specs')
        specs = specs if isinstance(specs, dict) else {}
        if specs.get('category') != 'marker' and not re.search(
            r'\b(?:маркер(?:ы|а|ов|ом|ами)?|markers?)\b', name, re.IGNORECASE,
        ):
            continue
        marker_type = specs.get('marker_type')
        result.append(name + (' ' + marker_type if isinstance(marker_type, str) else ''))
    return result


def _marker_media_issues(evidence: list[str], text: str) -> list[str]:
    """Catch observed positive marking claims; not a general surface parser."""
    issues = []
    for claim in _MARKER_PURPOSE.finditer(text):
        if re.search(r'\b(?:не|нельзя)\b', claim['prefix'], re.IGNORECASE):
            continue
        for medium in _MARKER_MEDIA:
            if medium.search(claim['media']) and not all(
                medium.search(source) and not re.search(r'\b(?:не|not)\b', source, re.IGNORECASE)
                for source in evidence
            ):
                issues.append(claim.group())
                break
    return issues


def _named_product_scopes(data: dict[str, object], text: str) -> list[tuple[str, dict[str, object]]]:
    """Keep an explicitly named SKU's claims bound to its own catalogue evidence.

    Longest overlapping names win; equal names remain ambiguous and retain all
    their evidence. Unnamed/group claims must hold for all selected markers.
    This deliberately does not infer identity from the property being claimed.
    """
    products = data.get('products')
    if not isinstance(products, list):
        return [(text, data)]
    named: dict[str, list[object]] = {}
    for product in products:
        if isinstance(product, dict) and isinstance(product.get('name'), str) and product['name'].strip():
            named.setdefault(product['name'].strip().casefold(), []).append(product)
    if not named:
        return [(text, data)]
    pattern = re.compile('|'.join(re.escape(name) for name in sorted(named, key=lambda name: (-len(name), name))), re.IGNORECASE)
    sentences: list[str] = []
    for sentence in re.findall(r'[^.!?\n]+(?:[.!?\n]+|$)', text):
        if sentences and re.match(r'\s*(?:предназначен\w*|подход\w*|он[аи]?)\b', sentence, re.IGNORECASE):
            sentences[-1] += sentence
        else:
            sentences.append(sentence)
    result = []
    for sentence in sentences:
        # Adversative clauses have separate subjects. Coordinated names joined
        # by "и" keep a shared predicate and must both support its claim.
        base_clauses = re.split(r';|,\s*(?:а|но)\s+', sentence, flags=re.IGNORECASE)
        clauses = []
        for base_clause in base_clauses:
            color_parts = re.split(_COLOR_SUBJECT_BOUNDARY, base_clause, flags=re.IGNORECASE)
            # Restrict this exception to the observed "Синий маркер
            # предназначен ..., чёрный [перманентный] — ..." construction.
            # Lists, relative/parenthetical verbs and plural predicates keep
            # their shared evidence context instead of losing the first SKU.
            independent = all(re.match(
                rf'\s*{_MARKER_COLOR}\s+маркер\s+(?:предназначен|подходит|используется|применяется)\b',
                part, re.IGNORECASE,
            ) for part in color_parts[:-1])
            collective_predicate = re.search(
                r'\b(?:предназначены|подходят|используются|применяются|являются)\b',
                base_clause, re.IGNORECASE,
            )
            if independent and not collective_predicate:
                clauses.extend(color_parts)
            else:
                clauses.append(base_clause)
        for clause in clauses:
            matches = list(pattern.finditer(clause))
            selected = [product for match in matches for product in named[match.group().casefold()]]
            collective = any(not any(match.start() <= group.start() < match.end() for match in matches)
                for group in re.finditer(r'\b(?:(?:все|оба|выбранные)\s+)?маркеры\b', clause, re.IGNORECASE))
            # A comma followed by a distinct color subject is another marker,
            # including the observed ellipsis "чёрный перманентный — ...".
            # Its identity comes from color, never the asserted use/type.
            elliptical_marker = bool(_MARKER_NOUN.search(sentence) and re.match(
                rf'\s*{_MARKER_COLOR}\s+(?:перманентный\s*)?[—–-]', clause, re.IGNORECASE,
            ))
            if not selected and (len(_MARKER_NOUN.findall(clause)) == 1 or elliptical_marker):
                def colors(value: str) -> set[str]:
                    return {color for word in re.findall(r'\w+', value)
                            if (color := normalize_attribute_value('ink_color', word))
                            in {'black','blue','red','green','yellow','white','cyan','magenta'}}
                mentioned = colors(clause)
                marker_products = [product for product in products
                    if isinstance(product, dict) and _marker_evidence({'products':[product]})]
                if len(mentioned) == 1:
                    selected = [product for product in marker_products
                        if mentioned <= colors(str(product.get('name','')) + ' ' + str(product.get('verified_specs',{})))]
            # Explicit quantification applies to every selected marker, even if
            # a descriptor could otherwise identify one of them.
            universal = re.search(r'\b(?:все|оба|эти|выбранные)\s+маркер\w*\b', clause, re.IGNORECASE)
            context = {**data, 'products': selected} if selected and not universal and (matches or not collective) else data
            result.append((clause, context))
    return result


def _surface_scopes(data: dict[str, object], text: str) -> list[tuple[str, dict[str, object]]]:
    """Scope observed surface guards by explicit variant references, never pool facts."""
    alternatives = data.get('alternatives')
    if not isinstance(alternatives, list) or not alternatives:
        return [(text, data)]
    variants = {1: data}
    for variant in alternatives:
        if isinstance(variant, dict) and type(variant.get('variant_number')) is int:
            number = variant['variant_number']
            if number in (2, 3):
                variants[number] = variant
    result: list[tuple[str, dict[str, object]]] = []
    for sentence in re.findall(r'[^.!?\n]+(?:[.!?\n]+|$)', text):
        # A property of both/all variants must be supported independently in each.
        if re.search(r'\b(?:обоих|оба|обе|обоим|всех|все|всем)\s+вариант\w*\b', sentence, re.IGNORECASE):
            result.extend((sentence, context) for context in variants.values())
            continue
        start = 0
        context = data
        for reference in _VARIANT_REFERENCE.finditer(sentence):
            result.append((sentence[start:reference.start()], context))
            ordinal = (reference['ordinal'] or '').casefold()
            number = int(reference['number']) if reference['number'] else (
                1 if ordinal.startswith('перв') else 2 if ordinal.startswith('втор') else 3
            )
            # References apply to this sentence only; unknown variants get no new evidence.
            context = variants.get(number, data)
            start = reference.start()
        result.append((sentence[start:], context))
    return result


def _paper_print_technology_issues(data: dict[str, object], text: str) -> list[str]:
    """Observed positive paper-use claims need product evidence, not wishes.

    This is a bounded guard, not a general compatibility classifier. A generic
    paper claim must hold for each selected paper in its own variant; an explicit
    product name scopes the claim to that product.
    """
    def strings(value: object) -> list[str]:
        if isinstance(value, str):
            return [value]
        if isinstance(value, dict):
            return [item for child in value.values() for item in strings(child)]
        if isinstance(value, list):
            return [item for child in value for item in strings(child)]
        return []

    products = data.get('products')
    if not isinstance(products, list):
        return []
    papers = []
    for product in products:
        if not isinstance(product, dict) or not isinstance(product.get('name'), str):
            continue
        specs = product.get('verified_specs')
        specs = specs if isinstance(specs, dict) else {}
        if specs.get('category') == 'paper' or re.search(r'\b(?:фото)?бумаг\w*\b|\bpaper\b', product['name'], re.IGNORECASE):
            papers.append((product['name'], [product['name'], *strings(specs)]))
    if not papers:
        return []
    claim = re.compile(
        r'\bбумаг\w*\b[^.!?;\n]{0,120}?\b(?P<negative>не\s+)?'
        r'(?:подходит|подходят|предназначен\w*|пригодн\w*|совместим\w*|для)\b'
        r'(?P<use>[^.!?;\n]*)', re.IGNORECASE,
    )
    technologies = (
        re.compile(r'\b(?:лазер\w*|laser)\b', re.IGNORECASE),
        re.compile(r'\b(?:струйн\w*|inkjet)\b', re.IGNORECASE),
        re.compile(r'\b(?:копир\w*|copier)\b', re.IGNORECASE),
        re.compile(r'\b(?:факс\w*|fax)\b', re.IGNORECASE),
    )
    negative = re.compile(r'\b(?:не|not|кроме|исключая)\s+(?:для\s+)?$', re.IGNORECASE)

    def confirms(sources: list[str], technology: re.Pattern[str]) -> bool:
        return any(not negative.search(source[:match.start()])
                   for source in sources for match in technology.finditer(source))

    def unsupported(match: re.Match[str]) -> bool:
        named = [(name, sources) for name, sources in papers if name.casefold() in match.group().casefold()]
        # A short catalog name can be a prefix of the explicitly named product.
        specific = [(name, sources) for name, sources in named
                    if not any(name != other and name.casefold() in other.casefold() for other, _ in named)]
        selected = specific or papers
        return any(technology.search(match['use'])
                   and not all(confirms(sources, technology) for _, sources in selected)
                   for technology in technologies)

    return [match.group() for match in claim.finditer(text) if not match['negative'] and unsupported(match)]


def _context_issues(facts: str, draft: ProposalNarrative) -> list[dict[str, str]]:
    """Targeted guards for observed unit/provenance substitutions, not NLP proof."""
    data = json.loads(facts)
    if not isinstance(data, dict):
        return []
    brief = data.get('manager_brief', '')
    if not isinstance(brief, str):
        brief = ''
    workplaces = re.compile(r'(?<![\w.,+\-])(\d{1,6})\s+рабоч\w*\s+мест\w*', re.IGNORECASE)
    employees = re.compile(r'(?<![\w.,+\-])(\d{1,6})\s+сотрудник\w*', re.IGNORECASE)
    negated = re.compile(r'\b(?:не|нет|без)\s*$', re.IGNORECASE)
    workplace_counts = {m.group(1) for m in workplaces.finditer(brief) if not negated.search(brief[:m.start()])}
    employee_counts = {m.group(1) for m in employees.finditer(brief) if not negated.search(brief[:m.start()])}
    attribution = re.compile(
        r'\b(?:вы\s+запросили|в\s+(?:ваш(?:ем|ей)\s+)?(?:запросе|заявке)\s+указан\w*'
        r'|по\s+ваш(?:ему|ей)\s+(?:запросу|заявке))'
        r'|\bсоответству\w*\s+(?:заявлен\w*\s+характеристик\w*|ваш\w*\s+(?:запрос\w*|заявк\w*))',
        re.IGNORECASE,
    )
    issues = []
    # Current product facts contain no verified consumption-rate evidence.
    # These observed effect claims cannot be waived by a model's supported vote.
    consumption = re.compile(r'\bэкономичн\w*\s+расход\w*|\b(?:снижа|уменьша|сокраща)(?:ет|ют)\s+расход\w*', re.IGNORECASE)
    print_quality = re.compile(r'\b(?:обеспечива(?:ет|ют)|гарантиру(?:ет|ют))\s+ч[её]тк\w*\s+(?:изображени\w*|печать\w*|текст\w*)', re.IGNORECASE)
    supplier_help = re.compile(r'\b(?:поможем|готовы\s+помочь)\s+сопостав\w*', re.IGNORECASE)
    for name, text in draft.model_dump().items():
        for pattern, reason in ((print_quality, 'unsupported_print_quality'),
                                (supplier_help, 'supplier_claim_without_company_facts')):
            if reason == 'supplier_claim_without_company_facts' and has_company_facts(data):
                continue
            for match in pattern.finditer(text):
                if not negated.search(text[:match.start()]):
                    issues.append({'section': name, 'quote': match.group(), 'reason': reason})
        for match in consumption.finditer(text):
            if not negated.search(text[:match.start()]):
                issues.append({'section': name, 'quote': match.group(), 'reason': 'unsupported_consumption_effect'})
        for match in employees.finditer(text):
            if match.group(1) in workplace_counts - employee_counts and not negated.search(text[:match.start()]):
                issues.append({'section': name, 'quote': match.group(), 'reason': 'unsupported_headcount'})
        if data.get('selection_origin') == 'manager_selection':
            for match in attribution.finditer(text):
                issues.append({'section': name, 'quote': match.group(), 'reason': 'unsupported_source_attribution'})
        for scoped_text, scoped_data in _surface_scopes(data, text):
            for claim in _paper_print_technology_issues(scoped_data, scoped_text):
                issues.append({'section': name, 'quote': claim, 'reason': 'unsupported_print_technology'})
            for product_text, product_data in _named_product_scopes(scoped_data, scoped_text):
                marker_evidence = _marker_evidence(product_data)
                evidence = [bool(
                    (_BOARD_WORD.search(source) or re.search(r'\bwhiteboard\b', source, re.IGNORECASE))
                    and not re.search(r'\b(?:не|not)\b', source, re.IGNORECASE)
                    ) for source in marker_evidence]
                group_claim = re.search(r'\b(?:все|оба|эти|выбранные)\s+маркер\w*\b', product_text, re.IGNORECASE)
                board_evidence = all(evidence) if product_data is not scoped_data or group_claim or _MARKER_NOUN.search(product_text) else any(evidence)
                if marker_evidence and not board_evidence:
                    # A confirmed catalogue name is an identity, not a separate
                    # property assertion about the other products in a list.
                    claim_text = product_text
                    own_products = product_data.get('products')
                    for product in own_products if isinstance(own_products,list) else []:
                        if isinstance(product,dict) and isinstance(product.get('name'),str):
                            claim_text = re.sub(re.escape(product['name']), '', claim_text, flags=re.IGNORECASE)
                    enumeration = (re.search(r'\bи\b', claim_text, re.IGNORECASE)
                        and len(_MARKER_NOUN.findall(claim_text)) >= 2 and not group_claim
                        and not re.search(r'\b(?:это|являются|подходят|предназначены)\b', claim_text, re.IGNORECASE))
                    if any(evidence) and enumeration:
                        # A grounded type label in an enumeration is not a
                        # compatibility predicate for the other marker type.
                        def remove_grounded_identity(match: re.Match[str], source_text: str = claim_text) -> str:
                            if re.search(r'\bперманентн\w*\s*$', source_text[:match.start()], re.IGNORECASE):
                                return match.group()
                            return ''
                        claim_text = _BOARD_IDENTITY.sub(remove_grounded_identity, claim_text)
                    for match in _BOARD_WORD.finditer(claim_text):
                        issues.append({'section': name, 'quote': match.group(), 'reason': 'unsupported_surface'})
                if marker_evidence:
                    for claim in _marker_media_issues(marker_evidence, product_text):
                        issues.append({'section': name, 'quote': claim, 'reason': 'unsupported_surface'})
    return issues


class NarrativeReviewRejected(LLMError):
    """Validated findings for one bounded revision; never put feedback in logs."""

    def __init__(self, review: _Review | None, context_issues: list[dict[str, str]] | None = None) -> None:
        super().__init__('unverified_narrative')
        if review is None and not context_issues:
            raise ValueError('deterministic_rejection_requires_findings')
        self.feedback: dict[str, object] = review.model_dump(mode='json') if review is not None else {
            'review_origin': 'deterministic_context', 'model_review_performed': False,
        }
        if context_issues:
            self.feedback['context_issues'] = context_issues
        context_sections = {issue['section'] for issue in context_issues or []}
        self.sections_to_revise = tuple(
            name for name in ProposalNarrative.model_fields
            if (review is not None and getattr(review, name).status != 'supported') or name in context_sections
        )
        self.can_revise = bool(self.sections_to_revise) and all(
            review is None or getattr(review, name).status != 'uncertain' for name in ProposalNarrative.model_fields
        )


def _schema(passages: dict[str, dict[str, str]]) -> dict[str, object]:
    properties = {}
    for name, parts in passages.items():
        issue = {'type': 'object', 'properties': {
            'passage_id': {'type': 'string', 'enum': list(parts)},
            'reason': {'type': 'string', 'enum': list(_REASONS)},
        }, 'required': ['passage_id', 'reason'], 'additionalProperties': False}
        properties[name] = {'type': 'object', 'properties': {
            'status': {'type': 'string', 'enum': ['supported', 'unsupported', 'uncertain']},
            'issues': {'type': 'array', 'items': issue},
        }, 'required': ['status', 'issues'], 'additionalProperties': False}
    return {'type': 'object', 'properties': properties,
            'required': list(ProposalNarrative.model_fields), 'additionalProperties': False}


async def review_proposal_narrative(
    facts: str, draft: ProposalNarrative, settings: LLMSettings, client: httpx.AsyncClient,
    *, before_request: Callable[[str], Awaitable[None] | None] | None = None,
) -> None:
    # Known local failures need no paid vote and no quota reservation. Do not
    # invent model verdicts for sections that have not been semantically checked.
    local_issues = _context_issues(facts, draft)
    if local_issues:
        raise NarrativeReviewRejected(None, local_issues)
    # Fixed-size exact slices cover every character, including whitespace/typos.
    # These are reference anchors, not a claim that a chunk is a complete sentence.
    passages = {name: {f'{name}:{start // 400}': value[start:start + 400]
                       for start in range(0, len(value), 400)}
                for name, value in draft.model_dump().items()}
    anchored = {name: [{'id': key, 'text': value} for key, value in parts.items()]
                for name, parts in passages.items()}
    payload = json.dumps({'facts': json.loads(facts), 'draft': anchored},
                         ensure_ascii=False, separators=(',', ':'))
    if len(payload) > 12_000:
        raise LLMError('review_context_too_large')

    def parse(raw: str) -> _Review:
        wire = _WireReview.model_validate(json.loads(raw, object_pairs_hook=_unique_json_object))
        sections: dict[str, _Section] = {}
        for name in ProposalNarrative.model_fields:
            section: _WireSection = getattr(wire, name)
            if any(issue.passage_id not in passages[name] for issue in section.issues):
                raise ValueError('invalid_review_passage')
            if section.status == 'supported' and section.issues:
                raise ValueError('inconsistent_review')
            if section.status == 'unsupported' and not section.issues:
                raise ValueError('missing_review_issue')
            sections[name] = _Section(status=section.status, issues=[
                _Issue(quote=passages[name][issue.passage_id], reason=issue.reason) for issue in section.issues
            ])
        result = _Review.model_validate(sections)
        context_issues = _context_issues(facts, draft)
        if context_issues or any(getattr(result, name).status != 'supported' for name in ProposalNarrative.model_fields):
            raise NarrativeReviewRejected(result, context_issues)
        return result

    if before_request is not None:
        gate = before_request(payload)
        if gate is not None:
            await gate
    await structured_inference(
        payload, settings, client, system_prompt=_PROMPT,
        schema_name='kpdesk_proposal_fact_review_v2', schema=_schema(passages), parse=parse,
    )
