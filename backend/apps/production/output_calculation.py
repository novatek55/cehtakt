"""Physical stage equivalents. Decimal arithmetic; no database writes."""
from collections import defaultdict
from decimal import Decimal
from datetime import timedelta

D = lambda value: Decimal(str(value or 0))
KEY = 'production_output_valuation'

def credit(quantity, capacity, used, key):
    value = min(max(D(quantity), D(0)), max(D(capacity) - used.get(key, D(0)), D(0)))
    used[key] = used.get(key, D(0)) + value
    return value

def frontiers(root, products, ingredients, operations, areas):
    result = defaultdict(dict)
    def weight(pid, area, trail=()):
        if pid in trail:
            raise ValueError('Цикл техкарты')
        p = products[pid]
        name = areas.get(area, '').casefold()
        children = ingredients.get(p['process_id'], [])
        if 'пил' in name:
            total = D(0)
            for child, qty in children:
                cp = products[child]
                if cp.get('unit', '').strip().casefold() in ('м', 'м.', 'метр', 'метры', 'п.м.', 'пог. м', 'м. пог.'):
                    total += D(qty)
                elif cp['make']:
                    total += D(qty) * weight(child, area, trail + (pid,))
            return total
        if 'свар' in name:
            return sum((D(qty) * (weight(child, area, trail + (pid,))
                       if area in operations.get(products[child]['process_id'], set()) else D(1))
                       for child, qty in children if products[child]['make']), D(0))
        return D(1)
    def walk(pid, units, seen_areas, trail):
        if pid in trail:
            raise ValueError('Цикл техкарты')
        p = products[pid]
        if not p['make']:
            return
        own = operations.get(p['process_id'], set())
        for area in own - seen_areas:
            w = weight(pid, area)
            node = result[area].setdefault(pid, {'units': D(0), 'weight': w})
            node['units'] += units
        for child, qty in ingredients.get(p['process_id'], []):
            walk(child, units * D(qty), seen_areas | own, trail + (pid,))
    walk(root, D(1), set(), ())
    for area, nodes in result.items():
        denominator = sum((v['units'] * v['weight'] for v in nodes.values()), D(0))
        for v in nodes.values():
            v['denominator'] = denominator
            v['unit'] = 'м' if 'пил' in areas.get(area, '').casefold() else ('дет.' if 'свар' in areas.get(area, '').casefold() else 'шт.')
    return dict(result)


def calculate(data, date_from, date_to, area_id=None, executor_id=None):
    products = {p['id']: {**p, 'make': p['process__make'], 'unit': p['process__unit__name'] or ''} for p in data['products']}
    jobs = {j['id']: j for j in data['jobs']}
    areas = {a['id']: a['name'] for a in data['areas']}
    ingredients, operations = defaultdict(list), defaultdict(set)
    for i in data['ingredients']:
        ingredients[i['process_id']].append((i['source_product_id'], D(i['quantity'])))
    operation_identity = {op['id']: (op['process_id'], op['area_id']) for op in data['process_operations']}
    for op in data['process_operations']:
        operations[op['process_id']].add(op['area_id'])
    cache, used, rows, employee_choices = {}, {}, [], {}
    order_lines = defaultdict(dict)
    for line in data.get('order_lines', []):
        group = order_lines[line['order_id']]
        group[line['product_id']] = group.get(line['product_id'], D(0)) + D(line['quantity'])
    descendant_cache = {}
    def descendants(pid, trail=()):
        if pid in trail:
            raise ValueError('Цикл техкарты')
        out = {pid}
        for child, qty in ingredients.get(products[pid]['process_id'], []):
            out |= descendants(child, trail + (pid,))
        return out
    def contains(pid, child):
        if pid not in descendant_cache:
            try:
                descendant_cache[pid] = descendants(pid)
            except (ValueError, KeyError, RecursionError):
                descendant_cache[pid] = {pid}
        return child in descendant_cache[pid]
    from datetime import datetime
    from zoneinfo import ZoneInfo
    moscow = ZoneInfo('Europe/Moscow')
    events = sorted((o for o in data['operations'] if o['status'] == 'done' and o['finished_at']), key=lambda o: (str(o['finished_at']), o['id']))
    for op in events:
        stamp = op['finished_at']
        if isinstance(stamp, str):
            stamp = datetime.fromisoformat(stamp.replace('Z', '+00:00'))
        original_day = stamp.astimezone(moscow).date()
        booked_day = original_day + timedelta(days=-1 if original_day.weekday() == 5 else 1 if original_day.weekday() == 6 else 0)
        day = booked_day.isoformat()
        if day > date_to:
            continue
        j = jobs[op['job_id']]
        root, trail = j, set()
        while root['parent_id'] in jobs and root['id'] not in trail:
            trail.add(root['id']); root = jobs[root['parent_id']]
        root_pid, capacity, scope = root['product_id'], D(root['quantity']), ('job', root['id'])
        ambiguous = False
        line_group = order_lines.get(j['sales_order_id'], {})
        candidates = [pid for pid in line_group if contains(pid, root_pid)]
        if root_pid in line_group:
            candidates = [root_pid]
        if len(candidates) == 1:
            root_pid = candidates[0]
            capacity = line_group[root_pid]
            scope = ('order', j['sales_order_id'], root_pid)
        elif len(candidates) > 1:
            ambiguous = True
        product, final = products[j['product_id']], products[root_pid]
        price = (final.get('options') or {}).get(KEY) or {}
        rate = price.get('unit_value')
        issue, equivalent, amount, physical, credited, basis = '', None, None, None, D(0), ''
        if ambiguous:
            issue = 'Общая деталь нескольких изделий — нужна привязка'
        elif j['rework_id'] or root['rework_id']:
            issue = 'Переделка — без повторного начисления'
        elif (op['area_id'] not in operations.get(product['process_id'], set())
              or (op.get('operation_id') is not None and operation_identity.get(op['operation_id']) != (product['process_id'], op['area_id']))):
            issue = 'Операция вне текущей техкарты'
        else:
            try:
                if root_pid not in cache:
                    cache[root_pid] = frontiers(root_pid, products, ingredients, operations, areas)
                node = cache[root_pid].get(op['area_id'], {}).get(j['product_id'])
                if not node:
                    issue = 'Внутренняя операция участка — учтена в следующем узле'
                elif not node['denominator'] or not node['weight']:
                    issue = 'Нет физической базы этапа'
                else:
                    credited = credit(op['done_quantity'], capacity * node['units'], used, (scope, op['area_id'], j['product_id']))
                    physical = credited * node['weight']
                    equivalent = physical / node['denominator']
                    basis = node['unit']
                    if rate is None:
                        issue = 'Нет оценки готового изделия'
                    else:
                        amount = (equivalent * D(rate)).quantize(D('0.01'))
            except (ValueError, KeyError, RecursionError):
                issue = 'Неполная или циклическая техкарта'
        if day < date_from or (area_id and op['area_id'] != area_id):
            continue
        employee = ' '.join(filter(None, [op.get('executor__first_name'), op.get('executor__last_name')])) or op.get('executor__username') or 'Исполнитель не указан'
        employee_choices[op['executor_id'] or 0] = {'id': op['executor_id'] or 0, 'name': employee}
        if executor_id is not None and (op['executor_id'] or 0) != executor_id:
            continue
        rows.append({'id': op['id'], 'date': day, 'originalDate': original_day.isoformat(), 'isWeekendTransfer': booked_day != original_day, 'finishedAt': stamp.isoformat(), 'areaId': op['area_id'], 'area': areas.get(op['area_id'], ''),
                     'executorId': op['executor_id'], 'employee': employee, 'jobId': j['id'], 'task': j['name'],
                     'order': j.get('sales_order__name') or '', 'product': product['name'], 'finishedProduct': final['name'], 'finishedProductId': root_pid, 'sku': final['sku'],
                     'good': D(op['done_quantity']), 'defect': D(op['defect_quantity']), 'credited': credited,
                     'physical': physical, 'physicalUnit': basis, 'equivalent': equivalent, 'unitValue': D(rate) if rate is not None else None,
                     'amount': amount, 'issue': issue})
    def aggregate(keys):
        out = {}
        for r in rows:
            key = tuple(r[k] for k in keys)
            if key not in out:
                out[key] = {**{k: r[k] for k in keys}, 'amount': D(0), 'operations': 0, 'unpriced': 0, 'excluded': 0, 'good': D(0), 'defect': D(0)}
            item = out[key]; item['operations'] += 1; item['good'] += r['good']; item['defect'] += r['defect']
            if r['amount'] is not None:
                item['amount'] += r['amount']
            elif r['issue'] == 'Нет оценки готового изделия':
                item['unpriced'] += 1
            else:
                item['excluded'] += 1
        return sorted(out.values(), key=lambda r: tuple(str(r[k]) for k in keys))
    return {'dateFrom': date_from, 'dateTo': date_to, 'rows': rows,
            'areas': aggregate(['areaId', 'area']), 'days': aggregate(['date', 'areaId', 'area']),
            'employeesByDay': aggregate(['date', 'areaId', 'area', 'executorId', 'employee']),
            'employees': aggregate(['areaId', 'area', 'executorId', 'employee']),
            'areaOptions': data['areas'],
            'employeeOptions': sorted(employee_choices.values(), key=lambda r:r['name']),
            'rates': [{'productId': p['id'], 'product': p['name'], 'sku': p['sku'], **((p.get('options') or {}).get(KEY) or {})}
                      for p in products.values() if KEY in (p.get('options') or {})],
            'notes': ['Оценки пользователя: 21.07–20.10.2026, начислено минус материальная себестоимость и налог, на единицу с учётом возвратов.',
                      'Выработка: физическая доля этапа × оценка готового изделия. Пила — метры; сварка — детали; остальные — штуки.',
                      'Участки показывают каждый свою выработку. Суммы разных участков не складываются в прибыль предприятия.',
                      'Исторические операции пересчитываются по текущей техкарте и сохранённой оценке. Готовые складские остатки без операции не начисляются.']}
