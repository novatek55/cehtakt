"""Transactional actions for visual groups; original jobs and ledger stay separate."""
import math
from decimal import Decimal, InvalidOperation
from types import SimpleNamespace

from django.contrib.auth import get_user_model
from django.db import transaction
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.directory.permissions import own_tasks_only
from .area_rules import area_staff, validate_executor
from .models import JobOperation, ProductionJob
from .views import (
    TaskStartView, TaskFinishView, TaskCancelStartView,
    _can_change_operation_state, _can_assign_executor, _job_inputs, _lock_task_stocks,
)


def _quantity(data, key, default=None):
    value = data.get(key, default)
    try:
        if isinstance(value, bool):
            raise ValueError
        result = Decimal(str(value))
        if (not result.is_finite() or result < 0 or result >= Decimal('100000000000')
                or result != result.quantize(Decimal('.001'))):
            raise ValueError
        return result
    except (InvalidOperation, ValueError, TypeError):
        raise ValueError('Некорректное количество: укажите неотрицательное число с точностью до 0,001.')


def _identity(op):
    return (op.job.product_id, op.job.process_id, op.area_id, op.operation_id,
            op.name, op.sequence, op.executor_id, op.status, op.job.rework_id)


class TaskBatchView(APIView):
    """PATCH /api/tasks/batch/ — invoke normal task actions in database queue order."""

    @transaction.atomic
    def patch(self, request):
        data = request.data
        ids = data.get('opIds')
        if (not isinstance(ids, list) or not ids
                or any(type(pk) is not int or pk <= 0 or pk > 9223372036854775807 for pk in ids)
                or len(set(ids)) != len(ids)):
            return Response({'error': 'Укажите неповторяющиеся идентификаторы операций.'}, status=400)
        action = data.get('action')
        handlers = {'start': TaskStartView, 'finish': TaskFinishView, 'cancel-start': TaskCancelStartView}
        if not isinstance(action, str) or action not in handlers:
            return Response({'error': 'Неизвестное действие.'}, status=400)
        try:
            amount = _quantity(data, 'quantity') if action == 'start' else Decimal(0)
            good = _quantity(data, 'good', 0) if action == 'finish' else Decimal(0)
            defect = _quantity(data, 'defect', 0) if action == 'finish' else Decimal(0)
            if action != 'cancel-start' and amount + good + defect <= 0:
                raise ValueError('Количество должно быть больше нуля.')
        except ValueError as exc:
            return Response({'error': str(exc)}, status=400)
        executor = data.get('executorId')
        if executor is not None and (type(executor) is not int or executor <= 0
                or executor > 9223372036854775807 or not get_user_model().objects.filter(pk=executor).exists()):
            return Response({'error': 'Исполнитель не найден.'}, status=400)

        # Match finish/cancellation lock order: every job, then operations, then stocks.
        job_ids = set(JobOperation.objects.filter(pk__in=ids).values_list('job_id', flat=True))
        list(ProductionJob.objects.select_for_update().filter(pk__in=job_ids).order_by('pk'))
        ops = list(JobOperation.objects.select_for_update().filter(pk__in=ids).order_by('pk'))
        if len(ops) != len(ids) or any(op.job_id not in job_ids for op in ops):
            return Response({'error': 'Состав заданий изменился. Обновите страницу.'}, status=404)
        for op in ops:
            if (not _can_change_operation_state(request.user, op.area_id)
                    or (own_tasks_only(request.user) and op.executor_id != request.user.id)):
                return Response({'error': 'Нет доступа к выбранным заданиям.'}, status=403)
        expected_status = 'waiting' if action == 'start' else 'in_progress'
        if any(op.status != expected_status or _identity(op) != _identity(ops[0]) for op in ops):
            return Response({'error': 'Состав или статус группы изменился. Обновите страницу.'}, status=400)
        ops.sort(key=lambda op: (op.job.rank, op.job_id, op.sequence, op.pk))
        stocks = _lock_task_stocks(ops[0].job)
        if action == 'start':
            total = sum(op.quantity for op in ops)
            _, _, max_units = _job_inputs(ops[0].job, stocks, planned_quantity=total)
            cap = total if max_units is None else min(total, Decimal(math.floor(max_units)))
            if amount > cap:
                transaction.set_rollback(True)
                return Response({'error': f'По плану и общему остатку можно взять не больше {max(0, cap):g} шт.'}, status=400)

        # Allocate goods first, then defect over the unused planned capacity.
        allocations = []
        for index, op in enumerate(ops):
            last = index == len(ops) - 1
            if action == 'start':
                take = min(amount, op.quantity)
                amount -= take
                payload = {'quantity': take} if take else None
            elif action == 'finish':
                take_good = good if last else min(good, op.quantity)
                good -= take_good
                take_defect = defect if last else min(defect, max(Decimal(0), op.quantity - take_good))
                defect -= take_defect
                payload = {'good': take_good, 'defect': take_defect} if take_good + take_defect else None
            else:
                payload = {}
            if payload is not None:
                allocations.append((op, payload))
        results = []
        handler = handlers[action]()
        for op, payload in allocations:
            if executor is not None:
                payload['executorId'] = executor
            response = handler.patch(SimpleNamespace(user=request.user, data=payload), pk=op.pk)
            if response.status_code >= 400:
                transaction.set_rollback(True)
                return Response({'error': f'Задание {op.job.name}: {response.data.get("error", "Не удалось выполнить действие.")}'},
                                status=response.status_code)
            results.append(response.data)
        return Response({'results': results, 'opIds': [row['id'] for row in results]})


class TaskAssignExecutorsView(APIView):
    """Minimal active staff list for users allowed to assign production tasks."""

    def get(self, request):
        if not _can_assign_executor(request.user):
            return Response({'error': 'Недостаточно прав для назначения исполнителя.'}, status=403)
        raw = request.query_params.get('areaIds') or request.query_params.get('areaId')
        if not raw:
            return Response([])
        try:
            areas = {int(value) for value in raw.split(',')}
            if not areas or any(area <= 0 or area > 9223372036854775807 for area in areas):
                raise ValueError
        except (TypeError, ValueError):
            return Response({'error': 'Некорректный участок.'}, status=400)
        users = get_user_model().objects.filter(is_active=True)
        for area in areas:
            users = users.filter(pk__in=area_staff(area).values('pk'))
        users = users.order_by('last_name', 'first_name', 'username')
        return Response([{'id': user.pk, 'name': user.get_full_name() or user.username} for user in users])


class TaskAssignView(APIView):
    """Assign every selected operation without changing quantities or work state."""

    @transaction.atomic
    def patch(self, request):
        if not _can_assign_executor(request.user):
            return Response({'error': 'Недостаточно прав для назначения исполнителя.'}, status=403)
        data = request.data
        if not isinstance(data, dict):
            return Response({'error': 'Некорректные данные назначения.'}, status=400)
        rows = data.get('rows')
        if not isinstance(rows, list) or not rows:
            return Response({'error': 'Выберите карточки для назначения.'}, status=400)
        expected = {}
        try:
            for row in rows:
                if not isinstance(row, dict):
                    raise ValueError('Некорректная выбранная операция.')
                pk = row.get('opId')
                if type(pk) is not int or not 0 < pk <= 9223372036854775807 or pk in expected:
                    raise ValueError('Укажите неповторяющиеся идентификаторы операций.')
                previous_executor = row.get('executorId')
                if previous_executor is not None and (type(previous_executor) is not int
                        or not 0 < previous_executor <= 9223372036854775807):
                    raise ValueError('Некорректный прежний исполнитель.')
                if type(row.get('status')) is not str or row['status'] not in dict(JobOperation.Status.choices):
                    raise ValueError('Некорректный статус выбранной операции.')
                expected[pk] = (previous_executor, row['status'],
                    *(_quantity(row, key) for key in ('quantity', 'startedQuantity', 'done', 'defect')))
        except ValueError as exc:
            return Response({'error': str(exc)}, status=400)
        executor = data.get('executorId')
        if (type(executor) is not int or not 0 < executor <= 9223372036854775807
                or not get_user_model().objects.filter(pk=executor, is_active=True).exists()):
            return Response({'error': 'Выберите действующего исполнителя.'}, status=400)

        # Same lock order as start, finish and cancellation: jobs, then operations.
        job_ids = set(JobOperation.objects.filter(pk__in=expected).values_list('job_id', flat=True))
        list(ProductionJob.objects.select_for_update().filter(pk__in=job_ids).order_by('pk'))
        ops = list(JobOperation.objects.select_for_update().filter(pk__in=expected).order_by('pk'))
        conflict = {'error': 'Выбранные работы изменились. Обновите задания и выберите карточки заново.'}
        if len(ops) != len(expected) or any(op.job_id not in job_ids for op in ops):
            return Response(conflict, status=409)
        for op in ops:
            if (not _can_change_operation_state(request.user, op.area_id)
                    or (own_tasks_only(request.user) and op.executor_id != request.user.id)):
                return Response({'error': 'Нет доступа к выбранным заданиям.'}, status=403)
            actual = (op.executor_id, op.status, op.quantity,
                      op.started_quantity, op.done_quantity, op.defect_quantity)
            if op.status == JobOperation.Status.DONE or actual != expected[op.pk]:
                return Response(conflict, status=409)
        try:
            for area_id in {op.area_id for op in ops}:
                validate_executor(executor, area_id)
        except ValueError as exc:
            return Response({'error': str(exc)}, status=400)
        changed = [op.pk for op in ops if op.executor_id != executor]
        if changed:
            JobOperation.objects.filter(pk__in=changed).update(executor_id=executor)
        return Response({'count': len(ops), 'changedCount': len(changed),
                         'quantity': float(sum((op.quantity for op in ops), Decimal(0))),
                         'executorId': executor, 'opIds': [op.pk for op in ops]})
