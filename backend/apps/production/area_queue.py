"""Ephemeral shared ordering of active task cards, independent of plan ranks."""
import fcntl
import hashlib
import json
import os
from pathlib import Path

from rest_framework.response import Response

QUEUE_PATH = Path('/var/lib/proplan/task-queues.json')


def read_orders():
    try:
        return json.loads(QUEUE_PATH.read_text())
    except FileNotFoundError:
        return {}


def card_groups(operations, orders):
    areas = {}
    for op in operations:
        key = (op.job.product_id, op.job.process_id, op.area_id, op.operation_id,
               op.name, op.sequence, op.executor_id, op.status, op.job.rework_id)
        areas.setdefault(op.area_id, {}).setdefault(key, []).append(op.id)
    result = {}
    for area, grouped in areas.items():
        groups = list(grouped.values())
        saved = {op_id: pos for pos, op_id in enumerate(orders.get(str(area), []))}
        fallback = {op_id: len(saved) + pos for pos, op_id in enumerate(
            op_id for group in groups for op_id in group)}
        groups.sort(key=lambda group: min(saved.get(op_id, fallback[op_id]) for op_id in group))
        result[area] = groups
    return result


def revision(groups):
    return hashlib.sha256(json.dumps(groups, separators=(',', ':')).encode()).hexdigest()


def active_operations():
    from .models import JobOperation
    return list(JobOperation.objects.exclude(status=JobOperation.Status.DONE)
                .select_related('job').order_by('job__rank', 'job__id', 'sequence', 'id'))


def queue_snapshot():
    operations = active_operations()
    orders = read_orders()
    active = {op.id for op in operations}
    cleaned = {area: [value for value in values if value in active]
               for area, values in orders.items()}
    cleaned = {area: values for area, values in cleaned.items() if values}
    if cleaned != orders:
        with QUEUE_PATH.with_suffix('.lock').open('a') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            operations = active_operations()
            active = {op.id for op in operations}
            orders = {area: [value for value in values if value in active]
                      for area, values in read_orders().items()}
            orders = {area: values for area, values in orders.items() if values}
            temporary = QUEUE_PATH.with_suffix('.tmp')
            temporary.write_text(json.dumps(orders, separators=(',', ':')))
            os.replace(temporary, QUEUE_PATH)
    groups = card_groups(operations, orders)
    positions = {op_id: pos for cards in groups.values()
                 for pos, card in enumerate(cards, 1) for op_id in card}
    return groups, positions


def move_card(request):
    from .views import _can_change_operation_state
    from apps.directory.permissions import visible_area_ids, own_tasks_only
    try:
        area = request.data['areaId']
        ids = request.data['opIds']
        position = request.data['position']
        if (type(area) is not int or type(position) is not int or
                not isinstance(ids, list) or not ids or
                any(type(value) is not int for value in ids) or len(set(ids)) != len(ids)):
            raise ValueError()
    except (KeyError, TypeError, ValueError):
        return Response({'error': 'Укажите участок, карточку и целую позицию.'}, status=400)
    visible = visible_area_ids(request.user)
    if ((visible is not None and area not in visible) or
            not _can_change_operation_state(request.user, area)):
        return Response({'error': 'Нет доступа к очереди этого участка.'}, status=403)
    if own_tasks_only(request.user):
        return Response({'error': 'Для перестановки нужен доступ ко всей очереди участка.'}, status=403)
    try:
        with QUEUE_PATH.with_suffix('.lock').open('a') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            orders = read_orders()
            groups = card_groups(active_operations(), orders)
            cards = groups.get(area, [])
            if request.data.get('revision') != revision(cards):
                return Response({'error': 'Очередь уже изменилась. Обновили карточки — повторите перемещение.'}, status=409)
            source = next((idx for idx, card in enumerate(cards) if set(card) == set(ids)), None)
            if source is None:
                return Response({'error': 'Карточка изменилась или уже выполнена. Обновите очередь.'}, status=409)
            if not 1 <= position <= len(cards):
                return Response({'error': f'Позиция должна быть от 1 до {len(cards)}.'}, status=400)
            cards.insert(position - 1, cards.pop(source))
            # Only active operation IDs are retained; no number or completed history is saved.
            orders = {str(key): [op_id for card in value for op_id in card]
                      for key, value in groups.items() if str(key) in orders or key == area}
            temporary = QUEUE_PATH.with_suffix('.tmp')
            try:
                with temporary.open('w') as stream:
                    json.dump(orders, stream, separators=(',', ':'))
                    stream.flush()
                    os.fsync(stream.fileno())
                os.replace(temporary, QUEUE_PATH)
            finally:
                temporary.unlink(missing_ok=True)
    except (OSError, ValueError, TypeError):
        return Response({'error': 'Не удалось сохранить очередь. Попробуйте ещё раз.'}, status=503)
    return Response({'position': position, 'revision': revision(cards)})
