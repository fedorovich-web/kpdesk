"""Equivalent human/catalogue spellings keep the same constraints and ML features."""
from decimal import Decimal

import pytest

from kpdesk.matching.constraints import check_constraints
from kpdesk.matching.features import build_pair_features
from kpdesk.matching.profiles import CATEGORY_ATTRIBUTES
from kpdesk.matching.schema import CatalogProduct, RequestedItem
from scripts.build_a46_taxonomy_coverage import build_taxonomy_coverage


@pytest.mark.parametrize('field,catalog_value,request_value', [
    ('marker_type','whiteboard','для досок'), ('marker_type','permanent','перманентный'),
    ('ink_color','black','чёрный'), ('ink_color','blue','синий'),
])
def test_live_extraction_values_match_equivalent_marker_catalogue_values(field: str, catalog_value: str, request_value: str) -> None:
    base={'marker_type':'whiteboard','ink_color':'синий'}
    product=CatalogProduct(sku='M',name='Учебный маркер',category='marker',sale_unit='штука',
        pieces_per_sale_unit=1,price_rub=Decimal(10),attributes={**base,field:catalog_value})
    canonical=RequestedItem(description='Учебный маркер',category='marker',attributes={**base,field:catalog_value})
    human=canonical.model_copy(update={'attributes':{**base,field:request_value}})
    assert check_constraints(human,product).status=='compatible'
    assert build_pair_features(human,product)==build_pair_features(canonical,product)


def test_normalization_never_makes_different_marker_kinds_equivalent() -> None:
    product=CatalogProduct(sku='M',name='Маркер перманентный',category='marker',sale_unit='штука',
        pieces_per_sale_unit=1,price_rub=Decimal(10),attributes={'marker_type':'permanent','ink_color':'черный'})
    request=RequestedItem(description='Маркер для досок',category='marker',attributes={'marker_type':'для досок','ink_color':'black'})
    assert check_constraints(request,product).status=='incompatible'


@pytest.mark.parametrize('category', sorted(CATEGORY_ATTRIBUTES))
def test_all_forty_profiles_preserve_evidence_and_require_review_when_incomplete(category: str) -> None:
    # Schema-derived authored regression, not independent SKU accuracy labels.
    raw=next(row for row in build_taxonomy_coverage()['catalog'] if row['category']==category)
    product=CatalogProduct.model_validate(raw)
    attributes=dict(product.attributes)
    if category in ('toner_cartridge','ink_cartridge'):
        compatible=attributes.pop('compatible_models')
        assert isinstance(compatible,list)
        attributes['printer_model']=compatible[0]
    request=RequestedItem.model_validate({
        'description':product.name,'category':category,'quantity':1,'unit':product.sale_unit,
        'brand':product.brand,'paper_format':product.paper_format,'paper_gsm':product.paper_gsm,
        'pen_ink_color':product.pen_ink_color,'pen_ink_type':product.pen_ink_type,'attributes':attributes,
    })
    decision=check_constraints(request,product)
    if category=='pen':
        # This existing authored fixture omits the required legacy ink type.
        # Do not manufacture it from a differently named optional attribute.
        assert product.pen_ink_type is None
        assert decision.status=='needs_review' and 'pen_ink_type_unknown' in decision.reason_codes
    else:
        assert decision.status=='compatible'
    features=build_pair_features(request,product)
    assert features.compatible==int(category!='pen') and features.needs_review==int(category=='pen')


@pytest.mark.parametrize('field,catalog_value,request_value', [
    ('print_technology','laser','лазерная'), ('printer_technology','inkjet','струйная'),
    ('oem_status','compatible','совместимый'), ('color','black','чёрный'),
    ('pen_ink_type','ballpoint','шариковая'),
])
def test_equivalent_values_are_field_scoped(field: str, catalog_value: str, request_value: str) -> None:
    from kpdesk.matching.attribute_values import normalize_attribute_value

    assert normalize_attribute_value(field,catalog_value)==normalize_attribute_value(field,request_value)
    # Identical spelling is not permission to alias unrelated attributes or brands.
    assert normalize_attribute_value('brand',catalog_value)!=normalize_attribute_value('brand',request_value)


@pytest.mark.parametrize('category', sorted(CATEGORY_ATTRIBUTES))
def test_all_forty_categories_refuse_an_explicit_property_conflict(category: str) -> None:
    raw=next(row for row in build_taxonomy_coverage()['catalog'] if row['category']==category)
    if category not in ('paper','pen') and not raw.get('attributes'):
        # Existing six optional-attribute fixtures omit all properties. Add one
        # explicit synthetic fact for this contradiction test only.
        rule=CATEGORY_ATTRIBUTES[category][0]
        raw={**raw,'attributes':{rule.name:1 if rule.value_kind is int else 'synthetic-value'}}
    product=CatalogProduct.model_validate(raw)
    body={'description':product.name,'category':category,'quantity':1,'unit':product.sale_unit,
        'paper_format':product.paper_format,'paper_gsm':product.paper_gsm,
        'pen_ink_color':product.pen_ink_color,'pen_ink_type':product.pen_ink_type,'attributes':dict(product.attributes)}
    if category=='paper':
        body['paper_gsm']=120 if product.paper_gsm!=120 else 80
    elif category=='pen':
        body['pen_ink_color']='red' if product.pen_ink_color!='red' else 'blue'
    else:
        rule=next(rule for rule in CATEGORY_ATTRIBUTES[category] if rule.name in product.attributes and rule.value_kind is not list)
        actual=product.attributes[rule.name]
        assert isinstance(body['attributes'],dict)
        body['attributes'][rule.name]=actual+1 if isinstance(actual,int) else 'other-'+str(actual)
    request=RequestedItem.model_validate(body)
    assert check_constraints(request,product).status=='incompatible'
