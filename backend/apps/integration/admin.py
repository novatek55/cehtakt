"""Админка сервисных клиентов: ключи не редактируются и не показываются."""
from django.contrib import admin

from .models import ServiceClient


@admin.register(ServiceClient)
class ServiceClientAdmin(admin.ModelAdmin):
    """Видны только имя и вкл/выкл — сам ключ восстановить нельзя, в БД лежит хеш."""
    list_display = ['name', 'key_prefix', 'is_active', 'last_used_at', 'created_at']
    list_filter = ['is_active']
    readonly_fields = ['key_hash', 'key_prefix', 'created_at', 'last_used_at']

    def has_add_permission(self, request):
        # Ключ выдаётся только командой create_service_client — иначе его негде показать.
        return False
