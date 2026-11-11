"""Клиенты API маркетплейсов: отправления Ozon FBS и сборочные задания WB.

Только стандартная библиотека — requests в проекте нет, а деплой не делает
pip install, поэтому новая зависимость уронила бы сервис при рестарте.

Оба клиента возвращают нормализованные строки:
{'external_posting': str, 'sku': str, 'name': str, 'quantity': Decimal,
 'external_status': str}
"""
import json
import os
import time
import urllib.parse
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from decimal import Decimal

# v3 unfulfilled/list retired on 2026-08-31 and returns a persistent 429.
# v4 returns top-level postings with has_next/cursor pagination.
OZON_URL = 'https://api-seller.ozon.ru/v4/posting/fbs/unfulfilled/list'
OZON_BY_BARCODE_URL = 'https://api-seller.ozon.ru/v2/posting/fbs/get-by-barcode'
OZON_ACT_LIST_URL = 'https://api-seller.ozon.ru/v2/posting/fbs/act/list'
OZON_ASSEMBLY_POSTING_LIST_URL = 'https://api-seller.ozon.ru/v1/assembly/carriage/posting/list'
OZON_RETURN_GIVEOUT_LIST_URL = 'https://api-seller.ozon.ru/v1/return/giveout/list'
OZON_RETURN_GIVEOUT_BARCODE_URL = 'https://api-seller.ozon.ru/v1/return/giveout/barcode'
OZON_RETURNS_LIST_URL = 'https://api-seller.ozon.ru/v1/returns/list'
OZON_WAREHOUSE_LIST_URL = 'https://api-seller.ozon.ru/v2/warehouse/list'
OZON_OFFER_LIST_URL = 'https://api-seller.ozon.ru/v3/product/list'
OZON_FBS_STOCKS_URL = 'https://api-seller.ozon.ru/v2/product/info/stocks-by-warehouse/fbs'
OZON_WAREHOUSE_STOCKS_URL = 'https://api-seller.ozon.ru/v1/product/info/warehouse/stocks'
OZON_PRODUCT_STOCKS_URL = 'https://api-seller.ozon.ru/v4/product/info/stocks'
OZON_UPDATE_STOCKS_URL = 'https://api-seller.ozon.ru/v2/products/stocks'
WB_URL = 'https://marketplace-api.wildberries.ru/api/v3/orders/new'
WB_ORDERS_URL = 'https://marketplace-api.wildberries.ru/api/v3/orders'
WB_ORDER_STATUSES_URL = 'https://marketplace-api.wildberries.ru/api/v3/orders/status'
WB_RETURNS_URL = 'https://returns-api.wildberries.ru/api/v1/claims'
WB_WAREHOUSES_URL = 'https://marketplace-api.wildberries.ru/api/v3/warehouses'
WB_STOCKS_URL = 'https://marketplace-api.wildberries.ru/api/v3/stocks/{warehouse_id}'
WB_ANALYTICS_STOCKS_URL = 'https://seller-analytics-api.wildberries.ru/api/analytics/v1/stocks-report/wb-warehouses'
WB_ANALYTICS_PAGE_LIMIT = 250000
WB_INTERNAL_STOCKS_REFRESH_URL = 'http://127.0.0.1:18088/api/internal/wb/stocks/refresh'

TIMEOUT = 60
PAGE_LIMIT = 100  # потолок Ozon: limit должен быть в (0, 100]
# Окно по cutoff (крайнему сроку сборки): назад — чтобы поймать просроченные и уже
# собранные (awaiting_deliver, у которых срок сборки в прошлом), вперёд — чтобы
# поймать отправления с будущей датой отгрузки.
CUTOFF_BACK_DAYS = 30
CUTOFF_FORWARD_DAYS = 60
MAX_PAGES = 200  # до 20000 отправлений, с запасом; защита от бага пагинации у Ozon

# Рабочие стадии Ozon, которые попадают в заявку: «Ожидают упаковки» и «Ожидают
# отгрузки». Остальное из unfulfilled (например delivering) отбрасываем.
OZON_STATUSES = ('awaiting_packaging', 'awaiting_deliver')
OZON_RETURN_PICKUP_STATUS_SYS = 'ArrivedAtReturnPlace'
OZON_RETURN_PICKUP_STATUS_LABEL = 'В пункте выдачи'

# Подписи стадий для интерфейса.
STATUS_LABELS = {
    'awaiting_packaging': 'Ожидает сборки',
    'awaiting_deliver': 'Ожидает отгрузки',
    'new': 'Новое',
    'confirm': 'На сборке',
    'assembled': 'Собрано',
    'return': 'Возврат FBS',
}
# Новые заказы берём только из /orders/new: в истории WB могут оставаться
# устаревшие записи со статусом new. История нужна для заказов, уже взятых
# WB в сборку, чтобы получить их supplyId.
WB_HISTORY_ORDER_STATUSES = {'confirm'}


class MarketplaceError(Exception):
    """Маркетплейс не ответил или ответил ошибкой — импорт этого источника отменяется."""


class MarketplaceRateLimitError(MarketplaceError):
    """Лимит API с задержкой до следующего разрешённого запроса в секундах."""

    def __init__(self, message, retry_after_seconds=None):
        super().__init__(message)
        self.retry_after_seconds = retry_after_seconds


def _retry_after_seconds(headers):
    try:
        return max(0, int(headers.get('X-RateLimit-Retry') or headers.get('Retry-After') or ''))
    except (AttributeError, TypeError, ValueError):
        return None


def _posting_warehouse(posting):
    delivery = posting.get('delivery_method') or {}
    if not isinstance(delivery, dict):
        delivery = {}
    return str(
        delivery.get('warehouse')
        or delivery.get('warehouse_name')
        or posting.get('warehouse')
        or posting.get('warehouse_name')
        or ''
    ).strip()


def _request(url, headers, payload=None, method='POST'):
    """HTTP-запрос с JSON. payload=None → GET без тела."""
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header('Content-Type', 'application/json')
    for k, v in headers.items():
        req.add_header(k, v)
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
            raw = resp.read().decode()
            if not raw.strip():
                return None
            return json.loads(raw)
    except urllib.error.HTTPError as e:
        body = e.read().decode(errors='replace')[:300]
        if e.code == 429:
            raise MarketplaceRateLimitError(
                f'{e.code} {e.reason}: {body}',
                _retry_after_seconds(e.headers),
            ) from e
        raise MarketplaceError(f'{e.code} {e.reason}: {body}') from e
    except urllib.error.URLError as e:
        raise MarketplaceError(f'нет связи: {e.reason}') from e
    except json.JSONDecodeError as e:
        raise MarketplaceError('ответ не является JSON') from e


def _request_raw(url, headers, payload=None, method='POST'):
    """Как _request, но возвращает сырые байты — для PDF с этикетками."""
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header('Content-Type', 'application/json')
    for k, v in headers.items():
        req.add_header(k, v)
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
            return resp.read()
    except urllib.error.HTTPError as e:
        body = e.read().decode(errors='replace')[:300]
        raise MarketplaceError(f'{e.code} {e.reason}: {body}') from e
    except urllib.error.URLError as e:
        raise MarketplaceError(f'нет связи: {e.reason}') from e


def _env_file_value(path, key):
    try:
        with open(path, encoding='utf-8') as fh:
            for line in fh:
                if not line.startswith(f'{key}='):
                    continue
                return line.split('=', 1)[1].strip().strip('"').strip("'")
    except OSError:
        return ''
    return ''


def _ozon_page(headers, cursor, now):
    payload = {
        'dir': 'ASC',
        'filter': {
            'cutoff_from': (now - timedelta(days=CUTOFF_BACK_DAYS)).strftime('%Y-%m-%dT%H:%M:%S.000Z'),
            'cutoff_to': (now + timedelta(days=CUTOFF_FORWARD_DAYS)).strftime('%Y-%m-%dT%H:%M:%S.000Z'),
        },
        'limit': PAGE_LIMIT,
        'cursor': cursor,
        'with': {'analytics_data': False, 'financial_data': False},
    }
    body = _request(OZON_URL, headers, payload)
    if not isinstance(body, dict) or not isinstance(body.get('postings'), list):
        raise MarketplaceError('Ozon: неожиданный формат списка отправлений')
    next_cursor = str(body.get('cursor') or '') if body.get('has_next') else ''
    if body.get('has_next') and (not next_cursor or next_cursor == cursor):
        raise MarketplaceError('Ozon: не получен новый курсор следующей страницы')
    return body['postings'], next_cursor


def fetch_ozon_postings(client_id, api_key):
    """Неотгруженные отправления Ozon рабочих стадий, развёрнутые в строки.

    Один запрос в unfulfilled/list на всё окно; стадию каждой строки берём из
    самого отправления (posting['status']) и оставляем только «Ожидают упаковки»
    и «Ожидают отгрузки» — в заявку должны попасть и те, что ещё собирать, и те,
    что готовы к передаче. Стадия сохраняется в external_status, чтобы их было
    видно по отдельности.
    """
    headers = {'Client-Id': client_id, 'Api-Key': api_key}
    now = datetime.now(timezone.utc)
    rows = []
    cursor = ''
    retries = 0
    retry_wait = 0
    for _ in range(MAX_PAGES):
        # Ozon limits this endpoint per second. Pace pagination and retry only
        # the failed read; keep already fetched pages without duplicating rows.
        if cursor:
            time.sleep(1.1)
        while True:
            try:
                postings, next_cursor = _ozon_page(headers, cursor, now)
                break
            except MarketplaceRateLimitError as error:
                delay = max(2 ** (retries + 1), error.retry_after_seconds or 0)
                # Bound retries across the entire import, not for every page.
                # Long cooldowns must not occupy a gunicorn worker indefinitely.
                if retries >= 3 or retry_wait + delay > 15:
                    raise
                time.sleep(delay)
                retry_wait += delay
                retries += 1
        for p in postings:
            status = str(p.get('status') or '')
            if status not in OZON_STATUSES:
                continue  # напр. delivering — не наш этап сборки/отгрузки
            number = str(p.get('posting_number') or '')
            warehouse = _posting_warehouse(p)
            for prod in p.get('products') or []:
                quantity = Decimal(str(prod.get('quantity') or 0))
                unit_count = int(quantity) if quantity == quantity.to_integral_value() else 1
                unit_quantity = Decimal('1') if unit_count > 1 else quantity
                for unit_index in range(max(unit_count, 0)):
                    rows.append({
                        'external_posting': number,
                        'sku': str(prod.get('offer_id') or '').strip(),
                        'name': str(prod.get('name') or '').strip(),
                        'quantity': unit_quantity,
                        'external_status': status,
                        'product_id': str(prod.get('sku') or ''),
                        'external_warehouse': warehouse,
                        'unit_index': unit_index,
                    })
        if not next_cursor:
            break
        cursor = next_cursor
    else:
        raise MarketplaceError('Ozon: превышен лимит страниц, импорт не завершён')
    return rows


def fetch_ozon_posting_by_barcode(client_id, api_key, barcode):
    """Найти отправление Ozon по коду, который сканер считал с этикетки."""
    code = str(barcode or '').strip()
    if not code:
        raise MarketplaceError('пустой штрихкод Ozon')
    body = _request(
        OZON_BY_BARCODE_URL,
        {'Client-Id': client_id, 'Api-Key': api_key},
        {'barcode': code},
    )
    return (body or {}).get('result') or {}


def fetch_ozon_carriages(client_id, api_key, date_from, date_to, warehouse_name=''):
    """Отгрузки Ozon FBS за период. Возвращают carriage id для листа подбора."""
    headers = {'Client-Id': client_id, 'Api-Key': api_key}
    body = _request(OZON_ACT_LIST_URL, headers, {
        'filter': {'date_from': date_from, 'date_to': date_to},
        'limit': 50,
    })
    rows = ((body or {}).get('result') or [])
    if warehouse_name:
        needle = ' '.join(warehouse_name.split()).casefold()
        rows = [
            row for row in rows
            if needle in ' '.join(str(row.get('delivery_method_name') or '').split()).casefold()
        ]
    return rows


def fetch_ozon_assembly_postings(client_id, api_key, carriage_id):
    """Строки листа подбора Ozon по конкретной отгрузке (carriage_id)."""
    headers = {'Client-Id': client_id, 'Api-Key': api_key}
    postings = []
    cursor = ''
    while True:
        payload = {
            'filter': {'carriage_id': int(carriage_id)},
            'limit': 100,
        }
        if cursor:
            payload['cursor'] = cursor
        body = _request(OZON_ASSEMBLY_POSTING_LIST_URL, headers, payload)
        postings.extend((body or {}).get('postings') or [])
        cursor = str((body or {}).get('cursor') or '')
        if not cursor:
            break
    return postings


def _wb_order_row(order, status, supply_id=''):
    row = {
        'external_posting': str(order.get('id') or ''),
        'sku': str(order.get('article') or '').strip(),
        'name': '',
        'quantity': Decimal('1'),
        'external_status': status,
        'product_id': '',
    }
    if supply_id:
        row['external_supply_id'] = str(supply_id)
    return row


def _wb_history_orders(token):
    headers = {'Authorization': token}
    date_from = int((datetime.now(timezone.utc) - timedelta(days=30)).timestamp())
    next_cursor = 0
    seen_cursors = set()
    for _ in range(MAX_PAGES):
        query = urllib.parse.urlencode({
            'limit': 1000,
            'next': next_cursor,
            'dateFrom': date_from,
        })
        body = _request(f'{WB_ORDERS_URL}?{query}', headers, payload=None, method='GET')
        orders = (body or {}).get('orders') or []
        if not orders:
            break
        for order in orders:
            yield order
        next_cursor = (body or {}).get('next') or 0
        if not next_cursor or next_cursor in seen_cursors:
            break
        seen_cursors.add(next_cursor)


def _wb_order_statuses(token, order_ids):
    statuses = {}
    clean_ids = []
    for order_id in order_ids:
        try:
            clean_ids.append(int(order_id))
        except (TypeError, ValueError):
            continue
    for chunk in _chunks(clean_ids, 1000):
        body = _request(
            WB_ORDER_STATUSES_URL,
            {'Authorization': token},
            payload={'orders': chunk},
            method='POST',
        )
        for row in (body or {}).get('orders') or []:
            order_id = str(row.get('id') or row.get('orderId') or '')
            if order_id:
                statuses[order_id] = str(row.get('supplierStatus') or '').strip()
    return statuses


def fetch_wb_orders(token):
    """Активные сборочные задания WB: новые и уже перенесённые «на сборку»."""
    body = _request(WB_URL, {'Authorization': token}, payload=None, method='GET')
    rows_by_id = {}
    for o in (body or {}).get('orders') or []:
        row = _wb_order_row(o, 'new')
        if row['external_posting']:
            rows_by_id[row['external_posting']] = row

    history_by_id = {
        str(order.get('id') or ''): order
        for order in _wb_history_orders(token)
        if order.get('id')
    }
    statuses = _wb_order_statuses(token, history_by_id.keys()) if history_by_id else {}
    for order_id, order in history_by_id.items():
        status = statuses.get(order_id)
        if status not in WB_HISTORY_ORDER_STATUSES:
            continue
        if status == 'new' and order_id in rows_by_id:
            continue
        rows_by_id[order_id] = _wb_order_row(order, status, order.get('supplyId') or '')
    return list(rows_by_id.values())


def fetch_ozon_warehouses(client_id, api_key):
    """Список складов Ozon Seller API v2 для FBS/rFBS."""
    headers = {'Client-Id': client_id, 'Api-Key': api_key}
    rows = []
    cursor = ''
    while True:
        payload = {'limit': 200}
        if cursor:
            payload['cursor'] = cursor
        body = _request(OZON_WAREHOUSE_LIST_URL, headers, payload)
        result = (body or {}).get('result') or body or {}
        warehouses = result.get('warehouses') or result.get('items') or []
        for wh in warehouses:
            rows.append({
                'warehouse_id': str(wh.get('warehouse_id') or wh.get('id') or ''),
                'name': str(wh.get('name') or wh.get('warehouse_name') or '').strip(),
                'status': str(wh.get('status') or '').strip(),
                'is_rfbs': bool(wh.get('is_rfbs')),
                'warehouse_type': str(wh.get('warehouse_type') or '').strip(),
            })
        cursor = str(result.get('cursor') or '')
        if not result.get('has_next') or not cursor:
            break
    return [r for r in rows if r['warehouse_id']]


def fetch_ozon_offer_ids(client_id, api_key):
    """Read exact article strings from the active Ozon catalog."""
    headers = {'Client-Id': client_id, 'Api-Key': api_key}
    payload = {'filter': {'visibility': 'ALL'}, 'limit': 1000}
    offer_ids = set()
    seen_cursors = set()
    while True:
        body = _request(OZON_OFFER_LIST_URL, headers, payload)
        result = body.get('result') if isinstance(body, dict) else None
        items = result.get('items') if isinstance(result, dict) else None
        if not isinstance(items, list):
            raise MarketplaceError('Ozon: некорректный ответ при получении списка артикулов')
        offer_ids.update(
            item['offer_id'] for item in items if item.get('offer_id') and not item.get('archived')
        )
        cursor = result.get('last_id')
        if len(items) < payload['limit'] or not cursor:
            return offer_ids
        if cursor in seen_cursors:
            raise MarketplaceError('Ozon: повторилась страница списка артикулов')
        seen_cursors.add(cursor)
        payload['last_id'] = cursor


def _chunks(values, size):
    values = list(values)
    for index in range(0, len(values), size):
        yield values[index:index + size]


def fetch_ozon_fbs_stocks(client_id, api_key, offer_ids, warehouse_ids=None):
    """FBS/rFBS-остатки Ozon по товарам и складам."""
    headers = {'Client-Id': client_id, 'Api-Key': api_key}
    clean_warehouse_ids = [str(v).strip() for v in (warehouse_ids or []) if str(v or '').strip()]
    rows = []
    clean_offer_ids = [str(v).strip() for v in offer_ids if str(v or '').strip()]
    for chunk in _chunks(clean_offer_ids, 1000):
        cursor = ''
        while True:
            payload = {'offer_id': chunk, 'limit': 1000}
            if cursor:
                payload['cursor'] = cursor
            try:
                body = _request(OZON_FBS_STOCKS_URL, headers, payload)
            except MarketplaceError as error:
                if 'missing a required role' in str(error):
                    raise MarketplaceError(
                        'Ozon: нет доступа к остаткам по складам. Для точной синхронизации '
                        'нужен API-ключ с ролью на метод stocks-by-warehouse/fbs.'
                    ) from error
                raise
            result = (body or {}).get('result') or body or {}
            items = result.get('items') or result.get('stocks') or result.get('products') or []
            for item in items:
                rows.append({
                    'sku': str(item.get('offer_id') or '').strip(),
                    'product_id': str(item.get('product_id') or item.get('sku') or ''),
                    'warehouse_id': str(item.get('warehouse_id') or ''),
                    'warehouse_name': str(item.get('warehouse_name') or '').strip(),
                    'quantity': Decimal(str(item.get('present') or 0)),
                    'reserved': Decimal(str(item.get('reserved') or 0)),
                    'free_stock': Decimal(str(item.get('free_stock') or 0)),
                    'name': str(item.get('name') or '').strip(),
                })
            cursor = str(result.get('cursor') or '')
            if not result.get('has_next') or not cursor:
                break
    if clean_warehouse_ids:
        selected = set(clean_warehouse_ids)
        rows = [row for row in rows if row['warehouse_id'] in selected]
    return rows


def fetch_ozon_fbs_stocks_by_warehouses(client_id, api_key, warehouse_ids, offer_ids=None):
    """FBS/rFBS-остатки Ozon: обойти каждый склад через warehouse/stocks."""
    headers = {'Client-Id': client_id, 'Api-Key': api_key}
    wanted = {str(v).strip() for v in (offer_ids or []) if str(v or '').strip()}
    rows = []
    for warehouse_id in [str(v).strip() for v in warehouse_ids if str(v or '').strip()]:
        cursor = ''
        while True:
            payload = {'warehouse_id': int(warehouse_id), 'limit': 1000}
            if cursor:
                payload['cursor'] = cursor
            body = _request(OZON_WAREHOUSE_STOCKS_URL, headers, payload)
            result = (body or {}).get('result') or body or {}
            items = result.get('stocks') or result.get('items') or result.get('products') or []
            for item in items:
                offer_id = str(item.get('offer_id') or '').strip()
                if wanted and offer_id not in wanted:
                    continue
                rows.append({
                    'sku': offer_id,
                    'product_id': str(item.get('product_id') or item.get('sku') or ''),
                    'warehouse_id': warehouse_id,
                    'warehouse_name': str(item.get('warehouse_name') or '').strip(),
                    'quantity': Decimal(str(item.get('present') or 0)),
                    'reserved': Decimal(str(item.get('reserved') or 0)),
                    'free_stock': Decimal(str(item.get('free_stock') or 0)),
                    'name': str(item.get('name') or '').strip(),
                })
            cursor = str(result.get('cursor') or '')
            if not result.get('has_next') or not cursor:
                break
    return rows


def fetch_ozon_product_fbs_stocks(client_id, api_key, offer_ids=None):
    """Fallback for summed Ozon FBS/rFBS stocks via the common stocks method."""
    headers = {'Client-Id': client_id, 'Api-Key': api_key}
    rows = []
    clean_offer_ids = [str(v).strip() for v in (offer_ids or []) if str(v or '').strip()]
    chunks = _chunks(clean_offer_ids, 1000) if clean_offer_ids else [[]]
    for chunk in chunks:
        cursor = ''
        while True:
            payload = {
                'filter': {'visibility': 'ALL'},
                'limit': 1000,
                'cursor': cursor,
            }
            if chunk:
                payload['filter']['offer_id'] = chunk
            body = _request(OZON_PRODUCT_STOCKS_URL, headers, payload)
            result = (body or {}).get('result') or body or {}
            for item in result.get('items') or []:
                quantity = Decimal(0)
                reserved = Decimal(0)
                warehouse_ids = []
                for stock in item.get('stocks') or []:
                    stock_type = str(stock.get('type') or '').lower()
                    if stock_type not in {'fbs', 'rfbs'}:
                        continue
                    quantity += Decimal(str(stock.get('present') or 0))
                    reserved += Decimal(str(stock.get('reserved') or 0))
                    for warehouse_id in stock.get('warehouse_ids') or []:
                        warehouse_id = str(warehouse_id or '').strip()
                        if warehouse_id and warehouse_id not in warehouse_ids:
                            warehouse_ids.append(warehouse_id)
                if quantity:
                    rows.append({
                        'sku': str(item.get('offer_id') or '').strip(),
                        'product_id': str(item.get('product_id') or ''),
                        'warehouse_id': ','.join(warehouse_ids),
                        'warehouse_name': '',
                        'quantity': quantity,
                        'reserved': reserved,
                        'free_stock': quantity - reserved,
                        'name': str(item.get('name') or '').strip(),
                    })
            cursor = str(result.get('cursor') or '')
            if not cursor:
                break
    return rows


def update_ozon_fbs_stock(client_id, api_key, warehouse_id, offer_id, quantity):
    payload = {'stocks': [{
        'offer_id': str(offer_id),
        'stock': int(quantity),
        'warehouse_id': int(warehouse_id),
    }]}
    body = _request(OZON_UPDATE_STOCKS_URL, {'Client-Id': client_id, 'Api-Key': api_key}, payload)
    rows = body.get('result') if isinstance(body, dict) else None
    if not isinstance(rows, list) or len(rows) != 1 or not isinstance(rows[0], dict):
        raise MarketplaceError(f'Ozon не подтвердил обновление остатка для «{offer_id}»')
    result = rows[0]
    errors = result.get('errors') or []
    if result.get('updated') is not True or errors:
        reasons = ', '.join(str(error.get('message') or error.get('code') or error) for error in errors)
        message = f'Ozon отклонил обновление остатка для «{offer_id}»'
        raise MarketplaceError(f'{message}: {reasons}' if reasons else message)


def fetch_wb_stock_cards(token):
    """Map seller barcodes to articles, reading every page of product cards."""
    cursor = {'limit': 100}
    result = {}
    for _ in range(MAX_PAGES):
        body = _request('https://content-api.wildberries.ru/content/v2/get/cards/list',
                        {'Authorization': token}, {'settings': {
                            'sort': {'ascending': True}, 'cursor': cursor,
                            'filter': {'withPhoto': -1}}})
        if not isinstance(body, dict) or not isinstance(body.get('cards'), list):
            raise MarketplaceError('WB: некорректный ответ списка карточек')
        for card in body['cards']:
            for size in card.get('sizes') or []:
                for barcode in size.get('skus') or []:
                    result[str(barcode)] = {
                        'sku': str(card.get('vendorCode') or barcode).strip(),
                        'name': str(card.get('title') or ''),
                        'chrt_id': size.get('chrtID'),
                    }
        next_cursor = body.get('cursor') or {}
        if next_cursor.get('total', len(body['cards'])) < 100:
            return result
        if not next_cursor.get('updatedAt') or not next_cursor.get('nmID'):
            raise MarketplaceError('WB: отсутствует курсор следующей страницы карточек')
        following = {'limit': 100, 'updatedAt': next_cursor['updatedAt'], 'nmID': next_cursor['nmID']}
        if following == cursor:
            raise MarketplaceError('WB: повторяется страница карточек')
        cursor = following
    raise MarketplaceError('WB: превышен лимит страниц карточек; обновление отменено')


def fetch_wb_fbs_stocks(token, warehouse_id, skus=None):
    """FBS-остатки Wildberries по складу продавца."""
    if skus is None:
        skus = list(fetch_wb_stock_cards(token))
    rows = []
    for chunk in _chunks(list(dict.fromkeys(skus)), 1000):
        body = _request(WB_STOCKS_URL.format(warehouse_id=warehouse_id),
                        {'Authorization': token}, {'skus': chunk})
        if not isinstance(body, dict) or not isinstance(body.get('stocks'), list):
            raise MarketplaceError('WB: некорректный ответ остатков склада')
        for item in body['stocks']:
            rows.append({
                'sku': str(item.get('sku') or '').strip(),
                'quantity': Decimal(str(item.get('amount') or 0)),
                'name': str(item.get('name') or '').strip(),
            })
    return rows


def _wb_analytics_items(body):
    if isinstance(body, list):
        return body
    if not isinstance(body, dict):
        return []
    result = body.get('result')
    data = body.get('data')
    candidates = [
        data.get('items') if isinstance(data, dict) else None,
        data.get('stocks') if isinstance(data, dict) else None,
        data if isinstance(data, list) else None,
        body.get('items'),
        body.get('stocks'),
        result.get('items') if isinstance(result, dict) else None,
        result.get('stocks') if isinstance(result, dict) else None,
        result.get('data') if isinstance(result, dict) else None,
    ]
    for candidate in candidates:
        if isinstance(candidate, list):
            return [item for item in candidate if isinstance(item, dict)]
    return []


def _wb_stock_decimal(item):
    for key in ('quantity', 'amount', 'stock', 'stocks', 'quantityFull'):
        if item.get(key) not in (None, ''):
            return Decimal(str(item.get(key) or 0))
    return Decimal(0)


def fetch_wb_analytics_fbs_stocks(token):
    """FBS-остатки WB через Seller API по всем доступным складам продавца."""
    cards = fetch_wb_stock_cards(token)
    if not cards:
        raise MarketplaceError('WB: список штрихкодов пуст; прежние остатки сохранены')
    rows = []
    for warehouse in fetch_wb_warehouses(token):
        warehouse_id = str(warehouse.get('warehouse_id') or '').strip()
        if not warehouse_id:
            continue
        totals = {}
        for stock in fetch_wb_fbs_stocks(token, warehouse_id, list(cards)):
            card = cards.get(stock['sku'], {'sku': stock['sku'], 'name': stock['name']})
            row = totals.setdefault(card['sku'], {
                'sku': card['sku'],
                'name': card['name'],
                'quantity': Decimal(0),
                'warehouse_id': warehouse_id,
                'warehouse_name': str(warehouse.get('name') or '').strip(),
            })
            row['quantity'] += stock['quantity']
        rows.extend(totals.values())
    return rows


def fetch_wb_browser_fbs_stocks():
    """WB остатки из seller web-кабинета через внутренний browser-сервис."""
    token = (
        os.environ.get('WB_INTERNAL_API_TOKEN')
        or _env_file_value(os.environ.get('WB_INTERNAL_ENV_FILE', '/etc/ozon-dashboard.env'), 'WB_INTERNAL_API_TOKEN')
    )
    if not token:
        raise MarketplaceError('WB_INTERNAL_API_TOKEN не задан')
    url = os.environ.get('WB_INTERNAL_STOCKS_REFRESH_URL', WB_INTERNAL_STOCKS_REFRESH_URL)
    body = _request(url, {'X-Internal-Token': token}, {'source': 'browser'})
    rows = []
    for item in (body or {}).get('items') or []:
        rows.append({
            'sku': str(
                item.get('vendor_code')
                or item.get('supplierArticle')
                or item.get('article')
                or item.get('nm_id')
                or item.get('nmId')
                or ''
            ).strip(),
            'quantity': Decimal(str(item.get('stocks') if item.get('stocks') is not None else item.get('quantity') or 0)),
            'name': str(item.get('name') or '').strip(),
            'warehouse_id': str(item.get('warehouse_id') or item.get('warehouseId') or 'browser').strip(),
            'warehouse_name': str(item.get('warehouse_name') or item.get('warehouseName') or 'WB кабинет').strip(),
        })
    return rows


def fetch_wb_warehouses(token):
    """Seller warehouses available for WB FBS stocks."""
    body = _request(WB_WAREHOUSES_URL, {'Authorization': token}, None, method='GET')
    items = body if isinstance(body, list) else (body or {}).get('warehouses') or (body or {}).get('items') or []
    rows = []
    for item in items:
        warehouse_id = str(item.get('id') or item.get('warehouse_id') or item.get('warehouseId') or '').strip()
        if not warehouse_id:
            continue
        rows.append({
            'warehouse_id': warehouse_id,
            'name': str(item.get('name') or item.get('warehouse_name') or '').strip(),
        })
    return rows


def update_wb_fbs_stock(token, warehouse_id, sku, quantity):
    cards = fetch_wb_stock_cards(token)
    article = ' '.join(str(sku or '').split())
    size_ids = {
        int(card['chrt_id']) for barcode, card in cards.items()
        if card.get('chrt_id') and (
            ' '.join(card['sku'].split()) == article or barcode == str(sku)
        )
    }
    if not size_ids:
        raise MarketplaceError(f'WB: не найдена карточка товара «{sku}»')
    if len(size_ids) != 1:
        raise MarketplaceError(f'WB: у «{sku}» несколько размеров; общий остаток нельзя отправить без распределения по размерам')
    if str(warehouse_id) not in {row['warehouse_id'] for row in fetch_wb_warehouses(token)}:
        raise MarketplaceError('WB: выбранный склад не найден в списке складов продавца')
    payload = {'stocks': [{'chrtId': size_ids.pop(), 'amount': int(quantity)}]}
    _request(WB_STOCKS_URL.format(warehouse_id=warehouse_id),
             {'Authorization': token}, payload, method='PUT')


def _as_decimal(value, default='1'):
    try:
        return Decimal(str(value if value not in (None, '') else default))
    except Exception:
        return Decimal(default)


def _ozon_return_products(item):
    products = item.get('products')
    if isinstance(products, list) and products:
        return products
    product = item.get('product')
    if isinstance(product, dict):
        return [product]
    return [item]


def _ozon_return_items(body):
    if not isinstance(body, dict):
        return []
    result = body.get('result')
    candidates = [
        body.get('returns'),
        body.get('items'),
        result.get('returns') if isinstance(result, dict) else None,
        result.get('items') if isinstance(result, dict) else None,
        result if isinstance(result, list) else None,
    ]
    for candidate in candidates:
        if isinstance(candidate, list):
            return [item for item in candidate if isinstance(item, dict)]
    return []


def _first_text(*values):
    for value in values:
        if value not in (None, ''):
            return str(value).strip()
    return ''


def _ozon_return_status(item):
    visual = item.get('visual') if isinstance(item, dict) else None
    if not isinstance(visual, dict):
        visual = {}
    status = visual.get('status')
    if not isinstance(status, dict):
        status = {}
    return (
        _first_text(status.get('sys_name'), item.get('visual_status_sys_name')),
        _first_text(status.get('display_name'), item.get('visual_status_name')),
    )


def _is_ozon_return_at_pickup_point(item):
    sys_name, label = _ozon_return_status(item)
    return (
        sys_name == OZON_RETURN_PICKUP_STATUS_SYS
        or label.casefold() == OZON_RETURN_PICKUP_STATUS_LABEL.casefold()
    )


def _ozon_return_place_name(item):
    place = item.get('place')
    if not isinstance(place, dict):
        place = {}
    target_place = item.get('target_place')
    if not isinstance(target_place, dict):
        target_place = {}
    return _first_text(
        place.get('name'),
        target_place.get('name'),
        place.get('address'),
        target_place.get('address'),
    )


def fetch_ozon_returns(client_id, api_key):
    """FBS-возвраты Ozon, доступные к получению в пункте выдачи."""
    headers = {'Client-Id': client_id, 'Api-Key': api_key}
    rows = []
    last_id = None
    for _ in range(MAX_PAGES):
        payload = {
            'filter': {'return_schema': 'FBS'},
            'limit': PAGE_LIMIT,
        }
        if last_id:
            payload['last_id'] = last_id
        body = _request(OZON_RETURNS_LIST_URL, headers, payload)
        items = _ozon_return_items(body)
        if not items:
            break
        for item in items:
            schema = str(item.get('schema') or item.get('posting_schema') or 'FBS').upper()
            if schema and schema != 'FBS':
                continue
            if not _is_ozon_return_at_pickup_point(item):
                continue
            return_number = _first_text(
                item.get('posting_number'),
                item.get('postingNumber'),
                item.get('return_id'),
                item.get('id'),
            )
            warehouse = _ozon_return_place_name(item)
            for product in _ozon_return_products(item):
                sku = _first_text(
                    product.get('offer_id'), product.get('offerId'), product.get('article'),
                    product.get('supplier_article'), item.get('offer_id'), item.get('product_offer_id'),
                )
                product_id = _first_text(
                    product.get('sku'), product.get('product_id'),
                    item.get('sku'), item.get('product_sku'), item.get('product_id'),
                )
                rows.append({
                    'external_posting': return_number,
                    'sku': sku,
                    'name': _first_text(product.get('name'), item.get('product_name')),
                    'quantity': _as_decimal(product.get('quantity'), default='1'),
                    'external_status': 'return',
                    'product_id': product_id,
                    'external_warehouse': warehouse,
                    'is_return': True,
                })
        if not (isinstance(body, dict) and body.get('has_next')):
            break
        last_id = _first_text(items[-1].get('id'), items[-1].get('return_id'))
        if not last_id:
            break
    return rows


def fetch_ozon_return_pickup_barcode(client_id, api_key):
    """Barcode used to receive Ozon return shipments."""
    body = _request(
        OZON_RETURN_GIVEOUT_BARCODE_URL,
        {'Client-Id': client_id, 'Api-Key': api_key},
        {},
    )
    result = (body or {}).get('result') if isinstance(body, dict) else None
    barcode = ''
    if isinstance(result, dict):
        barcode = str(result.get('barcode') or result.get('value') or result.get('code') or '')
    if not barcode and isinstance(body, dict):
        barcode = str(body.get('barcode') or body.get('value') or body.get('code') or '')
    barcode = ' '.join(barcode.strip().split())
    if not barcode:
        raise MarketplaceError('Ozon не вернул код получения возвратов')
    return barcode


def fetch_wb_returns(token):
    """FBS-возвраты WB из заявок покупателей, развёрнутые в строки заявки."""
    body = _request(f'{WB_RETURNS_URL}?is_archive=false', {'Authorization': token}, payload=None, method='GET')
    rows = []
    for item in ((body or {}).get('claims') or (body or {}).get('data') or []):
        return_id = str(item.get('id') or item.get('claimId') or item.get('orderId') or '')
        rows.append({
            'external_posting': return_id,
            'sku': str(item.get('article') or item.get('supplierArticle') or item.get('sku') or '').strip(),
            'name': str(item.get('name') or '').strip(),
            'quantity': _as_decimal(item.get('quantity')),
            'external_status': 'return',
            'product_id': str(item.get('nmId') or item.get('chrtId') or ''),
            'external_warehouse': '',
            'is_return': True,
        })
    return rows
