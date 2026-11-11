"""Serialize merge with live API requests, reject stale product references."""
import json
import re

from django.db import connection, transaction, OperationalError
from django.db.models.signals import pre_save
from django.dispatch import receiver
from django.http import JsonResponse, HttpResponse
from rest_framework.exceptions import APIException

from .models import Product, ProductMerge
from .merge_lock import material_lock, MergeBusy

PRODUCT_KEYS = {'product_id', 'productId', 'product', 'source_product_id', 'source_product', 'linkedProductId'}
MERGE_PATH = '/api/products/merge/'


class MergedReference(APIException):
    status_code = 409
    default_detail = 'Материал уже объединен. Обновите данные перед сохранением.'


@receiver(pre_save, dispatch_uid='catalog.reject_merged_reference')
def reject_merged_reference(sender, instance, raw=False, **kwargs):
    if raw or sender == ProductMerge or sender._meta.app_label not in {
            'catalog', 'stock', 'production', 'sales', 'purchases', 'shipments'}:
        return
    ids = set()
    if sender == Product:
        if instance.pk:
            ids.add(instance.pk)
    else:
        for field in sender._meta.concrete_fields:
            if field.is_relation and field.related_model == Product:
                value = getattr(instance, field.attname, None)
                if value:
                    ids.add(value)
    if ids and Product.all_objects.filter(pk__in=ids, merged_into__isnull=False).exists():
        raise MergedReference()


def _body_ids(value):
    ids = set()
    if isinstance(value, dict):
        for key, child in value.items():
            if key in PRODUCT_KEYS and isinstance(child, (int, str)) and str(child).isdigit():
                ids.add(int(child))
            if key == 'splits' and isinstance(child, dict):
                for allocation in child.values():
                    if isinstance(allocation, dict):
                        ids.update(int(k) for k in allocation if str(k).isdigit())
            ids.update(_body_ids(child))
    elif isinstance(value, list):
        for child in value:
            ids.update(_body_ids(child))
    return ids


class MaterialMergeGuardMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        if not request.path_info.startswith(('/api/', '/admin/')):
            return self.get_response(request)
        try:
            exclusive = request.path_info == MERGE_PATH and request.method == 'POST'
            with material_lock(exclusive=exclusive), transaction.atomic():
                if connection.vendor == 'postgresql':
                    from .merging import MERGE_LOCK
                    with connection.cursor() as cur:
                        cur.execute("SET LOCAL lock_timeout = '8s'")
                        cur.execute('SELECT pg_advisory_xact_lock' + ('' if exclusive else '_shared') + '(%s)', [MERGE_LOCK])
                if request.path_info != MERGE_PATH:
                    response = self._check_stale(request)
                    if response is not None:
                        return response
                response = self.get_response(request)
                if response.status_code >= 400:
                    transaction.set_rollback(True)
                return response
        except (OperationalError, MergeBusy):
            return JsonResponse({'error': 'Данные сейчас обновляются. Повторите действие через несколько секунд.'}, status=409)

    def _check_stale(self, request):
        match = re.match(r'^/api/(products|stock)/(\d+)(/.*)$', request.path_info)
        ids = {int(match[2])} if match else set()
        if request.method not in ('GET', 'HEAD', 'OPTIONS') and request.content_type == 'application/json':
            try:
                ids.update(_body_ids(json.loads(request.body)))
            except (ValueError, UnicodeDecodeError):
                pass  # The endpoint's parser reports malformed JSON normally.
        if not ids:
            return None
        aliases = dict(Product.all_objects.filter(pk__in=ids, merged_into__isnull=False)
                       .values_list('pk', 'merged_into_id'))
        if not aliases:
            return None
        if match and request.method in ('GET', 'HEAD'):
            from .merging import current_product_id
            target = current_product_id(int(match[2]))
            location = request.path.replace('/' + match[2] + '/', '/' + str(target) + '/', 1)
            if request.META.get('QUERY_STRING'):
                location += '?' + request.META['QUERY_STRING']
            response = HttpResponse(status=307)
            response['Location'] = location
            return response
        return JsonResponse({'error': 'Материал уже объединен. Обновите страницу перед сохранением.',
                             'merged_products': aliases}, status=409)
