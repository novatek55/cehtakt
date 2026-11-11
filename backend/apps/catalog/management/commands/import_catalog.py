"""Импорт каталога изделий из data/catalog.csv (реальный экспорт proplan).

CSV-колонки: Название, Артикул, Категория, Цена продажи, Стоимость,
Доходность, Маржинальная доходность, Время производства, Тип техпроцесса.

Экспорт даёт список изделий БЕЗ BOM/техпроцесса — их дособираем отдельно.
Каждая строка → Process (изделие) + Product (основной вариант).
Upsert по имени: повторный запуск обновляет, не плодит дубли.
Запуск: python manage.py import_catalog
"""
import csv
import re
from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand
from django.db import transaction

from apps.catalog.models import Process, Product
from apps.directory.models import Category

_UNITS = {'ч': 3600, 'мин': 60, 'с': 1}


def parse_duration(s: str) -> int:
    """'4 мин', '45 с', '1 ч 30 мин' → секунды."""
    total = 0.0
    for num, unit in re.findall(r'(\d+(?:[.,]\d+)?)\s*(ч|мин|с)', s or ''):
        total += float(num.replace(',', '.')) * _UNITS[unit]
    return int(total)


def parse_num(s: str) -> float:
    s = (s or '').strip().replace(' ', '').replace(' ', '').replace(',', '.')
    try:
        return float(s)
    except ValueError:
        return 0.0


class Command(BaseCommand):
    help = 'Импортирует каталог изделий из data/catalog.csv'

    @transaction.atomic
    def handle(self, *args, **options):
        path = Path(settings.BASE_DIR) / 'data' / 'catalog.csv'
        rows = list(csv.reader(path.open(encoding='utf-8-sig')))[1:]  # без заголовка

        cats: dict[str, Category] = {}
        created_p = updated_p = 0
        for r in rows:
            if len(r) < 10 or not r[1].strip():
                continue
            name = r[1].strip()
            sku = r[2].strip()
            cat_name = r[3].strip()
            sell = parse_num(r[4])
            cost = parse_num(r[5])
            duration = parse_duration(r[8])
            complexity = Process.Complexity.COMPLEX if 'ложн' in r[9] else Process.Complexity.SIMPLE

            category = None
            if cat_name:
                if cat_name not in cats:
                    cats[cat_name] = Category.objects.get_or_create(name=cat_name)[0]
                category = cats[cat_name]

            process, _ = Process.objects.update_or_create(
                name=name,
                defaults={'complexity': complexity, 'category': category},
            )
            _, is_new = Product.objects.update_or_create(
                process=process, name=name,
                defaults={
                    'sku': sku,
                    'default_sell_price': sell,
                    # себестоимость из экспорта (комбинированная) — как стартовое значение
                    'ingredients_cost': cost,
                    'total_production_duration': duration,
                },
            )
            created_p += is_new
            updated_p += not is_new

        self.stdout.write(self.style.SUCCESS(
            f'Каталог импортирован: изделий {Process.objects.count()}, '
            f'вариантов создано {created_p}/обновлено {updated_p}, категорий {Category.objects.count()}'
        ))
