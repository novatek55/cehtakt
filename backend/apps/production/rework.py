"""Defect provenance and incremental replenishment; no changes to customer quantities."""
from collections import defaultdict
from decimal import Decimal, ROUND_CEILING

from django.utils import timezone

from apps.stock.models import StockItem
from apps.catalog.models import Product
from .models import DefectRecord, JobOperation, ProductionJob, Reservation


def _user_name(user):
    return (user.get_full_name() or user.username) if user else ''


def lock_production_stocks(job, extra_ids=()):
    """Lock the complete component tree in ID order before any stock mutation.

    Rework can reserve deeper inputs than a normal completion consumes. Acquiring
    the same full scope up front also serializes defects sharing any component.
    """
    product_ids = {job.product_id, *extra_ids}
    frontier = {job.product_id}
    visited = set()
    while frontier:
        products = Product.objects.filter(pk__in=frontier).select_related('process')
        frontier = set()
        for product in products:
            if product.process_id in visited:
                continue
            visited.add(product.process_id)
            for ingredient in product.process.ingredients.prefetch_related('options'):
                inputs = {ingredient.source_product_id, *ingredient.options.values_list('source_product_id', flat=True)}
                frontier.update(inputs - product_ids)
                product_ids.update(inputs)
    stocks = {}
    for pid in sorted(product_ids):
        stock, _ = StockItem.objects.get_or_create(product_id=pid)
        stocks[pid] = StockItem.objects.select_for_update().get(pk=stock.pk)
    return stocks


def _pending(job):
    groups = defaultdict(Decimal)
    for op in job.operations.all():
        if op.status != JobOperation.Status.DONE:
            groups[(op.area_id, op.sequence, op.operation_id, op.name)] += op.quantity
    return max(groups.values(), default=Decimal(0))


def _ingredients(job):
    return job.product.process.ingredients.select_related('source_product__process').prefetch_related(
        'options__source_product__process')


def _allocate(ingredient, amount, balance, shortages=None):
    """Same priority/waterfall convention as completion, including purchased options."""
    products = [opt.source_product for opt in ingredient.options.all()] or [ingredient.source_product]
    allocations = []
    remaining = amount
    for product in products:
        take = min(remaining, max(Decimal(0), balance[product.pk]))
        if take:
            allocations.append((product, take))
            balance[product.pk] -= take
            remaining -= take
    if remaining:
        allocations.append((products[0], remaining))
        balance[products[0].pk] -= remaining
        if shortages is not None:
            shortages.append((products[0], remaining))
    return allocations


def _projected_balance(target_job_id):
    """Stock + unfinished output - unfinished BOM demand, shared across orders.

    Do not subtract committed again: legacy consume reservations cover only part
    of BOM demand. Reading the actual remaining work also counts planned supply
    that has no reservation, and prevents borrowing another order's components.
    """
    balance = defaultdict(Decimal, StockItem.objects.values_list('product_id', 'available_quantity'))
    jobs = list(ProductionJob.objects.filter(operations__in=JobOperation.objects.exclude(status='done'))
                .distinct().select_related('product__process').prefetch_related('operations').order_by('rank', 'id'))
    pending = {job.pk: _pending(job) for job in jobs}
    for job in jobs:
        balance[job.product_id] += pending[job.pk]
    # Reservations without remaining production work (e.g. order stock allocation)
    # are real claims too. Active jobs' reservations are already in BOM demand.
    for reservation in Reservation.objects.filter(kind='consume').exclude(job_id__in=pending):
        balance[reservation.product_id] -= reservation.quantity
    # First protect all other work; keep this source's actual OR allocation so
    # an unrelated primary-option shortage cannot trigger needless rework.
    allocations = {}
    for job in sorted(jobs, key=lambda item: item.pk == target_job_id):
        for ingredient in _ingredients(job):
            shortages = []
            assigned = _allocate(ingredient, ingredient.quantity * pending[job.pk], balance, shortages)
            if job.pk == target_job_id:
                allocations[ingredient.pk] = (assigned, shortages)
    return balance, allocations


def create_defect_record(op, user):
    if op.defect_quantity <= 0:
        return None
    job = op.job
    record = DefectRecord.objects.create(
        source_job=job, source_operation=op, quantity=op.defect_quantity,
        source={
            'jobId': job.pk, 'operationId': op.pk, 'taskNumber': job.name,
            'productName': job.product.name, 'areaId': op.area_id, 'areaName': op.area.name,
            'operationName': op.name, 'executorId': op.executor_id,
            'executorName': _user_name(op.executor),
            'recordedById': user.pk if user else None, 'recordedBy': _user_name(user),
            'finishedAt': op.finished_at.isoformat(),
            'orderId': job.sales_order_id, 'orderName': job.sales_order.name if job.sales_order_id else '',
            'parentReworkId': job.rework_id,
        })
    pending = _pending(job)
    lost = min(op.defect_quantity, pending)
    if lost <= 0:
        return record
    balance, source_allocations = _projected_balance(job.pk)

    def reserve(parent, ingredient, product, quantity):
        if quantity <= 0 or not parent.sales_order_id:
            return
        row = Reservation.objects.create(job=parent, sales_order_id=parent.sales_order_id,
            product=product, ingredient=ingredient, kind='consume', quantity=quantity)
        record.added_reservations.append({'id': row.pk, 'quantity': str(quantity)})

    def replenish(parent, ingredient, product, limit, visiting):
        shortage = min(limit, max(Decimal(0), -balance[product.pk]))
        if shortage <= 0 or product.process.buy:
            return
        if product.process_id in visiting:
            raise ValueError('В составе обнаружен цикл. Исправьте техкарту перед регистрацией брака.')
        operations = list(product.process.operations.all())
        if not operations:
            raise ValueError(f'Для переделки «{product.name}» не указан участок. Добавьте операцию в техкарту.')
        quantity = shortage.to_integral_value(rounding=ROUND_CEILING)
        child = ProductionJob.objects.create(
            name=f'{job.name[:190]}/Б{record.pk}.{len(record.generated_jobs) + 1}',
            product=product, process=product.process, parent=parent, sales_order_id=job.sales_order_id,
            quantity=quantity, batch=job.batch, customers=job.customers, rank=job.rank,
            finish_by_date=job.finish_by_date, scheduled_finish_date=job.scheduled_finish_date,
            rework=record, description=f'Переделка по браку Б{record.pk} · {job.name}'[:512])
        for operation in operations:
            JobOperation.objects.create(job=child, area=operation.area, operation=operation,
                name=operation.name, sequence=operation.sequence, quantity=quantity)
        record.generated_jobs.append({
            'jobId': child.pk, 'taskNumber': child.name, 'productName': product.name,
            'quantity': float(quantity), 'areaId': operations[0].area_id,
        })
        balance[product.pk] += quantity
        if job.sales_order_id:
            Reservation.objects.create(job=child, sales_order_id=job.sales_order_id, product=product,
                kind='produce', quantity=quantity)
        for child_ing in _ingredients(child):
            shortages = []
            allocations = _allocate(child_ing, child_ing.quantity * quantity, balance, shortages)
            for component, amount in allocations:
                reserve(child, child_ing, component, amount)
            for component, amount in shortages:
                replenish(child, child_ing, component, amount, visiting | {product.process_id})

    for ingredient in _ingredients(job):
        allocations, shortages = source_allocations.get(ingredient.pk, ([], []))
        budget = ingredient.quantity * lost
        current = defaultdict(Decimal)
        for row in Reservation.objects.filter(job=job, ingredient=ingredient, kind='consume'):
            current[row.product_id] += row.quantity
        for product, allocated in allocations:
            covered = min(allocated, current[product.pk])
            current[product.pk] -= covered
            amount = min(budget, allocated - covered)
            reserve(job, ingredient, product, amount)
            budget -= amount
        # Restore the inputs consumed by defects, without reserving the same old
        # unfinished work twice. New child demand is added to balance recursively.
        for product, shortage in shortages:
            replenish(job, ingredient, product, min(ingredient.quantity * lost, shortage), {job.product.process_id})
    record.save(update_fields=['generated_jobs', 'added_reservations'])
    return record


def cancel_defect_record(op, movement_product_ids=()):
    records = op.defect_records.filter(cancelled_at__isnull=True)
    for record in records:
        jobs = list(record.jobs.select_for_update().all())
        if any(operation.status != 'waiting' or operation.started_quantity
               for job in jobs for operation in job.operations.all()):
            raise ValueError(f'Переделка по браку Б{record.pk} уже начата. Сначала разберите связанные задания.')
        reservation_ids = [row['id'] for row in record.added_reservations]
        extra_ids = set(movement_product_ids)
        extra_ids.update(Reservation.objects.filter(pk__in=reservation_ids).values_list('product_id', flat=True))
        extra_ids.update(job.product_id for job in jobs)
        lock_production_stocks(op.job, extra_ids)
        generated_ids = {job.pk for job in jobs}
        for row in record.added_reservations:
            reservation = Reservation.objects.filter(pk=row['id']).first()
            if reservation is None or reservation.quantity != Decimal(row['quantity']):
                raise ValueError(f'Материалы переделки Б{record.pk} уже использованы. Нужна сверка перед отменой.')
            reservation.delete()
        # Refuse cascade deletion of unrelated work attached by a later edit.
        if ProductionJob.objects.filter(parent_id__in=generated_ids).exclude(pk__in=generated_ids).exists():
            raise ValueError('К переделке добавлены другие задания. Нужна сверка перед отменой.')
        record.jobs.all().delete()
        record.cancelled_at = timezone.now()
        record.save(update_fields=['cancelled_at'])


def defect_record_data(record):
    if record is None:
        return None
    jobs = {job.pk: job for job in record.jobs.all()}
    return {
        'id': record.pk, 'quantity': float(record.quantity), 'source': record.source,
        'createdAt': record.created_at.isoformat(),
        'cancelledAt': record.cancelled_at.isoformat() if record.cancelled_at else None,
        'sourceJobId': record.source_job_id,
        'jobs': [{**saved,
                  'exists': saved['jobId'] in jobs,
                  'status': jobs[saved['jobId']].status if saved['jobId'] in jobs else 'cancelled' if record.cancelled_at else 'deleted',
                  'made': float(jobs[saved['jobId']].made_quantity) if saved['jobId'] in jobs else 0,
                  } for saved in record.generated_jobs],
    }


def has_protected_rework(job):
    """A techcard resync must not rewrite the recorded defect's live obligations."""
    if job.rework_id:
        return True
    records = DefectRecord.objects.filter(cancelled_at__isnull=True)
    if job.sales_order_id:
        return records.filter(source_job__sales_order_id=job.sales_order_id).exists()
    return records.filter(source_job=job).exists()
