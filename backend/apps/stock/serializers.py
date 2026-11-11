from rest_framework import serializers

from apps.catalog.models import Product
from apps.catalog.services import calculate_product_cost, classify_product_type
from .models import StockMovement

_MONTHS = ['', 'янв.', 'февр.', 'марта', 'апр.', 'мая', 'июн.',
           'июл.', 'авг.', 'сент.', 'окт.', 'нояб.', 'дек.']


class StockCorrectionSerializer(serializers.ModelSerializer):
    """Документ корректировки: дата · товар · кто · было · стало · изменение · основание."""
    date = serializers.SerializerMethodField()
    product = serializers.CharField(source='product.name', read_only=True)
    sku = serializers.CharField(source='product.sku', read_only=True)
    user = serializers.SerializerMethodField()
    before = serializers.SerializerMethodField()
    after = serializers.SerializerMethodField()
    delta = serializers.SerializerMethodField()
    note = serializers.CharField()

    class Meta:
        model = StockMovement
        fields = ['id', 'date', 'product', 'sku', 'user', 'before', 'after', 'delta', 'note']

    def get_date(self, obj):
        d = obj.created_at
        return f'{d.day} {_MONTHS[d.month]} {d.year}, {d.strftime("%H:%M")}' if d else ''

    def get_user(self, obj):
        if not obj.created_by_id:
            return ''
        u = obj.created_by
        return (u.get_full_name() or u.username)

    def get_before(self, obj):
        return float(obj.quantity_before) if obj.quantity_before is not None else None

    def get_after(self, obj):
        return float(obj.quantity_after) if obj.quantity_after is not None else None

    def get_delta(self, obj):
        return float(obj.quantity_delta)


class StockMovementStatementSerializer(serializers.ModelSerializer):
    """Строка выписки по остатку: зачислено/списано, было/стало и источник."""
    date = serializers.SerializerMethodField()
    operation = serializers.SerializerMethodField()
    income = serializers.SerializerMethodField()
    expense = serializers.SerializerMethodField()
    before = serializers.SerializerMethodField()
    after = serializers.SerializerMethodField()
    source = serializers.SerializerMethodField()
    user = serializers.SerializerMethodField()

    class Meta:
        model = StockMovement
        fields = [
            'id', 'date', 'operation', 'income', 'expense',
            'before', 'after', 'source', 'user', 'note',
        ]

    def get_date(self, obj):
        d = obj.created_at
        return f'{d.day} {_MONTHS[d.month]} {d.year}, {d.strftime("%H:%M")}' if d else ''

    def get_operation(self, obj):
        return obj.get_kind_display()

    def get_income(self, obj):
        delta = obj.quantity_delta
        return float(delta) if delta > 0 else None

    def get_expense(self, obj):
        delta = obj.quantity_delta
        return float(abs(delta)) if delta < 0 else None

    def get_before(self, obj):
        return float(obj.quantity_before) if obj.quantity_before is not None else None

    def get_after(self, obj):
        return float(obj.quantity_after) if obj.quantity_after is not None else None

    def get_source(self, obj):
        if obj.job_id:
            return f'Карточка выпуска {obj.job.name}'
        return obj.note or obj.get_kind_display()

    def get_user(self, obj):
        if not obj.created_by_id:
            return ''
        u = obj.created_by
        return u.get_full_name() or u.username


class StockItemSerializer(serializers.ModelSerializer):
    """Строка склада (под фронт, camelCase). Тип вычисляется во вью."""
    sku = serializers.CharField()
    category = serializers.SerializerMethodField()
    supplier = serializers.SerializerMethodField()
    buyPrice = serializers.SerializerMethodField()
    valueInStock = serializers.SerializerMethodField()
    available = serializers.SerializerMethodField()
    committed = serializers.SerializerMethodField()
    expected = serializers.SerializerMethodField()
    inTransit = serializers.SerializerMethodField()
    ozonStock = serializers.SerializerMethodField()
    wbStock = serializers.SerializerMethodField()
    marketplaceStocks = serializers.SerializerMethodField()
    projected = serializers.SerializerMethodField()
    minLimit = serializers.SerializerMethodField()
    unit = serializers.SerializerMethodField()
    type = serializers.SerializerMethodField()

    class Meta:
        model = Product
        fields = ['id', 'name', 'sku', 'category', 'supplier',
                  'buyPrice', 'valueInStock', 'available', 'committed',
                  'expected', 'inTransit', 'ozonStock', 'wbStock',
                  'marketplaceStocks', 'projected', 'minLimit', 'unit', 'type']

    def get_minLimit(self, obj):
        return float(obj.min_limit or 0)

    def get_category(self, obj):
        return obj.process.category.name if obj.process.category else ''

    def get_supplier(self, obj):
        return obj.process.default_supplier.name if obj.process.default_supplier else ''

    def get_buyPrice(self, obj):
        return float(obj.default_buy_price)

    def get_unit(self, obj):
        return obj.process.unit.name if obj.process.unit else ''

    def _available(self, obj):
        stock = getattr(obj, 'stock', None)
        return float(stock.available_quantity) if stock else 0.0

    def get_available(self, obj):
        return self._available(obj)

    def _committed(self, obj):
        stock = getattr(obj, 'stock', None)
        recorded = stock.committed_quantity if stock else 0
        planned = self.context.get('planned_reservations', {}).get(obj.pk, 0)
        return round(float(recorded + planned), 4)

    def _expected(self, obj):
        stock = getattr(obj, 'stock', None)
        return float(stock.expected_quantity) if stock else 0.0

    def get_committed(self, obj):
        return self._committed(obj)

    def get_expected(self, obj):
        return self._expected(obj)

    def _in_transit(self, obj):
        return float(self.context.get('in_transit', {}).get(obj.id, 0) or 0)

    def get_inTransit(self, obj):
        return self._in_transit(obj)

    def _market_stock(self, obj, source):
        return float(self.context.get('marketplace_stocks', {}).get((obj.id, source), 0) or 0)

    def get_ozonStock(self, obj):
        return self._market_stock(obj, 'ozon')

    def get_wbStock(self, obj):
        return self._market_stock(obj, 'wb')

    def get_marketplaceStocks(self, obj):
        return self.context.get('marketplace_stock_details', {}).get(obj.id, [])

    def get_projected(self, obj):
        # Проекция: наличие − резерв + планируется к выпуску + заказано/в пути
        # (может быть отрицательной → дефицит).
        return round(self._available(obj) - self._committed(obj)
                     + self._expected(obj) + self._in_transit(obj), 3)

    def get_valueInStock(self, obj):
        unit_cost = float(calculate_product_cost(obj))
        return round(self._available(obj) * unit_cost, 2)

    def get_type(self, obj):
        return classify_product_type(obj.process, obj.id, self.context.get('semi_ids', set()))
