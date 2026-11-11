from rest_framework import serializers

from .models import ProductionJob


class ProductionJobSerializer(serializers.ModelSerializer):
    """Строка Плана (под фронт). areaCells: {участок: статус}; parentId/salesOrderId — для дерева."""
    batch = serializers.CharField()
    productName = serializers.SerializerMethodField()
    productSku = serializers.SerializerMethodField()
    quantity = serializers.SerializerMethodField()
    madeQuantity = serializers.SerializerMethodField()
    statusLabel = serializers.CharField(source='get_status_display')
    areaCells = serializers.SerializerMethodField()
    parentId = serializers.IntegerField(source='parent_id', allow_null=True)
    salesOrderId = serializers.IntegerField(source='sales_order_id', allow_null=True)
    orderName = serializers.SerializerMethodField()
    rank = serializers.IntegerField()
    finishedAt = serializers.SerializerMethodField()
    finishedArea = serializers.SerializerMethodField()
    finishedBy = serializers.SerializerMethodField()
    rework = serializers.SerializerMethodField()
    defectRecords = serializers.SerializerMethodField()

    class Meta:
        model = ProductionJob
        fields = ['id', 'name', 'batch', 'productName', 'productSku', 'quantity',
                  'madeQuantity', 'status', 'statusLabel', 'areaCells',
                  'parentId', 'salesOrderId', 'orderName', 'rank',
                  'finishedAt', 'finishedArea', 'finishedBy', 'rework', 'defectRecords']

    def get_rework(self, obj):
        from .rework import defect_record_data
        return defect_record_data(obj.rework)

    def get_defectRecords(self, obj):
        from .rework import defect_record_data
        return [defect_record_data(record) for record in obj.defect_records.all()]

    def get_productName(self, obj):
        return obj.product.name

    def get_productSku(self, obj):
        return obj.product.sku

    def get_quantity(self, obj):
        return float(obj.quantity)

    def get_madeQuantity(self, obj):
        return float(obj.made_quantity)

    def get_orderName(self, obj):
        return obj.sales_order.name if obj.sales_order_id else ''

    def get_areaCells(self, obj):
        return {op.area.name: op.get_status_display() for op in obj.operations.all()}

    @staticmethod
    def _last_finished_op(obj):
        """Последняя завершённая операция задания (по finished_at) — источник «дата/участок/кто»."""
        done = [op for op in obj.operations.all() if op.finished_at]
        return max(done, key=lambda op: op.finished_at) if done else None

    def get_finishedAt(self, obj):
        op = self._last_finished_op(obj)
        return op.finished_at.isoformat() if op else None

    def get_finishedArea(self, obj):
        op = self._last_finished_op(obj)
        return op.area.name if op else ''

    def get_finishedBy(self, obj):
        op = self._last_finished_op(obj)
        user = op.executor if op else None
        if not user:
            return ''
        return user.get_full_name() or user.username
