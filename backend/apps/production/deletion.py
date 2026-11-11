"""Preview and atomically compensate deletion of complete production job trees."""
from collections import defaultdict
from contextvars import ContextVar
from decimal import Decimal
import hashlib
import json

from django.core import signing
from rest_framework.exceptions import ValidationError
from django.db import transaction
from django.db.models import Q, Sum
from django.utils import timezone

from apps.directory.permissions import user_permissions
from apps.stock.models import StockItem, StockMovement
from .models import ProductionJob, JobOperation, Reservation, OperationCancellation, DefectRecord


_deleting = ContextVar('compensated_production_deletion', default=frozenset())
_salt = 'production.job-deletion.v1'


def can_delete_jobs(user):
    return bool(user and user.is_authenticated and (
        user.is_superuser or user_permissions(user).get('production_plan', {}).get('delete')))


def _ids(values):
    if not isinstance(values, list) or not values or any(type(v) is not int or v <= 0 for v in values):
        raise ValueError('Выберите задания для удаления.')
    return sorted(set(values))


def _scope(ids):
    found = set(ProductionJob.objects.filter(pk__in=ids).values_list('pk', flat=True))
    if found != set(ids):
        raise ValueError('Задание уже удалено или список изменился. Обновите план.')
    frontier = found.copy()
    while frontier:
        children = set(ProductionJob.objects.filter(parent_id__in=frontier).values_list('pk', flat=True)) - found
        found.update(children)
        frontier = children
    return sorted(found)


def _state(ids, lock=False):
    scope = _scope(ids)
    jobs_qs = ProductionJob.objects.filter(pk__in=scope).order_by('id')
    if lock:
        jobs_qs = jobs_qs.select_for_update()
    jobs = list(jobs_qs)
    if lock and _scope(ids) != scope:
        raise ValueError('Состав заданий изменился. Проверьте удаление заново.')
    operations = list(JobOperation.objects.filter(job_id__in=scope).order_by('id').values())
    moves = list(StockMovement.objects.filter(job_id__in=scope).order_by('id').values())
    own_reservations = list(Reservation.objects.filter(job_id__in=scope).order_by('id').values())
    product_ids = sorted({j.product_id for j in jobs} | {m['product_id'] for m in moves}
                         | {r['product_id'] for r in own_reservations})
    stocks_qs = StockItem.objects.filter(product_id__in=product_ids).order_by('product_id')
    if lock:
        stocks_qs = stocks_qs.select_for_update()
    stocks = {s.product_id: s for s in stocks_qs}
    # Load ledger/reservation state again after stock locks, since production,
    # shipping and reservation edits can run while lock acquisition waits.
    if lock:
        operations = list(JobOperation.objects.select_for_update().filter(job_id__in=scope).order_by('id').values())
        moves = list(StockMovement.objects.filter(job_id__in=scope).order_by('id').values())
        own_reservations = list(Reservation.objects.filter(job_id__in=scope).order_by('id').values())
        required = {m['product_id'] for m in moves} | {r['product_id'] for r in own_reservations}
        if not required.issubset(product_ids):
            raise ValueError('Материалы задания изменились. Проверьте удаление заново.')
    all_reservations = list(Reservation.objects.filter(product_id__in=product_ids).order_by('id').values())
    rework_ids = {job.rework_id for job in jobs if job.rework_id}
    defects = list(DefectRecord.objects.filter(Q(source_job_id__in=scope) | Q(pk__in=rework_ids)).order_by('id').values())
    from apps.catalog.models import Product
    products = {p.pk: p for p in Product.all_objects.filter(pk__in=product_ids).select_related('process__unit')}
    delta, release, expected = defaultdict(Decimal), defaultdict(Decimal), defaultdict(Decimal)
    for m in moves:
        delta[m['product_id']] -= m['quantity_delta']
    for r in own_reservations:
        (release if r['kind'] == 'consume' else expected)[r['product_id']] += r['quantity']
    external = defaultdict(Decimal)
    external_orders = defaultdict(set)
    from apps.sales.models import SalesOrder
    order_names = dict(SalesOrder.objects.filter(pk__in={r['sales_order_id'] for r in all_reservations}).values_list('id', 'name'))
    for r in all_reservations:
        if r['kind'] == 'consume' and r['job_id'] not in scope:
            external[r['product_id']] += r['quantity']
            external_orders[r['product_id']].add(order_names.get(r['sales_order_id'], str(r['sales_order_id'])))
    blockers = []
    move_ids = {m['id'] for m in moves}
    for op in operations:
        snapshot = op['completion_snapshot']
        if snapshot is not None and not set(snapshot.get('movement_ids', [])).issubset(move_ids):
            blockers.append(f'У работы #{op["id"]} неполная история движений. Нужна сверка склада.')
    moves_by_job = defaultdict(list)
    for m in moves:
        moves_by_job[m['job_id']].append(m)
    for job in jobs:
        if job.made_quantity > 0 and not any(m['kind'] == 'production' for m in moves_by_job[job.pk]):
            blockers.append(f'У задания «{job.name}» выпуск есть, но связанная история склада отсутствует. Нужна сверка.')
        for op in [o for o in operations if o['job_id'] == job.pk and o['status'] == 'done']:
            if op['completion_snapshot'] is None:
                try:
                    from .cancellation import _legacy_snapshot
                    _legacy_snapshot(JobOperation.objects.select_related('job__product__process').get(pk=op['id']))
                except ValueError:
                    blockers.append(f'Нельзя точно восстановить старую работу #{op["id"]} задания «{job.name}». Нужна сверка.')
    rows = []
    for pid in product_ids:
        stock = stocks.get(pid)
        before = stock.available_quantity if stock else Decimal(0)
        after = before + delta[pid]
        if stock is None and delta[pid]:
            blockers.append(f'«{products[pid].name}»: отсутствует складская карточка для связанного движения. Нужна сверка.')
        if delta[pid] < 0 and after < 0:
            blockers.append(f'«{products[pid].name}»: после отмены не хватает {-after:g}. Выпуск уже использован или отгружен; сначала отмените связанные операции.')
        elif delta[pid] < 0 and after < external[pid]:
            orders = ', '.join(sorted(external_orders[pid]))
            blockers.append(f'«{products[pid].name}»: после отмены останется {after:g}, а другим заданиям/заказам зарезервировано {external[pid]:g} ({orders}). Сначала освободите или перепланируйте этот резерв.')
        if delta[pid] or release[pid] or expected[pid]:
            unit = products[pid].process.unit
            rows.append({'productId': pid, 'name': products[pid].name, 'unit': str(unit) if unit else 'шт.',
                         'before': str(before), 'change': str(delta[pid]), 'after': str(after),
                         'release': str(release[pid]), 'expectedRemoved': str(expected[pid]),
                         'reservedElsewhere': str(external[pid])})
    # Rework outside the selected subtree must never be removed by surprise.
    for record in defects:
        if record['cancelled_at'] is None and record['source_job_id'] not in scope:
            blockers.append(f'Переделка Б{record["id"]} связана с исходным заданием. Удаляйте её вместе с исходным заданием или сначала отмените исходное выполнение с браком.')
        if record['cancelled_at'] is None and ProductionJob.objects.filter(rework_id=record['id']).exclude(pk__in=scope).exists():
            blockers.append(f'Переделка Б{record["id"]} связана с другими заданиями. Включите её в удаление или сначала отмените её выполнение.')
    state = {'selected': ids, 'jobs': list(jobs_qs.values()), 'operations': operations,
             'movements': moves, 'reservations': all_reservations, 'defects': defects,
             'stocks': [(pid, str(s.available_quantity), str(s.committed_quantity), str(s.expected_quantity)) for pid, s in sorted(stocks.items())]}
    fingerprint = hashlib.sha256(json.dumps(state, default=str, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
    return {'ids': scope, 'jobs': jobs, 'operations': operations, 'moves': moves, 'reservations': own_reservations,
            'stocks': stocks, 'delta': delta, 'defects': defects, 'fingerprint': fingerprint,
            'preview': {'jobs': [{'id': j.pk, 'name': j.name, 'product': products[j.product_id].name,
                                  'quantity': str(j.quantity), 'made': str(j.made_quantity)} for j in jobs],
                        'jobCount': len(jobs), 'operationCount': len(operations), 'stock': rows,
                        'blockers': list(dict.fromkeys(blockers)), 'canDelete': not blockers}}


def preview_job_deletion(ids, user):
    if not can_delete_jobs(user):
        raise PermissionError('Нет права на удаление производственных заданий.')
    ids = _ids(ids)
    state = _state(ids)
    return {**state['preview'], 'confirmation': signing.dumps(
        {'ids': ids, 'user': user.pk, 'fingerprint': state['fingerprint']}, salt=_salt)}


@transaction.atomic
def delete_job_tree(ids, user, confirmation):
    if not can_delete_jobs(user):
        raise PermissionError('Нет права на удаление производственных заданий.')
    ids = _ids(ids)
    try:
        signed = signing.loads(confirmation or '', salt=_salt, max_age=900)
    except (signing.BadSignature, TypeError):
        raise ValueError('Сначала просмотрите последствия удаления и подтвердите их. Предупреждение действует 15 минут.')
    if signed.get('ids') != ids or signed.get('user') != user.pk:
        raise ValueError('Подтверждение не соответствует выбранным заданиям.')
    state = _state(ids, lock=True)
    if signed.get('fingerprint') != state['fingerprint']:
        raise ValueError('После просмотра предупреждения данные изменились. Откройте удаление заново и проверьте новые последствия.')
    if state['preview']['blockers']:
        raise ValueError('\n'.join(state['preview']['blockers']))
    snapshot = {'deletion': True, 'job_name': ', '.join(j.name for j in state['jobs']),
                  'jobs': state['preview']['jobs'], 'operations': state['operations'],
                  'movement_ids': [m['id'] for m in state['moves']],
                  'stock': state['preview']['stock'], 'removed_reservations': state['reservations']}
    audit = OperationCancellation.objects.create(created_by=user, reason='Удаление заданий с отменой операций',
        snapshot=json.loads(json.dumps(snapshot, default=str)))
    compensation_ids = []
    for pid in sorted(state['delta']):
        delta = state['delta'][pid]
        if not delta:
            continue
        stock = state['stocks'].get(pid)
        if stock is None:
            raise ValueError('Отсутствует складская карточка. Нужна сверка.')
        before = stock.available_quantity
        stock.available_quantity += delta
        stock.save(update_fields=['available_quantity'])
        related = [m['id'] for m in state['moves'] if m['product_id'] == pid]
        compensation = StockMovement.objects.create(product_id=pid, kind='correction',
            quantity_delta=delta, quantity_before=before, quantity_after=stock.available_quantity,
            created_by=user, note=f'Отмена при удалении заданий; акт #{audit.pk}; движения ' + ','.join(map(str, related))[:160])
        compensation_ids.append(compensation.pk)
    # Preserve source names and operations in immutable history before cascade.
    DefectRecord.objects.filter(source_job_id__in=state['ids'], cancelled_at__isnull=True).update(cancelled_at=timezone.now())
    audit.snapshot['compensation_ids'] = compensation_ids
    audit.save(update_fields=['snapshot'])
    token = _deleting.set(frozenset(state['ids']))
    try:
        ProductionJob.objects.filter(pk__in=state['ids']).delete()
    finally:
        _deleting.reset(token)
    from .services import recalc_stock
    for pid in sorted(state['stocks']):
        recalc_stock(pid)
    return {'deleted': len(state['ids']), 'cancelledOperations': len(state['operations']), 'auditId': audit.pk}


def protect_uncompensated_delete(job):
    """Stop alternate ORM/cascade paths from silently orphaning stock facts."""
    if job.pk in _deleting.get():
        return
    net = StockMovement.objects.filter(job=job).values('product_id').annotate(total=Sum('quantity_delta'))
    if any(row['total'] for row in net) or job.made_quantity > 0:
        raise ValidationError({'error': 'Задание имеет выпуск или списания. Удалите его через План с просмотром последствий и откатом операций.'})
