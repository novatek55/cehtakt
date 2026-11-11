"""Сериализаторы отгрузок. Наружу camelCase — как в остальных модулях фронта."""
from rest_framework import serializers

from .models import Shipment, ShipmentLine, ShipmentScan
from .services import can_delete_shipment


class ShipmentLineSerializer(serializers.ModelSerializer):
    productId = serializers.IntegerField(source='product_id', allow_null=True, required=False)
    productName = serializers.SerializerMethodField()
    sku = serializers.SerializerMethodField()
    externalPosting = serializers.CharField(source='external_posting', read_only=True)
    externalLabelCode = serializers.SerializerMethodField()
    externalTrbxId = serializers.CharField(source='external_trbx_id', read_only=True)
    externalWarehouse = serializers.CharField(source='external_warehouse', read_only=True)
    manuallyEdited = serializers.BooleanField(source='manually_edited', read_only=True)
    externalStatus = serializers.SerializerMethodField()
    externalStatusCode = serializers.CharField(source='external_status', read_only=True)
    scanned = serializers.SerializerMethodField()
    scanMissing = serializers.SerializerMethodField()
    scanCodes = serializers.SerializerMethodField()
    itemScanned = serializers.SerializerMethodField()
    cargoScanned = serializers.SerializerMethodField()
    cargoRequired = serializers.SerializerMethodField()

    class Meta:
        model = ShipmentLine
        fields = ['id', 'productId', 'productName', 'sku', 'quantity',
                  'externalPosting', 'externalLabelCode', 'externalTrbxId', 'externalWarehouse',
                  'manuallyEdited', 'externalStatus', 'externalStatusCode',
                  'scanned', 'scanMissing', 'scanCodes',
                  'itemScanned', 'cargoScanned', 'cargoRequired']

    def get_productName(self, obj):
        return obj.product.name if obj.product_id else (obj.name_raw or '—')

    def get_sku(self, obj):
        return obj.product.sku if obj.product_id else obj.sku_raw

    def get_externalStatus(self, obj):
        """Человеческая подпись стадии отправления; пусто у ручных строк."""
        from .marketplaces import STATUS_LABELS
        return STATUS_LABELS.get(obj.external_status, obj.external_status or '')

    def get_externalLabelCode(self, obj):
        if obj.shipment.source == Shipment.Source.WB:
            code = ' '.join(str(getattr(obj, 'external_label_code', '')).strip().split())
            return code.rsplit('/', 1)[-1].strip() if '/' in code else code
        if obj.shipment.source != Shipment.Source.OZON:
            return ''
        head = str(obj.external_posting or '').split('-', 1)[0]
        return head[-4:] if head.isdigit() and len(head) >= 4 else ''

    def get_scanned(self, obj):
        return self.get_itemScanned(obj) and (
            not self.get_cargoRequired(obj) or self.get_cargoScanned(obj)
        )

    def get_scanMissing(self, obj):
        return bool(obj.shipment.scan_finished_at and not self.get_scanned(obj))

    def get_itemScanned(self, obj):
        return obj.scans.filter(kind=ShipmentScan.Kind.ITEM).exists()

    def get_cargoScanned(self, obj):
        return obj.scans.filter(kind=ShipmentScan.Kind.CARGO).exists()

    def get_cargoRequired(self, obj):
        return bool(
            obj.shipment.source == Shipment.Source.WB
            and (obj.external_trbx_id or getattr(obj, 'external_trbx_barcode', ''))
        )

    def get_scanCodes(self, obj):
        if obj.shipment.source == Shipment.Source.WB:
            rows = [
                ('Этикетка отправления', getattr(obj, 'external_label_barcode', '')),
                ('QR грузоместа', getattr(obj, 'external_trbx_barcode', '')),
                ('ID грузоместа', obj.external_trbx_id),
                ('Отправление', obj.external_posting),
            ]
        elif obj.shipment.source == Shipment.Source.OZON:
            rows = [
                ('Код этикетки', self.get_externalLabelCode(obj)),
                ('Отправление', obj.external_posting),
            ]
        else:
            rows = [
                ('Артикул товара', obj.product.sku if obj.product_id else ''),
                ('Артикул строки', obj.sku_raw),
            ]
        return [
            {'label': label, 'value': ' '.join(str(value).strip().split())}
            for label, value in rows
            if value
        ]


class ShipmentRowSerializer(serializers.ModelSerializer):
    linesCount = serializers.SerializerMethodField()
    totalQuantity = serializers.SerializerMethodField()
    createdAt = serializers.DateTimeField(source='created_at', read_only=True)
    shippedAt = serializers.DateTimeField(source='shipped_at', read_only=True)
    wbStage = serializers.SerializerMethodField()
    canDelete = serializers.SerializerMethodField()
    ozonUnacceptedPostings = serializers.JSONField(source='ozon_unaccepted_postings', read_only=True)

    class Meta:
        model = Shipment
        fields = ['id', 'name', 'source', 'status', 'note',
                  'linesCount', 'totalQuantity', 'createdAt', 'shippedAt',
                  'wbStage', 'canDelete', 'ozonUnacceptedPostings']

    def _lines(self, obj):
        return [l for l in obj.lines.all() if not l.deleted]

    def get_linesCount(self, obj):
        return len(self._lines(obj))

    def get_totalQuantity(self, obj):
        return float(sum(l.quantity for l in self._lines(obj)))

    def get_canDelete(self, obj):
        return can_delete_shipment(obj)

    def get_wbStage(self, obj):
        """Стадия сборки поставки WB (для степпера и чипа в списке).

        Выводится из состояния, не хранится: поставка создана → задания
        собраны → грузоместа созданы → передано → отгружено. У Ozon/ручных
        заявок — None (степпера нет)."""
        if obj.source != Shipment.Source.WB:
            return None
        if obj.status == Shipment.Status.SHIPPED:
            return 'shipped'
        if obj.handed_over_at:
            return 'handed_over'
        if not obj.external_supply_id:
            return 'imported'
        lines = [l for l in self._lines(obj) if l.external_posting]
        if any(l.external_status == 'new' for l in lines):
            return 'supply_created'
        if lines and all(l.external_trbx_id for l in lines):
            return 'boxed'
        if lines and all(l.external_status in {'assembled', 'confirm'} for l in lines):
            return 'collected'
        return 'supply_created'


class ShipmentDetailSerializer(ShipmentRowSerializer):
    lines = serializers.SerializerMethodField()
    canAssemble = serializers.SerializerMethodField()

    class Meta(ShipmentRowSerializer.Meta):
        fields = ShipmentRowSerializer.Meta.fields + ['lines', 'canAssemble']

    def get_lines(self, obj):
        return ShipmentLineSerializer(self._lines(obj), many=True).data

    def get_canAssemble(self, obj):
        """Можно ли нажимать «Собрать»: источник — маркетплейс, есть неудалённые
        строки в стадии сборки, и (для Ozon) у всех таких строк есть external_product_id."""
        assemble_stage = {Shipment.Source.OZON: 'awaiting_packaging', Shipment.Source.WB: 'new'}
        stage = assemble_stage.get(obj.source)
        if stage is None:
            return False
        assemblable = [l for l in self._lines(obj) if l.external_status == stage]
        if not assemblable:
            return False
        if obj.source == Shipment.Source.OZON:
            return all(l.external_product_id for l in assemblable)
        return True
