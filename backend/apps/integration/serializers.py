"""Сериализаторы внешнего контракта /api/v1/ — snake_case, без camelCase фронта."""
from datetime import timezone as dt_timezone

from rest_framework import serializers

from apps.catalog.services import classify_product_type


class StockRowSerializer(serializers.Serializer):
    """Строка остатка по номенклатуре. Склад в пропалане один — разреза нет."""
    product_id = serializers.IntegerField(source='id')
    sku = serializers.CharField()
    name = serializers.CharField()
    unit = serializers.SerializerMethodField()
    type = serializers.SerializerMethodField()
    available = serializers.SerializerMethodField()
    committed = serializers.SerializerMethodField()
    expected = serializers.SerializerMethodField()

    def get_unit(self, obj):
        # None, а не '': «значения нет» во всём внешнем контракте выражается одним
        # способом. Внутренний сериализатор фронта отдаёт здесь '' — это другой контракт.
        return obj.process.unit.name if obj.process.unit else None

    def get_type(self, obj):
        return classify_product_type(obj.process, obj.id, self.context.get('semi_ids', set()))

    def _stock(self, obj, field):
        stock = getattr(obj, 'stock', None)
        return float(getattr(stock, field)) if stock else 0.0

    def get_available(self, obj):
        return self._stock(obj, 'available_quantity')

    def get_committed(self, obj):
        return self._stock(obj, 'committed_quantity')

    def get_expected(self, obj):
        return self._stock(obj, 'expected_quantity')


class ProductionOutputSerializer(serializers.Serializer):
    """Одна завершённая операция = один факт выпуска."""
    operation_id = serializers.IntegerField(source='id')
    job_id = serializers.IntegerField()
    job_name = serializers.CharField(source='job.name')
    product_id = serializers.IntegerField(source='job.product_id')
    sku = serializers.CharField(source='job.product.sku')
    name = serializers.CharField(source='job.product.name')
    good_quantity = serializers.SerializerMethodField()
    defect_quantity = serializers.SerializerMethodField()
    area = serializers.CharField(source='area.name')
    executor = serializers.SerializerMethodField()
    # Строго UTC: по умолчанию DRF рендерит в TIME_ZONE и отдал бы «+03:00», тогда как
    # курсор всегда в UTC. Потребитель, собравший курсор из этого поля, прислал бы
    # строку с «+» (в query-строке это пробел) и получил 400. Плюс смещение проекта
    # гуляло бы между летом и зимой. DRF сам пишет такое время как «...Z».
    finished_at = serializers.DateTimeField(default_timezone=dt_timezone.utc)
    sales_order_id = serializers.IntegerField(source='job.sales_order_id')

    def get_good_quantity(self, obj):
        return float(obj.done_quantity or 0)

    def get_defect_quantity(self, obj):
        return float(obj.defect_quantity or 0)

    def get_executor(self, obj):
        # None, а не '': «значения нет» во всём контракте выражается одним способом.
        return obj.executor.username if obj.executor else None
