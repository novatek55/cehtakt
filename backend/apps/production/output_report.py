"""Admin-only daily monetary output report; reads current production facts."""
from datetime import date
from calendar import monthrange
from zoneinfo import ZoneInfo
from django.db import transaction
from django.utils import timezone
from rest_framework.permissions import BasePermission
from rest_framework.response import Response
from rest_framework.views import APIView
from apps.catalog.models import Product, Ingredient, Operation
from apps.directory.models import Area
from apps.sales.models import OrderLine
from .models import JobOperation, ProductionJob
from .output_calculation import calculate
from .output_targets import build_performance

class ReportAdminPermission(BasePermission):
    message = 'Отчёты доступны только администратору.'
    def has_permission(self, request, view):
        user = request.user
        if not user or not user.is_authenticated or not user.is_active:
            return False
        role = getattr(getattr(user, 'profile', None), 'role', None)
        return bool(user.is_superuser or (role and (role.name or '').strip().casefold() == 'администратор'))

class OutputReportView(APIView):
    permission_classes = [ReportAdminPermission]
    def get(self, request):
        today = timezone.now().astimezone(ZoneInfo('Europe/Moscow')).date()
        try:
            start = date.fromisoformat(request.query_params.get('dateFrom') or today.isoformat())
            end = date.fromisoformat(request.query_params.get('dateTo') or today.isoformat())
            area = int(request.query_params['areaId']) if request.query_params.get('areaId') else None
            employee = int(request.query_params['executorId']) if request.query_params.get('executorId') else None
            if area is not None and area <= 0 or employee is not None and employee < 0:
                raise ValueError()
        except (ValueError, TypeError):
            return Response({'error': 'Проверьте даты, участок и сотрудника.'}, status=400)
        group = request.query_params.get('group')
        if group not in (None, '', 'cutting') or (group and area):
            return Response({'error': 'Выберите участок или группу Пила + лазер.'}, status=400)
        if end < start or (end - start).days > 365:
            return Response({'error': 'Выберите период от 1 до 366 дней.'}, status=400)
        with transaction.atomic():
            data = {
                'products': list(Product.all_objects.values('id', 'name', 'sku', 'process_id', 'process__make', 'process__unit__name', 'options')),
                'ingredients': list(Ingredient.objects.values('process_id', 'source_product_id', 'quantity')),
                'process_operations': list(Operation.objects.values('id', 'process_id', 'area_id')),
                'jobs': list(ProductionJob.objects.values('id', 'parent_id', 'product_id', 'sales_order_id', 'sales_order__name', 'name', 'quantity', 'rework_id')),
                'order_lines': list(OrderLine.objects.values('id', 'order_id', 'product_id', 'quantity')),
                'operations': list(JobOperation.objects.filter(status=JobOperation.Status.DONE, finished_at__isnull=False).values(
                    'id', 'job_id', 'area_id', 'operation_id', 'done_quantity', 'defect_quantity', 'status', 'finished_at',
                    'executor_id', 'executor__first_name', 'executor__last_name', 'executor__username')),
                'areas': list(Area.objects.order_by('sort_index', 'id').values('id', 'name')),
            }
        if area is not None and area not in {a['id'] for a in data['areas']}:
            return Response({'error': 'Участок не найден.'}, status=400)
        result = calculate(data, start.isoformat(), end.isoformat(), area, employee)
        reference = calculate(data, start.replace(day=1).isoformat(), end.replace(day=monthrange(end.year, end.month)[1]).isoformat())
        result['performance'] = build_performance(data, reference, start.isoformat(), end.isoformat(), today.isoformat(), 'cutting' if group else area)
        if group:
            members = next((a.get('members', []) for a in result['performance']['areaOptions'] if a['id'] == 'cutting'), [])
            for key in ('rows', 'areas', 'days', 'employees', 'employeesByDay'):
                result[key] = [r for r in result[key] if r['areaId'] in members]
        result['generatedAt'] = timezone.now().isoformat()
        return Response(result)
