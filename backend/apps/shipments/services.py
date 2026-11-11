"""Логика отгрузки: предупреждения перед списанием и само списание со склада."""
import re
from datetime import timedelta
from decimal import Decimal
from itertools import groupby

from django.db import transaction
from django.utils import timezone

from apps.catalog.models import Product
from apps.stock.models import StockItem, StockMovement
from .models import MarketplaceImportRetry, MarketplaceProductAlias, MarketplaceSettings, Shipment, ShipmentLine

_NAME_RE = re.compile(r'^ОТГ-(\d+)$')
TARGET_WAREHOUSE = 'Балабаново'


def _norm_warehouse(value):
    return ' '.join((value or '').split()).casefold()


def is_target_warehouse(value, allow_blank=True):
    """Пустой склад допустим только там, где источник не передаёт склад."""
    warehouse = _norm_warehouse(value)
    return (allow_blank and not warehouse) or warehouse == _norm_warehouse(TARGET_WAREHOUSE)


def active_lines(shipment):
    """Строки состава без мягко удалённых."""
    return shipment.lines.filter(deleted=False).select_related('product')


def can_delete_shipment(shipment):
    """Можно ли удалить заявку из proplan без риска потерять внешнее событие."""
    if shipment.source == Shipment.Source.MANUAL:
        return True
    if shipment.status == Shipment.Status.SHIPPED:
        return False
    if shipment.source == Shipment.Source.WB:
        return shipment.handed_over_at is None
    if shipment.source == Shipment.Source.OZON:
        # Удаление очищает только локальную заявку; открытое отправление
        # остаётся в Ozon и снова попадёт в следующий импорт.
        return True
    return False


def _fmt(value):
    """Decimal без хвостовых нулей и без научной нотации: 10.000 -> '10'."""
    return format(value.normalize(), 'f')


def next_shipment_name():
    """Следующий номер ручной заявки: ОТГ-<максимальный занятый номер + 1>.

    Считаем от максимума имён вида «ОТГ-<N>», а не от количества заявок —
    иначе после удаления заявки номер задваивается.
    """
    max_n = 0
    for name in Shipment.objects.values_list('name', flat=True):
        m = _NAME_RE.match(name or '')
        if m:
            max_n = max(max_n, int(m.group(1)))
    return f'ОТГ-{max_n + 1}'


def collect_ship_warnings(shipment, lines=None):
    """Проблемы, о которых надо сообщить до списания.

    kind='unmatched' — строка без продукта (артикула нет в каталоге);
    kind='shortage'  — на складе меньше, чем нужно отгрузить, суммарно по
    всем активным строкам одного продукта (если продукт встречается
    в нескольких строках, дефицит считается по их сумме).
    Отгрузку не блокируют: пользователь подтверждает и остаток уходит в минус.
    """
    lines = list(lines if lines is not None else active_lines(shipment))
    product_ids = [l.product_id for l in lines if l.product_id]
    stock = {s.product_id: s.available_quantity or Decimal(0)
             for s in StockItem.objects.filter(product_id__in=product_ids)}

    warnings = []
    needed = {}  # product_id -> {'qty', 'line_id', 'product'}
    for line in lines:
        if line.product_id is None:
            label = line.sku_raw or line.name_raw or f'строка {line.id}'
            warnings.append({
                'lineId': line.id,
                'kind': 'unmatched',
                'message': f'{label}: нет в каталоге, не спишется со склада',
            })
            continue
        entry = needed.setdefault(line.product_id, {
            'qty': Decimal(0), 'line_id': line.id, 'product': line.product,
        })
        entry['qty'] += line.quantity

    for product_id, entry in needed.items():
        available = stock.get(product_id, Decimal(0))
        if entry['qty'] > available:
            warnings.append({
                'lineId': entry['line_id'],
                'kind': 'shortage',
                'message': f"{entry['product'].name}: нужно {_fmt(entry['qty'])}, на складе {_fmt(available)}",
            })
    return warnings


@transaction.atomic
def ship_shipment(shipment, user):
    """Списать состав со склада и перевести заявку в «Отгружено».

    Строки без продукта пропускаются. Остаток может уйти в минус — это
    осознанный выбор: факт отгрузки важнее чистоты остатка.
    """
    if shipment.status == Shipment.Status.SHIPPED:
        raise ValueError('Заявка уже отгружена')

    if shipment.note == RETURN_NOTE:
        raise ValueError('Возврат нельзя отгружать как обычную отгрузку')

    for line in active_lines(shipment):
        if line.product_id is None:
            continue
        stock, _ = StockItem.objects.get_or_create(product=line.product)
        before = stock.available_quantity or Decimal(0)
        after = before - line.quantity
        stock.available_quantity = after
        stock.save(update_fields=['available_quantity'])
        StockMovement.objects.create(
            product=line.product,
            kind=StockMovement.Kind.CONSUMPTION,
            quantity_delta=-line.quantity,
            quantity_before=before,
            quantity_after=after,
            note=f'Отгрузка {shipment.name}',
            created_by=user if user and user.is_authenticated else None,
        )
    shipment.status = Shipment.Status.SHIPPED
    shipment.shipped_at = timezone.now()
    shipment.save(update_fields=['status', 'shipped_at'])
    return shipment


@transaction.atomic
def receive_return_shipment(shipment, user, action):
    if shipment.note != RETURN_NOTE:
        raise ValueError('Это действие доступно только для возврата')
    if shipment.status == Shipment.Status.SHIPPED:
        raise ValueError('Возврат уже получен')
    if action not in ('stock', 'dispose'):
        raise ValueError('Неизвестное действие с возвратом')

    added_lines = 0
    added_quantity = Decimal(0)
    if action == 'stock':
        for line in active_lines(shipment):
            if line.product_id is None:
                continue
            stock, _ = StockItem.objects.get_or_create(product=line.product)
            before = stock.available_quantity or Decimal(0)
            after = before + line.quantity
            stock.available_quantity = after
            stock.save(update_fields=['available_quantity'])
            StockMovement.objects.create(
                product=line.product,
                kind=StockMovement.Kind.RECEIPT,
                quantity_delta=line.quantity,
                quantity_before=before,
                quantity_after=after,
                note=f'Возврат {shipment.name}',
                created_by=user if user and user.is_authenticated else None,
            )
            added_lines += 1
            added_quantity += line.quantity

    shipment.status = Shipment.Status.SHIPPED
    shipment.shipped_at = timezone.now()
    shipment.save(update_fields=['status', 'shipped_at'])
    return {
        'action': action,
        'addedLines': added_lines,
        'addedQuantity': _fmt(added_quantity),
    }


@transaction.atomic
def delete_shipment(shipment, user):
    """Удалить заявку, вернув на склад списанный остаток, если она была отгружена."""
    if not can_delete_shipment(shipment):
        raise ValueError('Эту заявку уже нельзя удалить из интерфейса')

    if shipment.status == Shipment.Status.SHIPPED:
        for line in active_lines(shipment):
            if line.product_id is None:
                continue
            stock, _ = StockItem.objects.get_or_create(product=line.product)
            before = stock.available_quantity or Decimal(0)
            after = before + line.quantity
            stock.available_quantity = after
            stock.save(update_fields=['available_quantity'])
            StockMovement.objects.create(
                product=line.product,
                kind=StockMovement.Kind.CORRECTION,
                quantity_delta=line.quantity,
                quantity_before=before,
                quantity_after=after,
                note=f'Возврат списания при удалении отгрузки {shipment.name}',
                created_by=user if user and user.is_authenticated else None,
            )

    shipment.delete()


@transaction.atomic
def replace_lines(shipment, rows):
    """Заменить состав заявки: rows — полный массив строк из фронта.

    Отсутствующие в rows строки помечаются deleted=True (не стираются — их
    помнит дедуп импорта). Изменённые получают manually_edited=True.
    Строки с quantity <= 0 трактуются как отсутствующие: новые не создаются,
    существующие уходят в deleted — нулевая/отрицательная позиция в
    отгрузке смысла не имеет.
    """
    existing = {l.id: l for l in shipment.lines.filter(deleted=False)}
    seen = set()

    for row in rows:
        line_id = row.get('id')
        product_id = row.get('productId') or None
        quantity = Decimal(str(row.get('quantity') or 0))
        if quantity <= 0:
            continue
        line = existing.get(line_id) if line_id else None
        if line is None:
            ShipmentLine.objects.create(
                shipment=shipment,
                product_id=product_id,
                sku_raw=(row.get('sku') or '').strip(),
                name_raw=(row.get('name') or '').strip(),
                quantity=quantity,
                manually_edited=True,
            )
            continue
        seen.add(line.id)
        changed = line.quantity != quantity or line.product_id != product_id
        if changed:
            line.quantity = quantity
            line.product_id = product_id
            line.manually_edited = True
            line.save(update_fields=['quantity', 'product', 'manually_edited'])

    stale = [l for lid, l in existing.items() if lid not in seen]
    for line in stale:
        line.deleted = True
        line.save(update_fields=['deleted'])


@transaction.atomic
def bind_line(shipment, line, product, user):
    """Привязать несопоставленную строку маркетплейса к товару и запомнить связь.

    1) проставляем товар этой строке;
    2) upsert алиаса (source, нормализованный offer_id) → товар, чтобы следующий
       импорт находил товар сам (Product.sku не трогаем);
    3) ретроактивно проставляем этот товар всем несопоставленным строкам того же
       источника с тем же нормализованным offer_id в активных заявках.
    """
    line.product = product
    line.manually_edited = True
    line.save(update_fields=['product', 'manually_edited'])

    offer_id = _norm_sku(line.sku_raw)
    if not offer_id:
        return  # нечего запоминать (например, ручная строка без артикула)

    MarketplaceProductAlias.objects.update_or_create(
        source=shipment.source, offer_id=offer_id,
        defaults={'product': product,
                  'created_by': user if user and user.is_authenticated else None},
    )

    duplicates = (ShipmentLine.objects
                  .filter(shipment__source=shipment.source,
                          shipment__status=Shipment.Status.IN_WORK,
                          product__isnull=True, deleted=False)
                  .exclude(pk=line.pk))
    for dup in duplicates:
        if _norm_sku(dup.sku_raw) == offer_id:
            dup.product = product
            dup.manually_edited = True
            dup.save(update_fields=['product', 'manually_edited'])


SOURCE_PREFIX = {
    Shipment.Source.OZON: 'OZON',
    Shipment.Source.WB: 'WB',
}

RETURN_NOTE = 'marketplace_return'
WB_RETURNS_RETRY_SOURCE = 'wb_returns'
WB_RETURNS_RETRY_FALLBACK_SECONDS = 15 * 60


@transaction.atomic
def schedule_wb_returns_retry(retry_after_seconds, message):
    """Сохранить единственную попытку импорта WB-возвратов после лимита API."""
    delay = retry_after_seconds if isinstance(retry_after_seconds, int) and retry_after_seconds > 0 \
        else WB_RETURNS_RETRY_FALLBACK_SECONDS
    next_attempt_at = timezone.now() + timedelta(seconds=delay)
    retry, created = MarketplaceImportRetry.objects.select_for_update().get_or_create(
        source=WB_RETURNS_RETRY_SOURCE,
        defaults={'next_attempt_at': next_attempt_at, 'last_error': str(message), 'attempts': 1},
    )
    if not created:
        retry.next_attempt_at = next_attempt_at
        retry.last_error = str(message)
        retry.attempts += 1
        retry.save(update_fields=['next_attempt_at', 'last_error', 'attempts', 'updated_at'])
    return next_attempt_at


def run_pending_wb_returns_retry():
    """Выполнить одну наступившую отложенную загрузку возвратов WB."""
    from .marketplaces import MarketplaceError, MarketplaceRateLimitError, fetch_wb_returns

    with transaction.atomic():
        retry = (MarketplaceImportRetry.objects.select_for_update()
                 .filter(source=WB_RETURNS_RETRY_SOURCE, next_attempt_at__lte=timezone.now())
                 .first())
        if retry is None:
            return {'status': 'idle'}

        settings = MarketplaceSettings.load()
        if not settings.wb_configured:
            retry.next_attempt_at = timezone.now() + timedelta(seconds=WB_RETURNS_RETRY_FALLBACK_SECONDS)
            retry.last_error = 'Не задан токен Wildberries'
            retry.attempts += 1
            retry.save(update_fields=['next_attempt_at', 'last_error', 'attempts', 'updated_at'])
            return {'status': 'retry_scheduled', 'retryAt': retry.next_attempt_at}

        try:
            rows = fetch_wb_returns(settings.wb_token)
        except MarketplaceRateLimitError as error:
            retry_at = schedule_wb_returns_retry(error.retry_after_seconds, str(error))
            return {'status': 'retry_scheduled', 'retryAt': retry_at}
        except MarketplaceError as error:
            retry_at = schedule_wb_returns_retry(None, str(error))
            return {'status': 'retry_scheduled', 'retryAt': retry_at}

        result = import_rows(Shipment.Source.WB, rows, None, is_return=True)
        retry.delete()
        return {'status': 'ok', **result}


def _norm_sku(sku):
    """Артикул без «пробельного шума»: повторные пробелы схлопнуты, края обрезаны.

    Ozon в offer_id иногда отдаёт лишние пробелы ('123 V  П_1800' против
    каталожного '123 V П_1800') — по такому мусору точное сравнение зря
    промахивается.
    """
    return ' '.join((sku or '').split())


def _match_product(source, sku):
    """Найти товар по артикулу маркетплейса.

    Порядок: точное совпадение по Product.sku (быстрый путь и приоритет к
    точным данным) → явная привязка-алиас (source, нормализованный offer_id) →
    нормализованное совпадение по Product.sku (чтобы лишние пробелы в offer_id
    не рвали сопоставление). Сырой артикул для сравнения не мутируем.
    """
    if not sku:
        return None
    exact = (Product.objects.filter(sku=sku, archived=False).first()
             or Product.objects.filter(sku=sku).first())
    if exact:
        return exact

    norm = _norm_sku(sku)

    alias = (MarketplaceProductAlias.objects
             .filter(source=source, offer_id=norm)
             .select_related('product').first())
    if alias:
        return alias.product

    if norm and norm != sku:
        return (Product.objects.filter(sku=norm, archived=False).first()
                or Product.objects.filter(sku=norm).first())
    return None


def _is_return_shipment(shipment):
    return shipment.note == RETURN_NOTE


def _seen_unit_counts(source, is_return=False):
    """Уже импортированные (номер отправления, артикул) по источнику.

    Учитываются ВСЕ заявки источника, включая отгруженные и мягко удалённые
    строки — иначе удалённая вручную позиция вернулась бы при следующей загрузке.
    """
    counts = {}
    qs = ShipmentLine.objects.filter(shipment__source=source)
    if is_return:
        qs = qs.filter(shipment__note=RETURN_NOTE)
    else:
        qs = qs.exclude(shipment__note=RETURN_NOTE)
    for posting, sku, quantity in (qs
                                  .values_list('external_posting', 'sku_raw', 'quantity')):
        key = (posting, sku)
        counts[key] = counts.get(key, Decimal(0)) + (quantity or Decimal(0))
    return counts


def _unit_rows(source, rows):
    if source != Shipment.Source.OZON:
        return list(rows)
    expanded = []
    for row in rows:
        quantity = Decimal(str(row.get('quantity') or 0))
        if quantity > 1 and quantity == quantity.to_integral_value():
            for unit_index in range(int(quantity)):
                expanded.append({**row, 'quantity': Decimal('1'), 'unit_index': unit_index})
            continue
        expanded.append({**row, 'unit_index': row.get('unit_index', 0)})
    return expanded


def _shipment_accepts_import_append(shipment):
    if shipment.scan_finished_at or shipment.scans.exists():
        return False
    if shipment.source == Shipment.Source.OZON:
        return not active_lines(shipment).filter(external_status='awaiting_deliver').exists()
    if shipment.source == Shipment.Source.WB:
        if shipment.external_supply_id or shipment.handed_over_at:
            return False
        return not active_lines(shipment).exclude(external_trbx_id='').exists()
    return True


def shipment_warehouse(shipment):
    line = active_lines(shipment).exclude(external_warehouse='').order_by('id').first()
    return line.external_warehouse if line else ''


def _shipment_warehouse_key(shipment):
    return _norm_warehouse(shipment_warehouse(shipment))


def _shipment_accepts_wb_import_append(shipment, supply_id=''):
    if shipment.scan_finished_at or shipment.scans.exists() or shipment.handed_over_at:
        return False
    if shipment.external_supply_id:
        return bool(supply_id) and shipment.external_supply_id == supply_id and not (
            active_lines(shipment).exclude(external_trbx_id='').exists()
        )
    if supply_id:
        return False
    return not active_lines(shipment).exclude(external_trbx_id='').exists()


def _active_import_shipment(source, is_return=False, warehouse='', supply_id=''):
    for shipment in (Shipment.objects
                     .filter(source=source, status=Shipment.Status.IN_WORK)
                     .order_by('id')):
        if _is_return_shipment(shipment) != is_return:
            continue
        if source == Shipment.Source.OZON and not is_return:
            if _shipment_warehouse_key(shipment) != _norm_warehouse(warehouse):
                continue
        if source == Shipment.Source.WB and not is_return:
            if not _shipment_accepts_wb_import_append(shipment, supply_id=supply_id):
                continue
            return shipment
        if _shipment_accepts_import_append(shipment):
            return shipment
    return None


def _import_group_key(source, row, is_return=False):
    if is_return:
        return ('return', '')
    if source == Shipment.Source.OZON:
        return ('warehouse', _norm_warehouse(row.get('external_warehouse', '')))
    if source == Shipment.Source.WB:
        supply_id = str(row.get('external_supply_id') or '').strip()
        if supply_id:
            return ('supply', supply_id)
    return ('default', '')


def _group_fresh_rows(source, rows, is_return=False):
    groups = {}
    for row in rows:
        groups.setdefault(_import_group_key(source, row, is_return), []).append(row)
    return list(groups.values())


def _shipment_name(source, rows, is_return=False):
    stamp = timezone.now().strftime('%d.%m.%Y %H:%M')
    prefix = SOURCE_PREFIX[source]
    if source == Shipment.Source.OZON and not is_return:
        warehouse = (rows[0].get('external_warehouse') or '').strip()
        if warehouse:
            return f'Ozon {warehouse} {stamp}'
    if source == Shipment.Source.WB and not is_return:
        supply_id = (rows[0].get('external_supply_id') or '').strip()
        if supply_id:
            return f'{prefix} {supply_id} {stamp}'
    if is_return:
        prefix = f'{prefix} ВОЗВРАТ'
    return f'{prefix} {stamp}'


def _row_supply_id(source, rows, is_return=False):
    if source != Shipment.Source.WB or is_return:
        return ''
    return str(rows[0].get('external_supply_id') or '').strip()


@transaction.atomic
def import_rows(source, rows, user, is_return=False):
    """Записать строки маркетплейса в активную заявку источника.

    Возвращает {'shipmentId', 'added', 'skipped', 'unmatched'}.
    Пустой вход не создаёт заявку.
    """
    if not rows:
        return {'shipmentId': None, 'added': 0, 'skipped': 0, 'unmatched': []}

    is_return = is_return or any(bool(r.get('is_return')) for r in rows)
    rows = _unit_rows(source, rows)
    seen = _seen_unit_counts(source, is_return=is_return)
    seen_batch = set()
    fresh = []
    skipped = 0
    for r in rows:
        if not is_return and source == Shipment.Source.OZON and not r.get('external_warehouse'):
            skipped += 1
            continue
        # Строка без обоих идентификаторов бесполезна: её нельзя ни сопоставить,
        # ни отследить дедупом — а общий ключ ('', '') склеил бы её с любой другой
        # такой же строкой и «съел» бы вторую как мнимый дубликат.
        if not r['external_posting'] and not r['sku']:
            skipped += 1
            continue
        key = (r['external_posting'], r['sku'])
        unit_key = (*key, int(r.get('unit_index') or 0))
        if seen.get(key, Decimal(0)) > Decimal(unit_key[2]):
            skipped += 1
            continue
        if unit_key in seen_batch:
            skipped += 1
            continue
        seen_batch.add(unit_key)
        fresh.append(r)

    # Нечего добавлять — не плодим пустую заявку на каждый лишний клик.
    if not fresh:
        active = _active_import_shipment(source, is_return=is_return)
        any_active = (Shipment.objects
                      .filter(source=source, status=Shipment.Status.IN_WORK)
                      .filter(note=RETURN_NOTE if is_return else '')
                      .order_by('id').first())
        return {'shipmentId': (active or any_active).id if (active or any_active) else None,
                'added': 0, 'skipped': skipped, 'unmatched': []}

    shipment_ids = []
    added = 0
    unmatched = []
    for group_rows in _group_fresh_rows(source, fresh, is_return=is_return):
        shipment = _active_import_shipment(
            source,
            is_return=is_return,
            warehouse=group_rows[0].get('external_warehouse', ''),
            supply_id=_row_supply_id(source, group_rows, is_return=is_return),
        )
        if shipment is None:
            shipment = Shipment.objects.create(
                source=source,
                name=_shipment_name(source, group_rows, is_return=is_return),
                note=RETURN_NOTE if is_return else '',
                external_supply_id=_row_supply_id(source, group_rows, is_return=is_return),
                created_by=user if user and user.is_authenticated else None,
            )
        shipment_ids.append(shipment.id)
        for r in group_rows:
            product = _match_product(source, r['sku'])
            if product is None and r['sku']:
                unmatched.append(r['sku'])
            ShipmentLine.objects.create(
                shipment=shipment,
                product=product,
                sku_raw=r['sku'],
                name_raw=r['name'],
                quantity=r['quantity'],
                external_posting=r['external_posting'],
                external_status=r.get('external_status', ''),
                external_product_id=r.get('product_id', ''),
                external_warehouse=r.get('external_warehouse', ''),
            )
            added += 1

    return {
        'shipmentId': shipment_ids[0] if shipment_ids else None,
        'shipmentIds': shipment_ids,
        'added': added,
        'skipped': skipped,
        'unmatched': sorted(set(unmatched)),
    }


def _create_shipment_lines(shipment, rows):
    created = 0
    unmatched = []
    for row in rows:
        product = _match_product(shipment.source, row.get('sku'))
        if product is None and row.get('sku'):
            unmatched.append(row.get('sku'))
        ShipmentLine.objects.create(
            shipment=shipment,
            product=product,
            sku_raw=row.get('sku') or '',
            name_raw=row.get('name') or '',
            quantity=row.get('quantity') or Decimal('1'),
            external_posting=row.get('external_posting') or '',
            external_status=row.get('external_status', ''),
            external_product_id=row.get('product_id', ''),
            external_warehouse=row.get('external_warehouse', ''),
        )
        created += 1
    return created, unmatched


def _new_ozon_rows(rows, current_postings, warehouse=''):
    seen = _seen_unit_counts(Shipment.Source.OZON)
    fresh = []
    seen_batch = set()
    warehouse_key = _norm_warehouse(warehouse)
    for row in _unit_rows(Shipment.Source.OZON, rows):
        posting = row.get('external_posting')
        if not posting or posting in current_postings:
            continue
        if not row.get('external_warehouse'):
            continue
        if warehouse_key and _norm_warehouse(row.get('external_warehouse', '')) != warehouse_key:
            continue
        if not posting and not row.get('sku'):
            continue
        key = (posting, row.get('sku'))
        unit_key = (*key, int(row.get('unit_index') or 0))
        if seen.get(key, Decimal(0)) > Decimal(unit_key[2]):
            continue
        if unit_key in seen_batch:
            continue
        seen_batch.add(unit_key)
        fresh.append(row)
    return fresh


def _ozon_new_postings_summary(rows):
    postings = {}
    for row in rows:
        posting = row.get('external_posting') or ''
        if not posting:
            continue
        item = postings.setdefault(posting, {
            'posting': posting,
            'quantity': Decimal('0'),
            'name': row.get('name') or '',
            'sku': row.get('sku') or '',
            'status': row.get('external_status') or '',
            'warehouse': row.get('external_warehouse') or '',
        })
        item['quantity'] += row.get('quantity') or Decimal('0')
    return [
        {**item, 'quantity': float(item['quantity'])}
        for item in postings.values()
    ]


def create_ozon_shipment_from_rows(rows, user):
    fresh = _new_ozon_rows(rows, set())
    if not fresh:
        return {'shipmentId': None, 'added': 0, 'unmatched': []}
    stamp = timezone.now().strftime('%d.%m.%Y %H:%M')
    shipment = Shipment.objects.create(
        source=Shipment.Source.OZON,
        name=f'Ozon {(fresh[0].get("external_warehouse") or "").strip()} {stamp}'.strip(),
        created_by=user if user and user.is_authenticated else None,
    )
    added, unmatched = _create_shipment_lines(shipment, fresh)
    return {'shipmentId': shipment.id, 'added': added, 'unmatched': sorted(set(unmatched))}


@transaction.atomic
def sync_ozon_shipment(shipment, rows, new_postings_action=None, user=None):
    """Сверить текущую Ozon-заявку с актуальными строками Ozon поштучно."""
    if shipment.source != Shipment.Source.OZON:
        raise ValueError('Синхронизация доступна только для Ozon')

    current = list(active_lines(shipment))
    posting_numbers = {line.external_posting for line in current if line.external_posting}
    if not posting_numbers:
        return {'updated': 0, 'added': 0, 'removed': 0, 'unmatched': []}

    warehouse = shipment_warehouse(shipment)
    warehouse_key = _norm_warehouse(warehouse)
    new_rows = _new_ozon_rows(rows, posting_numbers, warehouse=warehouse)
    if new_rows and new_postings_action not in {'add_current', 'create_new'}:
        total = sum((row.get('quantity') or Decimal('0') for row in new_rows), Decimal('0'))
        return {
            'status': 'choice_required',
            'updated': 0,
            'added': 0,
            'removed': 0,
            'unmatched': [],
            'newPostingsCount': len({row.get('external_posting') for row in new_rows if row.get('external_posting')}),
            'newTotalQuantity': float(total),
            'newPostings': _ozon_new_postings_summary(new_rows),
        }

    fresh = []
    seen_batch = set()
    for row in _unit_rows(Shipment.Source.OZON, rows):
        if row.get('external_posting') not in posting_numbers:
            continue
        if not row.get('external_warehouse'):
            continue
        if warehouse_key and _norm_warehouse(row.get('external_warehouse', '')) != warehouse_key:
            continue
        if not row.get('external_posting') and not row.get('sku'):
            continue
        key = (row.get('external_posting'), row.get('sku'))
        unit_key = (*key, int(row.get('unit_index') or 0))
        if unit_key in seen_batch:
            continue
        seen_batch.add(unit_key)
        fresh.append(row)
    if new_postings_action == 'add_current':
        fresh.extend(new_rows)

    existing_by_key = {}
    for key, lines in groupby(
        sorted(current, key=lambda line: (line.external_posting, line.sku_raw, line.id)),
        key=lambda line: (line.external_posting, line.sku_raw),
    ):
        existing_by_key[key] = list(lines)

    updated = added = 0
    unmatched = []
    touched = set()

    fresh.sort(key=lambda row: (row.get('external_posting') or '', row.get('sku') or '', row.get('unit_index') or 0))
    for key, group in groupby(fresh, key=lambda row: (row.get('external_posting'), row.get('sku'))):
        group_rows = list(group)
        existing = existing_by_key.get(key, [])
        for index, row in enumerate(group_rows):
            product = _match_product(Shipment.Source.OZON, row.get('sku'))
            if product is None and row.get('sku'):
                unmatched.append(row.get('sku'))

            if index < len(existing):
                line = existing[index]
                changed_fields = []
                values = {
                    'product': line.product if line.product_id else product,
                    'sku_raw': row.get('sku') or '',
                    'name_raw': row.get('name') or '',
                    'quantity': row.get('quantity') or Decimal('1'),
                    'external_status': row.get('external_status', ''),
                    'external_product_id': row.get('product_id', ''),
                    'external_warehouse': row.get('external_warehouse', ''),
                }
                for field, value in values.items():
                    if getattr(line, field) != value:
                        setattr(line, field, value)
                        changed_fields.append(field)
                if line.deleted:
                    line.deleted = False
                    changed_fields.append('deleted')
                if changed_fields:
                    line.save(update_fields=changed_fields)
                    updated += 1
                touched.add(line.id)
                continue

            line = ShipmentLine.objects.create(
                shipment=shipment,
                product=product,
                sku_raw=row.get('sku') or '',
                name_raw=row.get('name') or '',
                quantity=row.get('quantity') or Decimal('1'),
                external_posting=row.get('external_posting') or '',
                external_status=row.get('external_status', ''),
                external_product_id=row.get('product_id', ''),
                external_warehouse=row.get('external_warehouse', ''),
            )
            touched.add(line.id)
            added += 1

    created_shipment = None
    created_shipment_added = 0
    if new_postings_action == 'create_new' and new_rows:
        created = create_ozon_shipment_from_rows(new_rows, user)
        created_shipment = created['shipmentId']
        created_shipment_added = created['added']
        unmatched.extend(created['unmatched'])

    return {'updated': updated, 'added': added, 'removed': 0,
            'unmatched': sorted(set(unmatched)),
            'createdShipmentId': created_shipment,
            'createdShipmentAdded': created_shipment_added}
