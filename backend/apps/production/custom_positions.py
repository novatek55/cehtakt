"""Whole-order custom positions; intentionally no inventory or BOM side effects."""
from datetime import date
from decimal import Decimal
from django.db import transaction
from django.utils import timezone
from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework.exceptions import ValidationError
from rest_framework import serializers
from apps.catalog.models import Process, Product, ProductAttachment
from apps.directory.models import Area
from apps.sales.models import SalesOrder, OrderLine
from .models import ProductionJob, JobOperation


def custom_data(product):
    if product is None:
        return None
    value = (product.options or {}).get('customPosition')
    return value if isinstance(value, dict) else None


def order_is_custom(order):
    return any(custom_data(line.product) is not None for line in order.lines.select_related('product'))


def stage_ready(op):
    return not op.job.operations.filter(sequence__lt=op.sequence).exclude(status='done').exists()


def validate_stage(op):
    if not stage_ready(op):
        raise ValueError('Сначала завершите предыдущий этап заказной позиции.')


@transaction.atomic
def create_custom_jobs(order):
    # Same order lock for concurrent full/selected launches.
    SalesOrder.objects.select_for_update().get(pk=order.pk)
    stats = {'jobs': 0, 'operations': 0, 'reservations': 0, 'buy_shortage': 0}
    for line in order.lines.select_related('product__process'):
        data = custom_data(line.product)
        if data is None:
            raise ValueError('Заказная позиция должна быть отдельной заявкой.')
        if order.jobs.filter(product=line.product).exists():
            continue
        areas = list(Area.objects.filter(pk__in=data['areaIds'], archived=False).order_by('sort_index', 'id'))
        if len(areas) != len(data['areaIds']) or not areas:
            raise ValueError('Выбранный участок недоступен. Проверьте этапы заявки.')
        job = ProductionJob.objects.create(name=f'{order.name}/1', product=line.product,
            process=line.product.process, sales_order=order, batch=order.name,
            customers=order.customer_name, quantity=1, finish_by_date=order.finish_by_date,
            description=data.get('description', '')[:512])
        for sequence, area in enumerate(areas, 1):
            JobOperation.objects.create(job=job, area=area, name=area.name, quantity=1, sequence=sequence)
        stats['jobs'] += 1
        stats['operations'] += len(areas)
    return stats


def finish_custom(op, good, defect):
    validate_stage(op)
    if good != Decimal(1) or defect != Decimal(0) or op.quantity != Decimal(1):
        raise ValueError('Заказная позиция завершается целиком: один заказ, без частичного выпуска и брака.')
    op.status = 'done'
    op.done_quantity = 1
    op.defect_quantity = 0
    op.finished_at = timezone.now()
    op.completion_snapshot = {'movement_ids': [], 'reservations': [], 'reopen_quantity': '1', 'customPosition': True}
    op.save(update_fields=['status', 'done_quantity', 'defect_quantity', 'finished_at', 'completion_snapshot'])
    job = op.job
    complete = not job.operations.exclude(status='done').exists()
    job.made_quantity = 1 if complete else 0
    job.status = 'done' if complete else 'in_progress'
    job.save(update_fields=['made_quantity', 'status'])
    return {'good': 1, 'defect': 0, 'defectRecordId': None}


class CustomPositionView(APIView):
    """Sales-scoped creation, including uploads in one request."""
    def get(self, request):
        if request.query_params.get('order'):
            from django.shortcuts import get_object_or_404
            order = get_object_or_404(SalesOrder, pk=request.query_params['order'])
            stages = [dict(area=op.area.name, status=op.get_status_display(), executor=(op.executor.get_full_name() or op.executor.username) if op.executor else '')
                      for op in JobOperation.objects.filter(job__sales_order=order).select_related('area', 'executor').order_by('sequence', 'id')]
            return Response({'stages': stages, 'started': bool(stages)})
        return Response({'areas': list(Area.objects.filter(archived=False).order_by('sort_index', 'id').values('id', 'name'))})

    @transaction.atomic
    def post(self, request):
        from django.core.validators import URLValidator
        from django.core.exceptions import ValidationError as DjangoValidationError
        from apps.catalog.views import ALLOWED_DOCUMENT_EXTENSIONS, ALLOWED_DOCUMENT_MIME_TYPES, _uploaded_file_sha256
        name = str(request.data.get('name') or '').strip()
        title = str(request.data.get('title') or '').strip()
        description = str(request.data.get('description') or '').strip()
        if not name or len(name) > 128 or not title or len(title) > 255:
            raise ValidationError({'error': 'Укажите номер заявки (до 128 символов) и название позиции (до 255).'})
        if len(description) > 512:
            raise ValidationError({'error': 'Описание — не более 512 символов.'})
        raw = request.data.getlist('areaIds') if hasattr(request.data, 'getlist') else request.data.get('areaIds', [])
        try:
            ids = [int(value) for value in raw]
        except (ValueError, TypeError):
            raise ValidationError({'error': 'Некорректные участки.'})
        areas = list(Area.objects.filter(pk__in=ids, archived=False).order_by('sort_index', 'id'))
        if not ids or len(set(ids)) != len(ids) or len(areas) != len(ids):
            raise ValidationError({'error': 'Выберите хотя бы один действующий участок.'})
        url = str(request.data.get('filesUrl') or '').strip()
        if url:
            try:
                URLValidator(schemes=['http', 'https'])(url)
            except DjangoValidationError:
                raise ValidationError({'error': 'Укажите ссылку http:// или https://.'})
            if len(url) > 2048:
                raise ValidationError({'error': 'Ссылка слишком длинная.'})
        raw_date = request.data.get('finish_by_date')
        try:
            due = date.fromisoformat(raw_date) if raw_date else None
        except (ValueError, TypeError):
            raise ValidationError({'error': 'Некорректный срок.'})
        uploads = []
        for key, kind in [('image', 'image'), ('drawing', 'file')]:
            upload = request.FILES.get(key)
            if not upload:
                continue
            if upload.size > 25 * 1024 * 1024:
                raise ValidationError({'error': 'Максимальный размер файла — 25 МБ.'})
            if kind == 'image' and (upload.content_type not in ['image/png', 'image/jpeg', 'image/webp', 'image/gif']):
                raise ValidationError({'error': 'Картинка: PNG, JPEG, WEBP или GIF.'})
            if kind == 'file' and (upload.name.rsplit('.',1)[-1].lower() not in ALLOWED_DOCUMENT_EXTENSIONS and upload.content_type not in ALLOWED_DOCUMENT_MIME_TYPES):
                raise ValidationError({'error': 'Чертёж: PDF, DXF/DWG, Excel, Word, ZIP, STEP/STP.'})
            uploads.append((upload, kind))
        metadata = {'areaIds': [area.pk for area in areas], 'stages': [area.name for area in areas], 'description': description, 'filesUrl': url}
        process = Process.objects.create(name=title, archived=True, description=description)
        product = Product.objects.create(process=process, name=title, archived=True, options={'customPosition': metadata})
        order = SalesOrder.objects.create(name=name, customer_name=str(request.data.get('customer_name') or '')[:255],
            manager_name=str(request.data.get('manager_name') or '')[:255], finish_by_date=due,
            created_by=request.user, info=description)
        OrderLine.objects.create(order=order, product=product, quantity=1)
        saved = []
        try:
            for upload, kind in uploads:
                att = ProductAttachment(product=product, kind=kind, original_name=upload.name[:255],
                    content_type=upload.content_type or '', content_hash=_uploaded_file_sha256(upload), size=upload.size)
                att.file.save(upload.name, upload, save=False)
                saved.append(att.file)
                att.save()
        except Exception:
            for file in saved:
                file.delete(save=False)
            raise
        return Response({'id': order.pk, 'name': order.name, 'productId': product.pk, 'type': 'customPosition'}, status=201)
