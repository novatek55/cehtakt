from datetime import date
from decimal import Decimal

from rest_framework import serializers

from apps.catalog.models import Product
from .models import SalesOrder, OrderLine

_MONTHS = ['', 'янв.', 'февр.', 'марта', 'апр.', 'мая', 'июн.',
           'июл.', 'авг.', 'сент.', 'окт.', 'нояб.', 'дек.']


class SalesOrderRowSerializer(serializers.ModelSerializer):
    """Строка таблицы Заказов (форма под фронтовый SalesTable)."""
    number = serializers.CharField(source='name')
    client = serializers.CharField(source='customer_name')
    manager = serializers.CharField(source='manager_name')
    sum = serializers.SerializerMethodField()
    shipDate = serializers.SerializerMethodField()
    finishByDate = serializers.SerializerMethodField()
    createdById = serializers.IntegerField(source='created_by_id', read_only=True)
    canEdit = serializers.SerializerMethodField()
    overdue = serializers.SerializerMethodField()
    komplekt = serializers.SerializerMethodField()
    proizvod = serializers.SerializerMethodField()
    sklad = serializers.SerializerMethodField()
    otgruzka = serializers.SerializerMethodField()
    canProduce = serializers.SerializerMethodField()
    stage = serializers.SerializerMethodField()
    jobsProgress = serializers.SerializerMethodField()
    customPosition = serializers.SerializerMethodField()

    class Meta:
        model = SalesOrder
        fields = ['id', 'number', 'client', 'manager', 'sum', 'shipDate', 'finishByDate',
                  'createdById', 'canEdit', 'overdue', 'komplekt', 'proizvod', 'sklad',
                  'otgruzka', 'canProduce', 'stage', 'jobsProgress', 'customPosition']

    # --- вспомогательные (из аннотаций has_jobs/has_open_job и prefetch lines) ---
    def _partial_production(self, obj):
        if not getattr(obj, 'has_jobs', False):
            return False
        final_products = {line.product_id for line in obj.lines.all()
                          if line.product.process.make and not line.product.process.buy}
        root_products = {job.product_id for job in obj.jobs.all() if job.parent_id is None}
        return bool(final_products - root_products)

    def _all_done(self, obj):
        return (getattr(obj, 'has_jobs', False) and not getattr(obj, 'has_open_job', False)
                and not self._partial_production(obj))

    def _producible(self, obj):
        return any(l.product.process.make and not l.product.process.buy for l in obj.lines.all())

    def _has_bom(self, obj):
        bom = self.context.get('bom_process_ids', set())
        return any(l.product.process_id in bom for l in obj.lines.all())

    def get_customPosition(self, obj):
        from apps.production.custom_positions import custom_data
        from apps.catalog.views import attachment_dict
        for line in obj.lines.all():
            data = custom_data(line.product)
            if data is not None:
                files = list(line.product.attachments.all())
                return {**data, "title": line.product.name, "attachments": [attachment_dict(a, self.context.get("request")) for a in files]}
        return None

    def _sklad_status(self, obj):
        if self.get_customPosition(obj) is not None:
            return "—"
        sm = self.context.get('stock_map', {})
        lines = list(obj.lines.all())
        if not lines:
            return ''
        covered_all, any_stock = True, False
        for l in lines:
            av = sm.get(l.product_id, 0.0)
            if av > 0:
                any_stock = True
            if av < float(l.quantity):
                covered_all = False
        if covered_all:
            return 'Укомплектовано'
        return 'В наличии' if any_stock else 'Отсутствуют'

    def get_jobsProgress(self, obj):
        """Count created job cards; flag production nodes still to materialize."""
        from apps.production.models import ProductionJob
        from apps.production.services import _current_tree_jobs
        from .services import explode_order

        jobs = list(obj.jobs.all())
        done = sum(
            job.status == ProductionJob.Status.DONE
            and not (Decimal(job.made_quantity or 0) > 0
                     and Decimal(job.made_quantity) + Decimal('0.0001')
                     < Decimal(job.quantity or 0))
            for job in jobs)
        missing = 0
        if not obj.archived and self._producible(obj):
            if 'progress_catalog_cache' not in self.context:
                self.context['progress_catalog_cache'] = {
                    product.id: product for product in Product.objects.select_related('process')
                    .prefetch_related('process__ingredients__source_product__process',
                                      'process__ingredients__options__source_product__process')}
            if 'progress_planned_reservations' not in self.context:
                from apps.production.planned_reservations import uncovered_job_reservations
                self.context['progress_planned_reservations'] = uncovered_job_reservations()
            tree = explode_order(obj, 'with_stock', include_order_holds=True,
                                 include_job_ids=[job.id for job in jobs],
                                 catalog_cache=self.context['progress_catalog_cache'],
                                 planned_reservations=self.context['progress_planned_reservations'])['tree']
            by_path = _current_tree_jobs(obj, jobs, tree)

            def count_missing(node, path):
                if node['is_material']:
                    return 0
                # Stock-covered branches need no cards, just as in create-jobs.
                absent = path not in by_path and node['plan_deficit'] > 1e-9
                return int(absent) + sum(
                    count_missing(child, f'{path}.{index}')
                    for index, child in enumerate(node['children']))

            missing = sum(count_missing(root, f'r{index}')
                          for index, root in enumerate(tree))
        return {'total': len(jobs), 'done': done, 'missing': missing,
                'incomplete': missing > 0}

    def get_stage(self, obj):
        """Вкладка жизненного цикла: request | in_work | done."""
        if obj.archived:
            return 'done'
        if not getattr(obj, 'has_jobs', False):
            return 'request'
        return 'in_work' if (getattr(obj, 'has_open_job', False) or self._partial_production(obj)) else 'done'

    def get_proizvod(self, obj):
        """Производство: — | Произвести (кнопка) | В работе | Готово."""
        if not self._producible(obj):
            return '—'
        if not getattr(obj, 'has_jobs', False):
            return 'Произвести'  # фронт покажет кнопку (canProduce=true)
        if self._partial_production(obj):
            return 'В работе · частичная заявка'
        return 'Готово' if self._all_done(obj) else 'В работе'

    def get_canProduce(self, obj):
        """Заказ можно запустить в производство (нет заданий, продукт производимый)."""
        return self._producible(obj) and not getattr(obj, 'has_jobs', False)

    def get_komplekt(self, obj):
        """Комплектующие: Нет спецификации | Использованы | В наличии."""
        if self.get_customPosition(obj) is not None:
            return 'Без списания'
        if not self._has_bom(obj):
            return 'Нет спецификации'
        return 'Использованы' if self._all_done(obj) else 'В наличии'

    def get_sklad(self, obj):
        return self._sklad_status(obj)

    def get_otgruzka(self, obj):
        if self.get_customPosition(obj) is not None:
            return 'Готово' if self._all_done(obj) else 'Не выполнено'
        return 'Готов к отгрузке' if self._sklad_status(obj) == 'Укомплектовано' else 'Не укомплектовано'

    def get_sum(self, obj):
        return float(obj.total_amount)

    def get_shipDate(self, obj):
        d = obj.finish_by_date
        return f'{d.day} {_MONTHS[d.month]}' if d else ''

    def get_finishByDate(self, obj):
        return obj.finish_by_date.isoformat() if obj.finish_by_date else ''

    def get_canEdit(self, obj):
        request = self.context.get('request')
        user = request.user if request else None
        if not user or not user.is_authenticated:
            return False
        if user.is_superuser:
            return True
        role = getattr(getattr(user, 'profile', None), 'role', None)
        if role and role.permissions and role.permissions.get('sales', {}).get('update', False):
            return True
        return obj.created_by_id == user.id

    def get_overdue(self, obj):
        return bool(obj.finish_by_date and obj.finish_by_date < date.today())


class SalesOrderWriteSerializer(serializers.Serializer):
    """Создание заказа (заголовок)."""
    name = serializers.CharField()
    customer_name = serializers.CharField(required=False, allow_blank=True, default='')
    manager_name = serializers.CharField(required=False, allow_blank=True, default='')
    finish_by_date = serializers.DateField(required=False, allow_null=True, default=None)

    def create(self, data):
        request = self.context.get('request')
        user = request.user if request and request.user.is_authenticated else None
        return SalesOrder.objects.create(
            name=data['name'], customer_name=data.get('customer_name', ''),
            manager_name=data.get('manager_name', ''), finish_by_date=data.get('finish_by_date'),
            created_by=user,
            display_status='Не укомплектовано')

    def to_representation(self, instance):
        return {'id': instance.id, 'name': instance.name}


class OrderLineSerializer(serializers.ModelSerializer):
    """Позиция заказа: чтение — с именем/артикулом товара; запись — по product_id."""
    product_name = serializers.CharField(source='product.name', read_only=True)
    sku = serializers.CharField(source='product.sku', read_only=True)
    product_id = serializers.PrimaryKeyRelatedField(
        source='product', queryset=Product.objects.all(), write_only=True)
    subtotal = serializers.SerializerMethodField()

    class Meta:
        model = OrderLine
        fields = ['id', 'product_id', 'product_name', 'sku',
                  'quantity', 'price_per_unit', 'subtotal']

    def get_subtotal(self, obj):
        return float(obj.subtotal)


def recalc_order_total(order):
    """Пересчитать денормализованную сумму заказа по позициям."""
    total = sum((l.subtotal for l in order.lines.all()), Decimal('0'))
    order.total_amount = total
    order.save(update_fields=['total_amount'])
