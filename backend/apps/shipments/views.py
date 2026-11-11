"""HTTP API отгрузок. Паттерн вьюх — как в apps/sales."""
import logging
from datetime import timedelta
from decimal import Decimal, InvalidOperation

from django.http import HttpResponse
from django.db import IntegrityError
from django.utils import timezone
from rest_framework import generics
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.catalog.models import Product
from .labels import (
    build_ozon_assembly_picking_list_pdf, build_picking_list_pdf, build_wb_pdf,
    build_wb_picking_list_pdf,
    fetch_ozon_label_codes, fetch_ozon_label_pdf, fetch_wb_stickers, fetch_wb_stickers_with_barcodes,
    fetch_wb_trbx_stickers,
)
from .marketplaces import (
    MarketplaceError, MarketplaceRateLimitError, fetch_ozon_assembly_postings, fetch_ozon_carriages,
    fetch_ozon_posting_by_barcode, fetch_ozon_postings, fetch_ozon_return_pickup_barcode,
    fetch_ozon_returns, fetch_ozon_warehouses,
    fetch_wb_orders, fetch_wb_returns,
)
from .models import MarketplaceSettings, MarketplaceWarehouse, Shipment, ShipmentScan
from .serializers import ShipmentDetailSerializer, ShipmentRowSerializer
from .services import (
    RETURN_NOTE, bind_line, can_delete_shipment, collect_ship_warnings, import_rows, is_target_warehouse,
    delete_shipment, next_shipment_name, receive_return_shipment, replace_lines,
    schedule_wb_returns_retry, shipment_warehouse, ship_shipment, sync_ozon_shipment,
    TARGET_WAREHOUSE,
)
from .writeback import (
    assemble_shipment, handover_shipment,
    create_supply_for_shipment, create_trbx_for_shipment,
)

logger = logging.getLogger(__name__)

_SHIPPED_LOCKED = {'error': 'Отгруженную заявку нельзя изменить'}


def _get(pk):
    return Shipment.objects.prefetch_related('lines__product', 'lines__scans').filter(pk=pk).first()


def _normalize_scan_code(value):
    return ' '.join(str(value or '').strip().lstrip('*!').split()).casefold()


def _line_scan_code_pairs(line):
    """Коды, которыми можно найти строку отгрузки при сверке."""
    if line.shipment.source == Shipment.Source.OZON:
        return [
            (ShipmentScan.Kind.ITEM, line.external_label_barcode),
            (ShipmentScan.Kind.ITEM, getattr(line, 'external_label_code', '')),
            (ShipmentScan.Kind.ITEM, line.external_posting),
        ]
    if line.shipment.source == Shipment.Source.WB:
        return [
            (ShipmentScan.Kind.ITEM, line.external_label_barcode),
            (ShipmentScan.Kind.ITEM, getattr(line, 'external_label_code', '')),
            (ShipmentScan.Kind.ITEM, line.external_posting),
            (ShipmentScan.Kind.CARGO, line.external_trbx_barcode),
            (ShipmentScan.Kind.CARGO, line.external_trbx_id),
        ]
    product_sku = line.product.sku if line.product_id else ''
    return [
        (ShipmentScan.Kind.ITEM, product_sku),
        (ShipmentScan.Kind.ITEM, line.sku_raw),
    ]


def _find_scan_line(shipment, normalized_code):
    candidates = []
    for line in shipment.lines.filter(deleted=False).select_related('product', 'shipment').prefetch_related('scans'):
        for kind, code in _line_scan_code_pairs(line):
            if code and _normalize_scan_code(code) == normalized_code:
                candidates.append((line, kind))
    for line, kind in candidates:
        if not line.scans.filter(kind=kind).exists():
            return line, kind
    return candidates[0] if candidates else (None, '')


def _matched_line_code(line, normalized_code, fallback):
    for _, code in _line_scan_code_pairs(line):
        if code and _normalize_scan_code(code) == normalized_code:
            return ' '.join(str(code).strip().split())
    return fallback


def _save_wb_trbx_barcodes(lines, stickers):
    for line, code in zip(lines, stickers.keys()):
        code = ' '.join(str(code or '').strip().split())
        if not code or code == line.external_trbx_id or code == line.external_trbx_barcode:
            continue
        line.external_trbx_barcode = code
        line.save(update_fields=['external_trbx_barcode'])


def _save_wb_label_barcodes(shipment, barcodes, label_codes=None):
    if not barcodes and not label_codes:
        return
    for line in shipment.lines.filter(deleted=False):
        code = ' '.join(str(barcodes.get(line.external_posting) or '').strip().split())
        label_code = ' '.join(str((label_codes or {}).get(line.external_posting) or '').strip().split())
        update_fields = []
        if code and code != line.external_label_barcode:
            line.external_label_barcode = code
            update_fields.append('external_label_barcode')
        if label_code and label_code != getattr(line, 'external_label_code', ''):
            line.external_label_code = label_code
            update_fields.append('external_label_code')
        if update_fields:
            line.save(update_fields=update_fields)


def _find_ozon_scan_line_by_barcode(shipment, clean_code):
    settings = MarketplaceSettings.load()
    if not settings.ozon_configured:
        return None, ''
    try:
        posting = fetch_ozon_posting_by_barcode(
            settings.ozon_client_id,
            settings.ozon_api_key,
            clean_code,
        )
    except MarketplaceError:
        return None, ''
    posting_number = ' '.join(str(posting.get('posting_number') or '').strip().split())
    if not posting_number:
        return None, ''
    line = (shipment.lines
            .filter(deleted=False, external_posting=posting_number)
            .prefetch_related('scans')
            .order_by('id')
            .first())
    if not line:
        return None, ''
    barcodes = posting.get('barcodes') or {}
    label_code = ' '.join(str(
        barcodes.get('lower_barcode')
        or barcodes.get('upper_barcode')
        or clean_code
    ).strip().split())
    if label_code and line.external_label_barcode != label_code:
        line.external_label_barcode = label_code
        line.save(update_fields=['external_label_barcode'])
    return line, ShipmentScan.Kind.ITEM


def _as_bool(value):
    """force — защитный барьер: срабатывает только на настоящий True или 'true'/'1'."""
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in ('true', '1')
    return False


def _validate_name_note(body):
    """Проверить типы name/note (если переданы). Возвращает сообщение об ошибке или None."""
    name = body.get('name')
    if name is not None and not isinstance(name, str):
        return 'name должен быть строкой'
    note = body.get('note')
    if note is not None and not isinstance(note, str):
        return 'note должен быть строкой'
    return None


def _validate_lines(raw):
    """Проверить и очистить тело PUT .../lines/. Возвращает (rows, error)."""
    if not isinstance(raw, list):
        return None, 'lines должен быть списком'

    rows = []
    for row in raw:
        if not isinstance(row, dict):
            return None, 'каждая строка состава должна быть объектом'

        quantity_raw = row.get('quantity')
        if not quantity_raw:
            quantity = Decimal(0)
        else:
            try:
                quantity = Decimal(str(quantity_raw))
            except (InvalidOperation, ValueError, TypeError):
                return None, f'некорректное количество: {quantity_raw!r}'

        product_id = row.get('productId') or None
        if product_id is not None:
            try:
                exists = Product.objects.filter(pk=product_id).exists()
            except (ValueError, TypeError):
                return None, f'некорректный productId: {product_id!r}'
            if not exists:
                return None, f'товар с id {product_id} не найден'

        sku = (row.get('sku') or '').strip()
        name = (row.get('name') or '').strip()
        # Guard против пустых НОВЫХ строк: если id нет и нечем её опознать
        # (ни productId, ни sku, ни name) — не создаём. У существующей строки
        # (есть id) идентификатор уже есть сам по себе — не отбрасываем её
        # только из-за того, что фронт не прислал sku/name по проводу.
        if row.get('id') is None and product_id is None and not sku and not name:
            continue

        rows.append({
            'id': row.get('id'),
            'productId': product_id,
            'quantity': quantity,
            'sku': sku,
            'name': name,
        })
    return rows, None


class ShipmentListView(generics.ListAPIView):
    """GET /api/shipments/ — список заявок."""
    serializer_class = ShipmentRowSerializer
    pagination_class = None

    def get_queryset(self):
        return Shipment.objects.prefetch_related('lines__product')

    def post(self, request):
        """POST /api/shipments/ — создать пустую ручную заявку."""
        body = request.data if isinstance(request.data, dict) else {}
        error = _validate_name_note(body)
        if error:
            return Response({'error': error}, status=400)
        shipment = Shipment.objects.create(
            name=(body.get('name') or '').strip() or next_shipment_name(),
            note=(body.get('note') or '').strip(),
            created_by=request.user if request.user.is_authenticated else None,
        )
        return Response(ShipmentDetailSerializer(shipment).data, status=201)


class OzonDeliveryCheckView(APIView):
    """Сверить закрытые в системе «Цехтакт» Ozon-заявки с ещё неотгруженными в Ozon."""

    def post(self, request):
        settings = MarketplaceSettings.load()
        if not settings.ozon_configured:
            return Response({'error': 'Не заданы ключи Ozon'}, status=400)
        try:
            rows = fetch_ozon_postings(settings.ozon_client_id, settings.ozon_api_key)
        except MarketplaceError as error:
            return Response({'error': str(error)}, status=502)

        awaiting = {
            str(row.get('external_posting') or '').strip()
            for row in rows
            if row.get('external_status') in {'awaiting_packaging', 'awaiting_deliver'}
        }
        shipments = list(
            Shipment.objects.filter(source=Shipment.Source.OZON, status=Shipment.Status.SHIPPED)
            .exclude(note=RETURN_NOTE)
            .prefetch_related('lines')
        )
        flagged = 0
        for shipment in shipments:
            missing = sorted({
                line.external_posting
                for line in shipment.lines.all()
                if not line.deleted and line.external_posting in awaiting
            })
            if missing:
                flagged += 1
            if shipment.ozon_unaccepted_postings != missing:
                shipment.ozon_unaccepted_postings = missing
                shipment.save(update_fields=['ozon_unaccepted_postings'])
        return Response({
            'checkedShipments': len(shipments),
            'flaggedShipments': flagged,
        })


class ShipmentDetailView(APIView):
    """GET/PATCH/DELETE /api/shipments/<pk>/."""

    def get(self, request, pk):
        shipment = _get(pk)
        if not shipment:
            return Response({'error': 'не найдено'}, status=404)
        return Response(ShipmentDetailSerializer(shipment).data)

    def patch(self, request, pk):
        shipment = _get(pk)
        if not shipment:
            return Response({'error': 'не найдено'}, status=404)
        if shipment.status == Shipment.Status.SHIPPED:
            return Response(_SHIPPED_LOCKED, status=409)
        body = request.data if isinstance(request.data, dict) else {}
        error = _validate_name_note(body)
        if error:
            return Response({'error': error}, status=400)

        update_fields = []
        if 'name' in body:
            name = (body.get('name') or '').strip()
            if name and name != shipment.name:
                shipment.name = name
                update_fields.append('name')
        if 'note' in body and body.get('note') is not None:
            note = body.get('note').strip()
            if note != shipment.note:
                shipment.note = note
                update_fields.append('note')
        if update_fields:
            shipment.save(update_fields=update_fields)
        return Response(ShipmentDetailSerializer(shipment).data)

    def delete(self, request, pk):
        shipment = _get(pk)
        if not shipment:
            return Response({'error': 'не найдено'}, status=404)
        if not can_delete_shipment(shipment):
            return Response({'error': 'Эту заявку уже нельзя удалить из интерфейса'}, status=409)
        delete_shipment(shipment, request.user)
        return Response(status=204)


class ShipmentLinesView(APIView):
    """PUT /api/shipments/<pk>/lines/ {lines:[…]} — заменить состав целиком."""

    def put(self, request, pk):
        shipment = _get(pk)
        if not shipment:
            return Response({'error': 'не найдено'}, status=404)
        if shipment.status == Shipment.Status.SHIPPED:
            return Response(_SHIPPED_LOCKED, status=409)
        body = request.data if isinstance(request.data, dict) else {}
        if 'lines' not in body:
            return Response({'error': 'нет поля lines — для очистки состава передайте lines: []'}, status=400)
        rows, error = _validate_lines(body.get('lines'))
        if error:
            return Response({'error': error}, status=400)
        replace_lines(shipment, rows)
        return Response(ShipmentDetailSerializer(_get(pk)).data)


class ShipmentLineBindView(APIView):
    """POST /api/shipments/<pk>/lines/<line_id>/bind/ {productId}.

    Привязать несопоставленную строку маркетплейса к товару каталога и запомнить
    связь (алиас), чтобы следующий импорт находил товар сам. Право — update
    (правка существующей заявки), см. apps/directory/permissions._FORCE_ACTION.
    """

    def post(self, request, pk, line_id):
        shipment = _get(pk)
        if not shipment:
            return Response({'error': 'не найдено'}, status=404)
        if shipment.status == Shipment.Status.SHIPPED:
            return Response(_SHIPPED_LOCKED, status=409)
        line = shipment.lines.filter(pk=line_id, deleted=False).first()
        if not line:
            return Response({'error': 'строка не найдена'}, status=404)
        product_id = request.data.get('productId') if isinstance(request.data, dict) else None
        product = Product.objects.filter(pk=product_id).first() if product_id else None
        if not product:
            return Response({'error': 'товар не найден'}, status=400)
        bind_line(shipment, line, product, request.user)
        return Response(ShipmentDetailSerializer(_get(pk)).data)


class ShipmentShipView(APIView):
    """POST /api/shipments/<pk>/ship/ {force} — списать со склада и закрыть заявку."""

    def post(self, request, pk):
        shipment = _get(pk)
        if not shipment:
            return Response({'error': 'не найдено'}, status=404)
        if shipment.status == Shipment.Status.SHIPPED:
            return Response({'error': 'Заявка уже отгружена'}, status=409)
        body = request.data if isinstance(request.data, dict) else {}
        if shipment.note == RETURN_NOTE:
            return Response({'error': 'Возврат нельзя отгружать как обычную отгрузку'}, status=409)
        warnings = collect_ship_warnings(shipment)
        if warnings and not _as_bool(body.get('force')):
            return Response({'warnings': warnings}, status=409)
        ship_shipment(shipment, request.user)
        return Response(ShipmentDetailSerializer(_get(pk)).data)


class ShipmentReturnReceiveView(APIView):
    """POST /api/shipments/<pk>/return-receive/ {action} - receive marketplace return."""

    def post(self, request, pk):
        shipment = _get(pk)
        if not shipment:
            return Response({'error': 'РЅРµ РЅР°Р№РґРµРЅРѕ'}, status=404)
        body = request.data if isinstance(request.data, dict) else {}
        try:
            result = receive_return_shipment(shipment, request.user, body.get('action'))
        except ValueError as exc:
            return Response({'error': str(exc)}, status=409)
        return Response({
            **ShipmentDetailSerializer(_get(pk)).data,
            'returnReceive': result,
        })


class ShipmentScanView(APIView):
    """POST /api/shipments/<pk>/scan/ {code} — отметить строку как просканированную."""

    def post(self, request, pk):
        shipment = _get(pk)
        if not shipment:
            return Response({'error': 'не найдено'}, status=404)
        if shipment.status == Shipment.Status.SHIPPED:
            return Response(_SHIPPED_LOCKED, status=409)
        body = request.data if isinstance(request.data, dict) else {}
        code = body.get('code')
        if not isinstance(code, str) or not code.strip():
            return Response({'error': 'code должен быть непустой строкой'}, status=400)
        clean_code = ' '.join(code.strip().split())
        normalized_code = _normalize_scan_code(clean_code)

        existing = ShipmentScan.objects.filter(
            shipment=shipment, normalized_code=normalized_code,
        ).select_related('line').first()
        if existing:
            return Response({
                'status': 'duplicate',
                'lineId': existing.line_id,
                'code': existing.code,
                'message': 'Код уже отсканирован',
                **_scan_counts(shipment),
            })

        line, kind = _find_scan_line(shipment, normalized_code)
        if not line and shipment.source == Shipment.Source.OZON:
            line, kind = _find_ozon_scan_line_by_barcode(shipment, clean_code)
        if not line:
            return Response({
                'status': 'not_found',
                'code': clean_code,
                'message': 'Код не найден в этой отгрузке',
                **_scan_counts(shipment),
            }, status=404)
        stored_code = _matched_line_code(line, normalized_code, clean_code)
        kind_existing = line.scans.filter(kind=kind).first()
        if kind_existing:
            return Response({
                'status': 'duplicate',
                'lineId': line.id,
                'code': kind_existing.code,
                'message': 'Код уже отсканирован',
                **_scan_counts(shipment),
            })

        try:
            ShipmentScan.objects.create(
                shipment=shipment,
                line=line,
                kind=kind,
                code=stored_code,
                normalized_code=normalized_code,
                scanned_by=request.user if request.user.is_authenticated else None,
            )
        except IntegrityError:
            existing = ShipmentScan.objects.filter(
                shipment=shipment, normalized_code=normalized_code,
            ).first()
            return Response({
                'status': 'duplicate',
                'lineId': existing.line_id if existing else line.id,
                'code': clean_code,
                'message': 'Код уже отсканирован',
                **_scan_counts(shipment),
            })

        shipment.scan_finished_at = None
        shipment.save(update_fields=['scan_finished_at'])
        return Response({
            'status': 'scanned',
            'lineId': line.id,
            'code': stored_code,
            'message': 'Отсканировано',
            **_scan_counts(shipment),
        })


def _scan_counts(shipment):
    lines = list(shipment.lines.filter(deleted=False))
    required = {
        (line.id, kind)
        for line in lines
        for kind in _required_scan_kinds(line)
    }
    scanned = len({
        (scan.line_id, scan.kind)
        for scan in ShipmentScan.objects.filter(shipment=shipment, line__deleted=False)
        if (scan.line_id, scan.kind) in required
    })
    total = len(required)
    return {'total': total, 'scanned': scanned, 'missing': max(total - scanned, 0)}


def _required_scan_kinds(line):
    if line.shipment.source == Shipment.Source.WB and (line.external_trbx_id or line.external_trbx_barcode):
        return [ShipmentScan.Kind.ITEM, ShipmentScan.Kind.CARGO]
    return [ShipmentScan.Kind.ITEM]


class ShipmentScanFinishView(APIView):
    """POST /api/shipments/<pk>/scan/finish/ — завершить сверку и показать остатки."""

    def post(self, request, pk):
        shipment = _get(pk)
        if not shipment:
            return Response({'error': 'не найдено'}, status=404)
        if shipment.status == Shipment.Status.SHIPPED:
            return Response(_SHIPPED_LOCKED, status=409)
        shipment.scan_finished_at = timezone.now()
        shipment.save(update_fields=['scan_finished_at'])
        return Response(_scan_counts(shipment))


def _marketplace_keys_or_error(shipment):
    """(client_id, api_key, wb_token) либо Response с ошибкой."""
    s = MarketplaceSettings.load()
    if shipment.source == Shipment.Source.OZON:
        if not s.ozon_configured:
            return None, Response({'error': 'Не заданы ключи Ozon'}, status=400)
    elif shipment.source == Shipment.Source.WB:
        if not s.wb_configured:
            return None, Response({'error': 'Не задан токен Wildberries'}, status=400)
    else:
        return None, Response({'error': 'У ручной заявки нет отправлений'}, status=409)
    return (s.ozon_client_id, s.ozon_api_key, s.wb_token), None


def _wb_token_or_error(shipment):
    """Токен WB либо Response с ошибкой. Действия поставки — только для WB."""
    if shipment.source != Shipment.Source.WB:
        return None, Response(
            {'error': 'Это действие только для поставок Wildberries'}, status=409)
    s = MarketplaceSettings.load()
    if not s.wb_configured:
        return None, Response({'error': 'Не задан токен Wildberries'}, status=400)
    return s.wb_token, None


class ShipmentSupplyView(APIView):
    """POST /api/shipments/<pk>/supply/ — создать поставку WB (шаг «Создать поставку»)."""

    def post(self, request, pk):
        shipment = _get(pk)
        if not shipment:
            return Response({'error': 'не найдено'}, status=404)
        token, err = _wb_token_or_error(shipment)
        if err:
            return err
        try:
            result = create_supply_for_shipment(shipment, token)
        except Exception:
            logger.exception('Создание поставки заявки %s: неожиданная ошибка', shipment.id)
            return Response({'error': 'Не удалось создать поставку'}, status=502)
        if result['status'] == 'error':
            return Response({'error': result['message']}, status=502)
        return Response(result)


class ShipmentTrbxView(APIView):
    """POST /api/shipments/<pk>/trbx/ — создать грузоместа (1 короб = 1 отправление)."""

    def post(self, request, pk):
        shipment = _get(pk)
        if not shipment:
            return Response({'error': 'не найдено'}, status=404)
        token, err = _wb_token_or_error(shipment)
        if err:
            return err
        try:
            return Response(create_trbx_for_shipment(shipment, token))
        except Exception:
            logger.exception('Грузоместа заявки %s: неожиданная ошибка', shipment.id)
            return Response({'error': 'Не удалось создать грузоместа'}, status=502)


class ShipmentTrbxStickersView(APIView):
    """GET /api/shipments/<pk>/trbx/stickers/ — PDF с QR грузомест одним файлом."""

    def get(self, request, pk):
        shipment = _get(pk)
        if not shipment:
            return Response({'error': 'не найдено'}, status=404)
        token, err = _wb_token_or_error(shipment)
        if err:
            return err
        trbx_ids = []
        trbx_lines = []
        for line in shipment.lines.filter(deleted=False):
            if line.external_trbx_id and line.external_trbx_id not in trbx_ids:
                trbx_ids.append(line.external_trbx_id)
                trbx_lines.append(line)
        if not trbx_ids:
            return Response({'error': 'Сначала создайте грузоместа'}, status=409)
        try:
            stickers = fetch_wb_trbx_stickers(token, shipment.external_supply_id, trbx_ids)
            _save_wb_trbx_barcodes(trbx_lines, stickers)
            pdf = build_wb_pdf(stickers)
        except MarketplaceError as e:
            return Response({'error': str(e)}, status=502)
        except Exception:
            logger.exception('QR грузомест заявки %s: неожиданная ошибка', shipment.id)
            return Response({'error': 'Не удалось получить QR грузомест'}, status=502)
        resp = HttpResponse(pdf, content_type='application/pdf')
        resp['Content-Disposition'] = f'attachment; filename="trbx-{shipment.id}.pdf"'
        return resp


class ShipmentAssembleView(APIView):
    """POST /api/shipments/<pk>/assemble/ — собрать отправления в маркетплейсе.

    Необратимо меняет состояние реальных заказов покупателей.
    """

    def post(self, request, pk):
        shipment = _get(pk)
        if not shipment:
            return Response({'error': 'не найдено'}, status=404)
        keys, err = _marketplace_keys_or_error(shipment)
        if err:
            return err
        body = request.data if isinstance(request.data, dict) else {}
        assemble_stage = {
            Shipment.Source.OZON: 'awaiting_packaging',
            Shipment.Source.WB: 'new',
        }.get(shipment.source)
        assemble_lines = [
            line for line in shipment.lines.all()
            if not line.deleted
            and line.external_status == assemble_stage
            and is_target_warehouse(
                line.external_warehouse,
                allow_blank=shipment.source != Shipment.Source.OZON,
            )
        ]
        stock_warnings = [
            w for w in collect_ship_warnings(shipment, assemble_lines)
            if w.get('kind') == 'shortage'
        ]
        if stock_warnings and not _as_bool(body.get('force')):
            return Response({'warnings': stock_warnings}, status=409)
        try:
            return Response(assemble_shipment(shipment, *keys))
        except Exception:
            logger.exception('Сборка заявки %s: неожиданная ошибка', shipment.id)
            return Response({'error': 'Не удалось выполнить сборку'}, status=502)


class ShipmentHandoverView(APIView):
    """POST /api/shipments/<pk>/handover/ — передать собранное к отгрузке."""

    def post(self, request, pk):
        shipment = _get(pk)
        if not shipment:
            return Response({'error': 'не найдено'}, status=404)
        keys, err = _marketplace_keys_or_error(shipment)
        if err:
            return err
        try:
            result = handover_shipment(shipment, *keys)
        except Exception:
            logger.exception('Передача заявки %s: неожиданная ошибка', shipment.id)
            return Response({'error': 'Не удалось передать к отгрузке'}, status=502)
        if result['status'] == 'nothing':
            return Response({'error': 'Нет собранных отправлений'}, status=409)
        if result['status'] == 'error':
            return Response({'error': result['message']}, status=502)
        return Response(result)


def _mask(value):
    """Секрет наружу — только хвостом: ••••3456. Короткие значения (<=4) — целиком маска."""
    if not value:
        return ''
    if len(value) <= 4:
        return '••••'
    return '••••' + value[-4:]


def _warehouse_payload(row):
    return {
        'id': row.warehouse_id,
        'name': row.name,
        'status': row.status,
        'isRfbs': row.is_rfbs,
        'warehouseType': row.warehouse_type,
    }


class MarketplaceSettingsView(APIView):
    """GET/PATCH /api/shipments/settings/ — ключи маркетплейсов.

    Секреты наружу маскируются. В PATCH: ключа нет — не менять,
    пустая строка — очистить.
    """
    _SECRETS = {'ozonApiKey': 'ozon_api_key', 'wbToken': 'wb_token'}
    _PLAIN = {
        'ozonClientId': 'ozon_client_id',
        'ozonWarehouseId': 'ozon_warehouse_id',
        'wbWarehouseId': 'wb_warehouse_id',
    }
    _LISTS = {'ozonWarehouseIds': 'ozon_warehouse_ids'}

    def _payload(self, s):
        return {
            'ozonClientId': s.ozon_client_id,
            'ozonApiKey': _mask(s.ozon_api_key),
            'ozonWarehouseId': s.ozon_warehouse_id,
            'ozonWarehouseIds': s.ozon_warehouse_ids or ([s.ozon_warehouse_id] if s.ozon_warehouse_id else []),
            'ozonWarehouses': [
                _warehouse_payload(row)
                for row in MarketplaceWarehouse.objects.filter(source=Shipment.Source.OZON)
            ],
            'wbToken': _mask(s.wb_token),
            'wbWarehouseId': s.wb_warehouse_id,
            'ozonConfigured': s.ozon_configured,
            'wbConfigured': s.wb_configured,
        }

    def get(self, request):
        return Response(self._payload(MarketplaceSettings.load()))

    def patch(self, request):
        body = request.data if isinstance(request.data, dict) else {}
        s = MarketplaceSettings.load()
        for key, field in {**self._PLAIN, **self._SECRETS}.items():
            if key not in body:
                continue
            value = body.get(key)
            if not isinstance(value, str):
                return Response({'error': f'{key} должен быть строкой'}, status=400)
            setattr(s, field, value.strip())
        for key, field in self._LISTS.items():
            if key not in body:
                continue
            value = body.get(key)
            if not isinstance(value, list) or any(not isinstance(v, str) for v in value):
                return Response({'error': f'{key} должен быть списком строк'}, status=400)
            clean = []
            for item in value:
                item = item.strip()
                if item and item not in clean:
                    clean.append(item)
            setattr(s, field, clean)
        s.save()
        return Response(self._payload(s))


class OzonWarehouseListView(APIView):
    """POST /api/shipments/settings/ozon-warehouses/ — загрузить склады Ozon v2."""

    def post(self, request):
        s = MarketplaceSettings.load()
        if not s.ozon_configured:
            return Response({'error': 'Не заданы ключи Ozon'}, status=400)
        try:
            rows = fetch_ozon_warehouses(s.ozon_client_id, s.ozon_api_key)
        except MarketplaceError as e:
            return Response({'error': str(e)}, status=502)
        for row in rows:
            MarketplaceWarehouse.objects.update_or_create(
                source=Shipment.Source.OZON,
                warehouse_id=str(row.get('warehouse_id') or ''),
                defaults={
                    'name': row.get('name') or '',
                    'status': row.get('status') or '',
                    'is_rfbs': bool(row.get('is_rfbs')),
                    'warehouse_type': row.get('warehouse_type') or '',
                },
            )
        warehouses = MarketplaceWarehouse.objects.filter(source=Shipment.Source.OZON)
        return Response({'warehouses': [_warehouse_payload(row) for row in warehouses]})


# Ozon выдаёт этикетку только на собранное отправление; «Ожидает упаковки» ещё
# не собрано. У WB стикер есть у любого сборочного задания.
_LABEL_READY_STATUS = {
    Shipment.Source.OZON: {'awaiting_deliver'},
    Shipment.Source.WB: {'new', 'assembled', 'confirm'},
}


def _label_postings(shipment):
    """(готовые, неготовые) номера отправлений заявки, без повторов."""
    ready_statuses = _LABEL_READY_STATUS.get(shipment.source)
    if not ready_statuses:
        return [], []
    ready, not_ready = [], []
    for line in shipment.lines.filter(deleted=False):
        if not line.external_posting:
            continue
        bucket = ready if line.external_status in ready_statuses else not_ready
        if line.external_posting not in bucket:
            bucket.append(line.external_posting)
    return ready, not_ready


def _norm_text(value):
    return ' '.join((value or '').split()).casefold()


def _ozon_shipment_warehouse(shipment):
    return shipment_warehouse(shipment) or TARGET_WAREHOUSE


def _picking_lines(shipment, stage=None):
    lines = list(shipment.lines.filter(deleted=False).select_related('product'))
    if stage:
        lines = [line for line in lines if line.external_status == stage]
    if shipment.source == Shipment.Source.OZON:
        warehouse = _ozon_shipment_warehouse(shipment)
        return [
            line for line in lines
            if _norm_text(line.external_warehouse) == _norm_text(warehouse)
        ]
    return lines


def _ozon_picking_carriages(shipment, client_id, api_key):
    base = timezone.localtime(shipment.created_at).date()
    today = timezone.localdate()
    date_from = (min(base, today) - timedelta(days=1)).isoformat()
    date_to = (max(base, today) + timedelta(days=7)).isoformat()
    rows = fetch_ozon_carriages(client_id, api_key, date_from, date_to, _ozon_shipment_warehouse(shipment))
    return [
        {
            'id': row.get('id'),
            'label': f"Отгрузка {row.get('partial_num') or ''}".strip(),
            'departureDate': row.get('departure_date') or '',
            'deliveryMethodName': row.get('delivery_method_name') or '',
            'status': row.get('status') or '',
            'partialNum': row.get('partial_num'),
        }
        for row in rows
        if row.get('id')
    ]


def _missing_ozon_role(error):
    text = str(error).casefold()
    return 'missing a required role' in text or '403 forbidden' in text


def _ozon_local_picking_postings(shipment):
    postings = []
    for line in _picking_lines(shipment):
        product_name = line.product.name if line.product_id else line.name_raw
        sku = line.product.sku if line.product_id else line.sku_raw
        postings.append({
            'posting_number': line.external_posting,
            'products': [{
                'offer_id': sku,
                'product_name': product_name,
                'quantity': int(line.quantity) if line.quantity == line.quantity.to_integral_value() else float(line.quantity),
                'picture_url': '',
            }],
        })
    return postings


class ShipmentLabelsCheckView(APIView):
    """GET /api/shipments/<pk>/labels/check/ — что готово к печати, а что ещё нет."""

    def get(self, request, pk):
        shipment = _get(pk)
        if not shipment:
            return Response({'error': 'не найдено'}, status=404)
        ready, not_ready = _label_postings(shipment)
        return Response({'ready': ready, 'notReady': not_ready})


class ShipmentLabelsView(APIView):
    """GET /api/shipments/<pk>/labels/ — PDF с этикетками готовых отправлений."""

    def get(self, request, pk):
        shipment = _get(pk)
        if not shipment:
            return Response({'error': 'не найдено'}, status=404)
        ready, _ = _label_postings(shipment)
        posting = (request.query_params.get('posting') or '').strip()
        if posting:
            if posting not in ready:
                return Response({'error': 'Этикетка для этого отправления ещё не готова'}, status=409)
            ready = [posting]
        if not ready:
            return Response({'error': 'Нет отправлений с готовыми этикетками'}, status=409)

        s = MarketplaceSettings.load()
        try:
            if shipment.source == Shipment.Source.OZON:
                if not s.ozon_configured:
                    return Response({'error': 'Не заданы ключи Ozon'}, status=400)
                pdf = fetch_ozon_label_pdf(s.ozon_client_id, s.ozon_api_key, ready)
            else:
                if not s.wb_configured:
                    return Response({'error': 'Не задан токен Wildberries'}, status=400)
                stickers, barcodes, label_codes = fetch_wb_stickers_with_barcodes(s.wb_token, ready)
                _save_wb_label_barcodes(shipment, barcodes, label_codes)
                pdf = build_wb_pdf(stickers)
        except MarketplaceError as e:
            return Response({'error': str(e)}, status=502)
        except Exception:
            logger.exception('Этикетки заявки %s: неожиданная ошибка', shipment.id)
            return Response({'error': 'Не удалось получить этикетки'}, status=502)

        resp = HttpResponse(pdf, content_type='application/pdf')
        resp['Content-Disposition'] = f'attachment; filename="labels-{shipment.id}.pdf"'
        return resp


class ShipmentPickingListView(APIView):
    """GET /api/shipments/<pk>/picking-list/ — PDF с листом подбора отгрузки."""

    def get(self, request, pk):
        shipment = _get(pk)
        if not shipment:
            return Response({'error': 'не найдено'}, status=404)

        if shipment.source == Shipment.Source.OZON:
            try:
                postings = _ozon_local_picking_postings(shipment)
                if not postings:
                    return Response({'error': 'Нет строк для листа подбора'}, status=409)
                settings = MarketplaceSettings.load()
                if not settings.ozon_configured:
                    return Response({'error': 'Не заданы ключи Ozon'}, status=400)
                codes = fetch_ozon_label_codes(
                    settings.ozon_client_id, settings.ozon_api_key,
                    [posting['posting_number'] for posting in postings],
                )
                for posting in postings:
                    posting['label_code'] = codes[posting['posting_number']]
                title = f'Отгрузка из системы «Цехтакт», склад {_ozon_shipment_warehouse(shipment)}'
                pdf = build_ozon_assembly_picking_list_pdf(title, postings)
            except MarketplaceError as e:
                return Response({'error': str(e)}, status=409)
            except Exception:
                logger.exception('Лист подбора Ozon %s: неожиданная ошибка', shipment.id)
                return Response({'error': 'Не удалось получить лист подбора Ozon'}, status=502)

            resp = HttpResponse(pdf, content_type='application/pdf')
            resp['Content-Disposition'] = f'attachment; filename="ozon-picking-list-{shipment.id}.pdf"'
            return resp

        stage = (request.query_params.get('stage') or '').strip()
        if stage and stage not in {'awaiting_packaging', 'awaiting_deliver', 'new', 'assembled', 'confirm'}:
            return Response({'error': 'неизвестная группа листа подбора'}, status=400)
        lines = _picking_lines(shipment, stage or None)
        if not lines:
            return Response({'error': 'Нет строк для листа подбора'}, status=409)
        try:
            if shipment.source == Shipment.Source.WB:
                pdf = build_wb_picking_list_pdf(shipment, lines)
            else:
                pdf = build_picking_list_pdf(shipment, lines)
        except MarketplaceError as e:
            return Response({'error': str(e)}, status=409)
        except Exception:
            logger.exception('Лист подбора заявки %s: неожиданная ошибка', shipment.id)
            return Response({'error': 'Не удалось сформировать лист подбора'}, status=502)

        resp = HttpResponse(pdf, content_type='application/pdf')
        resp['Content-Disposition'] = f'attachment; filename="picking-list-{shipment.id}.pdf"'
        return resp


class ShipmentPickingListOptionsView(APIView):
    """GET /api/shipments/<pk>/picking-list/options/ — доступные отгрузки Ozon для листа подбора."""

    def get(self, request, pk):
        shipment = _get(pk)
        if not shipment:
            return Response({'error': 'не найдено'}, status=404)
        if shipment.source != Shipment.Source.OZON:
            return Response({'carriages': []})
        s = MarketplaceSettings.load()
        if not s.ozon_configured:
            return Response({'error': 'Не заданы ключи Ozon'}, status=400)
        try:
            return Response({'carriages': _ozon_picking_carriages(shipment, s.ozon_client_id, s.ozon_api_key)})
        except MarketplaceError as e:
            logger.warning('Список отгрузок Ozon для листа подбора %s: API недоступно (%s), используем данные системы «Цехтакт»', shipment.id, e)
            return Response({'carriages': [{
                'id': 0,
                'label': 'Из системы «Цехтакт»',
                'departureDate': '',
                'deliveryMethodName': f'Ozon {_ozon_shipment_warehouse(shipment)}',
                'status': 'fallback',
                'partialNum': None,
            }]})
        except Exception:
            logger.exception('Список отгрузок Ozon для листа подбора %s: неожиданная ошибка', shipment.id)
            return Response({'error': 'Не удалось получить список отгрузок Ozon'}, status=502)


class ShipmentOzonSyncView(APIView):
    """POST /api/shipments/<pk>/sync/ — обновить статусы и количество строк Ozon."""

    def post(self, request, pk):
        shipment = _get(pk)
        if not shipment:
            return Response({'error': 'не найдено'}, status=404)
        if shipment.source != Shipment.Source.OZON:
            return Response({'error': 'Синхронизация доступна только для Ozon'}, status=409)
        if shipment.status == Shipment.Status.SHIPPED:
            return Response(_SHIPPED_LOCKED, status=409)

        s = MarketplaceSettings.load()
        if not s.ozon_configured:
            return Response({'error': 'Не заданы ключи Ozon'}, status=400)
        try:
            rows = fetch_ozon_postings(s.ozon_client_id, s.ozon_api_key)
            result = sync_ozon_shipment(
                shipment,
                rows,
                request.data.get('newPostingsAction'),
                request.user,
            )
        except MarketplaceError as e:
            return Response({'error': str(e)}, status=502)
        except Exception:
            logger.exception('Синхронизация Ozon заявки %s: неожиданная ошибка', shipment.id)
            return Response({'error': 'Не удалось синхронизировать Ozon'}, status=502)
        if result.get('status') == 'choice_required':
            return Response(result, status=409)
        return Response(result)


class ShipmentReturnPickupCodeView(APIView):
    """POST /api/shipments/<pk>/return-pickup-code/ — код получения возвратов."""

    def post(self, request, pk):
        shipment = _get(pk)
        if not shipment:
            return Response({'error': 'не найдено'}, status=404)
        if shipment.note != 'marketplace_return':
            return Response({'error': 'Код получения доступен только для заявок возврата'}, status=409)

        if shipment.source == Shipment.Source.OZON:
            s = MarketplaceSettings.load()
            if not s.ozon_configured:
                return Response({'error': 'Не заданы ключи Ozon'}, status=400)
            try:
                code = fetch_ozon_return_pickup_barcode(s.ozon_client_id, s.ozon_api_key)
            except MarketplaceError as e:
                return Response({'error': str(e)}, status=502)
            except Exception:
                logger.exception('Код получения возвратов Ozon: неожиданная ошибка')
                return Response({'error': 'Не удалось получить код получения Ozon'}, status=502)
            return Response({'status': 'ok', 'source': shipment.source, 'code': code})

        if shipment.source == Shipment.Source.WB:
            return Response({
                'status': 'manual_required',
                'source': shipment.source,
                'message': 'WB не отдаёт код получения возврата через подключенный API. Получите код в личном кабинете Wildberries.',
            }, status=409)

        return Response({'error': 'У ручной заявки нет кода маркетплейса'}, status=409)


class ShipmentImportView(APIView):
    """POST /api/shipments/import/ — загрузить отправления из Ozon и WB.

    Идём в оба маркетплейса, на каждый создаём свою заявку. Ненастроенный
    источник пропускается, падение одного не отменяет импорт другого.
    """

    def post(self, request):
        s = MarketplaceSettings.load()
        results = {}

        if s.ozon_configured:
            results['ozon'] = self._run(
                Shipment.Source.OZON,
                lambda: fetch_ozon_postings(s.ozon_client_id, s.ozon_api_key),
                request.user)
            results['ozonReturns'] = self._run(
                Shipment.Source.OZON,
                lambda: fetch_ozon_returns(s.ozon_client_id, s.ozon_api_key),
                request.user,
                is_return=True)
        else:
            results['ozon'] = {'status': 'not_configured'}
            results['ozonReturns'] = {'status': 'not_configured'}

        if s.wb_configured:
            results['wb'] = self._run(
                Shipment.Source.WB,
                lambda: fetch_wb_orders(s.wb_token),
                request.user)
            results['wbReturns'] = self._run(
                Shipment.Source.WB,
                lambda: fetch_wb_returns(s.wb_token),
                request.user,
                is_return=True,
                retry_on_rate_limit=True)
        else:
            results['wb'] = {'status': 'not_configured'}
            results['wbReturns'] = {'status': 'not_configured'}

        return Response({'results': results})

    def _run(self, source, fetch, user, is_return=False, retry_on_rate_limit=False):
        try:
            rows = fetch()
        except MarketplaceRateLimitError as e:
            if retry_on_rate_limit:
                retry_at = schedule_wb_returns_retry(e.retry_after_seconds, str(e))
                return {
                    'status': 'retry_scheduled',
                    'message': 'Wildberries ограничил запросы',
                    'retryAt': retry_at.isoformat(),
                }
            return {'status': 'error', 'message': str(e)}
        except MarketplaceError as e:
            return {'status': 'error', 'message': str(e)}
        except Exception:
            # Данные извне ненадёжны (например, нечисловое quantity уронит Decimal) —
            # падение одного источника не должно валить импорт другого в 500.
            # Наружу отдаём общий текст, но подробности пишем в журнал сервиса:
            # иначе такой сбой на проде нечем будет диагностировать.
            logger.exception('Импорт из %s: неожиданная ошибка', source)
            return {'status': 'error', 'message': 'Не удалось обработать ответ маркетплейса'}
        outcome = import_rows(source, rows, user, is_return=is_return)
        return {'status': 'ok', **outcome}
