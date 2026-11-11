from datetime import date
from decimal import Decimal

from rest_framework import serializers

from apps.catalog.models import Product
from apps.directory.models import Supplier
from .models import PurchaseOrder, PurchaseOrderLine

_MONTHS = ['', 'янв.', 'февр.', 'марта', 'апр.', 'мая', 'июн.',
           'июл.', 'авг.', 'сент.', 'окт.', 'нояб.', 'дек.']


def _fmt_date(d):
    return f'{d.day} {_MONTHS[d.month]}' if d else ''


class PurchaseOrderRowSerializer(serializers.ModelSerializer):
    """Строка таблицы Закупок. Статус/сумма/поставщик — свод по позициям."""
    number = serializers.CharField(source='name')
    supplier = serializers.SerializerMethodField()
    sum = serializers.SerializerMethodField()
    deliveryDate = serializers.SerializerMethodField()
    overdue = serializers.SerializerMethodField()
    status = serializers.SerializerMethodField()
    statusLabel = serializers.SerializerMethodField()
    orderName = serializers.SerializerMethodField()

    class Meta:
        model = PurchaseOrder
        fields = ['id', 'number', 'supplier', 'sum', 'deliveryDate', 'overdue',
                  'status', 'statusLabel', 'orderName', 'kind']

    def get_supplier(self, obj):
        names = {l.supplier.name for l in obj.lines.all() if l.supplier_id}
        return ', '.join(sorted(names)) if names else (obj.supplier_name or '')

    def get_sum(self, obj):
        return float(obj.subtotal or 0)

    def get_deliveryDate(self, obj):
        dates = [l.expected_arrival_date for l in obj.lines.all() if l.expected_arrival_date]
        return _fmt_date(min(dates)) if dates else _fmt_date(obj.expected_arrival_date)

    def get_overdue(self, obj):
        dates = [l.expected_arrival_date for l in obj.lines.all() if l.expected_arrival_date]
        return bool(dates and min(dates) < date.today())

    def get_status(self, obj):
        return obj.computed_status()[0]

    def get_statusLabel(self, obj):
        return obj.computed_status()[1]

    def get_orderName(self, obj):
        return obj.sales_order.name if obj.sales_order_id else ''


class PurchaseOrderLineSerializer(serializers.ModelSerializer):
    """Позиция заявки: чтение с именами; запись по product_id/supplier_id."""
    product_id = serializers.PrimaryKeyRelatedField(
        source='product', queryset=Product.objects.all(), write_only=True)
    supplier_id = serializers.PrimaryKeyRelatedField(
        source='supplier', queryset=Supplier.objects.all(), write_only=True,
        required=False, allow_null=True)
    product_name = serializers.CharField(source='product.name', read_only=True)
    sku = serializers.CharField(source='product.sku', read_only=True)
    supplierName = serializers.SerializerMethodField()
    statusLabel = serializers.CharField(source='get_status_display', read_only=True)
    subtotal = serializers.SerializerMethodField()

    class Meta:
        model = PurchaseOrderLine
        fields = ['id', 'product_id', 'product_name', 'sku', 'quantity', 'price_per_unit',
                  'supplier_id', 'supplierName', 'status', 'statusLabel',
                  'received_quantity', 'expected_arrival_date', 'subtotal']

    def get_supplierName(self, obj):
        return obj.supplier.name if obj.supplier_id else ''

    def get_subtotal(self, obj):
        return float(obj.subtotal)


class PurchaseOrderWriteSerializer(serializers.Serializer):
    """Создание заявки (заголовок)."""
    name = serializers.CharField()
    supplier_name = serializers.CharField(required=False, allow_blank=True, default='')
    expected_arrival_date = serializers.DateField(required=False, allow_null=True, default=None)

    def create(self, data):
        return PurchaseOrder.objects.create(
            name=data['name'], supplier_name=data.get('supplier_name', ''),
            expected_arrival_date=data.get('expected_arrival_date'),
            kind=PurchaseOrder.Kind.MANUAL)

    def to_representation(self, instance):
        return {'id': instance.id, 'name': instance.name}


def recalc_purchase_total(order):
    """Пересчитать денормализованную сумму заявки по позициям."""
    order.total_amount = sum((l.subtotal for l in order.lines.all()), Decimal('0'))
    order.save(update_fields=['total_amount'])
