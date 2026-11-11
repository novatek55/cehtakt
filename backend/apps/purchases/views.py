from decimal import Decimal, InvalidOperation

from django.db import transaction
from django.shortcuts import get_object_or_404
from rest_framework import generics
from rest_framework.views import APIView
from rest_framework.response import Response

from apps.catalog.models import Product
from apps.stock.models import StockItem, StockMovement
from .models import PurchaseOrder, PurchaseOrderLine, LineStatus
from .document_ocr import OcrUnavailable, recognize_purchase_files
from .serializers import (
    PurchaseOrderRowSerializer, PurchaseOrderWriteSerializer,
    PurchaseOrderLineSerializer, recalc_purchase_total,
)


class PurchaseOrderListView(generics.ListCreateAPIView):
    """GET /api/purchases/ — закупки; POST — создать закупку."""
    pagination_class = None

    def get_serializer_class(self):
        return PurchaseOrderWriteSerializer if self.request.method == 'POST' else PurchaseOrderRowSerializer

    def get_queryset(self):
        return PurchaseOrder.objects.order_by('-created_at')


class PurchaseOrderBulkView(APIView):
    """POST /api/purchases/bulk/ {rows:[{name,supplier_name}]} — массовое добавление закупок."""

    def post(self, request):
        rows = request.data.get('rows') or []
        created = 0
        for r in rows:
            name = (r.get('name') or '').strip()
            if not name:
                continue
            PurchaseOrder.objects.create(
                name=name,
                supplier_name=(r.get('supplier_name') or '').strip(),
                status=PurchaseOrder.Status.REQUEST,
            )
            created += 1
        return Response({'created': created}, status=201)


class PurchaseDocumentOcrView(APIView):
    """POST /api/purchases/ocr/ multipart files[] -> recognized purchase draft rows."""

    def post(self, request):
        files = request.FILES.getlist('files')
        if not files:
            single = request.FILES.get('file')
            files = [single] if single else []
        if not files:
            return Response({'error': 'no files uploaded'}, status=400)
        try:
            return Response(recognize_purchase_files(files))
        except OcrUnavailable as exc:
            return Response({'error': str(exc)}, status=503)


class PurchaseOrderDetailView(APIView):
    """PATCH /api/purchases/<id>/ — сменить статус закупки (legacy)."""

    def patch(self, request, pk):
        po = PurchaseOrder.objects.filter(pk=pk).first()
        if not po:
            return Response({'error': 'не найдено'}, status=404)
        status = request.data.get('status')
        if status in dict(PurchaseOrder.Status.choices):
            po.status = status
            po.save(update_fields=['status'])
        return Response({'id': po.id, 'status': po.status, 'statusLabel': po.get_status_display()})


class PurchaseLinesView(generics.ListCreateAPIView):
    """GET /api/purchases/<order_id>/lines/ — позиции заявки; POST — добавить позицию."""
    serializer_class = PurchaseOrderLineSerializer
    pagination_class = None

    def get_queryset(self):
        return (PurchaseOrderLine.objects
                .filter(order_id=self.kwargs['order_id'])
                .select_related('product', 'supplier'))

    def perform_create(self, serializer):
        order = get_object_or_404(PurchaseOrder, pk=self.kwargs['order_id'])
        line = serializer.save(order=order)
        # Цена по умолчанию — цена покупки материала (для закупки, не продажи).
        if not line.price_per_unit and line.product.default_buy_price:
            line.price_per_unit = line.product.default_buy_price
            line.save(update_fields=['price_per_unit'])
        recalc_purchase_total(order)


class PurchaseLineDetailView(generics.RetrieveUpdateDestroyAPIView):
    """PATCH/DELETE /api/purchases/lines/<id>/ — правка/удаление позиции заявки."""
    serializer_class = PurchaseOrderLineSerializer
    queryset = PurchaseOrderLine.objects.select_related('product', 'supplier')

    def perform_update(self, serializer):
        line = serializer.save()
        recalc_purchase_total(line.order)

    def perform_destroy(self, instance):
        order = instance.order
        instance.delete()
        recalc_purchase_total(order)


class PurchaseLineReceiveView(APIView):
    """POST /api/purchases/lines/<id>/receive/ {quantity} — частичная приёмка позиции.

    Прибавляет фактически принятое на склад (available), пишет движение receipt,
    наращивает received_quantity; при received_quantity ≥ quantity строка → 'received'.
    Дефицит закрывается сам (остаток вырос). Метод PATCH — приёмка правит существующую
    позицию (маппится на право 'update', а не 'create').
    """

    def patch(self, request, pk):
        line = (PurchaseOrderLine.objects
                .select_related('product', 'order').filter(pk=pk).first())
        if not line:
            return Response({'error': 'позиция не найдена'}, status=404)
        if line.status == LineStatus.REQUEST:
            return Response({'error': 'Сначала отметьте позицию «Заказано»'}, status=400)
        if line.status == LineStatus.RECEIVED:
            return Response({'error': 'Позиция уже получена полностью'}, status=400)
        try:
            qty = Decimal(str(request.data.get('quantity')))
        except (InvalidOperation, TypeError, ValueError):
            return Response({'error': 'некорректное количество'}, status=400)
        if qty <= 0:
            return Response({'error': 'количество должно быть больше нуля'}, status=400)
        remaining = line.quantity - line.received_quantity
        if qty > remaining:
            return Response({'error': f'Больше остатка к приёмке ({remaining})'}, status=400)

        with transaction.atomic():
            stock, _ = StockItem.objects.get_or_create(product=line.product)
            before = stock.available_quantity or Decimal(0)
            after = before + qty
            stock.available_quantity = after
            stock.save(update_fields=['available_quantity'])
            StockMovement.objects.create(
                product=line.product, kind=StockMovement.Kind.RECEIPT,
                quantity_before=before, quantity_after=after,
                quantity_delta=qty, note=f'Приёмка по заявке {line.order.name}',
                created_by=request.user if request.user.is_authenticated else None)
            line.received_quantity += qty
            if line.received_quantity >= line.quantity:
                line.status = LineStatus.RECEIVED
            line.save(update_fields=['received_quantity', 'status'])
        return Response(PurchaseOrderLineSerializer(line).data, status=201)


class PurchaseFromStockView(APIView):
    """POST /api/purchases/from-stock/ {items:[{product_id, quantity}], name?} —
    заявка на пополнение склада (kind=stock) из выбранных на Складе материалов.
    Поставщик/цена — из карточки материала, статус строк 'request'."""

    def post(self, request):
        rows = []
        for it in (request.data.get('items') or []):
            pid = it.get('product_id')
            try:
                q = Decimal(str(it.get('quantity')))
            except (InvalidOperation, TypeError, ValueError):
                continue
            if pid and q > 0:
                rows.append((pid, q))
        if not rows:
            return Response({'error': 'нет позиций для закупки'}, status=400)
        with transaction.atomic():
            seq = PurchaseOrder.objects.filter(kind=PurchaseOrder.Kind.STOCK).count() + 1
            name = (request.data.get('name') or '').strip() or f'Закупка склад {seq}'
            po = PurchaseOrder.objects.create(name=name, kind=PurchaseOrder.Kind.STOCK)
            for pid, q in rows:
                product = Product.objects.select_related('process').filter(pk=pid).first()
                if not product:
                    continue
                PurchaseOrderLine.objects.create(
                    order=po, product=product, quantity=q,
                    supplier=product.process.default_supplier,
                    price_per_unit=product.default_buy_price or 0,
                    status=LineStatus.REQUEST)
            recalc_purchase_total(po)
        return Response({'id': po.id, 'name': po.name}, status=201)


class PurchaseAssortmentView(APIView):
    """POST /api/purchases/assortment/ {order_ids:[...]} — сводка по ассортименту:
    материал → суммарно нужно (строки 'request') + разбивка по заявкам/заказам."""

    def post(self, request):
        order_ids = request.data.get('order_ids') or []
        lines = (PurchaseOrderLine.objects
                 .filter(order_id__in=order_ids, status=LineStatus.REQUEST)
                 .select_related('product', 'supplier', 'order', 'order__sales_order'))
        agg = {}
        for l in lines:
            src = l.order.sales_order.name if l.order.sales_order_id else l.order.name
            a = agg.setdefault(l.product_id, {
                'product_id': l.product_id, 'name': l.product.name, 'sku': l.product.sku,
                'supplier': l.supplier.name if l.supplier_id else '',
                'total': Decimal('0'), 'byOrder': {}})
            a['total'] += l.quantity
            a['byOrder'][src] = a['byOrder'].get(src, Decimal('0')) + l.quantity
        result = [{
            'product_id': a['product_id'], 'name': a['name'], 'sku': a['sku'],
            'supplier': a['supplier'], 'total': float(a['total']),
            'byOrder': [{'order': k, 'quantity': float(v)} for k, v in a['byOrder'].items()],
        } for a in agg.values()]
        result.sort(key=lambda r: r['name'])
        return Response(result)


class PurchaseBulkOrderView(APIView):
    """POST /api/purchases/bulk-order/ {order_ids} — отметить все строки 'request'
    выбранных заявок как 'ordered' (массовый заказ у поставщика)."""

    def post(self, request):
        order_ids = request.data.get('order_ids') or []
        n = (PurchaseOrderLine.objects
             .filter(order_id__in=order_ids, status=LineStatus.REQUEST)
             .update(status=LineStatus.ORDERED))
        return Response({'ordered': n})


class PurchaseBulkDeleteView(APIView):
    """POST /api/purchases/bulk-delete/ {ids} — удалить заявки (позиции каскадом)."""

    def post(self, request):
        ids = request.data.get('ids') or []
        if not ids:
            return Response({'error': 'нет выбранных заявок'}, status=400)
        qs = PurchaseOrder.objects.filter(pk__in=ids)
        deleted = qs.count()
        qs.delete()
        return Response({'deleted': deleted})
