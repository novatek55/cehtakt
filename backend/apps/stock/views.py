import os
from collections import defaultdict
from decimal import Decimal, InvalidOperation
from pathlib import Path

from django.db.models import Count, F, Sum
from django.db import transaction
from rest_framework import generics
from rest_framework.views import APIView
from rest_framework.response import Response

from apps.catalog.models import Product
from apps.catalog.services import filter_products_by_type, semi_product_ids
from apps.production.planned_reservations import uncovered_job_reservations
from apps.purchases.models import PurchaseOrderLine, LineStatus
from apps.shipments.marketplaces import (
    MarketplaceError, fetch_ozon_fbs_stocks, fetch_ozon_offer_ids, fetch_ozon_warehouses,
    fetch_wb_analytics_fbs_stocks,
    update_ozon_fbs_stock, update_wb_fbs_stock, _request,
)
from apps.shipments.models import MarketplaceProductAlias, MarketplaceSettings, MarketplaceWarehouse, Shipment, ShipmentLine
from apps.shipments.services import _match_product, _norm_sku
from .models import MarketplaceStock, StockItem, StockMovement
from .serializers import StockItemSerializer, StockCorrectionSerializer, StockMovementStatementSerializer


class StockCorrectionView(APIView):
    """POST /api/stock/<product_id>/correction/ {delta, note} — ручная корректировка остатка.

    delta > 0 — прибавить, < 0 — убавить. Создаёт движение (kind=correction)
    и обновляет available_quantity складской позиции.
    """

    def post(self, request, product_id):
        product = Product.objects.filter(pk=product_id).first()
        if not product:
            return Response({'error': 'товар не найден'}, status=404)
        try:
            delta = Decimal(str(request.data.get('delta')))
        except (InvalidOperation, TypeError, ValueError):
            return Response({'error': 'некорректное количество'}, status=400)
        if delta == 0:
            return Response({'error': 'изменение не может быть нулевым'}, status=400)
        # Полуфабрикаты и продукция (производимые) — только целые количества.
        is_material = product.process.buy
        if not is_material and delta != delta.to_integral_value():
            return Response(
                {'error': 'Для полуфабрикатов и продукции количество должно быть целым'},
                status=400,
            )
        note = (request.data.get('note') or '').strip()
        stock, _ = StockItem.objects.get_or_create(product=product)
        before = stock.available_quantity or Decimal(0)
        after = before + delta
        stock.available_quantity = after
        stock.save(update_fields=['available_quantity'])
        StockMovement.objects.create(
            product=product,
            kind=StockMovement.Kind.CORRECTION,
            quantity_delta=delta,
            quantity_before=before,
            quantity_after=after,
            note=note,
            created_by=request.user if request.user.is_authenticated else None,
        )
        return Response({'available': float(after)}, status=201)


class StockCorrectionsListView(generics.ListAPIView):
    """GET /api/stock/corrections/ — журнал документов корректировки остатков."""
    serializer_class = StockCorrectionSerializer
    pagination_class = None

    def get_queryset(self):
        return (StockMovement.objects
                .filter(kind=StockMovement.Kind.CORRECTION)
                .select_related('product', 'created_by')
                .order_by('-created_at'))


class StockCorrectionsBulkDeleteView(APIView):
    """POST /api/stock/corrections/bulk-delete/ — удалить выбранные корректировки."""

    def _can_delete_any_correction(self, user):
        if not user or not user.is_authenticated:
            return False
        if user.is_superuser:
            return True
        role = getattr(getattr(user, 'profile', None), 'role', None)
        perms = (role.permissions or {}) if role else {}
        return bool(perms.get('stock', {}).get('delete', False))

    def post(self, request):
        ids = request.data.get('ids')
        if not isinstance(ids, list) or not ids:
            return Response({'error': 'выберите корректировки'}, status=400)
        try:
            ids = [int(value) for value in ids]
        except (TypeError, ValueError):
            return Response({'error': 'некорректный список корректировок'}, status=400)

        user = request.user
        can_delete_any = self._can_delete_any_correction(user)
        with transaction.atomic():
            corrections = list(
                StockMovement.objects
                .select_for_update()
                .filter(id__in=ids, kind=StockMovement.Kind.CORRECTION)
                .select_related('product')
            )
            if len(corrections) != len(set(ids)):
                return Response({'error': 'корректировка не найдена'}, status=404)
            if not can_delete_any:
                forbidden = [
                    correction.id for correction in corrections
                    if correction.created_by_id != user.id
                ]
                if forbidden:
                    return Response({'error': 'можно удалить только свои корректировки'}, status=403)

            product_ids = {correction.product_id for correction in corrections}
            stocks = {
                stock.product_id: stock
                for stock in StockItem.objects.select_for_update().filter(product_id__in=product_ids)
            }
            for correction in corrections:
                stock = stocks.get(correction.product_id)
                if not stock:
                    return Response({'error': 'остаток товара не найден'}, status=400)
                stock.available_quantity = (stock.available_quantity or Decimal(0)) - correction.quantity_delta
                stock.save(update_fields=['available_quantity'])
            deleted = len(corrections)
            StockMovement.objects.filter(id__in=[correction.id for correction in corrections]).delete()
        return Response({'deleted': deleted})


class StockMovementListView(generics.ListAPIView):
    """GET /api/stock/<product_id>/movements/ — выписка движений остатка по позиции."""
    serializer_class = StockMovementStatementSerializer
    pagination_class = None

    def get_queryset(self):
        return (StockMovement.objects
                .filter(product_id=self.kwargs['product_id'])
                .select_related('job', 'created_by')
                .order_by('-created_at', '-id'))


class StockListView(generics.ListAPIView):
    """GET /api/stock/?type=all|material|semi|product — склад с фильтром по типу."""
    serializer_class = StockItemSerializer
    pagination_class = None

    def _semi_ids(self):
        return semi_product_ids()

    def _in_transit(self):
        """{product_id: Σ(quantity − received) по строкам закупок в статусе 'Заказано'}."""
        rows = (PurchaseOrderLine.objects
                .filter(status=LineStatus.ORDERED)
                .values('product_id')
                .annotate(t=Sum(F('quantity') - F('received_quantity'))))
        return {r['product_id']: r['t'] for r in rows}

    def _marketplace_stocks(self):
        rows = MarketplaceStock.objects.values('product_id', 'source').annotate(t=Sum('quantity'))
        return {(r['product_id'], r['source']): r['t'] for r in rows}

    def _marketplace_stock_details(self):
        names = {
            (row.source, row.warehouse_id): row.name
            for row in MarketplaceWarehouse.objects.all()
        }
        settings = MarketplaceSettings.load()
        options = {
            Shipment.Source.OZON: _known_warehouse_ids(settings, Shipment.Source.OZON),
            Shipment.Source.WB: _known_warehouse_ids(settings, Shipment.Source.WB),
        }
        fallback_names = _known_warehouse_names(options)
        details = {}
        rows = list(MarketplaceStock.objects.order_by('source', 'warehouse_id'))
        specific_ozon_products = {
            row.product_id
            for row in rows
            if (
                row.source == Shipment.Source.OZON
                and len(_split_id_list(row.warehouse_id)) == 1
                and (row.quantity or Decimal(0)) > 0
            )
        }
        for row in rows:
            known_ids = options.get(row.source, [])
            row_warehouse_ids = _split_id_list(row.warehouse_id)
            if (
                row.source == Shipment.Source.OZON
                and row_warehouse_ids
                and not (row.quantity or Decimal(0))
                and not any(warehouse_id in known_ids for warehouse_id in row_warehouse_ids)
            ):
                continue
            if row.source == Shipment.Source.OZON and known_ids and len(row_warehouse_ids) != 1 and row.product_id in specific_ozon_products:
                continue
            warehouse_id = row.warehouse_id
            item = {
                'source': row.source,
                'warehouseId': warehouse_id,
                'warehouseName': names.get((row.source, warehouse_id), '')
                                 or fallback_names.get((row.source, warehouse_id), ''),
                'quantity': float(row.quantity or 0),
            }
            details.setdefault(row.product_id, []).append(item)

        product_ids = Product.objects.filter(archived=False).values_list('id', flat=True)
        for product_id in product_ids:
            product_details = details.setdefault(product_id, [])
            existing = {(item['source'], item['warehouseId']) for item in product_details}
            for source, warehouse_ids in options.items():
                source_value = source.value if hasattr(source, 'value') else str(source)
                for warehouse_id in warehouse_ids:
                    key = (source_value, warehouse_id)
                    if key in existing:
                        continue
                    product_details.append({
                        'source': source_value,
                        'warehouseId': warehouse_id,
                        'warehouseName': names.get(key, '') or fallback_names.get(key, ''),
                        'quantity': 0.0,
                    })
                    existing.add(key)
            product_details.sort(key=lambda item: _warehouse_sort_key(options, item))
        return details

    def get_serializer_context(self):
        ctx = super().get_serializer_context()
        ctx['semi_ids'] = self._semi_ids()
        ctx['in_transit'] = self._in_transit()
        planned = defaultdict(Decimal)
        for _, product_id, quantity in uncovered_job_reservations():
            planned[product_id] += quantity
        ctx['planned_reservations'] = planned
        ctx['marketplace_stocks'] = self._marketplace_stocks()
        ctx['marketplace_stock_details'] = self._marketplace_stock_details()
        return ctx

    def get_queryset(self):
        qs = (Product.objects
              .select_related('process', 'process__category', 'process__default_supplier', 'process__unit', 'stock')
              .order_by('name'))
        t = self.request.query_params.get('type', 'all')
        # Материал = «Закупать»; ПФ/Продукция = «Производить» (см. catalog.services)
        from apps.catalog.filters import filter_by_final_area
        qs = filter_products_by_type(qs, t, self._semi_ids())
        return filter_by_final_area(qs, self.request.query_params.get('final_area'))


def _parse_quantity(value):
    try:
        quantity = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return None, 'некорректное количество'
    if quantity < 0:
        return None, 'количество не может быть отрицательным'
    if quantity != quantity.to_integral_value():
        return None, 'остаток маркетплейса должен быть целым'
    return quantity, None


def _warehouse_id(settings, source, product=None):
    if source == Shipment.Source.OZON:
        ids = _ozon_warehouse_ids(settings)
        if len(ids) == 1:
            return ids[0]
        if product:
            stock_ids = list(MarketplaceStock.objects.filter(
                product=product,
                source=source,
            ).exclude(warehouse_id='').values_list('warehouse_id', flat=True).distinct())
            if len(stock_ids) == 1:
                return stock_ids[0]
        return ''
    return settings.wb_warehouse_id


def _ozon_warehouse_ids(settings):
    ids = settings.ozon_warehouse_ids or []
    if not ids and settings.ozon_warehouse_id:
        ids = [settings.ozon_warehouse_id]
    clean = []
    for value in ids:
        value = str(value or '').strip()
        if value and value not in clean:
            clean.append(value)
    return clean


def _split_id_list(value):
    ids = []
    for part in str(value or '').replace(';', ',').split(','):
        value = part.strip().strip('"').strip("'")
        if value and value not in ids:
            ids.append(value)
    return ids


def _ozon_fbs_warehouse_ids_from_env():
    values = _split_id_list(
        os.environ.get('FBS_WAREHOUSE_IDS')
        or _env_file_value(os.environ.get('OZON_DASHBOARD_ENV_FILE', '/etc/ozon-dashboard.env'), 'FBS_WAREHOUSE_IDS')
    )
    return list(dict.fromkeys(value.split(':', 1)[0].strip() for value in values))


def _ozon_fbs_warehouse_names_from_env():
    return _split_id_list(
        os.environ.get('FBS_WAREHOUSE_NAMES')
        or _env_file_value(os.environ.get('OZON_DASHBOARD_ENV_FILE', '/etc/ozon-dashboard.env'), 'FBS_WAREHOUSE_NAMES')
    )


def _known_warehouse_names(options):
    result = {}
    ozon_ids = options.get(Shipment.Source.OZON, [])
    ozon_names = _ozon_fbs_warehouse_names_from_env()
    default_ozon_name = _default_ozon_warehouse_name()
    for index, warehouse_id in enumerate(ozon_ids):
        if index < len(ozon_names) and ozon_names[index]:
            name = ozon_names[index]
        elif default_ozon_name:
            name = default_ozon_name if index == 0 else f'{default_ozon_name} {index + 1}'
        else:
            name = f'Склад Ozon {index + 1}'
        result[(Shipment.Source.OZON, warehouse_id)] = (
            name
        )
    return result


def _default_ozon_warehouse_name():
    return (
        ShipmentLine.objects
        .filter(shipment__source=Shipment.Source.OZON)
        .exclude(external_warehouse='')
        .values('external_warehouse')
        .annotate(total=Count('id'))
        .order_by('-total', 'external_warehouse')
        .values_list('external_warehouse', flat=True)
        .first()
    ) or ''


def _known_warehouse_ids(settings, source, include_catalog=False):
    if source == Shipment.Source.OZON:
        verified_ids = list(MarketplaceWarehouse.objects.filter(
            source=Shipment.Source.OZON, status='created',
            warehouse_type='fbs', is_rfbs=False,
        ).values_list('warehouse_id', flat=True))
        if verified_ids:
            return verified_ids
        active_fbs_ids = _ozon_fbs_warehouse_ids_from_env()
        if active_fbs_ids:
            return active_fbs_ids
        ids = _ozon_warehouse_ids(settings)
        if include_catalog:
            for warehouse_id in MarketplaceWarehouse.objects.filter(
                source=Shipment.Source.OZON,
            ).values_list('warehouse_id', flat=True):
                warehouse_id = str(warehouse_id or '').strip()
                if warehouse_id and warehouse_id not in ids:
                    ids.append(warehouse_id)
        stocks = MarketplaceStock.objects.filter(
            source=Shipment.Source.OZON,
        ).exclude(warehouse_id='')
        if not include_catalog:
            stocks = stocks.filter(quantity__gt=0)
        for warehouse_id in stocks.values_list('warehouse_id', flat=True).distinct():
            for value in _split_id_list(warehouse_id):
                if value and value not in ids:
                    ids.append(value)
        return ids
    ids = []
    warehouse_id = str(settings.wb_warehouse_id or '').strip()
    if warehouse_id:
        ids.append(warehouse_id)
    for warehouse_id in MarketplaceStock.objects.filter(
        source=Shipment.Source.WB,
    ).exclude(warehouse_id='').values_list('warehouse_id', flat=True).distinct():
        warehouse_id = str(warehouse_id or '').strip()
        if warehouse_id and warehouse_id not in ids:
            ids.append(warehouse_id)
    return ids


def _warehouse_sort_key(options, item):
    source = item.get('source')
    warehouse_id = item.get('warehouseId')
    source_order = {Shipment.Source.OZON: 0, Shipment.Source.WB: 1}.get(source, 9)
    warehouse_ids = options.get(source, [])
    warehouse_order = warehouse_ids.index(warehouse_id) if warehouse_id in warehouse_ids else len(warehouse_ids) + 1
    return (source_order, warehouse_order, warehouse_id or '')


def _norm_warehouse_name(value):
    return ' '.join(str(value or '').split()).casefold()


def _warehouse_id_by_name(settings, source, name):
    needle = _norm_warehouse_name(name)
    if not needle:
        return ''
    for row in MarketplaceWarehouse.objects.filter(source=source):
        if _norm_warehouse_name(row.name) == needle:
            return str(row.warehouse_id or '').strip()
    options = {source: _known_warehouse_ids(settings, source)}
    for (option_source, warehouse_id), warehouse_name in _known_warehouse_names(options).items():
        if option_source == source and _norm_warehouse_name(warehouse_name) == needle:
            return warehouse_id
    return ''


def _marketplace_stock_violation(product, total):
    available = _available(product)
    return {
        'productId': product.id,
        'name': product.name,
        'sku': product.sku or '',
        'available': float(available),
        'total': float(total),
        'excess': float(total - available),
    }


def _marketplace_stock_error(violations):
    first = violations[0]
    label = first['sku'] or first['name']
    suffix = f' Артикул: {label}.' if len(violations) == 1 else f' Ошибочных артикулов: {len(violations)}.'
    return (
        f'OZ + WB не могут быть больше остатка на складе по данным системы «Цехтакт». '
        f'Сейчас на складе {first["available"]}, получается {first["total"]}.'
        f'{suffix}'
    )


def _save_ozon_warehouses(rows):
    ids = []
    for row in rows:
        warehouse_id = str(row.get('warehouse_id') or '').strip()
        if not warehouse_id:
            continue
        MarketplaceWarehouse.objects.update_or_create(
            source=Shipment.Source.OZON,
            warehouse_id=warehouse_id,
            defaults={
                'name': row.get('name') or '',
                'status': row.get('status') or '',
                'is_rfbs': bool(row.get('is_rfbs')),
                'warehouse_type': row.get('warehouse_type') or '',
            },
        )
        if warehouse_id not in ids:
            ids.append(warehouse_id)
    return ids


def _cache_ozon_warehouses(settings):
    if not settings.ozon_configured:
        return
    rows = fetch_ozon_warehouses(settings.ozon_client_id, settings.ozon_api_key)
    if not any(row.get('status') == 'created' and row.get('warehouse_type') == 'fbs'
               and not row.get('is_rfbs') for row in rows):
        raise MarketplaceError('Ozon: API не вернул активных FBS-складов; обновление отменено')
    ids = _save_ozon_warehouses(rows)
    MarketplaceWarehouse.objects.filter(source=Shipment.Source.OZON).exclude(
        warehouse_id__in=ids,
    ).update(status='disabled')


def _source_label(source):
    return 'OZ' if source == Shipment.Source.OZON else 'WB'


def _upsert_market_stock(product, source, warehouse_id, quantity, user):
    MarketplaceStock.objects.update_or_create(
        product=product,
        source=source,
        warehouse_id=str(warehouse_id),
        defaults={
            'quantity': quantity,
            'updated_by': user if user and user.is_authenticated else None,
        },
    )


def _market_sum(product, patch_source=None, patch_quantity=None):
    total = Decimal(0)
    for row in MarketplaceStock.objects.filter(product=product):
        total += patch_quantity if row.source == patch_source else (row.quantity or Decimal(0))
    if patch_source and not MarketplaceStock.objects.filter(product=product, source=patch_source).exists():
        total += patch_quantity or Decimal(0)
    return total


def _market_sum_with_patches(product, patches):
    total = Decimal(0)
    seen = set()
    for row in MarketplaceStock.objects.filter(product=product):
        key = (product.id, row.source, row.warehouse_id)
        total += patches.get(key, row.quantity or Decimal(0))
        seen.add(key)
    for key, quantity in patches.items():
        product_id, _source, _warehouse_id = key
        if product_id == product.id and key not in seen:
            total += quantity
    return total


def _available(product):
    stock = getattr(product, 'stock', None)
    return stock.available_quantity if stock else Decimal(0)


def _market_offer_id(product, source):
    alias = MarketplaceProductAlias.objects.filter(
        source=source, product=product,
    ).order_by('-created_at', '-id').first()
    return alias.offer_id if alias else product.sku


def _ozon_offer_catalog(settings):
    catalog = defaultdict(set)
    for offer_id in fetch_ozon_offer_ids(settings.ozon_client_id, settings.ozon_api_key):
        catalog[_norm_sku(offer_id)].add(offer_id)
    return catalog


def _resolve_ozon_offer_id(catalog, offer_id):
    matches = catalog.get(_norm_sku(offer_id), ())
    if offer_id in matches:
        return offer_id
    if len(matches) == 1:
        return next(iter(matches))
    return ''


def _ozon_offer_ids(settings):
    values = set(Product.objects.filter(archived=False).exclude(sku='').values_list('sku', flat=True))
    values.update(MarketplaceProductAlias.objects.filter(
        source=Shipment.Source.OZON,
    ).exclude(offer_id='').values_list('offer_id', flat=True))
    catalog = _ozon_offer_catalog(settings)
    resolved = {_resolve_ozon_offer_id(catalog, value) for value in values}
    return sorted(resolved - {''})


def _send_ozon_stock(settings, catalog, warehouse_id, offer_id, quantity):
    exact_offer = _resolve_ozon_offer_id(catalog, offer_id)
    if not exact_offer:
        raise MarketplaceError(f'Ozon: в каталоге нет однозначного артикула для «{offer_id}»')
    update_ozon_fbs_stock(settings.ozon_client_id, settings.ozon_api_key, warehouse_id, exact_offer, quantity)


def _env_file_value(path, key):
    try:
        lines = Path(path).read_text().splitlines()
    except OSError:
        return ''
    prefix = f'{key}='
    for line in lines:
        line = line.strip()
        if not line or line.startswith('#') or not line.startswith(prefix):
            continue
        return line[len(prefix):].strip().strip('"').strip("'")
    return ''


def _wb_internal_token():
    return (
        os.environ.get('WB_INTERNAL_API_TOKEN')
        or _env_file_value(os.environ.get('WB_INTERNAL_ENV_FILE', '/etc/ozon-dashboard.env'), 'WB_INTERNAL_API_TOKEN')
    )


def update_wb_internal_stocks(items):
    token = _wb_internal_token()
    if not token:
        raise MarketplaceError('WB_INTERNAL_API_TOKEN не задан')
    url = os.environ.get(
        'WB_INTERNAL_STOCKS_UPDATE_URL',
        'http://127.0.0.1:18088/api/internal/wb/stocks/update',
    )
    return _request(url, {'X-Internal-Token': token}, {'apply': True, 'items': items})


class MarketplaceStockSyncView(APIView):
    """POST /api/stock/marketplaces/sync/ — получить FBS-остатки с настроенных складов."""

    def post(self, request):
        s = MarketplaceSettings.load()
        updated = {'ozon': 0, 'wb': 0}
        unmatched = []
        errors = []

        if s.ozon_configured:
            try:
                _cache_ozon_warehouses(s)
                count, miss = self._sync_source(
                    Shipment.Source.OZON,
                    fetch_ozon_fbs_stocks(
                        s.ozon_client_id,
                        s.ozon_api_key,
                        _ozon_offer_ids(s),
                        _known_warehouse_ids(s, Shipment.Source.OZON, include_catalog=True),
                    ),
                    None,
                    request.user,
                )
                updated['ozon'] = count
                unmatched.extend(miss)
            except MarketplaceError as e:
                errors.append({'source': Shipment.Source.OZON, 'message': str(e)})
        if s.wb_configured:
            try:
                count, miss = self._sync_source(
                    Shipment.Source.WB,
                    fetch_wb_analytics_fbs_stocks(s.wb_token),
                    None,
                    request.user,
                )
                updated['wb'] = count
                unmatched.extend(miss)
            except MarketplaceError as e:
                errors.append({'source': Shipment.Source.WB, 'message': str(e)})

        if not s.ozon_configured and not s.wb_configured:
            return Response({'error': 'Укажите ключи Ozon или токен WB в настройках интеграций'}, status=400)
        return Response({'updated': updated, 'unmatched': unmatched, 'errors': errors})

    @transaction.atomic
    def _sync_source(self, source, rows, warehouse_ids, user):
        updated_products = set()
        unmatched = []
        selected = None
        if warehouse_ids:
            selected = {str(value) for value in warehouse_ids} if isinstance(warehouse_ids, set) else {str(warehouse_ids)}
        if selected:
            MarketplaceStock.objects.filter(source=source, warehouse_id__in=selected).delete()
        else:
            MarketplaceStock.objects.filter(source=source).delete()
        for row in rows:
            warehouse_id = str(row.get('warehouse_id') or next(iter(selected or []), '')).strip()
            if not warehouse_id:
                warehouse_id = _warehouse_id_by_name(MarketplaceSettings.load(), source, row.get('warehouse_name') or '')
            if selected and warehouse_id not in selected:
                continue
            sku = row.get('sku') or ''
            product = _match_product(source, sku)
            quantity = Decimal(str(row.get('quantity') or 0))
            if product is None:
                if sku and quantity > 0:
                    unmatched.append({
                        'source': source,
                        'sku': sku,
                        'name': row.get('name') or '',
                        'quantity': float(quantity),
                        'warehouseId': warehouse_id,
                    })
                continue
            _upsert_market_stock(product, source, warehouse_id, quantity, user)
            updated_products.add(product.id)
        return len(updated_products), unmatched


class MarketplaceStockUpdateView(APIView):
    """PATCH /api/stock/<product_id>/marketplace-stock/ {source, quantity}."""

    def patch(self, request, product_id):
        product = Product.objects.select_related('stock').filter(pk=product_id).first()
        if not product:
            return Response({'error': 'товар не найден'}, status=404)
        body = request.data if isinstance(request.data, dict) else {}
        source = body.get('source')
        if source not in {Shipment.Source.OZON, Shipment.Source.WB}:
            return Response({'error': 'source должен быть ozon или wb'}, status=400)
        quantity, error = _parse_quantity(body.get('quantity'))
        if error:
            return Response({'error': error}, status=400)
        total = _market_sum(product, source, quantity)
        if total > _available(product):
            violation = _marketplace_stock_violation(product, total)
            return Response({
                'error': _marketplace_stock_error([violation]),
                'violations': [violation],
            }, status=409)

        s = MarketplaceSettings.load()
        warehouse_id = _warehouse_id(s, source, product)
        if not warehouse_id:
            if source == Shipment.Source.OZON:
                return Response({'error': 'OZ суммируется по всем складам Ozon. Ручное изменение доступно только когда у товара один склад Ozon.'}, status=409)
            return Response({'error': f'Укажите склад {_source_label(source)} в настройках интеграций'}, status=400)
        try:
            if source == Shipment.Source.OZON:
                if not s.ozon_configured:
                    return Response({'error': 'Не заданы ключи Ozon'}, status=400)
                _send_ozon_stock(s, _ozon_offer_catalog(s), warehouse_id, _market_offer_id(product, source), quantity)
            else:
                if not s.wb_configured:
                    return Response({'error': 'Не задан токен Wildberries'}, status=400)
                update_wb_fbs_stock(s.wb_token, warehouse_id, _market_offer_id(product, source), quantity)
        except MarketplaceError as e:
            return Response({'error': str(e)}, status=502)
        _upsert_market_stock(product, source, warehouse_id, quantity, request.user)
        return Response({'productId': product.id, 'source': source, 'quantity': float(quantity)})


class MarketplaceStockPushView(APIView):
    """POST /api/stock/marketplaces/push/ — отправить черновые FBS-остатки в Ozon и WB."""

    def post(self, request):
        body = request.data if isinstance(request.data, dict) else {}
        raw_items = body.get('items')
        if not isinstance(raw_items, list) or not raw_items:
            return Response({'error': 'Нет остатков для отправки'}, status=400)

        settings = MarketplaceSettings.load()
        products = {}
        patches = {}
        prepared = []
        for raw in raw_items:
            if not isinstance(raw, dict):
                return Response({'error': 'Некорректная строка остатка'}, status=400)
            source = raw.get('source')
            if source not in {Shipment.Source.OZON, Shipment.Source.WB}:
                return Response({'error': 'source должен быть ozon или wb'}, status=400)
            product_id = raw.get('productId')
            product = products.get(product_id)
            if product is None:
                product = Product.objects.select_related('stock').filter(pk=product_id).first()
                if not product:
                    return Response({'error': 'товар не найден'}, status=404)
                products[product_id] = product
            quantity, error = _parse_quantity(raw.get('quantity'))
            if error:
                return Response({'error': error}, status=400)
            warehouse_id = str(raw.get('warehouseId') or '').strip()
            if source == Shipment.Source.OZON:
                warehouse_id = warehouse_id or _warehouse_id(settings, source, product)
                if not warehouse_id:
                    return Response({'error': 'Выберите склад Ozon для отправки остатка'}, status=400)
                known_ids = _known_warehouse_ids(settings, source)
                if known_ids and warehouse_id not in known_ids:
                    return Response({'error': 'Неизвестный склад Ozon'}, status=400)
            else:
                warehouse_id = warehouse_id or settings.wb_warehouse_id or ''
                if not settings.wb_configured:
                    return Response({'error': 'Не задан токен Wildberries'}, status=400)
                if not warehouse_id:
                    return Response({'error': 'Выберите склад WB для отправки остатка'}, status=400)
            patches[(product.id, source, warehouse_id)] = quantity
            prepared.append({
                'product': product,
                'source': source,
                'warehouse_id': warehouse_id,
                'quantity': quantity,
            })

        violations = []
        for product in products.values():
            total = _market_sum_with_patches(product, patches)
            if total > _available(product):
                violations.append(_marketplace_stock_violation(product, total))
        if violations:
            return Response({
                'error': _marketplace_stock_error(violations),
                'violations': violations,
            }, status=409)

        ozon_items = []
        wb_items = []
        for item in prepared:
            product = item['product']
            source = item['source']
            warehouse_id = item['warehouse_id']
            quantity = item['quantity']
            offer_id = _market_offer_id(product, source)
            if source == Shipment.Source.OZON:
                if not settings.ozon_configured:
                    return Response({'error': 'Не заданы ключи Ozon'}, status=400)
                if not warehouse_id:
                    return Response({'error': 'OZ суммируется по всем складам Ozon. Отправка доступна только когда у товара один склад Ozon.'}, status=409)
                if not str(offer_id or '').strip():
                    return Response({'error': f'Укажите артикул Ozon для «{product.name}»'}, status=400)
                ozon_items.append((product, warehouse_id, offer_id, quantity))
            else:
                wb_items.append((product, warehouse_id, offer_id, quantity))

        sent = {'ozon': 0, 'wb': 0}
        errors = []
        if ozon_items:
            try:
                catalog = _ozon_offer_catalog(settings)
            except MarketplaceError as e:
                errors.append({'source': Shipment.Source.OZON, 'message': str(e)})
            else:
                for product, warehouse_id, offer_id, quantity in ozon_items:
                    try:
                        _send_ozon_stock(settings, catalog, warehouse_id, offer_id, quantity)
                        _upsert_market_stock(product, Shipment.Source.OZON, warehouse_id, quantity, request.user)
                        sent['ozon'] += 1
                    except MarketplaceError as e:
                        errors.append({'source': Shipment.Source.OZON, 'sku': offer_id, 'message': str(e)})

        for product, warehouse_id, offer_id, quantity in wb_items:
            try:
                update_wb_fbs_stock(settings.wb_token, warehouse_id, offer_id, quantity)
                _upsert_market_stock(product, Shipment.Source.WB, warehouse_id, quantity, request.user)
                sent['wb'] += 1
            except MarketplaceError as e:
                errors.append({'source': Shipment.Source.WB, 'sku': offer_id, 'message': str(e)})

        status = 207 if errors and (sent['ozon'] or sent['wb']) else 502 if errors else 200
        return Response({'sent': sent, 'errors': errors}, status=status)


class MarketplaceStockBindView(APIView):
    """POST /api/stock/marketplaces/bind/ — сопоставить артикул маркета с товаром."""

    def post(self, request):
        body = request.data if isinstance(request.data, dict) else {}
        source = body.get('source')
        if source not in {Shipment.Source.OZON, Shipment.Source.WB}:
            return Response({'error': 'source должен быть ozon или wb'}, status=400)
        sku = (body.get('sku') or '').strip()
        product = Product.objects.filter(pk=body.get('productId')).first()
        if not sku or not product:
            return Response({'error': 'Укажите артикул и товар из системы «Цехтакт»'}, status=400)
        quantity, error = _parse_quantity(body.get('quantity', 0))
        if error:
            return Response({'error': error}, status=400)
        s = MarketplaceSettings.load()
        warehouse_id = str(body.get('warehouseId') or '').strip() or _warehouse_id(s, source, product)
        if not warehouse_id:
            warehouse_id = ''
        MarketplaceProductAlias.objects.update_or_create(
            source=source,
            offer_id=_norm_sku(sku),
            defaults={'product': product,
                      'created_by': request.user if request.user.is_authenticated else None},
        )
        if not (product.sku or '').strip():
            product.sku = _norm_sku(sku)
            product.save(update_fields=['sku'])
        _upsert_market_stock(product, source, warehouse_id, quantity, request.user)
        return Response({'productId': product.id, 'source': source, 'sku': sku, 'quantity': float(quantity)})
