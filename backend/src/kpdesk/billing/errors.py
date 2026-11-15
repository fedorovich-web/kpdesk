"""Only closed, public billing refusal codes cross the SQL/HTTP boundary."""
from sqlalchemy.exc import DBAPIError

from kpdesk.api.errors import ApiError

_MESSAGES = {
    'kpdesk_billing_payment_required': (402, 'billing_payment_required',
        'Оплатите доступ организации, чтобы продолжить работу. Сохранённые КП доступны для просмотра.'),
    'kpdesk_billing_quote_limit': (409, 'billing_quote_limit',
        'Лимит новых КП за оплаченный период исчерпан. Редакции и скачивание PDF доступны.'),
    'kpdesk_billing_catalog_limit': (409, 'billing_catalog_limit',
        'Число товаров превышает лимит оплаченного тарифа.'),
    'kpdesk_billing_member_limit': (409, 'billing_member_limit',
        'Лимит участников тарифа исчерпан. Проверьте участников и действующие приглашения.'),
    'kpdesk_billing_order_conflict': (409, 'billing_order_conflict',
        'Проверьте текущий тариф и ранее созданные счета перед новой оплатой.'),
    'kpdesk_billing_unavailable': (503, 'billing_unavailable',
        'Оплата временно недоступна. Повторите проверку позже.'),
    'kpdesk_billing_forbidden': (403, 'billing_forbidden',
        'Оплатой управляет владелец организации.'),
}


def billing_error(error: BaseException) -> ApiError | None:
    seen: set[int] = set()
    current: BaseException | None = error
    while current is not None and id(current) not in seen and len(seen) < 8:
        seen.add(id(current))
        if isinstance(current, DBAPIError):
            diag = getattr(current.orig, 'diag', None)
            message = getattr(diag, 'message_primary', None)
            if isinstance(message, str) and message in _MESSAGES:
                return ApiError(*_MESSAGES[message])
        current = current.__cause__
    return None
