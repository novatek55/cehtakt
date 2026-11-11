"""Обратная запись в маркетплейсы: сборка отправлений и передача перевозчику.

Здесь единственное место в проекте, где мы меняем состояние чужой системы, и
эти изменения необратимы. Отсюда устройство: каждое отправление обрабатывается
отдельно, успех фиксируется в БД сразу после ответа маркетплейса, отказ по
одному не отменяет остальные.
"""
from django.utils import timezone

from .marketplaces import MarketplaceError, _request
OZON_SHIP_URL = 'https://api-seller.ozon.ru/v4/posting/fbs/ship'
OZON_AWAITING_DELIVERY_URL = 'https://api-seller.ozon.ru/v2/posting/fbs/awaiting-delivery'
WB_SUPPLIES_URL = 'https://marketplace-api.wildberries.ru/api/v3/supplies'
WB_MARKETPLACE_SUPPLIES_URL = 'https://marketplace-api.wildberries.ru/api/marketplace/v3/supplies'

# Из какой стадии возможна сборка и в какую отправление переходит после неё.
_ASSEMBLE_FROM = {'ozon': 'awaiting_packaging', 'wb': 'new'}
_ASSEMBLE_TO = {'ozon': 'awaiting_deliver', 'wb': 'assembled'}


def ozon_ship_posting(client_id, api_key, posting_number, products):
    """Собрать одно отправление Ozon. products: [{'product_id': str, 'quantity': int}]."""
    packages = []
    for product in products:
        quantity = int(product['quantity'])
        for _ in range(quantity):
            packages.append({
                'products': [{
                    'product_id': int(product['product_id']),
                    'quantity': 1,
                }],
            })
    payload = {
        'posting_number': posting_number,
        'packages': packages,
    }
    _request(OZON_SHIP_URL, {'Client-Id': client_id, 'Api-Key': api_key}, payload)


def ozon_awaiting_delivery(client_id, api_key, posting_numbers):
    """Передать собранные отправления к отгрузке."""
    _request(OZON_AWAITING_DELIVERY_URL, {'Client-Id': client_id, 'Api-Key': api_key},
             {'posting_number': list(posting_numbers)})


def wb_create_supply(token, name):
    body = _request(WB_SUPPLIES_URL, {'Authorization': token}, {'name': name})
    supply_id = str((body or {}).get('id') or '')
    if not supply_id:
        raise MarketplaceError('WB не вернул номер поставки')
    return supply_id


def wb_add_order(token, supply_id, order_id):
    """Добавить задание в поставку WB.

    Остаточный риск: WB Marketplace API не даёт ключа идемпотентности для этого
    вызова. Если соединение оборвётся ПОСЛЕ того, как WB применил изменение, но
    ДО того, как мы получили ответ, строка на нашей стороне останется в стадии
    'new' (мы не зафиксировали успех) — и следующий запуск сборки повторит
    запрос, добавив то же задание в поставку второй раз. Кодом это не закрыть;
    расхождение придётся ловить сверкой в кабинете WB.
    """
    try:
        orders = [int(order_id)]
    except (TypeError, ValueError) as e:
        raise MarketplaceError(f'некорректный номер задания WB: {order_id}') from e
    _request(f'{WB_MARKETPLACE_SUPPLIES_URL}/{supply_id}/orders',
             {'Authorization': token}, payload={'orders': orders}, method='PATCH')


def wb_deliver_supply(token, supply_id):
    _request(f'{WB_SUPPLIES_URL}/{supply_id}/deliver',
             {'Authorization': token}, payload={}, method='PATCH')


def _postings_to_assemble(shipment):
    """{номер отправления: [строки]} для стадии, из которой возможна сборка."""
    stage = _ASSEMBLE_FROM[shipment.source]
    assembled_stage = _ASSEMBLE_TO[shipment.source]
    ready_postings = {
        line.external_posting
        for line in shipment.lines.filter(deleted=False, external_status=assembled_stage)
        if line.external_posting
    }
    groups, skipped = {}, []
    for line in shipment.lines.filter(deleted=False):
        if not line.external_posting:
            continue
        # Внешнее отправление собирается целиком. Если хотя бы одна его строка
        # уже подтверждена маркетплейсом, не посылаем повторный запрос из-за
        # дублирующей устаревшей строки в стадии сборки.
        if line.external_posting in ready_postings or line.external_status != stage:
            if line.external_posting not in skipped:
                skipped.append(line.external_posting)
            continue
        groups.setdefault(line.external_posting, []).append(line)
    return groups, skipped


def assemble_shipment(shipment, ozon_client_id, ozon_api_key, wb_token):
    """Собрать отправления заявки. Возвращает {'ok', 'failed', 'skipped'}."""
    groups, skipped = _postings_to_assemble(shipment)
    ok, failed = [], []

    if shipment.source == 'wb' and groups and not shipment.external_supply_id:
        return {'ok': [], 'failed': [{'posting': '',
                'message': 'Сначала создайте поставку'}], 'skipped': skipped}

    for posting, lines in groups.items():
        try:
            if shipment.source == 'ozon':
                if any(not l.external_product_id for l in lines):
                    raise MarketplaceError(
                        'нет идентификатора товара — загрузите заявку заново')
                fractional = [l for l in lines if l.quantity != l.quantity.to_integral_value()]
                if fractional:
                    qtys = ', '.join(str(l.quantity) for l in fractional)
                    raise MarketplaceError(
                        f'отправление {posting}: дробное количество ({qtys}) — '
                        f'Ozon принимает только целое число, проверьте состав')
                ozon_ship_posting(ozon_client_id, ozon_api_key, posting, [
                    {'product_id': l.external_product_id, 'quantity': l.quantity}
                    for l in lines
                ])
            else:
                wb_add_order(wb_token, shipment.external_supply_id, posting)
        except MarketplaceError as e:
            failed.append({'posting': posting, 'message': str(e)})
            continue
        # Фиксируем сразу: обрыв на следующем отправлении не должен потерять этот успех.
        for line in lines:
            line.external_status = _ASSEMBLE_TO[shipment.source]
            line.save(update_fields=['external_status'])
        ok.append(posting)

    return {'ok': ok, 'failed': failed, 'skipped': skipped}


def handover_shipment(shipment, ozon_client_id, ozon_api_key, wb_token):
    """Передать собранное к отгрузке. Возвращает {'status', 'message'?}."""
    if shipment.source == 'ozon':
        ready = []
        for line in shipment.lines.filter(deleted=False, external_status='awaiting_deliver'):
            if line.external_posting and line.external_posting not in ready:
                ready.append(line.external_posting)
        if not ready:
            return {'status': 'nothing'}
        try:
            ozon_awaiting_delivery(ozon_client_id, ozon_api_key, ready)
        except MarketplaceError as e:
            return {'status': 'error', 'message': str(e)}
        return {'status': 'ok', 'count': len(ready)}

    if not shipment.external_supply_id:
        return {'status': 'nothing'}
    try:
        wb_deliver_supply(wb_token, shipment.external_supply_id)
    except MarketplaceError as e:
        return {'status': 'error', 'message': str(e)}
    shipment.handed_over_at = timezone.now()
    shipment.save(update_fields=['handed_over_at'])
    return {'status': 'ok', 'count': 1}


def create_supply_for_shipment(shipment, wb_token):
    """Создать поставку WB под заявку (шаг «Создать поставку»).

    Идемпотентно по факту наличия id: если поставка уже создана — не
    пересоздаём. Возвращает {'status': 'ok'|'exists'|'error', ...}."""
    if shipment.external_supply_id:
        return {'status': 'exists', 'supplyId': shipment.external_supply_id}
    name = f'Поставка от {timezone.now().strftime("%d.%m.%Y")}'
    try:
        shipment.external_supply_id = wb_create_supply(wb_token, name)
    except MarketplaceError as e:
        return {'status': 'error', 'message': str(e)}
    shipment.save(update_fields=['external_supply_id'])
    return {'status': 'ok', 'supplyId': shipment.external_supply_id}


def wb_create_trbx(token, supply_id, amount):
    """Создать amount грузомест в поставке. Возвращает список их id."""
    body = _request(f'{WB_SUPPLIES_URL}/{supply_id}/trbx',
                    {'Authorization': token}, {'amount': amount})
    ids = [str(t) for t in (body or {}).get('trbxIds') or []]
    if len(ids) != amount:
        raise MarketplaceError(f'WB создал {len(ids)} грузомест из {amount}')
    return ids


def wb_list_trbx(token, supply_id):
    """Вернуть ID уже созданных грузомест поставки WB."""
    body = _request(f'{WB_SUPPLIES_URL}/{supply_id}/trbx',
                    {'Authorization': token}, payload=None, method='GET')
    return [str(t.get('id') or '') for t in (body or {}).get('trbxes') or [] if t.get('id')]


def create_trbx_for_shipment(shipment, wb_token):
    """Создать грузоместа под собранные задания: 1 короб = 1 отправление.

    Как и вся обратная запись — по одному, с фиксацией успеха сразу после
    ответа WB: external_trbx_id строки сохраняется до перехода к следующей.
    Остаточный риск (как у wb_add_order): нет ключа идемпотентности — обрыв
    после применения на стороне WB, но до нашего ответа, оставит строку без
    external_trbx_id, и повтор создаст лишний короб. Ловится сверкой в кабинете.
    Возвращает {'ok': [...], 'failed': [{'posting','message'}]}."""
    if not shipment.external_supply_id:
        return {'ok': [], 'failed': [{'posting': '',
                'message': 'Сначала создайте поставку'}]}
    lines = [l for l in shipment.lines.filter(deleted=False, external_status='assembled')
             if l.external_posting and not l.external_trbx_id]
    if not lines:
        return {'ok': [], 'failed': []}
    used = {l.external_trbx_id for l in shipment.lines.filter(deleted=False) if l.external_trbx_id}
    try:
        trbx_ids = [t for t in wb_list_trbx(wb_token, shipment.external_supply_id) if t not in used]
        missing = len(lines) - len(trbx_ids)
        if missing > 0:
            trbx_ids.extend(wb_create_trbx(wb_token, shipment.external_supply_id, missing))
    except MarketplaceError as e:
        return {'ok': [], 'failed': [{'posting': '', 'message': str(e)}]}
    ok, failed = [], []
    for line, trbx_id in zip(lines, trbx_ids):
        line.external_trbx_id = trbx_id
        line.save(update_fields=['external_trbx_id'])
        ok.append(line.external_posting)
    return {'ok': ok, 'failed': failed}
