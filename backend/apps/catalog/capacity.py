"""Integer output capacity using the same shared waterfall as consumption."""
from decimal import Decimal, ROUND_FLOOR


def shared_material_capacity(requirements, available):
    groups = [(tuple(dict.fromkeys(ids)), Decimal(str(per)), label)
              for ids, per, label in requirements if Decimal(str(per)) > 0]
    if not groups:
        return None, ''
    stock = {pid: max(Decimal(str(value)), Decimal(0)) for pid, value in available.items()}
    upper = min(int((sum((stock.get(pid, Decimal(0)) for pid in ids), Decimal(0)) / per)
                    .to_integral_value(rounding=ROUND_FLOOR)) for ids, per, _ in groups)

    def feasible(units):
        pool = dict(stock)
        for ids, per, label in groups:
            remaining = per * units
            for pid in ids:
                take = min(remaining, pool.get(pid, Decimal(0)))
                pool[pid] = pool.get(pid, Decimal(0)) - take
                remaining -= take
                if remaining <= 0:
                    break
            if remaining > 0:
                return False, label
        return True, ''

    lower = 0
    while lower < upper:
        middle = (lower + upper + 1) // 2
        if feasible(middle)[0]:
            lower = middle
        else:
            upper = middle - 1
    return lower, feasible(lower + 1)[1]
