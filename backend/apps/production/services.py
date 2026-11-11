"""Производство: пересчёт склада из реестра резерва и материализация заданий заказа."""
from decimal import Decimal

from django.db import transaction
from django.db.models import Q, Sum
from django.utils import timezone

from apps.catalog.models import Product
from apps.stock.models import StockItem, StockMovement
from apps.sales.services import explode_order, order_bom_product_ids
from .area_rules import validate_executor
from .models import ProductionJob, JobOperation, Reservation


def recalc_stock(product_id):
    """StockItem.committed = Σ consume, expected = Σ produce по продукту (агрегаты реестра)."""
    agg = {r['kind']: r['q'] for r in
           Reservation.objects.filter(product_id=product_id).values('kind').annotate(q=Sum('quantity'))}
    si, _ = StockItem.objects.get_or_create(product_id=product_id)
    si.committed_quantity = agg.get('consume') or 0
    si.expected_quantity = agg.get('produce') or 0
    si.save(update_fields=['committed_quantity', 'expected_quantity'])


def order_has_started_jobs(order):
    """Есть ли у заданий заказа выпуск, старт или не ожидающая операция."""
    return any(_job_has_activity(job) for job in ProductionJob.objects.filter(
        sales_order=order, name__startswith=f'{order.name}/').prefetch_related('operations'))


def _job_has_activity(job):
    if (job.status != ProductionJob.Status.WAITING or job.started_at
            or Decimal(job.made_quantity or 0) > 0):
        return True
    return job.operations.filter(
        Q(status__in=[JobOperation.Status.IN_PROGRESS, JobOperation.Status.DONE])
        | Q(started_at__isnull=False) | Q(finished_at__isnull=False)
        | Q(started_quantity__gt=0) | Q(done_quantity__gt=0) | Q(defect_quantity__gt=0)
    ).exists()


def _job_tree_path(order, name):
    """Вернуть путь узла дерева для новых и старых форматов имени задания."""
    prefix = f'{order.name}/'
    if not name.startswith(prefix):
        return None
    suffix = name[len(prefix):]
    if suffix.startswith('этап-r'):
        return suffix[len('этап-'):]
    # Старые задания назывались Заказ/1.2 вместо Заказ/этап-r0.1.
    try:
        indexes = [int(part) - 1 for part in suffix.split('.')]
    except ValueError:
        return None
    if not indexes or any(index < 0 for index in indexes):
        return None
    return f'r{indexes[0]}' + ''.join(f'.{index}' for index in indexes[1:])


def _current_tree_jobs(order, jobs, tree, strict=False):
    """Match existing jobs to current BOM nodes after technology-card edits.

    Positional paths are valid only while their product still matches. A
    shifted job is reused only when its product has one unambiguous node.
    """
    nodes = {}
    paths_by_product = {}

    def collect(node, path):
        if not node['is_material']:
            nodes[path] = node['product_id']
            paths_by_product.setdefault(node['product_id'], []).append(path)
        for index, child in enumerate(node['children']):
            collect(child, f'{path}.{index}')

    for index, root in enumerate(tree):
        collect(root, f'r{index}')
    result = {}
    for job in jobs:
        old_path = _job_tree_path(order, job.name)
        if not old_path:
            continue
        if nodes.get(old_path) == job.product_id:
            path = old_path
        elif old_path not in nodes:
            # Out-of-tree staged jobs retain their own reservations and identity.
            path = old_path
        else:
            candidates = paths_by_product.get(job.product_id, [])
            if len(candidates) != 1:
                if strict:
                    raise ValueError(
                        'После изменения техкарты нельзя однозначно сопоставить '
                        f'задание №{job.id}. Требуется проверка его полуфабриката.')
                continue
            path = candidates[0]
        result.setdefault(path, []).append(job)
    return result


def order_job_status(order):
    """Агрегированный статус заданий по product_id для заказа.

    {product_id: 'done' | 'in_progress' | 'overdue' | 'waiting'}. Продукт считается
    'done' только если ВСЕ его задания готовы; иначе берём «наименее готовый» статус
    (для подсветки хода выполнения по узлам дерева разузлования).
    """
    # Приоритет незавершённости: чем меньше индекс, тем «дальше от готово».
    rank = {ProductionJob.Status.WAITING: 0, ProductionJob.Status.OVERDUE: 1,
            ProductionJob.Status.IN_PROGRESS: 2, ProductionJob.Status.DONE: 3}
    result = {}
    bom_product_ids = order_bom_product_ids(order)
    for pid, status, quantity, made_quantity in (ProductionJob.objects
                        .filter(sales_order=order, product_id__in=bom_product_ids,
                                name__startswith=f'{order.name}/')
                        .values_list('product_id', 'status', 'quantity', 'made_quantity')):
        made = Decimal(made_quantity or 0)
        if (status == ProductionJob.Status.DONE
                and made > 0
                and made + Decimal('0.0001') < Decimal(quantity or 0)):
            status = ProductionJob.Status.IN_PROGRESS
        cur = result.get(pid)
        if cur is None or rank[status] < rank[cur]:
            result[pid] = status
    jobs = list(ProductionJob.objects.filter(
        sales_order=order, name__startswith=f'{order.name}/').prefetch_related('operations'))
    tree = explode_order(order, 'with_stock', include_order_holds=True)['tree']
    jobs_by_path = _current_tree_jobs(order, jobs, tree)

    node_status = {}
    for path, path_jobs in jobs_by_path.items():
        job = max(path_jobs, key=lambda item: item.id)
        related_paths = [other_path for other_path in jobs_by_path
                         if other_path == path or other_path.startswith(f'{path}.')]
        has_started_descendant = any(
            _job_has_activity(item)
            for other_path in related_paths for item in jobs_by_path[other_path])
        has_job_ancestor = any(path.startswith(f'{other_path}.') for other_path in jobs_by_path)
        node_job_status = job.status
        if (node_job_status == ProductionJob.Status.DONE
                and Decimal(job.made_quantity or 0) > 0
                and Decimal(job.made_quantity) + Decimal('0.0001') < Decimal(job.quantity or 0)):
            node_job_status = ProductionJob.Status.IN_PROGRESS
        node_status[path] = {
            'job_id': job.id,
            'product_id': job.product_id,
            'status': node_job_status,
            'quantity': float(job.quantity),
            'can_replace': (not has_job_ancestor and not has_started_descendant
                            and not any(_job_has_activity(item) for item in path_jobs)),
        }
    # Дополнительные метаданные совместимы со старыми потребителями API,
    # которые ожидают ключи product_id со строковым статусом.
    if node_status:
        result['__nodes'] = node_status
    return result


def wipe_order_jobs(order):
    """Rebuild only jobs and holds represented by the order's BOM tree."""
    bom_product_ids = order_bom_product_ids(order)
    jobs = ProductionJob.objects.filter(sales_order=order, product_id__in=bom_product_ids,
                                 name__startswith=f'{order.name}/')
    Reservation.objects.filter(sales_order=order).filter(
        Q(job__in=jobs) | Q(job__isnull=True, product_id__in=bom_product_ids)
    ).delete()
    jobs.delete()


def create_jobs_for_order(order, mode, root_product=None, root_quantity=None):
    """Материализовать дерево заданий/операций/резервов по заказу. Возвращает счётчики.

    Предполагается, что прежние задания заказа уже снесены (wipe_order_jobs).
    """
    from .custom_positions import order_is_custom, create_custom_jobs
    if order_is_custom(order):
        return create_custom_jobs(order)
    additional_root = root_product is not None
    exploded = explode_order(order, mode, root_product=root_product, root_quantity=root_quantity,
                             include_own_reservations=additional_root)
    tree = exploded['tree']
    product_cache = {}

    def get_product(pid):
        if pid not in product_cache:
            product_cache[pid] = Product.objects.select_related('process').get(pk=pid)
        return product_cache[pid]

    stats = {'jobs': 0, 'operations': 0, 'reservations': 0, 'buy_shortage': 0}

    def materialize(node, parent_job, path, is_root=False):
        pid = node['product_id']
        if node.get('is_or'):
            for alt in node['alternatives']:
                if alt['from_stock'] > 0:
                    Reservation.objects.create(
                        product_id=alt['product_id'], sales_order=order, job=parent_job,
                        ingredient_id=node.get('ingredient_id'),
                        kind=Reservation.Kind.CONSUME, quantity=alt['from_stock'])
                    stats['reservations'] += 1
        elif node['from_stock'] > 0 and not (is_root and node['plan_deficit'] > 0):
            Reservation.objects.create(product_id=pid, sales_order=order, job=parent_job,
                                       ingredient_id=node.get('ingredient_id'),
                                       kind=Reservation.Kind.CONSUME, quantity=node['from_stock'])
            stats['reservations'] += 1
        if node['is_material']:
            if node['plan_deficit'] > 0:
                stats['buy_shortage'] += 1
            return
        if node['plan_deficit'] <= 0:
            if is_root and node['from_stock'] > 0:
                Reservation.objects.create(product_id=pid, sales_order=order, job=parent_job,
                                           kind=Reservation.Kind.CONSUME, quantity=node['from_stock'])
                stats['reservations'] += 1
            return
        product = get_product(pid)
        job = ProductionJob.objects.create(
            name=f'{order.name}/{path}', parent=parent_job, product=product,
            process=product.process, sales_order=order, batch=order.name,
            customers=order.customer_name, quantity=node['plan_deficit'],
            status=ProductionJob.Status.WAITING)
        stats['jobs'] += 1
        if is_root and node['from_stock'] > 0:
            Reservation.objects.create(product_id=pid, sales_order=order, job=job,
                                       kind=Reservation.Kind.CONSUME, quantity=node['from_stock'])
            stats['reservations'] += 1
        for op in product.process.operations.select_related('area').all():
            JobOperation.objects.create(job=job, area=op.area, operation=op, name=op.name,
                                        quantity=node['plan_deficit'], sequence=op.sequence,
                                        status=JobOperation.Status.WAITING)
            stats['operations'] += 1
        Reservation.objects.create(product_id=pid, sales_order=order, job=job,
                                   kind=Reservation.Kind.PRODUCE, quantity=node['plan_deficit'])
        stats['reservations'] += 1
        for i, child in enumerate(node['children'], start=1):
            materialize(child, job, f'{path}.{i}')

    for i, root in enumerate(tree, start=1):
        path = (f'PF-{root["product_id"]}-{ProductionJob.objects.filter(sales_order=order).count() + 1}'
                if additional_root else str(i))
        materialize(root, None, path, is_root=additional_root)

    if not additional_root:
        sync_purchase_request(order, exploded['materials_summary'])
    return stats


def set_order_material_reservation(order, mode, enabled):
    """Reserve free stock for the whole order without creating production jobs.

    Jobless CONSUME rows are order-level holds. When a later selected job needs
    the held stock, create_selected_jobs_for_order transfers those rows to jobs.
    """
    from .custom_positions import order_is_custom
    if order_is_custom(order):
        return {'enabled': False, 'reservations': 0, 'shortage': 0}
    with transaction.atomic():
        Reservation.objects.filter(sales_order=order, job__isnull=True,
                                   kind=Reservation.Kind.CONSUME).delete()
        if not enabled:
            return {'enabled': False, 'reservations': 0, 'shortage': 0}

        exploded = explode_order(order, mode)
        coverage = explode_order(order, mode, include_own_reservations=True)
        needs = {}

        def collect(node):
            if node.get('is_or'):
                for alt in node['alternatives']:
                    if alt['from_stock'] > 0:
                        key = (alt['product_id'], node.get('ingredient_id'))
                        needs[key] = needs.get(key, Decimal('0')) + Decimal(str(alt['from_stock']))
            elif node['from_stock'] > 0 and not (node.get('is_order_line') and node['plan_deficit'] > 0):
                key = (node['product_id'], node.get('ingredient_id'))
                needs[key] = needs.get(key, Decimal('0')) + Decimal(str(node['from_stock']))
            for child in node['children']:
                collect(child)

        for root in exploded['tree']:
            collect(root)
        count = 0
        for (product_id, ingredient_id), quantity in needs.items():
            Reservation.objects.create(
                product_id=product_id, sales_order=order, job=None,
                ingredient_id=ingredient_id, kind=Reservation.Kind.CONSUME,
                quantity=quantity)
            count += 1
        return {'enabled': True, 'reservations': count,
                'shortage': sum(1 for row in coverage['materials_summary'] if row['deficit'] > 1e-9)}


def create_selected_jobs_for_order(order, mode, selected_paths):
    """Add selected nodes and rebuild explicitly selected waiting tasks."""
    from .custom_positions import order_is_custom, create_custom_jobs
    if order_is_custom(order):
        if set(selected_paths) != {'r0'}:
            raise ValueError('Выберите заказную позицию целиком.')
        return create_custom_jobs(order)
    selected = {str(path) for path in selected_paths}
    # Do not treat reservations owned by unrelated staged jobs as free stock.
    # The order-level hold is shared; only reservations from the branch being
    # replaced may be released back into this calculation.
    exploded = explode_order(order, mode, include_order_holds=True)
    available_paths = set()
    nodes_by_path = {}

    def collect_products(node, path, depth):
        nodes_by_path[path] = node
        if not node['is_material']:
            available_paths.add(path)
        for index, child in enumerate(node['children']):
            collect_products(child, f'{path}.{index}', depth + 1)

    for index, root in enumerate(exploded['tree']):
        collect_products(root, f'r{index}', 0)
    if not selected or not selected.issubset(available_paths):
        raise ValueError('Выбранные полуфабрикаты отсутствуют в плане заказа.')
    for parent_path in tuple(selected):
        selected.update(path for path in available_paths
                       if path.startswith(f'{parent_path}.'))
    selected_roots = [path for path in selected if not any(
        path.startswith(f'{other}.') for other in selected)]
    order_jobs = list(ProductionJob.objects.filter(
        sales_order=order, name__startswith=f'{order.name}/').prefetch_related('operations'))
    existing_by_path = _current_tree_jobs(order, order_jobs, exploded['tree'], strict=True)

    # Adding the final product must not rebuild already materialized branches,
    # including a partially completed child whose remaining demand is nonzero.
    new_final_roots = {path for path in selected if '.' not in path
                       and path not in existing_by_path}
    preserved_job_paths = {path for path in existing_by_path
                           if any(path.startswith(f'{root}.')
                                  for root in new_final_roots)}
    selected.difference_update({path for path in selected
                               if any(path == existing or path.startswith(f'{existing}.')
                                      for existing in preserved_job_paths)})

    # A child task's stock reservations belong to its nearest task ancestor.
    # Require the parent to be selected with it; otherwise rebuilding the child
    # could duplicate or remove reservations owned by that parent.
    # A completed/started descendant that already covers its demand is reused
    # when its parent is added; it is not a task to recalculate or delete.
    satisfied_active_paths = {
        path for path in selected if path in existing_by_path
        and nodes_by_path.get(path, {}).get('plan_deficit', 0) <= 1e-9
        and any(_job_has_activity(job) for job in existing_by_path[path])
    }
    # When a selected parent contains a preserved active task, keep its nested
    # waiting cards too. They belong to that existing task tree and must not be
    # treated as independent tasks to recalculate.
    preserved_descendant_paths = {
        path for path in selected
        if any(path.startswith(f'{active_path}.')
               and any(root == active_path or active_path.startswith(f'{root}.')
                       for root in selected_roots)
               for active_path in satisfied_active_paths)
    }
    replacement_paths = [
        path for path in selected
        if path in existing_by_path
        and path not in satisfied_active_paths
        and path not in preserved_descendant_paths
    ]
    replacement_roots = [path for path in replacement_paths if not any(
        path.startswith(f'{other}.') for other in replacement_paths)]
    for path in replacement_roots:
        if any(path.startswith(f'{ancestor}.') for ancestor in existing_by_path
               if ancestor not in replacement_paths):
            raise ValueError('Чтобы пересчитать задание внутри другого задания, выберите родительский узел.')
        affected = [job for other_path, path_jobs in existing_by_path.items()
                    if other_path == path or other_path.startswith(f'{path}.')
                    for job in path_jobs]
        if any(_job_has_activity(job) for job in affected):
            raise ValueError('Начатые или завершённые задания нельзя пересчитать.')

    replace_job_ids = []
    if replacement_roots:
        replace_job_ids = [
            job.id for path, path_jobs in existing_by_path.items()
            if any(path == root or path.startswith(f'{root}.') for root in replacement_roots)
            for job in path_jobs
        ]
    exploded = explode_order(order, mode, include_order_holds=True,
                             include_job_ids=replace_job_ids,
                             production_roots=selected_roots)

    existing_jobs = {}
    for path, path_jobs in existing_by_path.items():
        existing_jobs[f'{order.name}/этап-{path}'] = max(path_jobs, key=lambda job: job.id)
    product_cache = {}
    stats = {'jobs': 0, 'operations': 0, 'reservations': 0, 'buy_shortage': 0,
             'skipped_existing': 0, 'replaced': len(replacement_roots)}

    def product(pid):
        if pid not in product_cache:
            product_cache[pid] = Product.objects.select_related('process').get(pk=pid)
        return product_cache[pid]

    def transfer_hold(product_id, ingredient_id, job, quantity):
        quantity = Decimal(str(quantity))
        if quantity <= 0:
            return
        # Keep the pre-existing order hold as a shared pool. Job reservations
        # cover only additional uncommitted stock; completion consumes the job
        # row first and then reduces the order-level hold if needed.
        Reservation.objects.create(
            product_id=product_id, sales_order=order, job=job,
            ingredient_id=ingredient_id, kind=Reservation.Kind.CONSUME,
            quantity=quantity)
        stats['reservations'] += 1

    def visit(node, path, parent_job=None, active=False, root=False):
        if node.get('is_or'):
            if active:
                for alt in node['alternatives']:
                    if alt['from_stock'] > 0:
                        transfer_hold(alt['product_id'], node.get('ingredient_id'),
                                      parent_job, alt['from_stock'])
            return
        pid = node['product_id']
        if node['from_stock'] > 0 and active and not (root and node['plan_deficit'] > 0):
            transfer_hold(pid, node.get('ingredient_id'), parent_job, node['from_stock'])
        if node['is_material']:
            if active and node['plan_deficit'] > 0:
                stats['buy_shortage'] += 1
            return

        is_selected = path in selected
        job_name = f'{order.name}/этап-{path}'
        current_job = parent_job
        child_active = active
        if job_name in existing_jobs:
            current_job = existing_jobs[job_name]
            if is_selected or path in preserved_job_paths:
                if parent_job and current_job.parent_id != parent_job.id:
                    current_job.parent = parent_job
                    current_job.save(update_fields=['parent'])
                child_active = True
                stats['skipped_existing'] += 1
            if path in preserved_job_paths:
                # Its existing operations and input reservations remain intact.
                return
        if is_selected and node['plan_deficit'] > 0:
            if job_name not in existing_jobs:
                p = product(pid)
                current_job = ProductionJob.objects.create(
                    name=job_name,
                    parent=parent_job, product=p, process=p.process, sales_order=order,
                    batch=order.name, customers=order.customer_name,
                    quantity=node['plan_deficit'], status=ProductionJob.Status.WAITING,
                    description='Частичная заявка по заказу: выбрана ветка производства.')
                existing_jobs[job_name] = current_job
                stats['jobs'] += 1
                if root and node['from_stock'] > 0:
                    transfer_hold(pid, None, current_job, node['from_stock'])
                for op in p.process.operations.select_related('area').all():
                    JobOperation.objects.create(job=current_job, area=op.area, operation=op,
                                                name=op.name, quantity=node['plan_deficit'],
                                                sequence=op.sequence,
                                                status=JobOperation.Status.WAITING)
                    stats['operations'] += 1
                Reservation.objects.create(product=p, sales_order=order, job=current_job,
                                           kind=Reservation.Kind.PRODUCE,
                                           quantity=node['plan_deficit'])
                stats['reservations'] += 1
            child_active = True

        # Only descend toward explicit selections. If this branch belongs to a
        # selected job, stock inputs are reserved; unselected deficits stay out
        # of production queues until their own checkbox is selected.
        for index, child in enumerate(node['children']):
            visit(child, f'{path}.{index}', current_job, child_active)

    with transaction.atomic():
        # Deleting only top-level selected branches cascades their waiting child
        # cards and job-linked reservations; activity was checked above.
        for path in replacement_roots:
            for job in existing_by_path[path]:
                job.delete()
            for name, job in list(existing_jobs.items()):
                job_path = _job_tree_path(order, name)
                if job_path == path or (job_path and job_path.startswith(f'{path}.')):
                    existing_jobs.pop(name, None)
        for index, root in enumerate(exploded['tree']):
            visit(root, f'r{index}', root=True)
        if stats['jobs'] == 0 and stats['skipped_existing'] == 0:
            raise ValueError('Задания не созданы: выбранные полуфабрикаты обеспечены доступным остатком.')
        # Use the pre-creation allocation; fresh job reservations must not
        # count a second time as missing inputs for these same new cards.
        sync_purchase_request(order, exploded['materials_summary'])
    return stats


def sync_purchase_request(order, materials_summary):
    """Этап 2: авто-заявка на закупку из дефицита материалов под заказ.

    Одна заявка на заказ (kind='order', sales_order=order). Идемпотентно:
    пересчитываются только позиции в статусе 'request'; позиции 'ordered'/
    'received' не трогаются. Уже заказанное (в пути) вычитается из потребности —
    полученное отражено в остатке и уже уменьшает дефицит.
    Возвращает заявку (PurchaseOrder) либо None, если заявки нет и дефицита нет.
    """
    from apps.purchases.models import PurchaseOrder, PurchaseOrderLine, LineStatus
    from apps.purchases.serializers import recalc_purchase_total

    deficits = {m['product_id']: Decimal(str(m['deficit']))
                for m in materials_summary if m['deficit'] > 1e-9}

    po = (PurchaseOrder.objects
          .filter(sales_order=order, kind=PurchaseOrder.Kind.ORDER)
          .first())
    if po is None and not deficits:
        return None
    if po is None:
        po = PurchaseOrder.objects.create(
            name=f'Закупка {order.name}', kind=PurchaseOrder.Kind.ORDER,
            sales_order=order)

    # уже в пути: по позициям 'ordered' — сколько заказано и ещё не принято
    covered = {}
    for l in po.lines.filter(status=LineStatus.ORDERED):
        covered[l.product_id] = (covered.get(l.product_id, Decimal('0'))
                                 + (l.quantity - l.received_quantity))

    po.lines.filter(status=LineStatus.REQUEST).delete()

    for pid, deficit in deficits.items():
        net = deficit - covered.get(pid, Decimal('0'))
        if net <= 0:
            continue
        product = Product.objects.select_related('process').get(pk=pid)
        PurchaseOrderLine.objects.create(
            order=po, product=product, quantity=net,
            supplier=product.process.default_supplier,
            price_per_unit=product.default_buy_price or 0,
            status=LineStatus.REQUEST)

    if not po.lines.exists():
        # дефицит исчез и заказов нет — пустая авто-заявка не нужна
        po.delete()
        return None

    recalc_purchase_total(po)
    return po


def reorder_job(job_id, direction):
    """Переместить задание в общей очереди (rank) выше/ниже на одну позицию.

    Переписывает ранги плотно (0..N−1). direction: 'up' | 'down'.
    Возвращает True при перемещении, False если задание не найдено или уже на краю.
    """
    jobs = list(ProductionJob.objects.order_by('rank', 'id'))
    ids = [j.id for j in jobs]
    if job_id not in ids:
        return False
    i = ids.index(job_id)
    j = i - 1 if direction == 'up' else i + 1
    if j < 0 or j >= len(jobs):
        return False
    jobs[i], jobs[j] = jobs[j], jobs[i]
    for pos, job in enumerate(jobs):
        if job.rank != pos:
            job.rank = pos
            job.save(update_fields=['rank'])
    return True


def reorder_jobs(ordered_ids):
    """Задать новый порядок очереди по списку id (drag-and-drop): rank = позиция."""
    for pos, jid in enumerate(ordered_ids):
        ProductionJob.objects.filter(pk=jid).update(rank=pos)


def _reduce_reservation(job, product_id, kind, amount):
    """Уменьшить ПЛАНОВЫЙ резерв на фактически исполненное.

    Иначе после реального движения склада план остался бы висеть и проекция
    (наличие − резерв + выпуск) задвоилась бы. Сигналы пересчитают StockItem.
    """
    rest = Decimal(str(amount))
    rows = Reservation.objects.filter(job=job, product_id=product_id, kind=kind).order_by('id')
    if kind == Reservation.Kind.CONSUME:
        rows = list(rows) + list(Reservation.objects.filter(
            sales_order=job.sales_order, job__isnull=True, product_id=product_id,
            kind=kind).order_by('id'))
    for r in rows:
        if rest <= 0:
            break
        take = min(r.quantity, rest)
        r.quantity -= take
        rest -= take
        if r.quantity <= 0:
            r.delete()
        else:
            r.save(update_fields=['quantity'])


def _reduce_group_reservation(job, ingredient_id, amount):
    """Уменьшить плановые CONSUME-резервы строки состава (группы ИЛИ) на потреблённый объём.

    Резервы ключуются по продукту, но тег ingredient позволяет гасить группу
    целиком, даже если фактический расклад на выпуске отличается от планового.
    """
    rest = Decimal(str(amount))
    if ingredient_id is None:
        return rest
    qs = list(Reservation.objects.filter(job=job, ingredient_id=ingredient_id,
                                         kind=Reservation.Kind.CONSUME).order_by('id'))
    qs += list(Reservation.objects.filter(sales_order=job.sales_order, job__isnull=True,
                                          ingredient_id=ingredient_id,
                                          kind=Reservation.Kind.CONSUME).order_by('id'))
    for r in qs:
        if rest <= 0:
            break
        take = min(r.quantity, rest)
        r.quantity -= take
        rest -= take
        if r.quantity <= 0:
            r.delete()
        else:
            r.save(update_fields=['quantity'])
    return rest


@transaction.atomic
def finish_operation(op, good, defect, user=None, splits=None):
    """Завершить операцию: списать входы по (годные+брак), выпустить годные на склад.

    Брак — материал потрачен, изделие не вышло. Плановые резервы уменьшаются на факт.
    """
    good = Decimal(str(good))
    defect = Decimal(str(defect))
    total = good + defect
    job = ProductionJob.objects.select_for_update().get(pk=op.job_id)
    op.refresh_from_db()
    if op.status == JobOperation.Status.DONE:
        raise ValueError('Операция уже завершена.')
    from .custom_positions import custom_data, finish_custom
    if custom_data(job.product) is not None:
        return finish_custom(op, good, defect)
    reservation_scope = Q(job=job) | Q(sales_order=job.sales_order, job__isnull=True,
                                          kind=Reservation.Kind.CONSUME)
    reservations_before = list(Reservation.objects.filter(reservation_scope).values())
    movement_ids = []
    original_quantity = op.quantity
    from .rework import lock_production_stocks
    lock_production_stocks(job)

    # 1) Списание входных компонентов по факту (годные + брак)
    splits = splits or {}
    for ing in (job.product.process.ingredients
                .prefetch_related('options__source_product').all()):
        need = Decimal(str(ing.quantity)) * total
        if need <= 0:
            continue
        opts = list(ing.options.order_by('priority', 'id'))
        # раскладка по вариантам: ручная (splits) или авто-waterfall по текущему остатку
        alloc = {}  # product_id -> qty
        manual = splits.get(ing.id)
        if manual:
            for pid, q in manual.items():
                alloc[int(pid)] = Decimal(str(q))
        elif len(opts) <= 1:
            pid = opts[0].source_product_id if opts else ing.source_product_id
            alloc[pid] = need
        else:
            remaining = need
            for opt in opts:
                stock = StockItem.objects.filter(product_id=opt.source_product_id).first()
                have = (stock.available_quantity if stock else Decimal(0)) or Decimal(0)
                take = min(remaining, max(have, Decimal(0)))
                if take > 0:
                    alloc[opt.source_product_id] = take
                    remaining -= take
                if remaining <= 0:
                    break
            if remaining > 0:  # хвост — на основной вариант (может уйти в минус)
                pid = opts[0].source_product_id
                alloc[pid] = alloc.get(pid, Decimal(0)) + remaining

        for pid, qty in alloc.items():
            if qty <= 0:
                continue
            stock, _ = StockItem.objects.get_or_create(product_id=pid)
            before = stock.available_quantity or Decimal(0)
            after = before - qty
            stock.available_quantity = after
            stock.save(update_fields=['available_quantity'])
            movement = StockMovement.objects.create(
                product_id=pid, kind=StockMovement.Kind.CONSUMPTION,
                quantity_delta=-qty, job=job,
                quantity_before=before, quantity_after=after,
                note=f'Списание по заданию {job.name}', created_by=user)
            movement_ids.append(movement.pk)
        fallback_rest = _reduce_group_reservation(job, ing.id, sum(alloc.values(), Decimal(0)))
        for pid, qty in alloc.items():
            if fallback_rest <= 0:
                break
            take = min(qty, fallback_rest)
            _reduce_reservation(job, pid, Reservation.Kind.CONSUME, take)
            fallback_rest -= take

    # 2) Выпуск годных на склад
    if good > 0:
        out, _ = StockItem.objects.get_or_create(product=job.product)
        before = out.available_quantity or Decimal(0)
        after = before + good
        out.available_quantity = after
        out.save(update_fields=['available_quantity'])
        movement = StockMovement.objects.create(
            product=job.product, kind=StockMovement.Kind.PRODUCTION,
            quantity_delta=good, job=job,
            quantity_before=before, quantity_after=after,
            note=f'Выпуск по заданию {job.name}', created_by=user)
        movement_ids.append(movement.pk)
    _reduce_reservation(job, job.product_id, Reservation.Kind.PRODUCE, good)
    reservations_after = dict(Reservation.objects.filter(reservation_scope).values_list('id', 'quantity'))
    restored = []
    for row in reservations_before:
        reduction = row['quantity'] - reservations_after.get(row['id'], Decimal(0))
        if reduction > 0:
            restored.append({key: row[key] for key in ('product_id', 'sales_order_id', 'ingredient_id', 'kind')}
                            | {'quantity': str(reduction)})
    op.completion_snapshot = {'movement_ids': movement_ids, 'reservations': restored,
                              'reopen_quantity': str(min(original_quantity, good))}

    # 3) Статусы
    planned = Decimal(str(op.quantity))
    # Остаток плана — это недостающие годные изделия. Брак уже списан как
    # фактическая работа и не должен уменьшать новое назначение.
    remaining = planned - good
    if remaining > 0:
        JobOperation.objects.create(
            job=job, area=op.area, operation=op.operation, name=op.name,
            quantity=remaining, sequence=op.sequence,
            status=JobOperation.Status.WAITING, executor=op.executor)
        op.quantity = total
    op.status = JobOperation.Status.DONE
    op.done_quantity = good
    op.defect_quantity = defect
    op.finished_at = timezone.now()
    op.save(update_fields=['quantity', 'status', 'done_quantity', 'defect_quantity', 'finished_at', 'completion_snapshot'])
    job.made_quantity = (job.made_quantity or Decimal(0)) + good
    fields = ['made_quantity']
    if not job.operations.exclude(status=JobOperation.Status.DONE).exists():
        job.status = ProductionJob.Status.DONE
        fields.append('status')
    job.save(update_fields=fields)
    from .rework import create_defect_record
    record = create_defect_record(op, user)
    return {'good': float(good), 'defect': float(defect), 'defectRecordId': record.pk if record else None}


def edit_group_distribution(job_id, area_id, rows, user=None):
    """Изменить распределение НЕ завершённой части группы (job, area) по исполнителям.

    Обобщение сплита: правит ожидающие И начатые строки, пока задание не завершено.
    - done-строки заблокированы (не входят в rows, не меняются);
    - начатую (in_progress/started) строку нельзя удалить и нельзя задать кол-во меньше
      уже взятого в работу; исполнителя и кол-во (вверх) — можно;
    - ожидающие строки можно добавлять/удалять/править;
    - сумма rows <= не завершённого объёма (pool); остаток — строка без исполнителя.

    Склад/резервы не трогаем: они привязаны к job, а не к разбивке операций участка.

    rows: [{'opId': int|None, 'executorId': int|None, 'quantity': number}].
    Возвращает актуальный список операций группы.
    """
    from .custom_positions import custom_data
    custom = custom_data(ProductionJob.objects.get(pk=job_id).product) is not None
    if custom and (len(rows or []) != 1 or Decimal(str(rows[0].get('quantity') or 0)) != 1):
        raise ValueError('Заказная позиция назначается целиком одному исполнителю участка.')
    ops = list(JobOperation.objects.filter(job_id=job_id, area_id=area_id))
    if not ops:
        raise ValueError('Группа не найдена.')
    editable = [o for o in ops if o.status != JobOperation.Status.DONE]
    if not editable:
        raise ValueError('Задание завершено — править нельзя.')
    by_id = {o.id: o for o in editable}
    started = {o.id: o for o in editable
               if o.status == JobOperation.Status.IN_PROGRESS or o.started_quantity}
    pool = sum((Decimal(str(o.quantity)) for o in editable), Decimal(0))
    template = editable[0]

    present_ids = {r.get('opId') for r in rows or [] if r.get('opId')}
    for sid in started:
        if sid not in present_ids:
            raise ValueError('Нельзя убрать уже начатую строку.')

    parsed = []  # (opId|None, executorId|None, Decimal qty)
    for r in rows or []:
        op_id = r.get('opId') or None
        qty = Decimal(str(r.get('quantity') or 0))
        if qty <= 0:
            if op_id in started:
                raise ValueError('У начатой строки кол-во не может быть меньше взятого в работу.')
            continue
        if op_id in started and qty < Decimal(str(started[op_id].started_quantity)):
            raise ValueError('Нельзя задать кол-во меньше уже взятого в работу.')
        executor_id = r.get('executorId') or None
        if executor_id is not None:
            executor_id = validate_executor(executor_id, area_id)
        parsed.append((op_id if op_id in by_id else None, executor_id, qty))
    # Пустой список допустим (нет начатых строк): весь объём уйдёт в остаток без исполнителя.
    total = sum((q for _, _, q in parsed), Decimal(0))
    if total > pool:
        raise ValueError(
            f'Сумма ({total:g}) больше не завершённого объёма ({pool:g}).')
    remainder = pool - total

    keep_ids = set()
    for op_id, ex, q in parsed:
        if op_id is not None:
            o = by_id[op_id]
            o.executor_id = ex
            o.quantity = q
            o.save(update_fields=['executor', 'quantity'])
            keep_ids.add(op_id)
        else:
            created = JobOperation.objects.create(
                job_id=job_id, area_id=area_id, operation=template.operation,
                name=template.name, quantity=q, sequence=template.sequence,
                status=JobOperation.Status.WAITING, executor_id=ex)
            keep_ids.add(created.id)
    # Удаляем ожидающие строки, которые убрали (начатые/завершённые не трогаем).
    for o in editable:
        if o.id not in keep_ids and o.id not in started:
            o.delete()
    if remainder > 0:
        JobOperation.objects.create(
            job_id=job_id, area_id=area_id, operation=template.operation,
            name=template.name, quantity=remainder, sequence=template.sequence,
            status=JobOperation.Status.WAITING, executor_id=None)
    return list(JobOperation.objects.filter(job_id=job_id, area_id=area_id).order_by('sequence', 'id'))


def delete_jobs(ids, user=None, confirmation=None):
    """Delete only after a confirmed stock-compensation preview."""
    from .deletion import delete_job_tree
    return delete_job_tree(ids, user, confirmation)


def _operation_protected_quantity(op):
    if op.status == JobOperation.Status.WAITING and not op.started_quantity:
        return Decimal(0)
    if op.status == JobOperation.Status.DONE:
        return Decimal(str(op.quantity))
    done = Decimal(str(op.done_quantity or 0)) + Decimal(str(op.defect_quantity or 0))
    started = Decimal(str(op.started_quantity or 0))
    return max(done, started)


def _shrink_protected_operations(job):
    """Keep only the factual part of started/done operations before rebuilding the plan."""
    groups = {}
    for op in job.operations.all():
        protected = _operation_protected_quantity(op)
        if protected <= 0:
            continue
        if Decimal(str(op.quantity)) != protected:
            op.quantity = protected
            op.save(update_fields=['quantity'])
        key = (op.sequence, op.area_id, op.operation_id, op.name)
        groups[key] = groups.get(key, Decimal(0)) + protected
    return max(groups.values(), default=Decimal(0))


def job_techcard_sync_preview(product):
    """Return open jobs for a product grouped by whether tech card ops can be applied safely."""
    rows = []
    jobs = (ProductionJob.objects
            .filter(product=product)
            .prefetch_related('operations')
            .select_related('sales_order')
            .order_by('rank', 'id'))
    for job in jobs:
        from .rework import has_protected_rework
        operations = list(job.operations.all())
        has_started_ops = any(op.status != JobOperation.Status.WAITING for op in operations)
        if has_protected_rework(job):
            sync_status = 'started'
            reason = 'Есть учёт брака: связанные задания и резервы сохраняются'
        elif job.status == ProductionJob.Status.DONE:
            sync_status = 'done'
            reason = 'Задание уже завершено'
        elif job.sales_order_id:
            protected = _shrink_protected_operations(job)
            remaining = Decimal(str(job.quantity)) - protected
            if remaining <= 0:
                sync_status = 'started'
                reason = 'Весь объём задания уже начат или выполнен'
            elif protected > 0 or has_started_ops or job.status != ProductionJob.Status.WAITING:
                sync_status = 'order_partial'
                reason = 'Можно обновить оставшийся объём по новой техкарте, выполненное останется'
            else:
                sync_status = 'order_updatable'
                reason = 'Можно обновить задание заказа по новой техкарте'
        elif has_started_ops or job.status != ProductionJob.Status.WAITING:
            sync_status = 'started'
            reason = 'Задание уже начато'
        else:
            sync_status = 'updatable'
            reason = 'Можно обновить операции по новой техкарте'
        rows.append({
            'id': job.id,
            'name': job.name,
            'quantity': float(job.quantity),
            'orderName': job.sales_order.name if job.sales_order_id else '',
            'syncStatus': sync_status,
            'reason': reason,
        })
    updatable_statuses = {'updatable', 'order_updatable', 'order_partial'}
    updatable = sum(1 for row in rows if row['syncStatus'] in updatable_statuses)
    order_updatable = sum(1 for row in rows if row['syncStatus'] in {'order_updatable', 'order_partial'})
    partial = sum(1 for row in rows if row['syncStatus'] == 'order_partial')
    return {
        'summary': {
            'total': len(rows),
            'updatable': updatable,
            'fullUpdatable': sum(1 for row in rows if row['syncStatus'] in {'updatable', 'order_updatable'}),
            'orderUpdatable': order_updatable,
            'partialUpdatable': partial,
            'blocked': len(rows) - updatable,
        },
        'jobs': rows,
    }


def _sync_one_waiting_job(job):
    master_ops = list(job.product.process.operations.select_related('area').all())
    JobOperation.objects.filter(job=job).delete()
    for op in master_ops:
        JobOperation.objects.create(
            job=job, area=op.area, operation=op, name=op.name,
            quantity=job.quantity, sequence=op.sequence,
            status=JobOperation.Status.WAITING,
        )
    job.process = job.product.process
    job.save(update_fields=['process'])


def _rebuild_order_job_reservations(job, quantity):
    Reservation.objects.filter(job=job).delete()
    if quantity <= 0:
        return
    Reservation.objects.create(
        product_id=job.product_id, sales_order=job.sales_order, job=job,
        kind=Reservation.Kind.PRODUCE, quantity=quantity)
    for ing in job.product.process.ingredients.prefetch_related('options__source_product').all():
        opts = list(ing.options.order_by('priority', 'id'))
        source_product_id = opts[0].source_product_id if opts else ing.source_product_id
        Reservation.objects.create(
            product_id=source_product_id, sales_order=job.sales_order, job=job,
            ingredient_id=ing.id, kind=Reservation.Kind.CONSUME,
            quantity=Decimal(str(ing.quantity)) * quantity)


def _sync_order_job_remaining(job):
    protected = _shrink_protected_operations(job)
    remaining = Decimal(str(job.quantity)) - protected
    if remaining <= 0:
        if protected > 0 and Decimal(str(job.quantity)) != protected:
            job.quantity = protected
            job.save(update_fields=['quantity'])
        return False

    JobOperation.objects.filter(
        job=job,
        status=JobOperation.Status.WAITING,
        started_quantity=0,
    ).delete()
    master_ops = list(job.product.process.operations.select_related('area').all())
    if not master_ops:
        if protected > 0:
            job.quantity = protected
            job.process = job.product.process
            job.save(update_fields=['quantity', 'process'])
        _rebuild_order_job_reservations(job, Decimal(0))
        return True
    for op in master_ops:
        JobOperation.objects.create(
            job=job, area=op.area, operation=op, name=op.name,
            quantity=remaining, sequence=op.sequence,
            status=JobOperation.Status.WAITING,
        )
    job.process = job.product.process
    job.save(update_fields=['process'])
    _rebuild_order_job_reservations(job, remaining)
    return True


def _current_child_product_ids(job):
    ids = set()
    for ing in job.product.process.ingredients.select_related('source_product__process').all():
        child = ing.source_product
        if child.process.buy:
            continue
        ids.add(child.id)
    return ids


def _job_can_be_reparented(job):
    """A not-yet-started child can safely follow a new assembly in the tech card."""
    return (
        job.status == ProductionJob.Status.WAITING
        and not job.operations.exclude(status=JobOperation.Status.WAITING).exists()
        and not job.operations.exclude(started_quantity=0).exists()
    )


def _create_order_child_job(parent, product, quantity, position):
    """Create the newly required producible node of an already created order job."""
    child = ProductionJob.objects.create(
        name=f'{parent.name}.{position}', parent=parent, product=product,
        process=product.process, sales_order=parent.sales_order,
        batch=parent.batch, customers=parent.customers, quantity=quantity,
        status=ProductionJob.Status.WAITING,
    )
    for op in product.process.operations.select_related('area').all():
        JobOperation.objects.create(
            job=child, area=op.area, operation=op, name=op.name,
            quantity=quantity, sequence=op.sequence,
            status=JobOperation.Status.WAITING,
        )
    Reservation.objects.create(
        product=product, sales_order=parent.sales_order, job=child,
        kind=Reservation.Kind.PRODUCE, quantity=quantity,
    )
    return child


def _sync_new_order_children(root_job):
    """Materialize new producible nodes and retain waiting descendants moved under them.

    The old tree may have components directly under a parent before a new assembly
    is inserted between them.  Reuse only untouched component jobs; started work
    stays in place and is never silently moved or deleted.
    """
    def source_product(ingredient):
        options = list(ingredient.options.order_by('priority', 'id'))
        return options[0].source_product if options else ingredient.source_product

    def reconcile(parent, planned_quantity, visiting, carried_children=()):
        if parent.product.process_id in visiting:
            return
        visiting = visiting | {parent.product.process_id}
        ingredients = list(
            parent.product.process.ingredients
            .select_related('source_product__process')
            .prefetch_related('options__source_product__process')
            .all()
        )
        direct_children = list(
            parent.children.select_related('product__process').prefetch_related('operations').all()
        )
        used_ids = set()
        desired_product_ids = {source_product(ingredient).id for ingredient in ingredients}
        carried_children = list(carried_children)
        for position, ingredient in enumerate(ingredients, start=1):
            product = source_product(ingredient)
            if product.process.buy:
                continue
            quantity = Decimal(str(planned_quantity)) * Decimal(str(ingredient.quantity))
            direct = [candidate for candidate in direct_children
                      if candidate.id not in used_ids and candidate.product_id == product.id]
            candidate = direct[0] if direct else next(
                (candidate for candidate in carried_children
                 if candidate.id not in used_ids
                 and candidate.product_id == product.id),
                None,
            )
            created = candidate is None
            if candidate is None:
                candidate = _create_order_child_job(parent, product, quantity, position)
            elif candidate.parent_id != parent.id:
                candidate.parent = parent
                candidate.save(update_fields=['parent'])

            used_ids.add(candidate.id)
            # Only direct children displaced by this new intermediate assembly can
            # be adopted by it.  Looking through the whole order would incorrectly
            # move an identical component from another branch of the product tree.
            carried = (
                [child for child in direct_children
                 if child.id not in used_ids and child.product_id not in desired_product_ids]
                if created else []
            )
            # Existing jobs may already have a smaller quantity because a part was
            # covered by stock or completed.  New descendants must follow that
            # real job quantity, not the original BOM demand from above.
            reconcile(candidate, candidate.quantity, visiting, carried)

    reconcile(root_job, root_job.quantity, set())


def _delete_or_trim_removed_job(job):
    for child in list(job.children.all()):
        _delete_or_trim_removed_job(child)
    protected = _shrink_protected_operations(job)
    if protected <= 0:
        job.delete()
        return 'deleted'
    JobOperation.objects.filter(
        job=job,
        status=JobOperation.Status.WAITING,
        started_quantity=0,
    ).delete()
    Reservation.objects.filter(job=job).delete()
    if Decimal(str(job.quantity)) != protected:
        job.quantity = protected
        job.save(update_fields=['quantity'])
    return 'trimmed'


def _prune_removed_child_jobs(job):
    current_ids = _current_child_product_ids(job)
    changed = 0
    for child in list(job.children.select_related('product', 'product__process').all()):
        if child.product_id not in current_ids:
            _delete_or_trim_removed_job(child)
            changed += 1
        else:
            changed += _prune_removed_child_jobs(child)
    return changed


@transaction.atomic
def apply_techcard_to_waiting_jobs(product):
    """Rebuild operations for safe jobs; preserve already started/done order work."""
    updated = 0
    skipped = []
    jobs = (ProductionJob.objects
            .select_for_update()
            .filter(product=product)
            .prefetch_related('operations')
            .select_related('sales_order', 'product__process')
            .order_by('rank', 'id'))
    for job in jobs:
        from .rework import has_protected_rework
        if has_protected_rework(job):
            skipped.append({'id': job.id, 'name': job.name, 'syncStatus': 'started'})
            continue
        if job.sales_order_id:
            if _sync_order_job_remaining(job):
                _sync_new_order_children(job)
                _prune_removed_child_jobs(job)
                updated += 1
            else:
                skipped.append({'id': job.id, 'name': job.name, 'syncStatus': 'started'})
            continue
        if job.status != ProductionJob.Status.WAITING:
            skipped.append({'id': job.id, 'name': job.name, 'syncStatus': 'started'})
            continue
        if job.operations.exclude(status=JobOperation.Status.WAITING).exists():
            skipped.append({'id': job.id, 'name': job.name, 'syncStatus': 'started'})
            continue
        _sync_one_waiting_job(job)
        updated += 1
    preview = job_techcard_sync_preview(product)
    return {'updated': updated, 'skipped': skipped, **preview}
