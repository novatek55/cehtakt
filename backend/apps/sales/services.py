"""Разузлование заказа (MRP net requirements) — чистый расчёт без записи в БД.

Обходит BOM (Process.ingredients) рекурсивно с защитой от циклов и жадным пулом
остатков (наличие−резерв): общий ПФ/материал в разных ветках не считается дважды.
"""
import math
from collections import defaultdict

from django.apps import apps
from django.db.models import Q, Sum

from apps.stock.models import StockItem
from apps.catalog.services import classify_product_type, semi_product_ids


def _round(x):
    return round(float(x), 4)


def _max_buildable(tree, materials_summary):
    """Shared stock cannot support several additive BOM rows twice."""
    from apps.catalog.capacity import shared_material_capacity
    from decimal import Decimal
    produced = sum(r['deficit'] for r in tree)
    if produced <= 1e-9:
        return None, ''
    requirements, available = [], {}

    def visit(node):
        if not node.get('count_shortage', True):
            return
        if node['is_material']:
            need = node.get('shortage_need', node['need'])
            if need > 1e-9:
                options = node['alternatives'] if node.get('is_or') else [node]
                for option in options:
                    pid = option['product_id']
                    free = max(Decimal(str(option['available'])) - Decimal(str(
                        option.get('committed_for_calc', option['committed']))), Decimal(0))
                    available[pid] = free * Decimal(str(produced))
                requirements.append(([option['product_id'] for option in options],
                                     Decimal(str(need)), node['name']))
        for child in node['children']:
            visit(child)

    for root in tree:
        visit(root)
    capacity, limiting = shared_material_capacity(requirements, available)
    return (int(math.floor(produced)), '') if capacity is None else (capacity, limiting)


def order_bom_product_ids(order, root_product=None):
    """Product IDs present in this order's BOM, excluding unrelated standalone jobs."""
    product_ids = set()
    seen_processes = set()

    def visit(product):
        if product is None or product.pk in product_ids:
            return
        product_ids.add(product.pk)
        process = product.process
        if not process or process.buy or process.pk in seen_processes:
            return
        seen_processes.add(process.pk)
        ingredients = (process.ingredients
                      .select_related('source_product__process')
                      .prefetch_related('options__source_product__process'))
        for ingredient in ingredients:
            options = list(ingredient.options.all())
            sources = ([option.source_product for option in options]
                       if options else [ingredient.source_product])
            for source in sources:
                visit(source)

    for line in order.lines.select_related('product__process').all():
        visit(line.product)
    visit(root_product)
    return product_ids


def explode_order(order, mode, root_product=None, root_quantity=None, include_own_reservations=False,
                  include_order_holds=False, include_job_ids=None, production_roots=None,
                  catalog_cache=None, planned_reservations=None):
    """mode: 'with_stock' | 'from_zero'. Возвращает {'tree': [...], 'materials_summary': [...]}.

    tree — лес: по корню на каждую позицию заказа. Если root_product задан,
    расчёт строится только для выбранного изделия, но с привязкой к заказу.
    """
    semi_ids = semi_product_ids()
    stock_map = {s.product_id: s for s in StockItem.objects.all()}
    ProductionJob = apps.get_model('production', 'ProductionJob')
    Reservation = apps.get_model('production', 'Reservation')
    # A caller rendering many orders can supply one prefetched catalogue.
    # Other callers retain the original query/selection behaviour.
    bom_product_ids = (catalog_cache.keys() if catalog_cache is not None
                       else order_bom_product_ids(order, root_product))
    made_map = {
        row['product_id']: float(row['made'] or 0)
        for row in (ProductionJob.objects
                    .filter(sales_order=order, product_id__in=bom_product_ids,
                           name__startswith=f'{order.name}/')
                    .values('product_id')
                    .annotate(made=Sum('made_quantity')))
    }
    # Reservations stay unavailable by default. Explicitly replaced job rows
    # and physically backed holds for this order can be reused by the planner.
    own_committed_map = {}
    if include_job_ids:
        own_committed_map = {
            row['product_id']: float(row['q'] or 0)
            for row in (Reservation.objects.filter(
                sales_order=order, job_id__in=include_job_ids,
                kind=Reservation.Kind.CONSUME).values('product_id')
                .annotate(q=Sum('quantity')))
        }
    recorded_own_committed_map = own_committed_map.copy()
    if planned_reservations is None:
        from apps.production.planned_reservations import uncovered_job_reservations
        planned_reservations = uncovered_job_reservations(stock_map.values())
    planned_by_product = defaultdict(float)
    releasable_jobs = set(include_job_ids or ())
    for job, pid, quantity in planned_reservations:
        planned_by_product[pid] += float(quantity)
        if job.sales_order_id == order.pk and job.pk in releasable_jobs:
            own_committed_map[pid] = own_committed_map.get(pid, 0.0) + float(quantity)

    def recorded_stock(pid):
        stock = stock_map.get(pid)
        if not stock:
            return 0.0, 0.0
        return float(stock.available_quantity), float(stock.committed_quantity)

    def raw(pid):
        available, recorded = recorded_stock(pid)
        return available, recorded + planned_by_product.get(pid, 0.0)

    order_hold_rows = []
    if include_order_holds:
        order_hold_rows = list(Reservation.objects.filter(
            sales_order=order, job__isnull=True,
            kind=Reservation.Kind.CONSUME).order_by('id')
            .values_list('id', 'product_id', 'ingredient_id', 'quantity'))
    order_hold_total = {}
    for _, pid, _, quantity in order_hold_rows:
        order_hold_total[pid] = order_hold_total.get(pid, 0.0) + float(quantity or 0)
    backed_order_holds = {}
    if order_hold_rows:
        remaining_by_product = {}
        for pid, hold_total in order_hold_total.items():
            # Additional planned demand must not displace an existing hold.
            available, committed = recorded_stock(pid)
            other_committed = max(
                committed - hold_total - recorded_own_committed_map.get(pid, 0.0), 0.0)
            remaining_by_product[pid] = min(
                hold_total, max(available - other_committed, 0.0))
        for _, pid, ingredient_id, quantity in order_hold_rows:
            backed = min(float(quantity or 0), remaining_by_product.get(pid, 0.0))
            key = (pid, ingredient_id)
            backed_order_holds[key] = backed_order_holds.get(key, 0.0) + backed
            remaining_by_product[pid] = max(
                remaining_by_product.get(pid, 0.0) - backed, 0.0)
    # A job's output and the order-level reservation for that same output are
    # two records of the same units. Do not count those units twice.
    order_hold_overlap = {}
    overlap_remaining = {pid: max(made_map.get(pid, 0.0), 0.0)
                         for pid in order_hold_total}
    for key, backed in backed_order_holds.items():
        pid, _ = key
        overlap = min(backed, overlap_remaining.get(pid, 0.0))
        order_hold_overlap[key] = order_hold_overlap.get(key, 0.0) + overlap
        overlap_remaining[pid] = max(overlap_remaining.get(pid, 0.0) - overlap, 0.0)
    order_hold_pool = {
        key: max(quantity - order_hold_overlap.get(key, 0.0), 0.0)
        for key, quantity in backed_order_holds.items()
    }
    made_overlap_pool = {}
    for (pid, _), quantity in order_hold_overlap.items():
        made_overlap_pool[pid] = made_overlap_pool.get(pid, 0.0) + quantity
    selected_roots = set(production_roots or ())
    pool = {}  # product_id -> оставшийся доступный остаток в этом расчёте
    made_pool = {}  # product_id -> оставшийся выпуск по этому заказу в этом расчёте

    def avail(pid):
        if pid not in pool:
            a, c = raw(pid)
            pool[pid] = max(a - max(c - own_committed_map.get(pid, 0.0), 0.0), 0.0)
        return pool[pid]

    def made_avail(pid):
        if pid not in made_pool:
            made_pool[pid] = max(made_map.get(pid, 0.0), 0.0)
        return made_pool[pid]

    def walk(product, need, visiting, is_order_line=False, count_shortage=True,
             shortage_need=None, path=None, ingredient_id=None):
        if catalog_cache is not None:
            product = catalog_cache[product.id]
        if path in selected_roots:
            count_shortage = True
            shortage_need = need
        if shortage_need is None:
            shortage_need = need
        process = product.process
        is_material = bool(process.buy)
        available, committed = raw(product.id)
        committed_for_calc = max(committed - own_committed_map.get(product.id, 0.0), 0.0)
        made = 0.0

        if not is_material:
            made = min(need, made_avail(product.id))
            if made > 0:
                made_pool[product.id] = max(made_avail(product.id) - made, 0.0)
                made_overlap = min(made, made_overlap_pool.get(product.id, 0.0))
                made_overlap_pool[product.id] = max(
                    made_overlap_pool.get(product.id, 0.0) - made_overlap, 0.0)
                # Выпуск по заданию обычно уже лежит в складском остатке этой же
                # детали. Убираем его из пула, чтобы не зачесть дважды: как
                # "изготовлено" и как свободный склад.
                pool[product.id] = max(avail(product.id) - (made - made_overlap), 0.0)

        # Production planning does not net the root finished-good order line from
        # stock, but it does net materials and child semi-finished components.
        take_stock = count_shortage and (is_material or (mode == 'with_stock' and not is_order_line))
        remaining_need = max((shortage_need if count_shortage else 0.0) - made, 0.0)
        hold_key = (product.id, ingredient_id)
        from_order_hold = 0.0
        if take_stock and count_shortage:
            from_order_hold = min(remaining_need, order_hold_pool.get(hold_key, 0.0))
            if from_order_hold > 0:
                order_hold_pool[hold_key] = max(
                    order_hold_pool.get(hold_key, 0.0) - from_order_hold, 0.0)
                remaining_need -= from_order_hold
        from_stock = min(remaining_need, avail(product.id)) if take_stock else 0.0
        if from_stock > 0:
            pool[product.id] = max(avail(product.id) - from_stock, 0.0)

        deficit = remaining_need - from_stock if count_shortage else 0.0
        if not is_material and deficit > 1e-9:
            deficit = float(math.ceil(deficit - 1e-9))  # ПФ/ГП — целое, вверх
        if deficit < 0:
            deficit = 0.0

        plan_deficit = deficit
        # Visible columns always share the same full-demand basis.
        visible_available = max(available - committed, 0.0)
        visible_hold = backed_order_holds.get((product.id, ingredient_id), 0.0)
        visible_made_overlap = min(made, visible_hold)
        visible_coverage = max(visible_available - (made - visible_made_overlap), 0.0) + max(
            made, visible_hold)
        if is_order_line and not is_material:
            # Only this order's recorded output covers its finished-goods line.
            # General warehouse stock and holds do not reduce production demand.
            visible_coverage = made
        deficit = max(need - visible_coverage, 0.0)
        if not is_material and mode == 'from_zero':
            deficit = need
        if not is_material and deficit > 1e-9:
            deficit = float(math.ceil(deficit - 1e-9))
        if deficit <= 1e-9:
            source = 'stock'
        elif is_material:
            source = 'buy'
        else:
            source = 'produce'

        node = {
            'product_id': product.id,
            'name': product.name,
            'sku': product.sku,
            'product_type': classify_product_type(process, product.id, semi_ids),
            'is_material': is_material,
            'is_order_line': is_order_line,
            'is_or': False,
            'need': _round(need),
            'shortage_need': _round(shortage_need),
            'available': _round(available),
            'committed': _round(committed),
            'committed_for_calc': _round(committed_for_calc),
            'made': _round(made),
            'from_order_hold': _round(from_order_hold),
            'from_stock': _round(from_stock),
            'deficit': _round(deficit),
            'plan_deficit': _round(plan_deficit),
            'source': source,
            'count_shortage': count_shortage,
            'children': [],
        }

        # Отображаем полную техкарту от общего need, а дефицит считаем только
        # по незакрытому хвосту. Так "Нужно" остаётся потребностью заказа, а не
        # потребностью к дозапуску.
        child_count_shortage = count_shortage and plan_deficit > 0
        if not is_material and need > 0 and process.id not in visiting:
            v2 = visiting | {process.id}
            ings = (process.ingredients.all() if catalog_cache is not None else
                    process.ingredients.prefetch_related('options__source_product__process').all())
            for child_index, ing in enumerate(ings):
                qty = float(ing.quantity)
                child_need = qty * need
                child_shortage_need = qty * plan_deficit if child_count_shortage else 0.0
                node['children'].append(walk_ingredient(
                    ing, child_need, v2, child_count_shortage,
                    shortage_need=child_shortage_need, path=f'{path}.{child_index}'))
        return node

    def walk_ingredient(ing, need, visiting, count_shortage=True, shortage_need=None, path=None):
        if shortage_need is None:
            shortage_need = need
        opts = (sorted(ing.options.all(), key=lambda option: (option.priority, option.id))
                if catalog_cache is not None else
                list(ing.options.select_related('source_product__process').order_by('priority', 'id')))
        # 0-1 вариант → прежнее поведение (материал или ПФ)
        if len(opts) <= 1:
            prod = opts[0].source_product if opts else ing.source_product
            n = walk(prod, need, visiting, count_shortage=count_shortage,
                     shortage_need=shortage_need, path=path,
                     ingredient_id=ing.id)
            n['ingredient_id'] = ing.id
            return n

        # ИЛИ-группа материалов: waterfall по приоритету
        remaining = shortage_need if count_shortage else 0.0
        alternatives = []
        group_available = 0.0   # итоги по группе: наличие/резерв суммируем по всем вариантам
        group_committed = 0.0
        group_visible_coverage = 0.0
        for opt in opts:
            p = opt.source_product
            a_raw, c_raw = raw(p.id)
            c_calc = max(c_raw - own_committed_map.get(p.id, 0.0), 0.0)
            group_available += a_raw
            group_committed += c_raw
            hold_key = (p.id, ing.id)
            backed_hold = backed_order_holds.get(hold_key, 0.0)
            hold_take = min(remaining, order_hold_pool.get(hold_key, 0.0)) if (
                count_shortage and remaining > 0) else 0.0
            if hold_take > 0:
                order_hold_pool[hold_key] = max(
                    order_hold_pool.get(hold_key, 0.0) - hold_take, 0.0)
                remaining -= hold_take
            take = min(remaining, avail(p.id)) if count_shortage and remaining > 0 else 0.0
            if take > 0:
                pool[p.id] = max(avail(p.id) - take, 0.0)
                remaining -= take
            group_visible_coverage += max(a_raw - c_raw, 0.0) + backed_hold
            alternatives.append({
                'product_id': p.id, 'name': p.name, 'sku': p.sku,
                'priority': opt.priority,
                'available': _round(a_raw), 'committed': _round(c_raw),
                'committed_for_calc': _round(c_calc),
                'from_order_hold': _round(hold_take),
                'from_stock': _round(take),
            })
        deficit = remaining if count_shortage and remaining > 1e-9 else 0.0
        primary = opts[0].source_product
        total_from_stock = sum(alt['from_stock'] for alt in alternatives) if count_shortage else 0.0
        total_from_order_hold = sum(
            alt['from_order_hold'] for alt in alternatives) if count_shortage else 0.0
        return {
            'product_id': primary.id,
            'name': primary.name, 'sku': primary.sku,
            'product_type': 'material',
            'is_material': True, 'is_or': True,
            'ingredient_id': ing.id,
            'need': _round(need),
            'shortage_need': _round(shortage_need),
            # итоги ИЛИ-группы, а не только основного варианта
            'available': _round(group_available),
            'committed': _round(group_committed),
            'made': _round(made_map.get(primary.id, 0.0)),
            'from_stock': _round(total_from_stock),
            'from_order_hold': _round(total_from_order_hold),
            'deficit': _round(max(need - group_visible_coverage, 0.0)),
            'plan_deficit': _round(deficit),
            'source': 'stock' if deficit <= 1e-9 else 'buy',
            'count_shortage': count_shortage,
            'alternatives': alternatives,
            'children': [],
        }

    tree = []
    if root_product is not None:
        tree.append(walk(root_product, float(root_quantity or 1), set(), is_order_line=False, path='r0'))
    else:
        for root_index, line in enumerate(order.lines.select_related('product', 'product__process').all()):
            tree.append(walk(line.product, float(line.quantity), set(), is_order_line=True,
                             count_shortage=production_roots is None, path=f'r{root_index}'))

    # сводка материалов — сумма листьев-материалов по всему заказу
    summary = {}

    def collect(node):
        if not node.get('count_shortage', True):
            return
        if node['is_material'] and node.get('is_or'):
            prim_id = node['alternatives'][0]['product_id']
            for alt in node['alternatives']:
                s = summary.setdefault(alt['product_id'], {
                    'product_id': alt['product_id'], 'name': alt['name'], 'sku': alt['sku'],
                    'available': alt['available'], 'committed': alt['committed'],
                    'need': 0.0, 'deficit': 0.0,
                })
                s['need'] += alt['from_order_hold'] + alt['from_stock']
            summary[prim_id]['need'] += node['plan_deficit']
            summary[prim_id]['deficit'] += node['plan_deficit']
        elif node['is_material']:
            s = summary.setdefault(node['product_id'], {
                'product_id': node['product_id'], 'name': node['name'], 'sku': node['sku'],
                'available': node['available'], 'committed': node['committed'],
                'need': 0.0, 'deficit': 0.0,
            })
            s['need'] += node.get('shortage_need', node['need'])
            s['deficit'] += node['plan_deficit']
        for c in node['children']:
            collect(c)

    for root in tree:
        collect(root)

    materials_summary = [
        {**s, 'need': _round(s['need']), 'deficit': _round(s['deficit'])}
        for s in summary.values()
    ]
    max_buildable, limiting = _max_buildable(tree, materials_summary)
    return {
        'tree': tree,
        'materials_summary': materials_summary,
        'orderQty': _round(float(root_quantity or 1) if root_product is not None
                           else sum(float(l.quantity) for l in order.lines.all())),
        'producing': _round(sum(r['deficit'] for r in tree)),
        'maxBuildable': max_buildable,
        'limitingMaterial': limiting,
    }
