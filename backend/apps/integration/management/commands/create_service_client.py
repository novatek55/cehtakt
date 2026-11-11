"""Выдать ключ внешнему сервису: manage.py create_service_client "Имя сервиса".

Ключ печатается один раз — в БД лежит только хеш, восстановить нельзя.
"""
from django.core.management.base import BaseCommand, CommandError
from django.db import IntegrityError

from apps.integration.models import ServiceClient


class Command(BaseCommand):
    help = 'Создать сервисного клиента для /api/v1/ и напечатать его ключ'

    def add_arguments(self, parser):
        parser.add_argument('name', help='Имя сервиса-потребителя')

    def handle(self, *args, **options):
        name = options['name'].strip()
        if not name:
            raise CommandError('Имя не может быть пустым')
        if ServiceClient.objects.filter(name=name).exists():
            raise CommandError(f'Клиент «{name}» уже существует')
        try:
            client, key = ServiceClient.issue(name)
        except IntegrityError:
            # Гонка: имя занято между проверкой exists() и созданием.
            raise CommandError(f'Клиент «{name}» уже существует')
        self.stdout.write(f'Клиент: {client.name} (id={client.id}, префикс {client.key_prefix})')
        self.stdout.write(f'KEY: {key}')
        self.stdout.write(self.style.WARNING(
            'Сохраните ключ — он показывается один раз и в БД не хранится.'))
