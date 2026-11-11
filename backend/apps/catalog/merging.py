"""Preview and atomically merge duplicate purchased materials."""
import hashlib
import json
from decimal import Decimal

from django.apps import apps
from django.db import connection, transaction
from django.db.models import Q, Sum

from .models import Product, Process, ProductMerge, Ingredient, IngredientOption
from .merge_lock import exclusive_merge
from apps.production.models import JobOperation, ProductionJob, Reservation
from apps.production.services import recalc_stock
from apps.stock.models import StockItem, StockMovement, MarketplaceStock

MERGE_LOCK = 71620261007


class MergeError(ValueError):
    def __init__(self, message, status=400):
        super().__init__(message)
        self.status = status


def current_product_id(product_id):
    seen = set()
    while product_id and product_id not in seen:
        seen.add(product_id)
        target = Product.all_objects.filter(pk=product_id).values_list('merged_into_id', flat=True).first()
        if not target:
            return product_id
        product_id = target
    raise MergeError('Повреждена цепочка объединений. Требуется проверка.', 409)


def product_relations():
    # Fail closed when future schema adds a relation that needs special handling.
    return [(model, field) for model in apps.get_models() for field in model._meta.concrete_fields
            if field.is_relation and field.related_model == Product
            and model not in (Product, ProductMerge)]


def _pair(source_id, target_id):
    if isinstance(source_id, bool) or isinstance(target_id, bool):
        raise MergeError('Выберите две разные позиции.')
    try:
        source_id, target_id = int(source_id), int(target_id)
    except (ValueError, TypeError):
        raise MergeError('Выберите две разные позиции.')
    if source_id <= 0 or target_id <= 0 or source_id == target_id:
        raise MergeError('Выберите две разные позиции.')
    pair = {p.pk: p for p in Product.all_objects.filter(pk__in=[source_id, target_id])
            .select_related('process', 'process__unit', 'process__category', 'process__default_supplier')}
    if len(pair) != 2:
        raise MergeError('Одна из выбранных позиций не найдена.', 404)
    source, target = pair[source_id], pair[target_id]
    if source.merged_into_id or target.merged_into_id:
        raise MergeError('Позиция уже объединена. Обновите список и выберите актуальные карточки.', 409)
    return source, target


def _card(p):
    stock = StockItem.objects.filter(product_id=p.pk).first()
    return {'id': p.pk, 'name': p.name, 'sku': p.sku, 'barcode': p.inner_barcode,
            'unit': p.process.unit.name if p.process.unit_id else '',
            'category': p.process.category.name if p.process.category_id else '',
            'supplier': p.process.default_supplier.name if p.process.default_supplier_id else '',
            'buy_price': str(p.default_buy_price), 'sell_price': str(p.default_sell_price),
            'min_limit': str(p.min_limit), 'options': p.options,
            'available': str(stock.available_quantity if stock else Decimal('0'))}


def _snapshot_rows(ids):
    rows = []
    for op in JobOperation.objects.exclude(completion_snapshot=None).order_by('id'):
        data = op.completion_snapshot
        if any(row.get('product_id') in ids for row in data.get('reservations', [])):
            rows.append({'id': op.pk, 'completion_snapshot': data})
    return rows


def _state(source, target):
    ids = [source.pk, target.pk]
    relations = {}
    for model, field in product_relations():
        relations[f'{model._meta.label}.{field.name}'] = list(model._base_manager
            .filter(**{field.attname + '__in': ids}).order_by('pk').values())
    process_ids = sorted({source.process_id, target.process_id})
    return {'products': list(Product.all_objects.filter(pk__in=ids).order_by('pk').values()),
            'processes': list(Process.objects.filter(pk__in=process_ids).order_by('pk').values()),
            'relations': relations, 'completion_snapshots': _snapshot_rows(ids),
            'process_jobs': list(ProductionJob.objects.filter(process_id__in=process_ids)
                                 .order_by('pk').values()),
            'own_bom': list(Ingredient.objects.filter(process_id__in=process_ids).order_by('pk').values()),
            'own_operations': list(apps.get_model('catalog', 'Operation').objects
                                   .filter(process_id__in=process_ids).order_by('pk').values()),
            'affected_jobs': list(ProductionJob.objects.filter(
                Q(product_id=source.pk) | Q(product__process__ingredients__source_product_id=source.pk)
                | Q(product__process__ingredients__options__source_product_id=source.pk))
                .distinct().order_by('pk').values('id', 'product_id', 'process_id', 'status'))}


def _signature(state):
    return hashlib.sha256(json.dumps(state, sort_keys=True, ensure_ascii=False, default=str)
                          .encode('utf-8')).hexdigest()


def preview_merge(source_id, target_id):
    source, target = _pair(source_id, target_id)
    state = _state(source, target)
    blockers = []
    if not source.process.unit_id or source.process.unit_id != target.process.unit_id:
        blockers.append('Единицы измерения должны быть заданы и совпадать.')
    for p in (source, target):
        if p.archived or p.process.archived:
            blockers.append(f'«{p.name}»: архивная позиция. Сначала восстановите ее.')
        if not p.process.buy or p.process.make:
            blockers.append(f'«{p.name}»: объединение доступно для закупаемых материалов без производства.')
        if p.process.ingredients.exists() or p.process.operations.exists() or p.process.graph:
            blockers.append(f'«{p.name}»: у материала есть собственный техпроцесс или состав. Нужна отдельная сверка.')
    known = {'catalog.ProductAttachment.product', 'catalog.Ingredient.source_product',
             'catalog.IngredientOption.source_product', 'sales.OrderLine.product',
             'purchases.PurchaseOrderLine.product', 'production.ProductionJob.product',
             'production.Reservation.product', 'stock.StockItem.product',
             'stock.StockMovement.product', 'stock.MarketplaceStock.product',
             'shipments.ShipmentLine.product', 'shipments.MarketplaceProductAlias.product'}
    if set(state['relations']) - known:
        blockers.append('Обнаружена новая связь в базе. Объединение требует обновления обработчика.')
    a, b = _card(source), _card(target)
    warnings = ['Проверьте размеры, толщину, марку и покрытие: одинаковая единица измерения не означает одинаковый материал.',
                'Остатки складываются. Если обе карточки содержат одно и то же физическое наличие, сначала выполните складскую корректировку.']
    differences = [field for field in ('name', 'sku', 'barcode', 'category', 'supplier',
                                      'buy_price', 'sell_price', 'min_limit', 'options') if a[field] != b[field]]
    if differences:
        warnings.append('Настройки основной карточки сохранятся. Отличающиеся поля показаны в сравнении.')
    market_keys = set()
    for row in state['relations'].get('stock.MarketplaceStock.product', []):
        key = row['source'], row['warehouse_id']
        if key in market_keys:
            blockers.append('Обе позиции сопоставлены одному складу маркетплейса. Сначала сверите остатки маркетплейса.')
            break
        market_keys.add(key)
    counts = {key: sum(row[field.attname] == source.pk for row in rows)
              for model, field in product_relations()
              for key, rows in [(f'{model._meta.label}.{field.name}', state['relations'][f'{model._meta.label}.{field.name}'])]}
    counts['completion_snapshots'] = sum(any(row.get('product_id') == source.pk
        for row in op['completion_snapshot'].get('reservations', [])) for op in state['completion_snapshots'])
    counts['affected_jobs'] = len(state['affected_jobs'])
    sums = dict(Reservation.objects.filter(product_id__in=[source.pk, target.pk])
                .values('kind').annotate(q=Sum('quantity')).values_list('kind', 'q'))
    have = Decimal(a['available']) + Decimal(b['available'])
    committed = sums.get('consume', Decimal(0))
    expected = sums.get('produce', Decimal(0))
    return {'source': a, 'target': b, 'differences': differences, 'warnings': warnings,
            'blockers': blockers, 'counts': counts, 'revision': _signature(state),
            'result': {'available': str(have), 'committed': str(committed),
                       'expected': str(expected), 'free': str(have - committed)}}


def _lock_merge(source_id, target_id):
    ids = [source_id, target_id]
    if connection.vendor == 'postgresql':
        with connection.cursor() as cur:
            cur.execute("SET LOCAL lock_timeout = '8s'")
            cur.execute('SELECT pg_advisory_xact_lock(%s)', [MERGE_LOCK])
            tables = {m._meta.db_table for m, _ in product_relations()}
            tables.update(m._meta.db_table for m in (Product, Process, ProductMerge, JobOperation, IngredientOption))
            cur.execute('LOCK TABLE ' + ', '.join(connection.ops.quote_name(t) for t in sorted(tables))
                        + ' IN SHARE ROW EXCLUSIVE MODE')
    # Lock before any preview balance/revision read (including on MySQL).
    process_ids = list(Product.all_objects.select_for_update().filter(pk__in=ids)
                       .order_by('pk').values_list('process_id', flat=True))
    list(Process.objects.select_for_update().filter(pk__in=process_ids).order_by('pk').values_list('pk', flat=True))
    for model, field in sorted(product_relations(), key=lambda item: item[0]._meta.label):
        list(model._base_manager.select_for_update().filter(**{field.attname + '__in': ids})
             .order_by('pk').values_list('pk', flat=True))
    list(JobOperation.objects.select_for_update().exclude(completion_snapshot=None)
         .order_by('pk').values_list('pk', flat=True))


@exclusive_merge
@transaction.atomic
def merge_materials(source_id, target_id, revision, user):
    _lock_merge(source_id, target_id)
    existing = ProductMerge.objects.filter(source_id=source_id).first()
    if existing:
        # A retry is valid only for the very same preview and target.
        if existing.target_id == int(target_id) and existing.revision == revision:
            return {'target_id': current_product_id(existing.target_id), 'merge_id': existing.pk,
                    'already_merged': True}
        raise MergeError('Позиция уже объединена. Обновите список.', 409)
    preview = preview_merge(source_id, target_id)
    if not revision or revision != preview['revision']:
        raise MergeError('Данные изменились после предварительного просмотра. Выполните проверку еще раз.', 409)
    if preview['blockers']:
        raise MergeError(' '.join(preview['blockers']))
    source, target = _pair(source_id, target_id)
    list(Product.all_objects.select_for_update().filter(pk__in=[source.pk, target.pk]).values_list('pk', flat=True))
    movement_ids = list(StockMovement.objects.filter(product_id=source.pk).values_list('pk', flat=True))
    # Copy balances exactly once; do not generate receipts or consumption.
    target_stock, _ = StockItem.objects.get_or_create(product_id=target.pk)
    target_stock.available_quantity = Decimal(preview['result']['available'])
    target_stock.save(update_fields=['available_quantity'])
    StockItem.objects.filter(product_id=source.pk).delete()
    for model, field in product_relations():
        if model == StockItem:
            continue
        model._base_manager.filter(**{field.attname: source.pk}).update(**{field.attname: target.pk})
    ProductionJob.objects.filter(product_id=target.pk, process_id=source.process_id).update(process_id=target.process_id)
    # Keep BOM row IDs (reservations use them) and quantities. OR alternatives
    # do not represent additive consumption, so equal alternatives are deduped.
    ingredient_ids = IngredientOption.objects.filter(source_product_id=target.pk).values_list('ingredient_id', flat=True)
    for ingredient_id in set(ingredient_ids):
        options = list(IngredientOption.objects.filter(ingredient_id=ingredient_id,
                       source_product_id=target.pk).order_by('priority', 'pk'))
        if len(options) > 1:
            IngredientOption.objects.filter(pk__in=[o.pk for o in options[1:]]).delete()
    # Only operational snapshots are rewritten. Immutable cancellation/defect
    # audit records retain their original facts, and IDs resolve via tombstones.
    snapshot_ids = []
    for op in JobOperation.objects.exclude(completion_snapshot=None):
        snapshot = op.completion_snapshot
        changed = False
        for row in snapshot.get('reservations', []):
            if row.get('product_id') == source.pk:
                row['product_id'] = target.pk
                changed = True
        if changed:
            JobOperation.objects.filter(pk=op.pk).update(completion_snapshot=snapshot)
            snapshot_ids.append(op.pk)
    # An earlier tombstone can point directly at the new canonical product.
    Product.all_objects.filter(merged_into_id=source.pk).update(merged_into_id=target.pk)
    Product.all_objects.filter(pk=source.pk).update(merged_into_id=target.pk, archived=True)
    if source.process_id != target.process_id and not Product.objects.filter(process_id=source.process_id).exists():
        Process.objects.filter(pk=source.process_id).update(archived=True)
    recalc_stock(target.pk)
    remaining = {f'{m._meta.label}.{f.name}': m._base_manager.filter(**{f.attname: source.pk}).count()
                 for m, f in product_relations()}
    if any(remaining.values()) or _snapshot_rows([source.pk]):
        raise MergeError('Не все связи перенесены. Объединение отменено.', 409)
    target_stock.refresh_from_db()
    if target_stock.available_quantity != Decimal(preview['result']['available']):
        raise MergeError('Проверка складского остатка не прошла. Объединение отменено.', 409)
    details = {**preview, 'movement_ids': movement_ids, 'snapshot_ids': snapshot_ids,
               'final_stock': {'available': str(target_stock.available_quantity),
                               'committed': str(target_stock.committed_quantity),
                               'expected': str(target_stock.expected_quantity)}}
    event = ProductMerge.objects.create(source_id=source.pk, target_id=target.pk,
                                        created_by=user, revision=revision, details=details)
    return {'target_id': target.pk, 'merge_id': event.pk, 'already_merged': False,
            'counts': preview['counts'], 'result': details['final_stock']}
