"""Внешний сервисный API /api/v1/ — только чтение."""
from datetime import timedelta

from django.db.models import Q
from django.utils import timezone
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.catalog.models import Product
from apps.catalog.services import filter_products_by_type, semi_product_ids
from apps.production.models import JobOperation

from .auth import ServiceKeyAuthentication
from .cursor import clamp_limit, decode_cursor, encode_cursor, parse_offset
from .serializers import ProductionOutputSerializer, StockRowSerializer

# Защитный лаг выдачи выпуска: не отдаём строки свежее, чем now() - лаг.
#
# finish_operation проставляет finished_at внутри транзакции, а строка становится
# видимой только после COMMIT — порядок коммитов может не совпадать с порядком меток.
# Транзакция A взяла время T_A и ждёт блокировку строки ProductionJob (две операции
# одного задания на разных участках конкурируют именно за неё); транзакция B взяла
# более позднее T_B и закоммитилась первой. Потребитель успевает забрать B и сдвинуть
# курсор на T_B; коммит A приходит позже, но её T_A < T_B — строка навсегда остаётся
# ниже курсора и не будет отдана никогда, без единого признака потери.
#
# Лаг должен покрывать окно между присвоением метки и COMMIT. Пять секунд заведомо
# больше этого окна для finish_operation и стоят потребителю лишь такой же задержки
# доставки. На SQLite гонки нет (писатели сериализуются), на MySQL/InnoDB — есть.
OUTPUT_LAG_SECONDS = 5


class ServiceAPIView(APIView):
    """База для всех /api/v1/: своя аутентификация вместо глобальных настроек.

    В проекте глобально включены TokenAuthentication и RolePermission (роли
    пользователей) — сервисному клиенту они не подходят, поэтому переопределяем.
    """
    authentication_classes = [ServiceKeyAuthentication]
    permission_classes = [IsAuthenticated]


class PingView(ServiceAPIView):
    """GET /api/v1/ping/ — проверка ключа и живости API."""

    def get(self, request):
        return Response({'ok': True, 'service': request.auth.name})


class StockView(ServiceAPIView):
    """GET /api/v1/stock/?type=&sku=&limit=&offset= — остатки по номенклатуре."""

    def get(self, request):
        semi_ids = semi_product_ids()
        qs = (Product.objects
              .select_related('process', 'process__unit', 'stock')
              .order_by('name', 'id'))
        qs = filter_products_by_type(qs, request.query_params.get('type'), semi_ids)

        skus = request.query_params.getlist('sku')
        if skus:
            qs = qs.filter(sku__in=skus)

        count = qs.count()
        offset = parse_offset(request.query_params.get('offset'))
        limit = clamp_limit(request.query_params.get('limit'))
        rows = qs[offset:offset + limit]
        data = StockRowSerializer(rows, many=True, context={'semi_ids': semi_ids}).data
        return Response({'results': data, 'count': count})


class ProductionOutputView(ServiceAPIView):
    """GET /api/v1/production/output/?since=&limit= — выпуск, инкрементально.

    Одна запись = одна завершённая операция. Курсор по (finished_at, id):
    операции с одинаковой меткой времени не теряются и не дублируются.
    """

    def get(self, request):
        raw_since = request.query_params.get('since')
        try:
            cursor = decode_cursor(raw_since)
        except ValueError:
            return Response({'error': 'некорректный курсор since'}, status=400)

        limit = clamp_limit(request.query_params.get('limit'))
        settled_before = timezone.now() - timedelta(seconds=OUTPUT_LAG_SECONDS)
        qs = (JobOperation.objects
              .filter(status=JobOperation.Status.DONE, finished_at__isnull=False,
                      finished_at__lte=settled_before)
              .select_related('job', 'job__product', 'area', 'executor')
              .order_by('finished_at', 'id'))
        if cursor:
            ts, op_id = cursor
            qs = qs.filter(Q(finished_at__gt=ts) | Q(finished_at=ts, id__gt=op_id))

        # +1 строка сверх лимита — дешёвый способ узнать, есть ли продолжение
        rows = list(qs[:limit + 1])
        has_more = len(rows) > limit
        rows = rows[:limit]
        # Курсор всегда в нашей канонической записи: и новый, и возвращаемый без
        # изменений — иначе API отдало бы строку, которую само не выпускало.
        if rows:
            next_since = encode_cursor(rows[-1].finished_at, rows[-1].id)
        elif cursor:
            next_since = encode_cursor(*cursor)
        else:
            next_since = ''
        return Response({
            'results': ProductionOutputSerializer(rows, many=True).data,
            'next_since': next_since,
            'has_more': has_more,
        })
