"""Daily section targets and zero-filled risk series; no database writes."""
from collections import defaultdict
from datetime import date, timedelta
from decimal import Decimal, ROUND_HALF_UP
from .output_calculation import D, KEY, frontiers

MONTHLY_EXPENSES = D('1000000')
WORKDAYS = 22
BASE_DAILY = MONTHLY_EXPENSES / WORKDAYS

def money(value):
    return D(value).quantize(D('.01'), rounding=ROUND_HALF_UP)

def percent(value, plan):
    return (D(value) / plan * 100).quantize(D('.1'), rounding=ROUND_HALF_UP) if plan > 0 else None

def build_performance(data, reference, date_from, date_to, today, area_filter=None):
    start, end, now = map(date.fromisoformat, (date_from, date_to, today))
    products = {p['id']: {**p, 'make': p['process__make'], 'unit': p['process__unit__name'] or ''} for p in data['products']}
    bom, ops = defaultdict(list), defaultdict(set)
    names = {a['id']: a['name'] for a in data['areas']}
    for row in data['ingredients']:
        bom[row['process_id']].append((row['source_product_id'], D(row['quantity'])))
    for row in data['process_operations']:
        ops[row['process_id']].add(row['area_id'])
    routes, rates = {}, {}
    for pid, p in products.items():
        value = ((p.get('options') or {}).get(KEY) or {}).get('unit_value')
        if value is None or D(value) <= 0 or not p['make']:
            continue
        try:
            tree = frontiers(pid, products, bom, ops, names)
            routes[pid] = {aid for aid, nodes in tree.items() if any(n['weight'] > 0 and n['denominator'] > 0 for n in nodes.values())}
            rates[pid] = D(value)
        except (ValueError, KeyError, RecursionError):
            continue
    eligible = set().union(*routes.values()) if routes else set()
    area_list = [a for a in data['areas'] if a['id'] in eligible]
    saw = next((a['id'] for a in area_list if a['name'].strip().casefold() == 'пила'), None)
    laser = next((a['id'] for a in area_list if a['name'].strip().casefold() == 'лазер'), None)
    members = [saw, laser] if saw and laser else []
    packaging = {a['id'] for a in area_list if a['name'].strip().casefold() == 'упаковка'}
    monthly_mix, fallback = defaultdict(lambda: defaultdict(Decimal)), defaultdict(Decimal)
    byname = {p['name']: p['id'] for p in products.values()}
    for row in reference['rows']:
        if row['areaId'] in packaging and row['amount'] is not None and D(row['amount']) > 0:
            pid = row.get('finishedProductId') or byname.get(row.get('finishedProduct'))
            if pid in routes:
                monthly_mix[row['date'][:7]][pid] += D(row['amount'])
    for line in data.get('order_lines', []):
        if line['product_id'] in routes:
            fallback[line['product_id']] += max(D(line['quantity']), D(0)) * rates[line['product_id']]
    if not sum(fallback.values(), D(0)):
        fallback.update(rates)
    all_months = set()
    day = start
    dates = []
    while day <= end:
        if day.weekday() < 5:
            dates.append(day); all_months.add(day.strftime('%Y-%m'))
        day += timedelta(days=1)
    shares, basis = {}, []
    for month in sorted(all_months):
        mix = monthly_mix.get(month) or fallback
        total = sum(mix.values(), D(0))
        shares[month] = {aid: sum((v for pid, v in mix.items() if aid in routes[pid]), D(0)) / total if total > 0 else D(0) for aid in eligible}
        basis.append({'month': month, 'source': 'Упакованный ассортимент за месяц' if monthly_mix.get(month) else 'Состав заказов; при отсутствии — каталог',
                      'referenceAmount': money(total), 'shares': [{'areaId': a['id'], 'area': a['name'], 'percent': percent(shares[month][a['id']], D(1))} for a in area_list]})
    observed = defaultdict(lambda: {'amount': D(0), 'operations': 0, 'unpriced': 0, 'excluded': 0})
    for row in reference['rows']:
        if not date_from <= row['date'] <= date_to or row['areaId'] not in eligible:
            continue
        item = observed[(row['date'], row['areaId'])]
        item['operations'] += 1
        if row['amount'] is not None:
            item['amount'] += D(row['amount'])
        elif row.get('issue') == 'Нет оценки готового изделия':
            item['unpriced'] += 1
        else:
            item['excluded'] += 1
    options = ([{'id': 'cutting', 'name': 'Пила + лазер', 'members': members}] if members else []) + area_list
    rows, summaries = [], []
    for area in options:
        aid = area['id']
        ids = members if aid == 'cutting' else [aid]
        amount_total, elapsed_raw, full_raw, last_full_rounded = D(0), D(0), D(0), D(0)
        streak = max_streak = idle_days = zero_days = workdays = unpriced = operations = 0
        last = None
        for day in dates:
            stamp = day.isoformat(); month = stamp[:7]
            working, future, current = day.weekday() < 5, day > now, day == now
            factor = sum((shares[month].get(i, D(0)) for i in ids), D(0))
            raw_plan = BASE_DAILY * (D(1) if aid == 'cutting' and factor > 0 else factor) if working else D(0)
            full_raw += raw_plan
            plan = money(full_raw) - last_full_rounded; last_full_rounded = money(full_raw)
            items = [observed[(stamp, i)] for i in ids]
            raw_amount = sum((i['amount'] for i in items), D(0))
            amount = money(raw_amount / factor) if aid == 'cutting' and factor > 0 else money(raw_amount)
            count, unknown, excluded = (sum(i[k] for i in items) for k in ('operations', 'unpriced', 'excluded'))
            peer = laser if aid == saw else saw if aid == laser else None
            peer_work = observed[(stamp, peer)]['operations'] > 0 if peer else False
            if not future:
                elapsed_raw += raw_plan; amount_total += amount
                operations += count; unpriced += unknown
                if working:
                    workdays += 1
                if working and not current:
                    if unknown:
                        streak = 0
                    elif amount < plan and plan > 0:
                        streak += 1
                    else:
                        streak = 0
                    max_streak = max(max_streak, streak)
                    if not count:
                        zero_days += 1
                        if not peer_work:
                            idle_days += 1
            if future:
                status = 'План' if working else 'Выходной'
            elif not working:
                status = 'Выпуск в выходной' if count else 'Выходной'
            elif unknown:
                status = 'Неполная оценка'
            elif not count and peer_work:
                status = 'Работа на лазере' if peer == laser else 'Работа на пиле'
            elif not count:
                status = 'Нет выпуска'
            elif not amount and excluded:
                status = 'Внутренняя работа / исключено'
            elif plan <= 0:
                status = 'Вне базового ассортимента'
            else:
                status = 'Норма выполнена' if amount >= plan else 'Ниже нормы'
            if current:
                status = 'Сегодня · ' + status.lower()
            last = {'date': stamp, 'areaId': aid, 'area': area['name'], 'amount': None if future else amount, 'plan': plan,
                    'completion': percent(amount, plan) if not future else None,
                    'gap': money(amount - plan) if not future else None,
                    'cumulativeAmount': money(amount_total) if not future else None, 'cumulativePlan': money(elapsed_raw) if not future else None,
                    'cumulativeGap': money(amount_total - elapsed_raw) if not future else None,
                    'cumulativeCompletion': percent(amount_total, money(elapsed_raw)) if not future else None,
                    'belowStreak': streak, 'status': status, 'operations': count if not future else 0, 'unpriced': unknown if not future else 0,
                    'isWorkingDay': working, 'isFuture': future, 'isToday': current,
                    'peerWorking': peer_work, 'routeShare': percent(factor, D(1)), 'isGroup': aid == 'cutting'}
            rows.append(last)
        plan_total = money(elapsed_raw)
        summaries.append({'areaId': aid, 'area': area['name'], 'amount': money(amount_total), 'plan': plan_total, 'periodPlan': money(full_raw),
                          'completion': percent(amount_total, plan_total), 'gap': money(amount_total - plan_total), 'workingDays': workdays,
                          'belowStreak': streak, 'maxBelowStreak': max_streak, 'idleDays': idle_days, 'zeroDays': zero_days,
                          'unpriced': unpriced, 'operations': operations, 'isGroup': aid == 'cutting',
                          'risk': 'incomplete' if unpriced else ('below' if money(amount_total - plan_total) < 0 and plan_total > 0 else 'above' if plan_total > 0 else 'no_plan')})
    overall = overall_performance(summaries, rows, members)
    if area_filter is not None:
        rows = [r for r in rows if r['areaId'] == area_filter]
        summaries = [r for r in summaries if r['areaId'] == area_filter]
    return {'monthlyExpenses': MONTHLY_EXPENSES, 'workdaysPerMonth': WORKDAYS, 'baseDailyTarget': money(BASE_DAILY),
            'schedule': 'Пятидневка: понедельник — пятница; 22 расчётных дня в месяц', 'days': rows, 'areas': summaries,
            'overall': overall, 'areaOptions': options, 'routeBasis': basis, 'today': today,
            'notes': ['Норма участка = 1 000 000 / 22 × доля ассортимента, проходящего через участок.',
                      'Доля определяется по упакованному ассортименту полного месяца; если его нет — по составу заказов, затем каталогу. Фильтры её не меняют.',
                      'Пила + лазер: сумма фактов / сумма долей маршрута, сравнивается с одной базовой нормой. Процент равен сумме фактов / сумме планов обоих участков.',
                      'Выходные исключены. Выпуск субботы отнесён на пятницу, воскресенья — на понедельник; исходная дата видна в операциях. Сегодня предварительно, будущие дни только в плане.',
                      'Общее выполнение — среднее процентов участков с положительной нормой; Пила + лазер считается одной группой. Каждый участок имеет одинаковый вес. Фильтры участка и сотрудника не меняют общий показатель.',
                      'Серия недовыпуска считается по завершённым рабочим дням выбранного периода. Выходные не прерывают серию.',
                      'План, риски и график относятся ко всему участку. Фильтр сотрудника меняет только таблицы выработки и операций.',
                      'Отклонение показывает риск по производственной оценке. Оно не заменяет фактическую прибыль от продаж.']}


def overall_performance(summaries, rows, shared_members):
    """Equal-weight section percentages; never sum monetary output across stages."""
    def mean(items, cumulative=False):
        amount_key, plan_key = ('cumulativeAmount', 'cumulativePlan') if cumulative else ('amount', 'plan')
        values = [(item, D(item[amount_key]) / D(item[plan_key]) * 100)
                  for item in items if item.get(amount_key) is not None and D(item.get(plan_key)) > 0]
        if not values:
            return {'completion': None, 'sectionCount': 0, 'belowCount': 0, 'bottleneck': None, 'incompleteSections': 0}
        low, value = min(values, key=lambda x: x[1])
        return {'completion': (sum((v for _, v in values), D(0)) / len(values)).quantize(D('.1'), rounding=ROUND_HALF_UP),
                'sectionCount': len(values), 'belowCount': sum(v < 100 for _, v in values),
                'bottleneck': {'areaId': low['areaId'], 'area': low['area'], 'completion': value.quantize(D('.1'), rounding=ROUND_HALF_UP)},
                'incompleteSections': sum(item.get('unpriced', 0) > 0 for item, _ in values)}
    selected = [a for a in summaries if a['areaId'] not in shared_members]
    result = mean(selected)
    byday = defaultdict(list)
    for row in rows:
        if row['areaId'] not in shared_members:
            byday[row['date']].append(row)
    days = []
    for stamp, items in sorted(byday.items()):
        current, cumulative = mean(items), mean(items, True)
        bottleneck = current['bottleneck']
        future = any(i['isFuture'] for i in items)
        today = any(i['isToday'] for i in items)
        days.append({'date': stamp, 'areaId': 'overall', 'area': 'Все участки · среднее',
                     'completion': current['completion'], 'cumulativeCompletion': cumulative['completion'],
                     'belowCount': current['belowCount'], 'sectionCount': current['sectionCount'],
                     'bottleneckName': bottleneck['area'] if bottleneck else '',
                     'bottleneckCompletion': bottleneck['completion'] if bottleneck else None,
                     'isFuture': future, 'isToday': today, 'isWorkingDay': True,
                     'status': 'План' if future else 'Сегодня · предварительно' if today else 'Неполная оценка' if current['incompleteSections'] else 'Ниже нормы' if current['completion'] is not None and current['completion'] < 100 else 'Норма выполнена' if current['completion'] is not None else 'Нет нормы'})
    return {**result, 'days': days}
