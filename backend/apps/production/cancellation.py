"""Compensate a completed operation without deleting its stock ledger entries."""
from collections import defaultdict
from datetime import timedelta
from decimal import Decimal

from django.db import transaction

from apps.stock.models import StockItem, StockMovement
from .models import JobOperation, ProductionJob, Reservation, OperationCancellation


def can_cancel_completion(user):
    if not user or not user.is_authenticated:
        return False
    role = getattr(getattr(user, 'profile', None), 'role', None)
    return user.is_superuser or bool(role and (role.permissions or {}).get('company', {}).get('update'))


def _legacy_snapshot(op):
    error = 'Нельзя однозначно восстановить старое выполнение по истории склада. Нужна сверка движений.'
    if not op.finished_at or op.defect_quantity:
        raise ValueError(error)
    start = op.finished_at - timedelta(seconds=10)
    previous = (op.job.operations.filter(finished_at__lt=op.finished_at)
                .order_by('-finished_at').first())
    if previous and previous.finished_at > start:
        start = previous.finished_at
    last_cancel = op.job.cancellations.filter(created_at__lt=op.finished_at).first()
    if last_cancel and last_cancel.created_at > start:
        start = last_cancel.created_at
    if op.job.operations.exclude(pk=op.pk).filter(finished_at=op.finished_at).exists():
        raise ValueError(error)
    movements = list(StockMovement.objects.filter(job=op.job, created_at__gt=start,
                    kind__in=['consumption', 'production'],
                    created_at__lte=op.finished_at).order_by('id'))
    expected = defaultdict(Decimal)
    restored = []
    reopen = min(op.quantity, op.done_quantity)
    for ing in op.job.product.process.ingredients.prefetch_related('options').all():
        opts = list(ing.options.all())
        if len(opts) > 1:
            raise ValueError(error)
        pid = opts[0].source_product_id if opts else ing.source_product_id
        amount = ing.quantity * op.done_quantity
        expected[(pid, 'consumption')] -= amount
        # Legacy executions did not record reservation reductions. Allocate the
        # returned inputs to the reopened plan, rather than invent historic deltas.
        reserve_amount = ing.quantity * reopen
        if reserve_amount and op.job.sales_order_id:
            restored.append({'product_id': pid, 'ingredient_id': ing.pk, 'kind': 'consume',
                             'sales_order_id': op.job.sales_order_id, 'quantity': str(reserve_amount)})
    if op.done_quantity:
        expected[(op.job.product_id, 'production')] += op.done_quantity
        if op.job.sales_order_id:
            restored.append({'product_id': op.job.product_id, 'ingredient_id': None, 'kind': 'produce',
                             'sales_order_id': op.job.sales_order_id, 'quantity': str(reopen)})
    actual = defaultdict(Decimal)
    for movement in movements:
        actual[(movement.product_id, movement.kind)] += movement.quantity_delta
    if not movements or dict(actual) != {key: value for key, value in expected.items() if value}:
        raise ValueError(error)
    # A movement already compensated by an earlier cancellation cannot be claimed again.
    ids = {m.pk for m in movements}
    for audit in op.job.cancellations.all():
        if ids.intersection(audit.snapshot.get('movement_ids', [])):
            raise ValueError(error)
    return {'movement_ids': sorted(ids), 'reservations': restored,
            'reopen_quantity': str(reopen), 'legacy': True}


@transaction.atomic
def cancel_completion(operation_id, reason, user):
    if not can_cancel_completion(user):
        raise PermissionError('Отмена выполнения доступна только администратору.')
    if not isinstance(reason, str) or not reason.strip() or len(reason.strip()) > 500:
        raise ValueError('Укажите причину отмены (до 500 символов).')
    op = JobOperation.objects.get(pk=operation_id)
    job = ProductionJob.objects.select_for_update().get(pk=op.job_id)
    op = JobOperation.objects.select_for_update().select_related('executor', 'job__product__process').get(pk=op.pk)
    if op.status != JobOperation.Status.DONE:
        raise ValueError('Можно отменить только завершённую работу.')
    from .custom_positions import custom_data
    if custom_data(job.product) is not None and job.operations.filter(sequence__gt=op.sequence).exclude(status='waiting').exists():
        raise ValueError('Сначала отмените работу на последующих этапах заказной позиции.')
    snapshot = op.completion_snapshot if op.completion_snapshot is not None else _legacy_snapshot(op)
    movements = list(StockMovement.objects.filter(pk__in=snapshot['movement_ids'], job=job))
    if len(movements) != len(snapshot['movement_ids']):
        raise ValueError('История складских движений неполна. Отмена невозможна.')
    from .rework import cancel_defect_record
    cancel_defect_record(op, {movement.product_id for movement in movements})
    deltas = defaultdict(Decimal)
    for movement in movements:
        deltas[movement.product_id] -= movement.quantity_delta
    stocks = {}
    for pid in sorted(deltas):
        stock, _ = StockItem.objects.get_or_create(product_id=pid)
        stocks[pid] = StockItem.objects.select_for_update().get(pk=stock.pk)
        if deltas[pid] < 0 and stocks[pid].available_quantity + deltas[pid] < 0:
            raise ValueError('Недостаточно выпущенных деталей на складе: они уже использованы или отгружены. Сначала восстановите остаток.')
    audit = OperationCancellation.objects.create(job=job, operation=op, area=op.area,
        created_by=user, reason=reason.strip(), snapshot={**snapshot, 'job_name': job.name,
            'good': str(op.done_quantity), 'defect': str(op.defect_quantity),
            'executor': (op.executor.get_full_name() or op.executor.username) if op.executor else '',
            'finished_at': op.finished_at.isoformat() if op.finished_at else None})
    for movement in movements:
        stock = stocks[movement.product_id]
        before = stock.available_quantity
        stock.available_quantity -= movement.quantity_delta
        stock.save(update_fields=['available_quantity'])
        StockMovement.objects.create(product_id=stock.product_id, job=job, kind='correction',
            quantity_delta=-movement.quantity_delta, quantity_before=before,
            quantity_after=stock.available_quantity, created_by=user,
            note=f'Отмена выполнения #{op.pk}, движение #{movement.pk}: {reason.strip()}'[:255])
    for row in snapshot['reservations']:
        if row['sales_order_id'] != job.sales_order_id:
            raise ValueError('Заказ задания изменён. Нужна сверка резервов перед отменой.')
        from apps.catalog.models import Ingredient
        row = dict(row)
        if row['ingredient_id'] and not Ingredient.objects.filter(pk=row['ingredient_id']).exists():
            row['ingredient_id'] = None
        Reservation.objects.create(job=job, **row)
    good = op.done_quantity
    op.quantity = Decimal(snapshot['reopen_quantity'])
    op.done_quantity = op.defect_quantity = op.started_quantity = 0
    op.started_at = op.finished_at = None
    op.status = JobOperation.Status.WAITING
    op.completion_snapshot = None
    if op.quantity > 0:
        op.save()
    else:
        op.delete()
    job.made_quantity = max(Decimal(0), job.made_quantity - good)
    job.status = (ProductionJob.Status.IN_PROGRESS if job.operations.filter(status__in=['in_progress', 'done']).exists()
                  else ProductionJob.Status.WAITING)
    if job.status == ProductionJob.Status.WAITING:
        job.started_at = None
    job.save(update_fields=['made_quantity', 'status', 'started_at'])
    return audit
