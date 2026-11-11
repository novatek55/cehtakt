import math
from collections import defaultdict
from datetime import date, datetime, time
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.db.models import Prefetch
from django.shortcuts import get_object_or_404
from django.utils import timezone
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.directory.models import Area
from apps.directory.permissions import visible_area_ids, own_tasks_only
from apps.catalog.models import Product
from .area_rules import validate_executor, is_area_member
from .models import ProductionJob, JobOperation, Reservation
from .serializers import ProductionJobSerializer
from .cancellation import can_cancel_completion, cancel_completion
from .rework import defect_record_data
from .custom_positions import custom_data, stage_ready, validate_stage
from django.db import transaction

from .services import (
    reorder_job, reorder_jobs, delete_jobs, finish_operation, edit_group_distribution,
    job_techcard_sync_preview, apply_techcard_to_waiting_jobs,
)


def _row_dict(op, max_units, can_change_state=True):
    """Строка «Работы» — одна операция группы (job, area). max_units — потолок по материалу."""
    max_start = float(op.quantity) if max_units is None else min(float(op.quantity), math.floor(max_units))
    return {
        'opId': op.id,
        'operationId': op.operation_id,
        'operationName': op.name,
        'sequence': op.sequence,
        'executorId': op.executor_id,
        'executorName': (op.executor.get_full_name() or op.executor.username) if op.executor else '',
        'quantity': float(op.quantity),
        'startedQuantity': float(op.started_quantity),
        'done': float(op.done_quantity),
        'defect': float(op.defect_quantity),
        'status': op.status,
        'startedAt': op.started_at.isoformat() if op.started_at else None,
        'finishedAt': op.finished_at.isoformat() if op.finished_at else None,
        'canStart': op.status == JobOperation.Status.WAITING and (max_units is None or math.floor(max_units) >= 1),
        'canChangeState': can_change_state,
        'maxStart': max(0.0, round(max_start, 3)),
    }


def _can_change_operation_state(user, area_id):
    """Start/finish/cancel/status changes are limited to the user's assigned areas."""
    return _can_assign_executor(user) or is_area_member(user, area_id)


class ProduceView(APIView):
    """POST /api/produce/ {product_id, quantity} — создать задание на производство."""

    def post(self, request):
        product = Product.objects.filter(pk=request.data.get('product_id')).select_related('process').first()
        if not product:
            return Response({'error': 'изделие не найдено'}, status=404)
        qty = request.data.get('quantity') or 1
        job = ProductionJob.objects.create(
            name=f'{product.name} — производство', product=product, process=product.process,
            quantity=qty, status=ProductionJob.Status.WAITING)
        return Response({'id': job.id, 'name': job.name}, status=201)


class ProduceBulkView(APIView):
    """POST /api/produce/bulk/ {rows:[{product, quantity}]} — массовое создание заданий.

    product — имя или артикул изделия; сопоставляется с каталогом.
    Возвращает created и список нераспознанных строк (unmatched).
    """

    def post(self, request):
        rows = request.data.get('rows') or []
        created, unmatched = 0, []
        for r in rows:
            key = (r.get('product') or '').strip()
            if not key:
                continue
            product = (Product.objects.filter(sku__iexact=key).first()
                       or Product.objects.filter(name__iexact=key).first())
            if not product:
                unmatched.append(key)
                continue
            qty = r.get('quantity') or 1
            ProductionJob.objects.create(
                name=f'{product.name} — производство', product=product,
                process_id=product.process_id, quantity=qty, status=ProductionJob.Status.WAITING)
            created += 1
        return Response({'created': created, 'unmatched': unmatched}, status=201)


class JobOperationUpdateView(APIView):
    """PATCH /api/tasks/<id>/ — сменить статус операции («Начать» → готово)."""

    def patch(self, request, pk):
        op = JobOperation.objects.filter(pk=pk).first()
        if not op:
            return Response({'error': 'не найдено'}, status=404)
        if not _can_change_operation_state(request.user, op.area_id):
            return Response({'error': 'Нет доступа к этому участку.'}, status=403)
        status = request.data.get('status')
        if status in (JobOperation.Status.IN_PROGRESS, JobOperation.Status.DONE):
            return Response({'error': 'Используйте действие Начать или Завершить с выбором сотрудника участка.'}, status=400)
        if status in dict(JobOperation.Status.choices):
            op.status = status
            op.save(update_fields=['status'])
        return Response({'id': op.id, 'status': op.status})


def _job_inputs(job, stock_map, planned_quantity=None):
    """Входные компоненты задания (ингредиенты продукта): наличие/нужно/дефицит.

    Возвращает (inputs, can_start). Для входа-ПФ прикладываем sourceJobId — это
    конкретное дочернее задание ЭТОГО задания, выпускающее вход (для линий-связей).
    can_start: хватает хотя бы на 1 шт (для серого/жёлтого «Начать»).
    """
    if custom_data(job.product) is not None:
        return [], True, None
    ings = job.product.process.ingredients.prefetch_related(
        'options__source_product__process').all()
    # дети именно этого задания: продукт → задание-источник
    child_by_product = {ch.product_id: ch.id for ch in job.children.all()}
    from apps.catalog.capacity import shared_material_capacity
    inputs, requirements = [], []
    planned_pool = {pid: max(float(stock.available_quantity), 0.0) for pid, stock in stock_map.items()}
    planned_quantity = float(planned_quantity if planned_quantity is not None else job.quantity)
    for ing in ings:
        opts = list(ing.options.select_related('source_product__process').order_by('priority', 'id'))
        per = float(ing.quantity)
        need = per * planned_quantity
        if len(opts) > 1:
            # ИЛИ-группа: доступность = сумма остатков всех вариантов
            avail, alts, remaining = 0.0, [], need
            for opt in opts:
                osp = opt.source_product
                st = stock_map.get(osp.id)
                a = float(st.available_quantity) if st else 0.0
                take = min(remaining, planned_pool.get(osp.id, 0.0))
                planned_pool[osp.id] = max(planned_pool.get(osp.id, 0.0) - take, 0.0)
                remaining -= take
                avail += a
                alts.append({'name': osp.name, 'sku': osp.sku, 'inStock': round(a, 3),
                             'planned': round(take, 3), 'isMaterial': bool(osp.process.buy)})
            sp = opts[0].source_product  # основной вариант — для имени/дефицита
        else:
            sp = opts[0].source_product if opts else ing.source_product
            stock = stock_map.get(sp.id)
            avail = float(stock.available_quantity) if stock else 0.0
            alts = None
            take = min(max(need, 0.0), planned_pool.get(sp.id, 0.0))
            planned_pool[sp.id] = max(planned_pool.get(sp.id, 0.0) - take, 0.0)
            remaining = max(0.0, need - take)
        requirements.append(([opt.source_product_id for opt in opts] if opts else [sp.id], per, sp.name))
        row = {
            'name': sp.name,
            'sku': sp.sku,
            'need': round(need, 3),
            'inStock': round(avail, 3),
            'shortage': round(max(0.0, remaining), 3),
            'isMaterial': bool(sp.process.buy),
            'sourceJobId': child_by_product.get(sp.id),
            'isOr': alts is not None,
        }
        if alts is not None:
            row['alternatives'] = alts
        inputs.append(row)
    max_units, _ = shared_material_capacity(requirements,
        {pid: stock.available_quantity for pid, stock in stock_map.items()})
    can_start = (max_units is None) or (max_units >= 1)
    return inputs, can_start, max_units


class TasksKanbanView(APIView):
    """GET /api/tasks/ — канбан заданий: колонки-участки; карточки в порядке очереди (job.rank).

    На карточке: входные компоненты (inputs) + canStart (цвет «Начать»).
    """

    def get(self, request):
        from apps.stock.models import StockItem
        from .area_queue import queue_snapshot, revision
        queue_groups, queue_positions = queue_snapshot()
        stock_map = {s.product_id: s for s in StockItem.objects.all()}
        ops = (JobOperation.objects
               .select_related('area', 'job', 'job__product', 'job__product__process', 'executor', 'job__rework', 'job__sales_order')
               .prefetch_related('job__children', 'job__rework__jobs')
               .order_by('area__sort_index', 'job__rank', 'job__id', 'sequence', 'id'))
        if request.query_params.get('status') == JobOperation.Status.DONE:
            ops = ops.filter(status=JobOperation.Status.DONE)
        else:
            ops = ops.exclude(status=JobOperation.Status.DONE)
        visible = visible_area_ids(request.user)
        # «Только свои»: показываем лишь карточки, где пользователь — исполнитель.
        own_only = own_tasks_only(request.user)
        uid = request.user.id if request.user.is_authenticated else None
        if own_only:
            ops = ops.filter(executor_id=uid)

        # Группируем операции по (area, job): sibling-операции сплита → одна карточка.
        groups = {}
        order = []
        for op in ops:
            key = (op.area_id, op.job_id)
            if key not in groups:
                groups[key] = []
                order.append(key)
            groups[key].append(op)

        columns = {}
        for key in order:
            area_id, _job_id = key
            group_ops = groups[key]
            if own_only and not any(o.executor_id == uid for o in group_ops):
                continue
            rep = group_ops[0]
            col = columns.setdefault(area_id, {
                'area': rep.area.name,
                'sortIndex': rep.area.sort_index,
                'cards': [],
            })
            need = sum(float(o.quantity) for o in group_ops)
            inputs, can_start, max_units = _job_inputs(rep.job, stock_map, planned_quantity=need)
            # Потолок для «годные+брак» и сколько можно взять в работу (по остатку + плану).
            max_by_stock = None if max_units is None else float(math.floor(max_units))
            max_start = need if max_units is None else min(need, math.floor(max_units))
            single = len(group_ops) == 1
            statuses = {o.status for o in group_ops}
            if statuses == {JobOperation.Status.DONE}:
                gstatus = JobOperation.Status.DONE
            elif (JobOperation.Status.IN_PROGRESS in statuses
                  or any(o.started_quantity for o in group_ops)):
                gstatus = JobOperation.Status.IN_PROGRESS
            else:
                gstatus = JobOperation.Status.WAITING
            can_change_state = _can_change_operation_state(request.user, area_id)
            col['cards'].append({
                'id': rep.id,
                'jobId': rep.job_id,
                'orderId': rep.job.sales_order_id,
                'orderName': rep.job.sales_order.name if rep.job.sales_order_id else '',
                'productId': rep.job.product_id,
                'processId': rep.job.process_id,
                'areaId': area_id,
                'taskNumber': rep.job.name,
                'rework': defect_record_data(rep.job.rework),
                'productName': rep.job.product.name,
                'customPosition': custom_data(rep.job.product),
                'quantity': need,
                'areaName': rep.area.name,
                'status': gstatus,
                'canStart': can_start and (custom_data(rep.job.product) is None or stage_ready(rep)),
                'canChangeState': can_change_state,
                'maxStart': max(0.0, round(max_start, 3)),
                'maxByStock': max_by_stock,  # null = входов нет, потолка нет
                'startedQuantity': (float(rep.started_quantity) if single
                                    else sum(float(o.started_quantity) for o in group_ops)),
                'executorId': rep.executor_id if single else None,
                'executorName': (rep.executor.get_full_name() or rep.executor.username)
                                if (single and rep.executor) else '',
                'done': sum(float(o.done_quantity) for o in group_ops),
                'defect': sum(float(o.defect_quantity) for o in group_ops),
                'split': not single,
                'rows': [{**_row_dict(o, max_units, can_change_state),
                          'queueNumber': queue_positions.get(o.id)} for o in group_ops],
                'inputs': inputs,
            })
        result = sorted(columns.values(), key=lambda c: c['sortIndex'])
        for c in result:
            c['count'] = len(c['cards'])
            area = c['cards'][0]['areaId']
            c['queueCount'] = len(queue_groups.get(area, []))
            c['queueRevision'] = revision(queue_groups.get(area, []))
            c['canReorder'] = not own_only and _can_change_operation_state(request.user, area)
        return Response({'columns': result})

    def patch(self, request):
        from .area_queue import move_card
        return move_card(request)


class ProductionPlanView(APIView):
    """GET /api/production/ — План: задания (порядок rank) + участки. ordinal = порядковый №."""

    def get(self, request):
        jobs = (ProductionJob.objects
                .select_related('product', 'process', 'sales_order', 'rework')
                .prefetch_related('rework__jobs', 'defect_records__jobs')
                .prefetch_related(Prefetch('operations', queryset=JobOperation.objects.select_related('area', 'executor')))
                .order_by('rank', 'id'))
        rows = ProductionJobSerializer(jobs, many=True).data
        for pos, row in enumerate(rows, start=1):
            row['ordinal'] = pos
        used = set()
        for r in rows:
            used.update(r['areaCells'].keys())
        areas = list(Area.objects.filter(name__in=used).order_by('sort_index', 'id').values_list('name', flat=True))
        output_rows = []
        finished_ops = (JobOperation.objects
                        .filter(status=JobOperation.Status.DONE, finished_at__isnull=False)
                        .select_related('area', 'executor', 'job', 'job__product', 'job__sales_order', 'job__rework')
                        .prefetch_related('job__rework__jobs')
                        .order_by('-finished_at', '-id'))
        if request.query_params.get('includeOutputRows') == 'false':
            finished_ops = finished_ops.none()
        for op in finished_ops:
            user = op.executor
            output_rows.append({
                'id': op.id,
                'operationId': op.id,
                'jobId': op.job_id,
                'areaId': op.area_id,
                'rework': defect_record_data(op.job.rework),
                'taskNumber': op.job.name,
                'batch': op.job.batch,
                'productName': op.job.product.name,
                'productSku': op.job.product.sku,
                'quantity': float(op.done_quantity),
                'defectQuantity': float(op.defect_quantity),
                'finishedAt': op.finished_at.isoformat(),
                'area': op.area.name,
                'finishedBy': (user.get_full_name() or user.username) if user else '',
                'orderName': op.job.sales_order.name if op.job.sales_order_id else '',
            })
        return Response({'jobs': rows, 'areas': areas, 'outputRows': output_rows})


class ProductionReportView(APIView):
    """Сводка выпуска и выработки за даты завершения операций."""

    def get(self, request):
        today = timezone.localdate()
        try:
            date_from = date.fromisoformat(request.query_params.get('dateFrom') or today.isoformat())
            date_to = date.fromisoformat(request.query_params.get('dateTo') or today.isoformat())
        except ValueError:
            return Response({'error': 'Укажите даты в формате ГГГГ-ММ-ДД.'}, status=400)
        if date_to < date_from:
            return Response({'error': 'Дата окончания раньше даты начала.'}, status=400)
        if date_to.toordinal() - date_from.toordinal() > 365:
            return Response({'error': 'Период отчёта не может превышать 366 дней.'}, status=400)
        start_at = timezone.make_aware(datetime.combine(date_from, time.min))
        end_at = timezone.make_aware(datetime.combine(date_to, time.max))

        operations = (JobOperation.objects
                      .filter(status=JobOperation.Status.DONE,
                              finished_at__gte=start_at, finished_at__lte=end_at)
                      .select_related('area', 'executor', 'job', 'job__product', 'job__sales_order', 'job__rework')
                      .order_by('area__sort_index', 'area_id', 'executor__last_name', 'executor__first_name', 'finished_at', 'id'))
        area_id = request.query_params.get('areaId')
        if area_id:
            try:
                area_id = int(area_id)
            except (TypeError, ValueError):
                return Response({'error': 'Некорректный участок.'}, status=400)
            operations = operations.filter(area_id=area_id)

        area_totals = defaultdict(lambda: {'areaId': None, 'area': '', 'good': 0, 'defect': 0, 'people': set(), 'operations': 0})
        employee_days = defaultdict(lambda: {'executorId': None, 'employee': 'Исполнитель не указан', 'areaId': None, 'area': '', 'date': '', 'good': 0, 'defect': 0, 'operations': 0})
        product_totals = defaultdict(lambda: {'areaId': None, 'area': '', 'productName': '', 'productSku': '', 'good': 0, 'defect': 0})
        rows = []
        for op in operations:
            employee = (op.executor.get_full_name() or op.executor.username) if op.executor else 'Исполнитель не указан'
            day = timezone.localtime(op.finished_at).date().isoformat()
            good, defect = float(op.done_quantity), float(op.defect_quantity)
            area = area_totals[op.area_id]
            area.update(areaId=op.area_id, area=op.area.name,
                        good=area['good'] + good, defect=area['defect'] + defect,
                        operations=area['operations'] + 1)
            if op.executor_id:
                area['people'].add(op.executor_id)
            employee_row = employee_days[(op.area_id, op.executor_id, day)]
            employee_row.update(executorId=op.executor_id, employee=employee,
                                areaId=op.area_id, area=op.area.name, date=day,
                                good=employee_row['good'] + good,
                                defect=employee_row['defect'] + defect,
                                operations=employee_row['operations'] + 1)
            product = product_totals[(op.area_id, op.job.product_id)]
            product.update(areaId=op.area_id, area=op.area.name,
                           productName=op.job.product.name, productSku=op.job.product.sku,
                           good=product['good'] + good, defect=product['defect'] + defect)
            rows.append({
                'id': op.id, 'operationId': op.id, 'jobId': op.job_id, 'areaId': op.area_id,
                'taskNumber': op.job.name, 'batch': op.job.batch,
                'productName': op.job.product.name, 'productSku': op.job.product.sku,
                'quantity': good, 'defectQuantity': defect,
                'finishedAt': op.finished_at.isoformat(), 'area': op.area.name,
                'rework': defect_record_data(op.job.rework),
                'finishedBy': employee if op.executor_id else '', 'orderName': op.job.sales_order.name if op.job.sales_order_id else '',
            })

        areas = [{**value, 'people': len(value['people'])}
                 for value in sorted(area_totals.values(), key=lambda item: item['area'].casefold())]
        staff = sorted(employee_days.values(), key=lambda item: (item['area'].casefold(), item['employee'].casefold(), item['date']))
        products = sorted(product_totals.values(), key=lambda item: (item['area'].casefold(), item['productName'].casefold()))
        return Response({
            'dateFrom': date_from.isoformat(), 'dateTo': date_to.isoformat(),
            'totals': {'good': sum(row['good'] for row in areas),
                       'defect': sum(row['defect'] for row in areas),
                       'operations': len(rows)},
            'areaOptions': list(Area.objects.order_by('sort_index', 'id').values('id', 'name')),
            'areas': areas, 'employeesByDay': staff, 'productsByArea': products, 'rows': rows,
        })


class JobMoveView(APIView):
    """POST /api/production/jobs/<job_id>/move/ {direction} — переместить задание выше/ниже в очереди."""

    def post(self, request, job_id):
        direction = request.data.get('direction')
        if direction not in ('up', 'down'):
            return Response({'error': "direction должен быть 'up' или 'down'"}, status=400)
        moved = reorder_job(job_id, direction)
        return Response({'moved': moved})


class ProductReservationsView(APIView):
    """Break down recorded and planned input reservations by order and job."""

    def get(self, request, pk):
        from .planned_reservations import uncovered_job_reservations

        product = Product.objects.filter(pk=pk).first()
        if not product:
            return Response({'error': 'не найдено'}, status=404)

        by_order = {}

        def add(order, job, quantity):
            entry = by_order.setdefault(order.pk if order else None, {
                'orderId': order.pk if order else None,
                'orderName': order.name if order else 'Без заказа',
                'customer': (order.customer_name or '') if order else '',
                'quantity': Decimal(0),
                'jobs': {},
            })
            entry['quantity'] += quantity
            if job:
                item = entry['jobs'].setdefault(job.pk, {'jobName': job.name, 'quantity': Decimal(0)})
                item['quantity'] += quantity

        recorded = (Reservation.objects.filter(product_id=pk, kind=Reservation.Kind.CONSUME)
                    .select_related('sales_order', 'job').order_by('id'))
        for row in recorded:
            add(row.sales_order, row.job, row.quantity)
        for job, product_id, quantity in uncovered_job_reservations():
            if product_id == pk:
                add(job.sales_order, job, quantity)

        orders = sorted(by_order.values(), key=lambda entry: entry['orderName'])
        for entry in orders:
            entry['quantity'] = round(float(entry['quantity']), 4)
            entry['jobs'] = [
                {**job, 'quantity': round(float(job['quantity']), 4)}
                for job in entry['jobs'].values()
            ]
        return Response({
            'product': {'id': product.id, 'name': product.name, 'sku': product.sku},
            'committed': round(sum(entry['quantity'] for entry in orders), 4),
            'orders': orders,
        })


class ProductJobsSyncPreviewView(APIView):
    """GET /api/products/<id>/jobs-sync-preview/ — задания, которые затронет новая техкарта."""

    def get(self, request, pk):
        product = Product.objects.filter(pk=pk).select_related('process').first()
        if not product:
            return Response({'error': 'изделие не найдено'}, status=404)
        return Response(job_techcard_sync_preview(product))


class ProductJobsSyncView(APIView):
    """POST /api/products/<id>/jobs-sync/ — применить техкарту только к безопасным заданиям."""

    def post(self, request, pk):
        product = Product.objects.filter(pk=pk).select_related('process').first()
        if not product:
            return Response({'error': 'изделие не найдено'}, status=404)
        return Response(apply_techcard_to_waiting_jobs(product))


def _can_assign_executor(user):
    """Назначать исполнителя вручную может только админ (superuser или доступ к «Настройкам»).

    Остальные (мастер/цех) — исполнителем всегда становится тот, кто авторизован.
    """
    if not user or not user.is_authenticated:
        return False
    if user.is_superuser:
        return True
    role = getattr(getattr(user, 'profile', None), 'role', None)
    if role and 'старш' in (role.name or '').lower():
        return True
    perms = (role.permissions or {}) if role else {}
    return bool((perms.get('company') or {}).get('update'))


def _locked_task_operation(pk):
    """Read current state after acquiring the same job -> operation locks as batches."""
    job_id = get_object_or_404(JobOperation.objects.only('job_id'), pk=pk).job_id
    get_object_or_404(ProductionJob.objects.select_for_update(), pk=job_id)
    # No joined FOR UPDATE: nullable executor/operation relations cannot be locked
    # through an outer join on PostgreSQL.
    return get_object_or_404(JobOperation.objects.select_for_update(), pk=pk)


def _lock_task_stocks(job):
    from .rework import lock_production_stocks
    if custom_data(job.product) is not None:
        return {}
    return lock_production_stocks(job)


class TaskStartView(APIView):
    """PATCH /api/tasks/<pk>/start/ {quantity, executorId} — взять операцию в работу.

    PATCH (а не POST) — потому что это изменение операции: право «update» на
    production_tasks есть у роли Мастер (исполнителя), права «create» у неё нет.
    Исполнитель: админ может выбрать; не-админу принудительно ставится он сам.
    Склад не трогаем: материал списывается на «Завершить» (по годным+браку).
    """

    @transaction.atomic
    def patch(self, request, pk):
        from apps.stock.models import StockItem
        op = _locked_task_operation(pk)
        if not _can_change_operation_state(request.user, op.area_id):
            return Response({'error': 'Нет доступа к этому участку.'}, status=403)
        try:
            if _can_assign_executor(request.user):
                selected_executor = validate_executor(request.data.get('executorId'), op.area_id)
            else:
                selected_executor = validate_executor(request.user.pk, op.area_id)
                if request.data.get('executorId') not in (None, request.user.pk, str(request.user.pk)):
                    return Response({'error': 'Нельзя выполнять за другого сотрудника.'}, status=403)
        except ValueError as exc:
            return Response({'error': str(exc)}, status=400)
        if op.status == JobOperation.Status.DONE:
            return Response({'error': 'Операция уже завершена.'}, status=400)
        # Не-админ не может перехватить долю сплита, назначенную другому исполнителю.
        if (op.executor_id and request.user.is_authenticated
                and op.executor_id != request.user.id and not _can_assign_executor(request.user)):
            return Response({'error': 'Эта доля назначена другому исполнителю.'}, status=403)
        try:
            qty = float(request.data.get('quantity'))
        except (TypeError, ValueError):
            return Response({'error': 'некорректное количество'}, status=400)
        if qty <= 0:
            return Response({'error': 'Количество должно быть больше нуля.'}, status=400)
        if qty > float(op.quantity):
            return Response({'error': 'Нельзя взять больше, чем в задании.'}, status=400)
        if custom_data(op.job.product) is not None:
            try:
                validate_stage(op)
            except ValueError as exc:
                return Response({'error': str(exc)}, status=400)
            if qty != 1:
                return Response({'error': 'Заказная позиция берётся в работу целиком.'}, status=400)
        # Защита: нельзя взять больше, чем позволяет остаток входов (то же, что показывает окно).
        stock_map = {s.product_id: s for s in StockItem.objects.all()}
        _inputs, _can, max_units = _job_inputs(op.job, stock_map, planned_quantity=op.quantity)
        max_start = float(op.quantity) if max_units is None else min(float(op.quantity), math.floor(max_units))
        if max_start <= 0:
            return Response({'error': 'Нет входных компонентов на складе — начать нельзя.'}, status=400)
        if qty > max_start:
            return Response(
                {'error': f'По остатку можно взять не больше {max_start:g} шт.'}, status=400)
        op.executor_id = selected_executor
        op.status = JobOperation.Status.IN_PROGRESS
        op.started_quantity = qty
        op.started_at = timezone.now()
        op.save(update_fields=['status', 'started_quantity', 'started_at', 'executor'])
        return Response({'id': op.id, 'status': op.status, 'startedQuantity': float(op.started_quantity)})


class TaskFinishView(APIView):
    """PATCH /api/tasks/<pk>/finish/ {good, defect} — завершить операцию.

    PATCH — изменение операции (право «update»), чтобы мастер цеха мог завершать.
    Списывает входные компоненты по (годные+брак), выпускает годные на склад.
    """

    @transaction.atomic
    def patch(self, request, pk):
        op = _locked_task_operation(pk)
        if not _can_change_operation_state(request.user, op.area_id):
            return Response({'error': 'Нет доступа к этому участку.'}, status=403)
        try:
            if _can_assign_executor(request.user):
                selected_executor = validate_executor(request.data.get('executorId'), op.area_id)
            else:
                selected_executor = validate_executor(request.user.pk, op.area_id)
                if request.data.get('executorId') not in (None, request.user.pk, str(request.user.pk)):
                    return Response({'error': 'Нельзя выполнять за другого сотрудника.'}, status=403)
        except ValueError as exc:
            return Response({'error': str(exc)}, status=400)
        if op.status == JobOperation.Status.DONE:
            return Response({'error': 'Операция уже завершена.'}, status=400)
        try:
            good = float(request.data.get('good') or 0)
            defect = float(request.data.get('defect') or 0)
        except (TypeError, ValueError):
            return Response({'error': 'некорректное количество'}, status=400)
        if good < 0 or defect < 0:
            return Response({'error': 'Количество не может быть отрицательным.'}, status=400)
        total = good + defect
        if total <= 0:
            return Response({'error': 'Укажите годные и/или брак.'}, status=400)
        # Потолок — не план, а материал: брак может быть сверх плана (напилили 82,
        # годных 80, 2 в брак), но только если материала хватало на 82.
        stock_map = _lock_task_stocks(op.job)
        _inputs, _can, max_units = _job_inputs(op.job, stock_map, planned_quantity=op.quantity)
        if max_units is not None:
            max_total = math.floor(max_units)
            if total > max_total:
                return Response(
                    {'error': f'Материала хватает только на {max_total:g} шт (годные + брак).'},
                    status=400)
        executor_changed = op.executor_id != selected_executor
        op.executor_id = selected_executor
        with transaction.atomic():
            if executor_changed:
                op.save(update_fields=['executor'])
            try:
                res = finish_operation(op, good, defect, user=request.user if request.user.is_authenticated else None)
            except ValueError as exc:
                transaction.set_rollback(True)
                return Response({'error': str(exc)}, status=400)
        return Response({'id': op.id, 'status': op.status, **res})


class TaskCancelFinishView(APIView):
    def patch(self, request, pk):
        if not can_cancel_completion(request.user):
            return Response({'error': 'Отмена выполнения доступна только администратору.'}, status=403)
        get_object_or_404(JobOperation, pk=pk)
        try:
            audit = cancel_completion(pk, request.data.get('reason'), request.user)
        except ValueError as exc:
            return Response({'error': str(exc)}, status=400)
        return Response({'id': pk, 'cancellationId': audit.pk})


class TaskCancelStartView(APIView):
    """PATCH /api/tasks/<pk>/cancel-start/ — отменить взятие в работу, если факт ещё не внесён."""

    @transaction.atomic
    def patch(self, request, pk):
        op = _locked_task_operation(pk)
        if not _can_change_operation_state(request.user, op.area_id):
            return Response({'error': 'Нет доступа к этому участку.'}, status=403)
        if op.status == JobOperation.Status.DONE:
            return Response({'error': 'Операция уже завершена.'}, status=400)
        if op.status != JobOperation.Status.IN_PROGRESS and not op.started_quantity:
            return Response({'error': 'Операция не взята в работу.'}, status=400)
        if op.done_quantity or op.defect_quantity:
            return Response({'error': 'Уже внесён выпуск или брак — отменить взятие нельзя.'}, status=400)
        if (op.executor_id and request.user.is_authenticated
                and op.executor_id != request.user.id and not _can_assign_executor(request.user)):
            return Response({'error': 'Эта строка назначена другому исполнителю.'}, status=403)
        op.status = JobOperation.Status.WAITING
        op.started_quantity = 0
        op.started_at = None
        op.save(update_fields=['status', 'started_quantity', 'started_at'])
        return Response({
            'id': op.id,
            'status': op.status,
            'startedQuantity': float(op.started_quantity),
        })


class TaskGroupView(APIView):
    """GET /api/tasks/group/?job=<id>&area=<id> — деталь карточки (группа операций).

    Возвращает шапку, агрегаты «Выполнение», строки «Работы» (по исполнителям) и
    материалы. rows включают и завершённые операции (история «кто делал»).
    """

    def get(self, request):
        from apps.catalog.views import attachment_dict
        from apps.stock.models import StockItem
        job_id = request.query_params.get('job')
        area_id = request.query_params.get('area')
        job = get_object_or_404(
            ProductionJob.objects.select_related('product__process'), pk=job_id)
        visible = visible_area_ids(request.user)
        try:
            area_id = int(area_id)
        except (TypeError, ValueError):
            return Response({'error': 'некорректный участок'}, status=400)
        can_change_state = _can_change_operation_state(request.user, area_id)
        ops = list(JobOperation.objects
                   .filter(job_id=job_id, area_id=area_id)
                   .select_related('area', 'executor')
                   .order_by('sequence', 'id'))
        if not ops:
            return Response({'error': 'Группа не найдена.'}, status=404)
        # «Только свои»: карточку можно открыть, лишь если пользователь в ней исполнитель.
        if own_tasks_only(request.user) and not any(o.executor_id == request.user.id for o in ops):
            return Response({'error': 'Эта карточка не назначена на вас.'}, status=403)

        stock_map = {s.product_id: s for s in StockItem.objects.all()}
        need = sum(float(o.quantity) for o in ops)
        done = sum(float(o.done_quantity) for o in ops)
        defect = sum(float(o.defect_quantity) for o in ops)
        remaining = max(0.0, round(need - done - defect, 3))
        inputs, _can_start, max_units = _job_inputs(job, stock_map, planned_quantity=remaining)
        product_attachments = list(job.product.attachments.all())
        product_image = next((a for a in product_attachments if a.kind == 'image'), None)
        # Править распределение можно, пока в группе есть хоть одна не завершённая операция.
        has_active = any(o.status != JobOperation.Status.DONE for o in ops)
        return Response({
            'jobId': job.id,
            'areaId': area_id,
            'taskNumber': job.name,
            'description': job.description,
            'rework': defect_record_data(job.rework),
            'defectRecords': [defect_record_data(record) for record in
                              job.defect_records.filter(source__areaId=area_id).prefetch_related('jobs')],
            'areaName': ops[0].area.name,
            'productName': job.product.name,
            'customPosition': custom_data(job.product),
            'attachments': {
                'image': attachment_dict(product_image, request) if product_image else None,
                'files': [attachment_dict(a, request) for a in product_attachments if a.kind == 'file'],
            },
            'maxByStock': None if max_units is None else float(math.floor(max_units)),
            'totals': {
                'need': need,
                'started': sum(float(o.started_quantity) for o in ops),
                'done': done,
                'defect': defect,
                'remaining': remaining,
            },
            'rows': [{**_row_dict(o, max_units, can_change_state),
                      **({'canStart': o.status == 'waiting' and stage_ready(o)} if custom_data(job.product) is not None else {}),
                      'legacyCompletion': o.status == JobOperation.Status.DONE and o.completion_snapshot is None} for o in ops],
            'inputs': inputs,
            'canEdit': _can_assign_executor(request.user) and has_active,
            'canAssign': _can_assign_executor(request.user),
            'canCancelCompletion': can_cancel_completion(request.user),
            'cancellations': [{
                'id': audit.pk, 'reason': audit.reason, 'createdAt': audit.created_at.isoformat(),
                'createdBy': (audit.created_by.get_full_name() or audit.created_by.username) if audit.created_by else '',
                'executor': audit.snapshot.get('executor', ''),
                'good': float(audit.snapshot['good']), 'defect': float(audit.snapshot['defect']),
                'finishedAt': audit.snapshot.get('finished_at'),
            } for audit in job.cancellations.filter(area_id=area_id).select_related('created_by')],
        })


class TaskGroupEditView(APIView):
    """POST /api/tasks/group/edit/ {job, area, rows:[{opId?, executorId, quantity}]}.

    Правит распределение НЕ завершённой части группы по исполнителям. Только админ.
    Начатые строки нельзя убрать/уменьшить ниже взятого; ожидающие — свободно.
    """

    def post(self, request):
        if not _can_assign_executor(request.user):
            return Response({'error': 'Недостаточно прав для редактирования.'}, status=403)
        job_id = request.data.get('job')
        area_id = request.data.get('area')
        try:
            area_id_int = int(area_id)
        except (TypeError, ValueError):
            return Response({'error': 'некорректный участок'}, status=400)
        try:
            with transaction.atomic():
                ops = edit_group_distribution(
                    job_id, area_id_int, request.data.get('rows') or [],
                    user=request.user if request.user.is_authenticated else None)
        except ValueError as exc:
            return Response({'error': str(exc)}, status=400)
        return Response({'ops': [o.id for o in ops]})


class JobsReorderView(APIView):
    """POST /api/production/jobs/reorder/ {ids} — задать порядок очереди перетаскиванием."""

    def post(self, request):
        ids = request.data.get('ids') or []
        reorder_jobs(ids)
        return Response({'ok': True, 'count': len(ids)})


class JobsBulkDeleteView(APIView):
    """Preview deletion effects, then compensate only the confirmed state."""
    from rest_framework.permissions import IsAuthenticated
    permission_classes = [IsAuthenticated]

    def post(self, request):
        from .deletion import preview_job_deletion
        try:
            ids = request.data.get('ids')
            if request.data.get('preview') is True:
                return Response(preview_job_deletion(ids, request.user))
            return Response(delete_jobs(ids, request.user, request.data.get('confirmation')))
        except PermissionError as exc:
            return Response({'error': str(exc)}, status=403)
        except ValueError as exc:
            return Response({'error': str(exc)}, status=400)

