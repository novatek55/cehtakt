"""Импорт реальных остатков склада из proplan (запрос ListInventoryProducts).

Логинится тем же способом, что и import_from_proplan (креды из env PROPLAN_LOGIN/PASSWORD),
тянет products{ availableQuantity valueInStock expectedQuantity committedQuantity
defaultBuyPrice process{ buy make category unit } } и обновляет StockItem + Process-флаги.

Запуск (пароль из файла, не в командной строке):
  $env:PROPLAN_LOGIN="..."; $env:PROPLAN_PASSWORD=(Get-Content proplan_pw.txt -Raw).Trim()
  python manage.py import_stock
"""
import os

from django.core.management.base import BaseCommand, CommandError

from apps.catalog.management.commands.import_from_proplan import _gql, LOGIN_MUT
from apps.catalog.models import Process, Product
from apps.directory.models import Category, Unit
from apps.stock.models import StockItem

STOCK_Q = '''query ListInventoryProducts {
  products {
    id archived name sku availableQuantity valueInStock
    expectedQuantity committedQuantity defaultBuyPrice
    process { id buy make category { name } unit { name } }
  }
}'''


def _num(v):
    return float(v) if isinstance(v, (int, float)) else 0.0


class Command(BaseCommand):
    help = 'Импортирует остатки склада из proplan'

    def handle(self, *args, **opts):
        login = os.environ.get('PROPLAN_LOGIN', '')
        password = os.environ.get('PROPLAN_PASSWORD', '')
        if not login or not password:
            raise CommandError('Задай env PROPLAN_LOGIN и PROPLAN_PASSWORD (пароль из файла).')

        token = _gql(LOGIN_MUT, {'login': login, 'password': password})['login']['accessToken']
        self.stdout.write('Авторизация ок, тяну остатки…')
        products = _gql(STOCK_Q, token=token)['products']

        cats, units = {}, {}
        created = updated = 0
        for p in products:
            if p.get('archived'):
                continue
            name = (p.get('name') or '').strip()
            if not name:
                continue
            pr = p.get('process') or {}
            cat = None
            cname = ((pr.get('category') or {}).get('name') or '').strip()
            if cname:
                cats.setdefault(cname, Category.objects.get_or_create(name=cname)[0])
                cat = cats[cname]
            unit = None
            uname = ((pr.get('unit') or {}).get('name') or '').strip()
            if uname:
                units.setdefault(uname, Unit.objects.get_or_create(name=uname)[0])
                unit = units[uname]

            product = Product.objects.filter(name=name).select_related('process').first()
            if product:
                proc = product.process
                proc.make = bool(pr.get('make'))
                proc.buy = bool(pr.get('buy'))
                if cat:
                    proc.category = cat
                if unit:
                    proc.unit = unit
                proc.save(update_fields=['make', 'buy', 'category', 'unit'])
            else:
                proc = Process.objects.create(
                    name=name, make=bool(pr.get('make')), buy=bool(pr.get('buy')),
                    category=cat, unit=unit)
                product = Product.objects.create(process=proc, name=name)
                created += 1

            product.default_buy_price = _num(p.get('defaultBuyPrice'))
            product.sku = (p.get('sku') or product.sku or '')
            product.save(update_fields=['default_buy_price', 'sku'])

            StockItem.objects.update_or_create(
                product=product,
                defaults={
                    'available_quantity': _num(p.get('availableQuantity')),
                    'committed_quantity': _num(p.get('committedQuantity')),
                    'expected_quantity': _num(p.get('expectedQuantity')),
                },
            )
            updated += 1

        self.stdout.write(self.style.SUCCESS(
            f'Остатки импортированы: обработано {updated}, новых продуктов {created}, '
            f'позиций на складе {StockItem.objects.count()}'))
