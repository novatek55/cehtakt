"""Импорт закупок из proplan (запрос pendingAndOpenPurchaseOrders).

Логин как в import_from_proplan. Сумма = Σ(строки: количество×цена).
Статус-enum → русская метка. Идемпотентно (upsert по номеру).

Запуск (креды из env):
  python manage.py import_purchases
"""
import os
from datetime import datetime

from django.core.management.base import BaseCommand, CommandError

from apps.catalog.management.commands.import_from_proplan import _gql, LOGIN_MUT
from apps.purchases.models import PurchaseOrder

PO_Q = '''query Purchases {
  pendingAndOpenPurchaseOrders {
    id name status expectedArrivalDate
    supplier { name }
    purchaseOrderLines { quantity pricePerUnit }
  }
}'''

STATUS = {
    'PENDING': PurchaseOrder.Status.REQUEST,
    'ORDERED': PurchaseOrder.Status.ORDERED,
    'RECEIVED': PurchaseOrder.Status.RECEIVED,
}


def _date(iso):
    if not iso:
        return None
    try:
        return datetime.fromisoformat(iso.replace('Z', '+00:00')).date()
    except (ValueError, AttributeError):
        return None


class Command(BaseCommand):
    help = 'Импортирует закупки из proplan'

    def handle(self, *args, **opts):
        login = os.environ.get('PROPLAN_LOGIN', '')
        password = os.environ.get('PROPLAN_PASSWORD', '')
        if not login or not password:
            raise CommandError('Задай env PROPLAN_LOGIN и PROPLAN_PASSWORD.')
        token = _gql(LOGIN_MUT, {'login': login, 'password': password})['login']['accessToken']

        rows = _gql(PO_Q, token=token)['pendingAndOpenPurchaseOrders']
        n = 0
        for r in rows:
            total = sum((ln.get('quantity') or 0) * (ln.get('pricePerUnit') or 0)
                        for ln in (r.get('purchaseOrderLines') or []))
            PurchaseOrder.objects.update_or_create(
                name=r.get('name') or f"#{r.get('id')}",
                defaults={
                    'supplier_name': (r.get('supplier') or {}).get('name') or '',
                    'total_amount': total,
                    'status': STATUS.get(r.get('status'), PurchaseOrder.Status.REQUEST),
                    'expected_arrival_date': _date(r.get('expectedArrivalDate')),
                },
            )
            n += 1
        self.stdout.write(self.style.SUCCESS(
            f'Закупки импортированы: {n} (всего в БД {PurchaseOrder.objects.count()})'))
