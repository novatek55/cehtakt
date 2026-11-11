from rest_framework import serializers

from .models import Product, Process
from .services import calculate_product_cost
from apps.directory.models import Category, Unit


class ProductWriteSerializer(serializers.Serializer):
    """Создание изделия: создаёт Process + Product."""
    name = serializers.CharField()
    sku = serializers.CharField(required=False, allow_blank=True, default='')
    make = serializers.BooleanField(default=True)
    buy = serializers.BooleanField(default=False)
    default_sell_price = serializers.DecimalField(max_digits=14, decimal_places=2, required=False, default=0)
    default_buy_price = serializers.DecimalField(max_digits=14, decimal_places=2, required=False, default=0)
    category = serializers.CharField(required=False, allow_blank=True, default='')
    unit = serializers.CharField(required=False, allow_blank=True, default='')

    def create(self, data):
        category = Category.objects.get_or_create(name=data['category'])[0] if data.get('category') else None
        unit = Unit.objects.get_or_create(name=data['unit'])[0] if data.get('unit') else None
        # Материал (закупаемый) по умолчанию не производится.
        buy = data['buy']
        make = False if buy else data['make']
        process = Process.objects.create(
            name=data['name'], make=make, buy=buy, category=category, unit=unit)
        return Product.objects.create(
            process=process, name=data['name'], sku=data.get('sku', ''),
            default_sell_price=data.get('default_sell_price', 0),
            default_buy_price=data.get('default_buy_price', 0))

    def to_representation(self, instance):
        return {'id': instance.id, 'name': instance.name}


class ProductListSerializer(serializers.ModelSerializer):
    """Строка каталога — колонки как в proplan (Продукция)."""
    category = serializers.CharField(source='process.category.name', default='', allow_null=True)
    complexity = serializers.CharField(source='process.get_complexity_display')
    unit = serializers.CharField(source='process.unit.name', default='', allow_null=True)
    make = serializers.BooleanField(source='process.make')
    buy = serializers.BooleanField(source='process.buy')
    cost = serializers.SerializerMethodField()
    profit = serializers.SerializerMethodField()
    margin = serializers.SerializerMethodField()

    class Meta:
        model = Product
        fields = [
            'id', 'name', 'sku', 'category', 'unit', 'make', 'buy',
            'default_sell_price', 'default_buy_price', 'cost', 'profit', 'margin',
            'total_production_duration', 'complexity',
        ]

    def get_cost(self, obj):
        return calculate_product_cost(obj)

    def get_profit(self, obj):
        return obj.default_sell_price - calculate_product_cost(obj)

    def get_margin(self, obj):
        sell = obj.default_sell_price
        if not sell:
            return None
        cost = calculate_product_cost(obj)
        return round(float((sell - cost) / sell) * 100, 1)
