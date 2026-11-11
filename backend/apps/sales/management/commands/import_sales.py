"""Импорт заказов (Продажи→Заказы) из proplan (extendedOpenSalesOrdersTableData).

Логин как в import_from_proplan. Статусы-enum маппятся в русские метки
(их фронт красит через toneOf). Идемпотентно (upsert по номеру заказа).

Запуск (креды из env, пароль из файла):
  python manage.py import_sales
"""
import os
from datetime import datetime

from django.core.management.base import BaseCommand, CommandError

from apps.catalog.management.commands.import_from_proplan import _gql, LOGIN_MUT
from apps.sales.models import SalesOrder

ORDERS_Q = '''query Orders {
  extendedOpenSalesOrdersTableData {
    rank id name finishByDate subtotal status customerName managerName
    ingredientsStatus { status }
    productionStatus { status }
    stockStatus { status }
  }
}'''

# enum статуса → русская метка (фронт красит по метке через toneOf)
LABEL = {
    'OVERDUE': 'Просрочено',
    'PROCESSED': 'Использованы',
    'PRODUCED': 'Готово',
    'IN_STOCK': 'В наличии',
    'PACKED': 'Укомплектовано',
    'NO_RECIPE': 'Отсутствуют',
    'NOT_PACKED': 'Не укомплектовано',
    'CONFIRMED': 'Не готово',
    'UNDEFINED': '',
}


def _label(cell):
    return LABEL.get((cell or {}).get('status'), '') if isinstance(cell, dict) else ''


def _date(iso):
    if not iso:
        return None
    try:
        return datetime.fromisoformat(iso.replace('Z', '+00:00')).date()
    except (ValueError, AttributeError):
        return None


class Command(BaseCommand):
    help = 'Импортирует заказы (Продажи) из proplan'

    def handle(self, *args, **opts):
        login = os.environ.get('PROPLAN_LOGIN', '')
        password = os.environ.get('PROPLAN_PASSWORD', '')
        if not login or not password:
            raise CommandError('Задай env PROPLAN_LOGIN и PROPLAN_PASSWORD.')
        token = _gql(LOGIN_MUT, {'login': login, 'password': password})['login']['accessToken']

        rows = _gql(ORDERS_Q, token=token)['extendedOpenSalesOrdersTableData']
        n = 0
        for r in rows:
            SalesOrder.objects.update_or_create(
                name=r.get('name') or f"#{r.get('id')}",
                defaults={
                    'customer_name': r.get('customerName') or '',
                    'manager_name': r.get('managerName') or '',
                    'total_amount': r.get('subtotal') or 0,
                    'finish_by_date': _date(r.get('finishByDate')),
                    'rank': r.get('rank') or 0,
                    'ingredients_status': _label(r.get('ingredientsStatus')),
                    'production_status': _label(r.get('productionStatus')),
                    'stock_status': _label(r.get('stockStatus')),
                    'display_status': LABEL.get(r.get('status'), ''),
                },
            )
            n += 1
        self.stdout.write(self.style.SUCCESS(
            f'Заказы импортированы: {n} (всего в БД {SalesOrder.objects.count()})'))
