from django.db import transaction
from django.db.models import Exists, OuterRef
from django.shortcuts import get_object_or_404
from rest_framework import generics, status
from rest_framework.views import APIView
from rest_framework.response import Response

from apps.catalog.models import Product
from apps.production.models import ProductionJob
from .models import SalesOrder, OrderLine
from .serializers import (
    SalesOrderRowSerializer, SalesOrderWriteSerializer,
    OrderLineSerializer, recalc_order_total,
)
from .services import explode_order
from apps.production.services import (
    create_jobs_for_order, wipe_order_jobs, order_has_started_jobs, order_job_status,
    create_selected_jobs_for_order, set_order_material_reservation,
)


def _selected_root(data):
    product_id = data.get('product_id')
    if not product_id:
        return None, None, None
    product = Product.objects.select_related('process').filter(pk=product_id).first()
    if not product:
        return None, None, Response({'error': 'ПФ не найден'}, status=404)
    if not (product.process.make and not product.process.buy):
        return None, None, Response({'error': 'Можно выбрать только производимый ПФ.'}, status=400)
    try:
        quantity = float(data.get('quantity') or 1)
    except (TypeError, ValueError):
        return None, None, Response({'error': 'некорректное количество'}, status=400)
    if quantity <= 0:
        return None, None, Response({'error': 'Количество должно быть больше нуля.'}, status=400)
    return product, quantity, None


def can_edit_order(user, order):
    if not user or not user.is_authenticated:
        return False
    if user.is_superuser:
        return True
    role = getattr(getattr(user, 'profile', None), 'role', None)
    if role and role.permissions and role.permissions.get('sales', {}).get('update', False):
        return True
    return order.created_by_id == user.id


def rename_order_jobs(order, old_name, new_name):
    if not new_name:
        return
    for job in ProductionJob.objects.filter(sales_order=order):
        fields = []
        source_names = []
        if old_name and old_name != new_name:
            source_names.append(old_name)
        if job.batch and job.batch != new_name and job.batch not in source_names:
            source_names.append(job.batch)
        for source_name in source_names:
            prefix = f'{source_name}/'
            if job.name == source_name:
                job.name = new_name
                fields.append('name')
                break
            if job.name.startswith(prefix):
                job.name = f'{new_name}/{job.name[len(prefix):]}'
                fields.append('name')
                break
        if job.batch == old_name:
            job.batch = new_name
            fields.append('batch')
        elif job.batch and job.batch != new_name and 'name' in fields:
            job.batch = new_name
            fields.append('batch')
        if fields:
            job.save(update_fields=fields)


class SalesOrderListView(generics.ListCreateAPIView):
    """GET /api/sales/ — заказы; POST — создать заказ."""
    pagination_class = None

    def get_serializer_class(self):
        return SalesOrderWriteSerializer if self.request.method == 'POST' else SalesOrderRowSerializer

    def get_queryset(self):
        # Все заказы (вкл. архив) с признаками для вкладок:
        # has_jobs — есть задания; has_open_job — есть незавершённое задание.
        jobs = ProductionJob.objects.filter(sales_order_id=OuterRef('pk'))
        return (SalesOrder.objects
                .annotate(
                    has_jobs=Exists(jobs),
                    has_open_job=Exists(jobs.exclude(status=ProductionJob.Status.DONE)))
                .prefetch_related('lines__product__process', 'jobs')
                .order_by('rank', 'id'))

    def get_serializer_context(self):
        ctx = super().get_serializer_context()
        if self.request.method != 'POST':
            from apps.catalog.models import Ingredient
            from apps.stock.models import StockItem
            # Процессы, у которых есть спецификация (BOM), и остатки для стадии «Склад».
            ctx['bom_process_ids'] = set(Ingredient.objects.values_list('process_id', flat=True))
            ctx['stock_map'] = {s.product_id: float(s.available_quantity) for s in StockItem.objects.all()}
        return ctx


class SalesOrderBulkView(APIView):
    """POST /api/sales/bulk/ {rows:[{name,customer_name,manager_name}]} — массовое добавление заказов."""

    def post(self, request):
        rows = request.data.get('rows') or []
        created = 0
        for r in rows:
            name = (r.get('name') or '').strip()
            if not name:
                continue
            SalesOrder.objects.create(
                name=name,
                customer_name=(r.get('customer_name') or '').strip(),
                manager_name=(r.get('manager_name') or '').strip(),
                created_by=request.user if request.user.is_authenticated else None,
                display_status='Не укомплектовано',
            )
            created += 1
        return Response({'created': created}, status=201)


class SalesOrderDetailView(APIView):
    """PATCH /api/sales/<id>/ — правка шапки заказа автором или админом."""

    def patch(self, request, pk):
        order = get_object_or_404(SalesOrder, pk=pk)
        if not can_edit_order(request.user, order):
            return Response({'detail': 'Недостаточно прав для редактирования заказа.'},
                            status=status.HTTP_403_FORBIDDEN)

        data = request.data
        old_name = order.name
        name = data.get('name', order.name)
        if name is not None:
            name = str(name).strip()
            if not name:
                return Response({'name': ['Название заказа обязательно.']},
                                status=status.HTTP_400_BAD_REQUEST)
            order.name = name
        if 'customer_name' in data:
            order.customer_name = (data.get('customer_name') or '').strip()
        if 'manager_name' in data:
            order.manager_name = (data.get('manager_name') or '').strip()
        if 'finish_by_date' in data:
            raw_date = data.get('finish_by_date')
            if raw_date in ('', None):
                order.finish_by_date = None
            else:
                from datetime import date as date_cls
                try:
                    order.finish_by_date = date_cls.fromisoformat(str(raw_date))
                except ValueError:
                    return Response({'finish_by_date': ['Неверная дата.']},
                                    status=status.HTTP_400_BAD_REQUEST)
        with transaction.atomic():
            order.save(update_fields=['name', 'customer_name', 'manager_name', 'finish_by_date'])
            rename_order_jobs(order, old_name, order.name)
        return Response(SalesOrderRowSerializer(order, context={'request': request}).data)


class OrderLinesView(generics.ListCreateAPIView):
    """GET /api/sales/<order_id>/lines/ — позиции заказа; POST — добавить позицию."""
    serializer_class = OrderLineSerializer
    pagination_class = None

    def get_queryset(self):
        return (OrderLine.objects
                .filter(order_id=self.kwargs['order_id'])
                .select_related('product'))

    def perform_create(self, serializer):
        order = get_object_or_404(SalesOrder, pk=self.kwargs['order_id'])
        from rest_framework.exceptions import ValidationError
        from apps.production.custom_positions import order_is_custom, custom_data
        if order_is_custom(order) or custom_data(serializer.validated_data.get('product')) is not None:
            raise ValidationError({'error': 'Заказная позиция — отдельная заявка, один заказ целиком.'})
        serializer.save(order=order)
        recalc_order_total(order)


class OrderLineDetailView(generics.RetrieveUpdateDestroyAPIView):
    """PATCH/DELETE /api/sales/lines/<id>/ — правка количества/цены или удаление позиции."""
    serializer_class = OrderLineSerializer
    queryset = OrderLine.objects.select_related('product')

    def perform_update(self, serializer):
        from rest_framework.exceptions import ValidationError
        from apps.production.custom_positions import custom_data
        if custom_data(serializer.instance.product) is not None or custom_data(serializer.validated_data.get('product', serializer.instance.product)) is not None:
            raise ValidationError({'error': 'Состав заказной позиции фиксирован: один заказ целиком.'})
        line = serializer.save()
        recalc_order_total(line.order)

    def perform_destroy(self, instance):
        from rest_framework.exceptions import ValidationError
        from apps.production.custom_positions import custom_data
        if custom_data(instance.product) is not None:
            raise ValidationError({'error': 'Удалите заявку целиком через список заказов.'})
        order = instance.order
        instance.delete()
        recalc_order_total(order)


class SalesOrderBulkDeleteView(APIView):
    """POST /api/sales/bulk-delete/ {ids} — удалить заказы.

    Снимаем задания+резервы (recalc склада через сигнал), затем удаляем заказ
    (позиции удаляются каскадом).
    """

    def post(self, request):
        ids = request.data.get('ids') or []
        if not ids:
            return Response({'error': 'нет выбранных заказов'}, status=400)
        deleted = 0
        for order in SalesOrder.objects.filter(pk__in=ids):
            wipe_order_jobs(order)
            order.delete()
            deleted += 1
        return Response({'deleted': deleted})


class OrderExplodeView(APIView):
    """POST /api/sales/<order_id>/explode/ {mode} — превью разузлования (без записи).

    mode: 'with_stock' (нетируем всё) | 'from_zero' (ПФ производим все, материал нетируем).
    Возвращает {tree, materials_summary}.
    """

    def post(self, request, order_id):
        order = get_object_or_404(SalesOrder, pk=order_id)
        mode = request.data.get('mode', 'with_stock')
        if mode not in ('with_stock', 'from_zero'):
            return Response({'error': "mode должен быть 'with_stock' или 'from_zero'"}, status=400)
        product, quantity, error = _selected_root(request.data)
        if error:
            return error
        calculation = explode_order(order, mode, root_product=product, root_quantity=quantity,
                                     include_own_reservations=product is not None,
                                     include_order_holds=bool(request.data.get('include_order_holds')))
        if request.data.get('export') == 'xlsx':
            from .materials_export import materials_excel
            return materials_excel(order, calculation, mode, product, quantity)
        return Response(calculation)


class OrderJobStatusView(APIView):
    """GET /api/sales/<order_id>/job-status/ — статус заданий по product_id.

    Для режима «Ход выполнения»: подсветка узлов дерева, чьи задания готовы.
    Возвращает {product_id: 'done'|'in_progress'|'overdue'|'waiting'}.
    """

    def get(self, request, order_id):
        order = get_object_or_404(SalesOrder, pk=order_id)
        return Response(order_job_status(order))


class OrderCreateJobsView(APIView):
    """POST /api/sales/<order_id>/create-jobs/ {mode} — материализовать задания заказа.

    Повторный запуск: если по заказу есть начатые операции → 400; иначе прежние
    задания+резервы сносятся и создаются заново.
    """

    def post(self, request, order_id):
        order = get_object_or_404(SalesOrder, pk=order_id)
        mode = request.data.get('mode', 'with_stock')
        if mode not in ('with_stock', 'from_zero'):
            return Response({'error': "mode должен быть 'with_stock' или 'from_zero'"}, status=400)
        product, quantity, error = _selected_root(request.data)
        if error:
            return error
        from apps.production.custom_positions import order_is_custom, create_custom_jobs
        if order_is_custom(order):
            paths = request.data.get('selected_paths')
            if product is not None or (paths is not None and paths != ['r0']):
                return Response({'error': 'Заказная позиция запускается целиком, без выбора других ПФ.'}, status=400)
            try:
                stats = create_custom_jobs(order)
            except ValueError as exc:
                return Response({'error': str(exc)}, status=400)
            return Response(stats, status=201)
        selected_paths = request.data.get('selected_paths')
        if selected_paths is not None:
            if product is not None:
                return Response({'error': 'Выборочные задания создаются только для полного дерева заказа.'}, status=400)
            if not isinstance(selected_paths, list) or not selected_paths:
                return Response({'error': 'Выберите хотя бы один полуфабрикат.'}, status=400)
            try:
                if any(not isinstance(path, str) for path in selected_paths):
                    raise ValueError
                stats = create_selected_jobs_for_order(order, mode, selected_paths)
            except ValueError as exc:
                return Response({'error': str(exc)}, status=400)
            except TypeError:
                return Response({'error': 'Некорректный список полуфабрикатов.'}, status=400)
            return Response(stats, status=201)
        if product is None and order_has_started_jobs(order):
            return Response({'error': 'По заказу есть начатые задания — пересоздание запрещено.'}, status=400)
        with transaction.atomic():
            if product is None:
                wipe_order_jobs(order)
            stats = create_jobs_for_order(order, mode, root_product=product, root_quantity=quantity)
        return Response(stats, status=201)


class OrderMaterialReservationView(APIView):
    """GET/POST /api/sales/<order_id>/material-reservation/ — резерв заказа без заданий."""

    def get(self, request, order_id):
        order = get_object_or_404(SalesOrder, pk=order_id)
        rows = order.reservations.filter(
            job__isnull=True, kind='consume').select_related('product')
        return Response({'enabled': rows.exists(), 'reservations': [
            {'product_id': r.product_id, 'name': r.product.name,
             'quantity': float(r.quantity)} for r in rows]})

    def post(self, request, order_id):
        order = get_object_or_404(SalesOrder, pk=order_id)
        mode = request.data.get('mode', 'with_stock')
        if mode not in ('with_stock', 'from_zero'):
            return Response({'error': "mode должен быть 'with_stock' или 'from_zero'"}, status=400)
        enabled = bool(request.data.get('enabled'))
        stats = set_order_material_reservation(order, mode, enabled)
        return Response(stats)
