"""Импорт техкарт из XLSX редактора proplan-editor (3 листа).

Листы (в порядке загрузки): Продукты → Спецификации → Техпроцессы.
Формат колонок — из proplan-editor/docs/import-to-conductor.md.
Сопоставление сущностей по НАЗВАНИЮ (продукты грузятся первыми).

Запуск: python manage.py import_tech_cards <путь_к_xlsx>
"""
import re
from pathlib import Path

import openpyxl
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from apps.catalog.models import Process, Product, Ingredient, Operation
from apps.directory.models import Area, Category, Unit, Supplier


def num(v):
    if v is None:
        return 0.0
    if isinstance(v, (int, float)):
        return float(v)
    s = str(v).strip().replace(' ', '').replace(' ', '').replace(',', '.')
    try:
        return float(s)
    except ValueError:
        return 0.0


def duration_sec(v):
    """Число (сек) или '5 мин'/'2 ч 30 мин'/'45 с'."""
    if isinstance(v, (int, float)):
        return int(v)
    total = 0.0
    units = {'ч': 3600, 'мин': 60, 'с': 1}
    for n, u in re.findall(r'(\d+(?:[.,]\d+)?)\s*(ч|мин|с)', str(v or '')):
        total += float(n.replace(',', '.')) * units[u]
    if total == 0:
        total = num(v)
    return int(total)


def truthy(v):
    return str(v).strip() in ('+', '1', 'да', 'true', 'True', 'x', 'X', '✓')


def header_map(ws):
    """{нормализованный заголовок: индекс колонки} по первой строке."""
    hdr = next(ws.iter_rows(min_row=1, max_row=1, values_only=True))
    return {str(h).strip().lower(): i for i, h in enumerate(hdr) if h}


def find_col(hmap, *keywords):
    for key, idx in hmap.items():
        if all(k in key for k in keywords):
            return idx
    return None


def pick_sheet(wb, *keywords):
    for ws in wb.worksheets:
        if any(all(k in ws.title.lower() for k in kw) for kw in keywords):
            return ws
    return None


class Command(BaseCommand):
    help = 'Импортирует техкарты (Продукты/Спецификации/Техпроцессы) из XLSX'

    def add_arguments(self, parser):
        parser.add_argument('path', help='Путь к XLSX-файлу редактора')

    @transaction.atomic
    def handle(self, *args, **options):
        path = Path(options['path'])
        if not path.exists():
            raise CommandError(f'Файл не найден: {path}')
        wb = openpyxl.load_workbook(path, read_only=True, data_only=True)

        prod_ws = pick_sheet(wb, ('продукт',)) or wb.worksheets[0]
        spec_ws = pick_sheet(wb, ('спецификаци',), ('специфик',))
        tech_ws = pick_sheet(wb, ('техпроцесс',), ('тех',))

        n_proc, n_ing, n_op = self._products(prod_ws)
        if spec_ws:
            n_ing = self._specs(spec_ws)
        if tech_ws:
            n_op = self._ops(tech_ws)

        self.stdout.write(self.style.SUCCESS(
            f'Импорт техкарт: изделий всего {Process.objects.count()}, '
            f'создано связей BOM {n_ing}, операций {n_op}'
        ))

    def _products(self, ws):
        h = header_map(ws)
        c_name = find_col(h, 'наименован') or 0
        c_sku = find_col(h, 'артикул', 'продукт')
        c_bar = find_col(h, 'штрихкод')
        c_cat = find_col(h, 'категори')
        c_unit = find_col(h, 'единиц')
        c_sup = find_col(h, 'поставщик')
        c_make = find_col(h, 'производить') if find_col(h, 'производить', 'умолчани') is None else None
        c_make = find_col(h, 'производить') and next((i for k, i in h.items() if 'производить' in k and 'умолчан' not in k), None)
        c_buy = next((i for k, i in h.items() if k.strip() == 'закупать' or ('закупать' in k and 'комплект' not in k)), None)
        c_makedef = find_col(h, 'производить', 'умолчани')
        c_desc = find_col(h, 'описани')
        c_sell = find_col(h, 'цена', 'продаж')
        c_buyp = find_col(h, 'цена', 'покуп')
        c_min = find_col(h, 'минимальн')

        cache_cat, cache_unit, cache_sup = {}, {}, {}
        n = 0
        for row in ws.iter_rows(min_row=2, values_only=True):
            name = (row[c_name] or '').strip() if c_name is not None else ''
            if not name:
                continue
            category = self._lookup(Category, cache_cat, row, c_cat)
            unit = self._lookup(Unit, cache_unit, row, c_unit)
            supplier = self._lookup(Supplier, cache_sup, row, c_sup)
            process, _ = Process.objects.update_or_create(name=name, defaults={
                'make': truthy(row[c_make]) if c_make is not None else True,
                'buy': truthy(row[c_buy]) if c_buy is not None else False,
                'make_by_default': truthy(row[c_makedef]) if c_makedef is not None else True,
                'category': category, 'unit': unit, 'default_supplier': supplier,
                'description': (row[c_desc] or '') if c_desc is not None else '',
            })
            Product.objects.update_or_create(process=process, name=name, defaults={
                'sku': (row[c_sku] or '') if c_sku is not None else '',
                'inner_barcode': (row[c_bar] or '') if c_bar is not None else '',
                'default_sell_price': num(row[c_sell]) if c_sell is not None else 0,
                'default_buy_price': num(row[c_buyp]) if c_buyp is not None else 0,
                'min_limit': num(row[c_min]) if c_min is not None else 0,
            })
            n += 1
        self.stdout.write(f'  Продукты: обработано {n}')
        return n, 0, 0

    def _lookup(self, Model, cache, row, col):
        if col is None or row[col] is None or not str(row[col]).strip():
            return None
        key = str(row[col]).strip()
        if key not in cache:
            cache[key] = Model.objects.get_or_create(name=key)[0]
        return cache[key]

    def _specs(self, ws):
        h = header_map(ws)
        c_parent = find_col(h, 'продукт')
        c_comp = find_col(h, 'комплектующ')
        c_qty = find_col(h, 'количеств')
        c_note = find_col(h, 'примечани')
        n = 0
        for row in ws.iter_rows(min_row=2, values_only=True):
            pname = (row[c_parent] or '').strip() if c_parent is not None else ''
            cname = (row[c_comp] or '').strip() if c_comp is not None else ''
            if not pname or not cname:
                continue
            parent = Process.objects.filter(name=pname).first()
            comp = Product.objects.filter(name=cname).first()
            if not parent or not comp:
                continue
            Ingredient.objects.update_or_create(
                process=parent, source_product=comp,
                defaults={
                    'quantity': num(row[c_qty]) if c_qty is not None else 1,
                    'notes': (row[c_note] or '') if c_note is not None else '',
                },
            )
            n += 1
        self.stdout.write(f'  Спецификации: связей {n}')
        return n

    def _ops(self, ws):
        h = header_map(ws)
        c_prod = find_col(h, 'наименован')
        c_op = find_col(h, 'операци')
        c_area = find_col(h, 'участок')
        c_cost = find_col(h, 'стоимост')
        c_dur = find_col(h, 'длительн')
        area_cache = {}
        n = 0
        for row in ws.iter_rows(min_row=2, values_only=True):
            pname = (row[c_prod] or '').strip() if c_prod is not None else ''
            aname = (row[c_area] or '').strip() if c_area is not None else ''
            if not pname or not aname:
                continue
            process = Process.objects.filter(name=pname).first()
            if not process:
                continue
            if aname not in area_cache:
                area_cache[aname] = Area.objects.get_or_create(name=aname)[0]
            Operation.objects.create(
                process=process, area=area_cache[aname],
                name=(row[c_op] or aname) if c_op is not None else aname,
                sequence=n,
                duration_seconds=duration_sec(row[c_dur]) if c_dur is not None else 0,
                cost_per_hour_override=num(row[c_cost]) if c_cost is not None else None,
            )
            n += 1
        self.stdout.write(f'  Техпроцессы: операций {n}')
        return n
