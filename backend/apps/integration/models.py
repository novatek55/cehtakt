"""Сервисные клиенты внешнего API /api/v1/.

Ключ выдаётся один раз при создании; в БД хранится только SHA-256 от него.
Ключи высокоэнтропийные (token_urlsafe), поэтому быстрый хеш здесь достаточен
и позволяет искать клиента одним индексированным запросом.
"""
import hashlib
import secrets

from django.db import models

KEY_PREFIX_LEN = 8


def generate_key():
    """Новый сервисный ключ (~43 символа, URL-safe)."""
    return secrets.token_urlsafe(32)


def hash_key(key):
    """SHA-256 от ключа в hex — то, что лежит в БД."""
    return hashlib.sha256(key.encode('utf-8')).hexdigest()


class ServiceClient(models.Model):
    """Внешний сервис-потребитель API. Пользовательские токены сюда не подходят."""
    name = models.CharField(max_length=128, unique=True, help_text='Имя сервиса-потребителя')
    key_hash = models.CharField(max_length=64, unique=True)
    key_prefix = models.CharField(max_length=KEY_PREFIX_LEN, blank=True,
                                  help_text='Начало ключа — чтобы опознать в списке')
    is_active = models.BooleanField(default=True, help_text='Отключение без удаления')
    created_at = models.DateTimeField(auto_now_add=True)
    last_used_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ['name']

    def __str__(self):
        return f'{self.name} ({self.key_prefix}…)'

    # Утиная типизация под DRF IsAuthenticated: клиент — не User, но
    # аутентифицированный субъект запроса.
    @property
    def is_authenticated(self):
        return True

    @classmethod
    def issue(cls, name):
        """Создать клиента и вернуть (клиент, сырой_ключ). Ключ больше нигде не хранится."""
        key = generate_key()
        client = cls.objects.create(
            name=name, key_hash=hash_key(key), key_prefix=key[:KEY_PREFIX_LEN])
        return client, key
