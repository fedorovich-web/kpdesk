"""Stable API errors, without leaking SQL, secrets or request content."""

from uuid import uuid4

from fastapi import Request
from fastapi.responses import JSONResponse


def llm_error_message(code: str) -> str:
    """Fixed customer copy; never include provider bodies or request content."""
    return {
        'invalid_output': 'Ответ ИИ не прошёл проверку. Проверьте исходные данные и уточните запрос.',
        'incomplete_output': 'ИИ вернул неполный ответ. Результат не принят. Сократите запрос или повторите попытку позже.',
        'unverified_narrative': 'Не удалось подтвердить факты в тексте КП. Проверьте товары и условия предложения.',
        'provider_timeout': 'Сервис ИИ не ответил вовремя. Проверьте результат запроса перед повторной генерацией.',
        'provider_unreachable': 'Не удалось связаться с сервисом ИИ. Проверьте результат запроса перед повторной попыткой.',
        'provider_error': 'Сервис ИИ вернул ошибку. Проверьте результат запроса перед повторной попыткой.',
        'rate_limited': 'Сервис ИИ ограничил частоту запросов. Повторите попытку позже.',
        'inference_quota_exceeded': 'Достигнут лимит запросов к ИИ. Проверьте доступный лимит перед новой генерацией.',
        'duplicate_inference_in_progress': 'Повтор запроса к ИИ заблокирован. Проверьте результат предыдущего запроса перед новой генерацией.',
        'organization_forbidden': 'Нет доступа к запросам ИИ для этой организации. Обратитесь к её руководителю.',
        'review_context_too_large': 'В КП слишком много данных для проверки текста. Сократите задачу или разделите предложение.',
        'correction_context_too_large': 'В КП слишком много данных для исправления текста. Сократите задачу или разделите предложение.',
        'provider_auth_failed': 'Подключение к ИИ требует проверки настроек. Обратитесь к администратору KPDesk.',
        'provider_model_unavailable': 'Выбранная модель ИИ сейчас недоступна. Обратитесь к администратору KPDesk.',
        'quota_ledger_unavailable': 'Не удалось подтвердить учёт запроса к ИИ. Проверьте результат перед повторной генерацией.',
    }.get(code, 'Не удалось выполнить запрос к ИИ. Проверьте результат перед повторной попыткой.')


def quote_conflict_message(code: str) -> str:
    """Explain an existing refusal without exposing storage diagnostics."""
    return {
        'stale_catalog': 'Активный прайс изменился. Обновите подбор и проверьте цены перед сохранением КП.',
        'stale_revision': 'КП изменилось после открытия. Откройте последнюю версию и проверьте изменения.',
        'stale_snapshot': 'Данные КП изменились после проверки. Откройте последнюю версию и проверьте её перед утверждением.',
        'idempotency_conflict': 'Этот запрос уже использован для другого действия с КП. Проверьте сохранённый результат перед новой попыткой.',
        'approved_document_required': 'Сначала проверьте и утвердите КП. После этого можно создать ссылку для клиента.',
        'quote_already_approved': 'КП уже утверждено. Откройте его утверждённую версию.',
        'quote_not_ready_for_approval': 'КП пока не готово к утверждению. Проверьте состав, количества, цены и ограничение бюджета.',
        'share_issue_conflict': 'Не удалось подтвердить создание ссылки. Проверьте список ссылок и текущую версию КП перед повторной попыткой.',
    }.get(code, 'Не удалось подтвердить выполнение действия с КП. Обновите предложение и проверьте его состояние перед повторной попыткой.')


def quote_pdf_error_message(code: str) -> str:
    """Safe display text only; PDF refusal codes and HTTP statuses stay in routes."""
    return {
        'incomplete_draft': 'КП пока не готово к PDF. Проверьте состав, количества, цены и ограничение бюджета.',
        'incomplete_commercial_document': 'Не удалось подтвердить целостность утверждённого КП. Откройте сохранённую версию; если ошибка повторится, обратитесь к администратору KPDesk.',
        'inconsistent_quote_total': 'Суммы в КП не согласованы. Проверьте расчёт перед скачиванием PDF.',
        'inconsistent_quote_rules': 'Условия расчёта КП не прошли проверку. Проверьте правила и суммы предложения.',
        'inconsistent_quote_brand_policy': 'Товары в КП не соответствуют выбранным ограничениям по брендам. Проверьте состав предложения.',
        'inconsistent_document_alternative': 'Дополнительный вариант КП не прошёл проверку. Заново подберите варианты и проверьте их состав и суммы.',
        'quote_already_approved': 'КП уже утверждено. Откройте его утверждённую версию.',
    }.get(code, 'Не удалось подготовить PDF. Попробуйте позже или обратитесь к администратору KPDesk.')


def company_logo_error_message(reason: str) -> str:
    """Keep useful known validation hints; never echo unknown decoder diagnostics."""
    return {
        'Файл логотипа должен быть не больше 1 МиБ.':
            'Файл логотипа пуст или превышает 1 МиБ. Выберите другой PNG, JPEG или WebP.',
        'Требуется статичное изображение до 4096 пикселей по стороне и 4 мегапикселей.':
            'Выберите статичное изображение до 4096 пикселей по каждой стороне и до 4 мегапикселей.',
        'Логотип полностью прозрачен.':
            'Логотип полностью прозрачен. Выберите изображение с видимыми элементами.',
        'Не удалось прочитать логотип. Выберите корректный PNG, JPEG или WebP.':
            'Не удалось прочитать логотип. Выберите корректный PNG, JPEG или WebP.',
        'Логотип слишком сложный для КП. Выберите изображение с меньшим количеством деталей.':
            'Не удалось подготовить компактный файл логотипа для КП. Выберите изображение меньшего размера или с меньшим количеством деталей.',
    }.get(reason, 'Не удалось подготовить логотип. Выберите корректный PNG, JPEG или WebP и повторите загрузку.')


def _bounded_retry_after(
    status_code: int, seconds: int | None,
) -> dict[str, str] | None:
    if seconds is None:
        return None
    if status_code != 429 or type(seconds) is not int or not 0 <= seconds <= 3600:
        raise ValueError("Invalid bounded retry-after interval.")
    return {"Retry-After": str(seconds)}


class ApiError(Exception):
    def __init__(
        self,
        status_code: int,
        code: str,
        message: str,
        *,
        field: str | None = None,
        row: int | None = None,
        retry_after_seconds: int | None = None,
    ) -> None:
        super().__init__(message)
        _bounded_retry_after(status_code, retry_after_seconds)
        self.retry_after_seconds = retry_after_seconds
        self.status_code = status_code
        self.code = code
        self.message = message
        self.field = field
        self.row = row


def current_request_id(request: Request) -> str:
    return str(getattr(request.state, "request_id", uuid4().hex))


def json_error(
    request: Request,
    status_code: int,
    code: str,
    message: str,
    *,
    field: str | None = None,
    row: int | None = None,
    retry_after_seconds: int | None = None,
) -> JSONResponse:
    data: dict[str, str | int | None] = {
        "code": code,
        "message": message,
        "request_id": current_request_id(request),
    }
    if field is not None:
        data["field"] = field
    if row is not None:
        data["row"] = row
    return JSONResponse(
        data, status_code=status_code,
        headers=_bounded_retry_after(status_code, retry_after_seconds),
    )
