"""Авто-регистрация всех моделей приложения в админке (бесплатный CRUD)."""
from django.apps import apps as django_apps
from django.contrib import admin
from .models import OperationCancellation, DefectRecord


@admin.register(OperationCancellation)
class OperationCancellationAdmin(admin.ModelAdmin):
    list_display = ['id', 'job', 'created_at', 'created_by', 'reason']

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False

@admin.register(DefectRecord)
class DefectRecordAdmin(OperationCancellationAdmin):
    list_display = ['id', 'source_job', 'quantity', 'created_at', 'cancelled_at']


for _model in django_apps.get_app_config('production').get_models():
    try:
        admin.site.register(_model)
    except admin.sites.AlreadyRegistered:
        pass
