"""Аутентификация внешних сервисов по ключу: Authorization: Bearer <key>.

Полностью отдельна от rest_framework.authtoken: пользовательский токен
(«Token …») сюда не проходит, сервисный ключ не работает на /api/….
"""
from django.utils import timezone
from rest_framework import authentication, exceptions

from .models import ServiceClient, hash_key


class ServiceKeyAuthentication(authentication.BaseAuthentication):
    keyword = 'Bearer'

    def authenticate(self, request):
        # Не через authentication.get_authorization_header(): та функция
        # принудительно кодирует строку в iso-8859-1 (работая с реальными
        # заголовками как с байтами) и падает с UnicodeEncodeError, если в
        # заголовке оказались не-latin1 символы (например, невалидный ключ
        # с кириллицей). Заголовок и так уже строка в request.META — берём
        # её напрямую, без лишнего кодирования.
        header = request.META.get('HTTP_AUTHORIZATION', '')
        if not header:
            return None  # → анонимный запрос → 401 от IsAuthenticated
        parts = header.split()
        if len(parts) != 2 or parts[0] != self.keyword:
            raise exceptions.AuthenticationFailed(
                'Ожидается заголовок Authorization: Bearer <key>')
        client = ServiceClient.objects.filter(key_hash=hash_key(parts[1])).first()
        if client is None:
            raise exceptions.AuthenticationFailed('Неизвестный ключ')
        if not client.is_active:
            raise exceptions.PermissionDenied('Сервисный клиент отключён')
        # update() вместо save() — не трогаем остальные поля и не гоняем сигналы
        ServiceClient.objects.filter(pk=client.pk).update(last_used_at=timezone.now())
        return (client, client)

    def authenticate_header(self, request):
        # Без этого DRF отвечает 403 вместо 401 на анонимный запрос
        return self.keyword
