from django.db import OperationalError
from rest_framework.permissions import BasePermission, IsAuthenticated
from rest_framework.views import APIView
from rest_framework.response import Response
from .merging import MergeError, merge_materials, preview_merge
from .models import ProductMerge


class CanMergeMaterials(BasePermission):
    message = 'Для объединения нужны права изменения склада и каталога.'

    def has_permission(self, request, view):
        if not request.user.is_authenticated:
            return False
        if request.user.is_superuser:
            return True
        role = getattr(getattr(request.user, 'profile', None), 'role', None)
        permissions = (role.permissions or {}) if role else {}
        return all(permissions.get(page, {}).get('update') for page in ('stock', 'catalog'))


class MaterialMergePreviewView(APIView):
    permission_classes = [IsAuthenticated, CanMergeMaterials]

    def post(self, request):
        try:
            return Response(preview_merge(request.data.get('source_id'), request.data.get('target_id')))
        except MergeError as exc:
            return Response({'error': str(exc)}, status=exc.status)


class MaterialMergeView(APIView):
    permission_classes = [IsAuthenticated, CanMergeMaterials]

    def post(self, request):
        if request.data.get('confirm_equivalent') is not True or request.data.get('confirm_stock_sum') is not True:
            return Response({'error': 'Подтвердите одинаковый материал и сложение складских остатков.'}, status=400)
        try:
            # Validate integer IDs before an ORM lookup, including retries.
            from .merging import _pair
            source_id, target_id = request.data.get('source_id'), request.data.get('target_id')
            if type(source_id) is not int or type(target_id) is not int or min(source_id, target_id) <= 0:
                raise MergeError('Выберите две позиции.')
            return Response(merge_materials(source_id, target_id, request.data.get('revision'), request.user))
        except MergeError as exc:
            return Response({'error': str(exc)}, status=exc.status)
        except OperationalError:
            return Response({'error': 'Сейчас выполняется другая операция. Повторите предварительную проверку через несколько секунд.'}, status=409)


class MaterialMergeHistoryView(APIView):
    permission_classes = [IsAuthenticated, CanMergeMaterials]

    def get(self, request):
        records = ProductMerge.objects.select_related('created_by').all()[:100]
        return Response([{'id': r.pk, 'date': r.created_at.isoformat(),
                          'user': r.created_by.get_username() if r.created_by else '',
                          'source': r.details['source'], 'target': r.details['target'],
                          'counts': r.details['counts'], 'result': r.details['final_stock']}
                         for r in records])
