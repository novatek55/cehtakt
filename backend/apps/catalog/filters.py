from django.db.models import OuterRef, Subquery
from rest_framework.exceptions import ValidationError
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.directory.models import Area
from .models import Operation


def filter_by_final_area(queryset, value):
    """Match the final operation of the product's own process, in editor order."""
    if value in (None, ''):
        return queryset
    try:
        area_id = int(value)
        if area_id <= 0 or area_id > 9223372036854775807:
            raise ValueError
    except (TypeError, ValueError):
        raise ValidationError({'final_area': 'Выберите корректный участок.'})
    final_operation = Operation.objects.filter(process_id=OuterRef('process_id')).order_by('-sequence', '-id')
    return queryset.alias(final_area_id=Subquery(final_operation.values('area_id')[:1])).filter(final_area_id=area_id)


class FinalAreaOptionsView(APIView):
    """Area names for filtering, authorized by the catalog/stock page permission."""
    def get(self, request):
        return Response(list(Area.objects.order_by('name', 'id').values('id', 'name')))
