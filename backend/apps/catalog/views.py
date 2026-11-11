"""Служебный endpoint импорта техкарт из proplan-editor.

Принимает POST с деревом техпроцесса (JSON) и грузит в БД.
CORS открыт для локального редактора (localhost:5173), CSRF отключён —
это локальный dev-инструмент миграции, не публичный API.
"""
import json
import hashlib

from django.db.models import Case, IntegerField, ProtectedError, Q, Value, When
from django.http import FileResponse, JsonResponse, HttpResponse
from django.urls import reverse
from django.views.decorators.csrf import csrf_exempt
from rest_framework import generics
from rest_framework.permissions import AllowAny

from rest_framework.views import APIView
from rest_framework.response import Response

from .models import Product, Ingredient, Process, IngredientOption, ProductAttachment
from .serializers import ProductListSerializer, ProductWriteSerializer
from .copying import copy_product
from .filters import filter_by_final_area
from .services import (TYPE_PRODUCT, filter_products_by_type, import_tech_tree,
                       semi_product_ids)


ALLOWED_DOCUMENT_EXTENSIONS = {
    'pdf', 'dxf', 'dwg', 'xls', 'xlsx', 'doc', 'docx', 'zip', 'step', 'stp',
}
ALLOWED_DOCUMENT_MIME_TYPES = {
    'application/pdf',
    'application/dxf',
    'application/x-dxf',
    'image/vnd.dxf',
    'application/acad',
    'application/x-acad',
    'application/autocad_dwg',
    'application/dwg',
    'application/x-dwg',
    'image/vnd.dwg',
    'application/vnd.ms-excel',
    'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
    'application/msword',
    'application/vnd.openxmlformats-officedocument.wordprocessingml.document',
    'application/zip',
    'application/x-zip-compressed',
    'application/step',
    'application/iges',
    'model/step',
    'model/step+xml',
    'model/stp',
}


def attachment_dict(att: ProductAttachment, request=None) -> dict:
    url = reverse('product-attachment-file', kwargs={'pk': att.pk}) if att else ''
    if request is not None and url:
        url = request.build_absolute_uri(url)
    return {
        'id': att.id,
        'kind': att.kind,
        'name': att.original_name,
        'url': url,
        'contentType': att.content_type,
        'size': att.size,
    }


def _uploaded_file_sha256(upload) -> str:
    hasher = hashlib.sha256()
    for chunk in upload.chunks():
        hasher.update(chunk)
    if hasattr(upload, 'seek'):
        upload.seek(0)
    return hasher.hexdigest()


class ProductCopyView(APIView):
    """POST /api/products/<id>/copy/ — независимая копия техкарты."""

    def post(self, request, pk):
        product = generics.get_object_or_404(Product.objects.select_related('process'), pk=pk)
        copied = copy_product(product)
        return Response({'id': copied.pk, 'name': copied.name}, status=201)


class ProductListView(generics.ListCreateAPIView):
    """GET /api/products/ — каталог; POST — создать изделие."""

    def get_serializer_class(self):
        return ProductWriteSerializer if self.request.method == 'POST' else ProductListSerializer

    def get_queryset(self):
        qs = Product.objects.select_related('process', 'process__category').order_by('name')
        q = (self.request.query_params.get('q') or '').strip()
        if q:
            qs = qs.filter(Q(name__icontains=q) | Q(sku__icontains=q)).annotate(
                search_rank=Case(
                    When(name__iexact=q, then=Value(0)),
                    When(sku__iexact=q, then=Value(0)),
                    When(name__istartswith=q, then=Value(1)),
                    When(sku__istartswith=q, then=Value(1)),
                    default=Value(2),
                    output_field=IntegerField(),
                )
            ).order_by('search_rank', 'name')
        t = self.request.query_params.get('type')
        if t and t != 'all':
            # Материал = помечен «Закупать»; ПФ/Продукция = «Производить» (catalog.services)
            semi_ids = semi_product_ids()
            if t == 'finished':
                # Готовая продукция для подбора в заказ: продукция с «ГП» в имени
                # (в данных нет явного признака ГП; эвристика согласована с пользователем).
                qs = filter_products_by_type(qs, TYPE_PRODUCT, semi_ids).filter(name__contains='ГП')
            else:
                qs = filter_products_by_type(qs, t, semi_ids)
        return filter_by_final_area(qs, self.request.query_params.get('final_area'))


def _product_blockers(p):
    """Связанные объекты, из-за которых изделие нельзя удалить."""
    from apps.production.models import ProductionJob, Reservation
    from apps.sales.models import OrderLine
    from apps.purchases.models import PurchaseOrderLine
    used_in = list(Ingredient.objects.filter(source_product=p)
                   .select_related('process').values_list('process__name', flat=True))
    used_in += list(IngredientOption.objects.filter(source_product=p)
                    .select_related('ingredient__process')
                    .values_list('ingredient__process__name', flat=True))
    return {
        'jobs': list(ProductionJob.objects.filter(product=p).values_list('name', flat=True)),
        'orders': list(OrderLine.objects.filter(product=p).select_related('order').values_list('order__name', flat=True)),
        'purchases': list(PurchaseOrderLine.objects.filter(product=p).select_related('order').values_list('order__name', flat=True)),
        'usedIn': sorted(set(used_in)),
        'reservations': list(Reservation.objects.filter(product=p).values_list('sales_order__name', flat=True).distinct()),
    }


def _detach_from_compositions(p):
    """Убрать изделие из всех составов (BOM и взаимозаменяемые альтернативы).

    Только это — чтобы изделие, участвующее лишь в техкартах, можно было удалить.
    Реальные документы (заказы, закупки, задания, резервы) НЕ трогаем: если изделие
    используется в них, удаление остаётся заблокированным, и пользователь разбирается
    с документами вручную.
    """
    IngredientOption.objects.filter(source_product=p).delete()
    Ingredient.objects.filter(source_product=p).delete()


def _type_of(process, product_id, semi_ids):
    """Тип изделия для дерева состава: material | semi | product.

    ВНИМАНИЕ: это не то же самое, что catalog.services.classify_product_type.
    Здесь изделие без обоих флагов (ни make, ни buy) считается semi/product,
    а в общей функции — материалом. Различие сохранено намеренно, чтобы не менять
    поведение дерева состава; объединять — только осознанно и с тестом.
    """
    if process.buy:
        return 'material'
    return 'semi' if product_id in semi_ids else 'product'


class ProductBulkDeleteView(APIView):
    """POST /api/products/bulk-delete/ {ids:[...], force?} — удалить отмеченные изделия.

    Без force: занятые изделия попадают в blocked со списком связанных объектов.
    С force=true: изделие убирается из составов техкарт (BOM/альтернативы) и удаляется.
    Реальные документы (заказы/закупки/задания/резервы) не трогаются — если изделие
    используется в них, оно останется в blocked даже с force.
    """

    def post(self, request):
        ids = request.data.get('ids') or []
        force = bool(request.data.get('force'))
        if not ids:
            return Response({'error': 'нет выбранных изделий'}, status=400)
        deleted, blocked = 0, []
        for p in Product.objects.filter(pk__in=ids):
            if force:
                _detach_from_compositions(p)
            try:
                p.delete()
                deleted += 1
            except ProtectedError:
                blocked.append({'id': p.id, 'name': p.name, 'refs': _product_blockers(p)})
        return Response({'deleted': deleted, 'blocked': blocked})


def _build_tree(process: Process, quantity: float, visiting: set, ingredient_id=None, product_id=None) -> dict:
    """Рекурсивно строит дерево техкарты из Process (BOM + операции)."""
    prod = process.products.first()
    attachments = list(prod.attachments.all()) if prod else []
    image = next((a for a in attachments if a.kind == ProductAttachment.Kind.IMAGE), None)
    node = {
        'id': process.id,
        'ingredientId': ingredient_id,  # id связи (для редактирования/удаления); None у корня
        'productId': product_id,        # id изделия-компонента
        'name': process.name,
        'article': prod.sku if prod else '',
        'category': process.category.name if process.category else '',
        'note': process.description or '',
        'price': float(prod.default_sell_price) if prod else 0,
        'purchasePrice': float(prod.default_buy_price) if prod else 0,
        'minStock': float(prod.min_limit) if prod else 0,
        'paintAreaPerUnit': (
            prod.options.get('paint_area_per_unit') if prod and isinstance(prod.options, dict) else None
        ),
        'image': attachment_dict(image) if image else None,
        'files': [attachment_dict(a) for a in attachments if a.kind == ProductAttachment.Kind.FILE],
        'quantity': quantity,
        'unit': process.unit.name if process.unit else 'шт.',
        'make': process.make,
        'buy': process.buy,
        'operations': [
            {
                'name': op.name,
                'area': op.area.name,
                'duration': op.duration_seconds,
                'isParallel': op.is_parallel,
                'costPerHour': float(op.cost_per_hour_override if op.cost_per_hour_override is not None else op.area.cost_per_hour),
            }
            for op in process.operations.select_related('area').all()
        ],
        'children': [],
    }
    if process.id in visiting:
        return node
    visiting = visiting | {process.id}
    for ing in process.ingredients.select_related('source_product__process__unit').all():
        child = _build_tree(ing.source_product.process, float(ing.quantity), visiting,
                            ingredient_id=ing.id, product_id=ing.source_product_id)
        opts = list(ing.options.select_related('source_product').order_by('priority', 'id'))
        if len(opts) > 1:
            child['alternatives'] = [
                {'name': o.source_product.name, 'article': o.source_product.sku}
                for o in opts if o.source_product_id != ing.source_product_id
            ]
        node['children'].append(child)
    return node


class ProductAttachmentUploadView(APIView):
    """POST /api/product-attachments/ multipart file, kind=image|file."""

    def post(self, request):
        upload = request.FILES.get('file')
        if not upload:
            return Response({'error': 'файл не выбран'}, status=400)
        kind = request.data.get('kind') or ProductAttachment.Kind.FILE
        if kind not in dict(ProductAttachment.Kind.choices):
            return Response({'error': 'некорректный тип файла'}, status=400)
        if kind == ProductAttachment.Kind.IMAGE and not (upload.content_type or '').startswith('image/'):
            return Response({'error': 'картинка должна быть файлом изображения'}, status=400)
        if kind == ProductAttachment.Kind.FILE:
            ext = (upload.name.rsplit('.', 1)[-1] if '.' in upload.name else '').lower()
            content_type = (upload.content_type or '').split(';', 1)[0].lower()
            if ext not in ALLOWED_DOCUMENT_EXTENSIONS and content_type not in ALLOWED_DOCUMENT_MIME_TYPES:
                return Response({'error': 'разрешены PDF, DXF/DWG, Excel, Word, ZIP, STEP/STP'}, status=400)
        att = ProductAttachment.objects.create(
            kind=kind,
            file=upload,
            original_name=upload.name,
            content_type=upload.content_type or '',
            content_hash=_uploaded_file_sha256(upload),
            size=upload.size or 0,
        )
        return Response(attachment_dict(att, request), status=201)


class ProductAttachmentFileView(APIView):
    """GET /api/product-attachments/<id>/file/ — открыть сохранённое вложение."""
    permission_classes = [AllowAny]
    authentication_classes = []

    def get(self, request, pk):
        att = generics.get_object_or_404(ProductAttachment, pk=pk)
        response = FileResponse(att.file.open('rb'), content_type=att.content_type or 'application/octet-stream')
        response['Content-Disposition'] = f'inline; filename="{att.original_name}"'
        return response


class IngredientDetailView(APIView):
    """PATCH /api/ingredients/<id>/ {quantity} — изменить кол-во; DELETE — убрать из состава."""

    def patch(self, request, pk):
        ing = Ingredient.objects.filter(pk=pk).first()
        if not ing:
            return Response({'error': 'не найдено'}, status=404)
        qty = request.data.get('quantity')
        if qty is not None:
            try:
                ing.quantity = float(qty)
            except (TypeError, ValueError):
                return Response({'error': 'некорректное количество'}, status=400)
            ing.save(update_fields=['quantity'])
        return Response({'id': ing.id, 'quantity': float(ing.quantity)})

    def delete(self, request, pk):
        deleted, _ = Ingredient.objects.filter(pk=pk).delete()
        if not deleted:
            return Response({'error': 'не найдено'}, status=404)
        return Response(status=204)


class ProcessIngredientCreateView(APIView):
    """POST /api/processes/<process_id>/ingredients/ {source_product_id, quantity} — добавить компонент."""

    def post(self, request, process_id):
        process = Process.objects.filter(pk=process_id).first()
        if not process:
            return Response({'error': 'техпроцесс не найден'}, status=404)
        src = Product.objects.filter(pk=request.data.get('source_product_id')).first()
        if not src:
            return Response({'error': 'компонент не найден'}, status=404)
        if src.process_id == process.id:
            return Response({'error': 'нельзя добавить изделие в собственный состав'}, status=400)
        qty = request.data.get('quantity') or 1
        if Ingredient.objects.filter(process=process, source_product=src).count() > 1:
            return Response({'error': 'В составе несколько строк этого материала. Измените нужную строку в техкарте.'}, status=409)
        ing, created = Ingredient.objects.get_or_create(
            process=process, source_product=src, defaults={'quantity': qty})
        if not created:
            ing.quantity = qty
            ing.save(update_fields=['quantity'])
        return Response({'id': ing.id, 'created': created}, status=201 if created else 200)


class ProductTreeView(APIView):
    """GET /api/products/<id>/tree/ — дерево состава; POST /save/ — сохранить дерево целиком."""

    def get(self, request, pk):
        product = Product.objects.select_related('process', 'process__unit').filter(pk=pk).first()
        if not product:
            return Response({'error': 'не найдено'}, status=404)
        return Response({'tree': _build_tree(product.process, 1, set())})

    def post(self, request, pk):
        """Сохранить отредактированное дерево (формат редактора) — upsert через import_tech_tree."""
        product = Product.objects.select_related('process').filter(pk=pk).first()
        if not product:
            return Response({'error': 'не найдено'}, status=404)
        tree = request.data.get('tree') or request.data
        try:
            # root_product=product — корень переименовывается «на месте», id изделия сохраняется
            stats = import_tech_tree(tree, root_product=product)
        except Exception as e:  # noqa: BLE001
            return Response({'error': str(e)}, status=400)
        return Response({'ok': True, **stats})


class ProductUsedInView(APIView):
    """GET /api/products/<id>/used-in/ — в состав каких изделий входит элемент (прямые родители).

    Состав хранится на уровне Process; у процесса может быть несколько вариантов-изделий —
    каждый выводим отдельной строкой с одинаковой нормой.
    """

    def get(self, request, pk):
        product = Product.objects.select_related('process').filter(pk=pk).first()
        if not product:
            return Response({'error': 'не найдено'}, status=404)
        semi_ids = semi_product_ids()
        rows = []
        ings = (Ingredient.objects
                .filter(source_product_id=pk)
                .select_related('process', 'process__unit')
                .prefetch_related('process__products'))
        for ing in ings:
            unit = ing.process.unit.name if ing.process.unit else 'шт.'
            for parent in ing.process.products.all():
                rows.append({
                    'id': parent.id, 'name': parent.name, 'sku': parent.sku,
                    'type': _type_of(ing.process, parent.id, semi_ids),
                    'quantity': float(ing.quantity), 'unit': unit,
                })
        rows.sort(key=lambda r: r['name'])
        return Response({
            'self': {
                'name': product.name, 'sku': product.sku,
                'type': _type_of(product.process, product.id, semi_ids),
                'make': product.process.make, 'buy': product.process.buy,
            },
            'usedIn': rows,
        })


def _cors(resp: HttpResponse) -> HttpResponse:
    resp['Access-Control-Allow-Origin'] = '*'
    resp['Access-Control-Allow-Methods'] = 'POST, OPTIONS'
    resp['Access-Control-Allow-Headers'] = 'Content-Type'
    return resp


@csrf_exempt
def import_techcard(request):
    if request.method == 'OPTIONS':
        return _cors(HttpResponse(status=204))
    if request.method != 'POST':
        return _cors(JsonResponse({'error': 'POST only'}, status=405))
    try:
        tree = json.loads(request.body.decode('utf-8'))
        stats = import_tech_tree(tree)
        return _cors(JsonResponse({'ok': True, **stats}))
    except Exception as e:  # noqa: BLE001 — вернуть текст ошибки инструменту
        return _cors(JsonResponse({'ok': False, 'error': str(e)}, status=400))
