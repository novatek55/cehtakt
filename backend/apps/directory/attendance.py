"""Personal read-only attendance report from the BioTime purchasing board."""
from __future__ import annotations

from datetime import date, datetime, time, timedelta
from decimal import Decimal, ROUND_HALF_UP
from typing import Any

from django.contrib.auth import get_user_model
from django.db import IntegrityError, connections, transaction
from django.utils import timezone
from django.utils.decorators import method_decorator
from django.views.decorators.cache import never_cache
from rest_framework.permissions import BasePermission, IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from .models import UserProfile


STANDARD_HOURS = 8
DEFAULT_SHIFT_START = '09:00'
LATE_GRACE_MINUTES = 15
MISSING_MARK_PENALTY_EMPLOYEE = '26'
MISSING_MARK_PENALTY_FROM = date(2026, 9, 1)
MISSING_MARK_PENALTY_CENTS = -100000


def _business_today() -> date:
    return timezone.localtime(timezone.now()).date()


def _parse_date(value: str | None, fallback: date) -> date:
    if not value:
        return fallback
    try:
        return date.fromisoformat(value)
    except ValueError as error:
        raise ValueError('Дата должна иметь формат ГГГГ-ММ-ДД') from error


def _to_datetime(value: Any) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value
    if isinstance(value, date):
        return datetime.combine(value, time.min)
    try:
        return datetime.fromisoformat(str(value))
    except ValueError:
        return None


def _to_date(value: Any) -> date | None:
    if isinstance(value, date) and not isinstance(value, datetime):
        return value
    if isinstance(value, datetime):
        return value.date()
    try:
        return date.fromisoformat(str(value))
    except ValueError:
        return None


def _money_cents(value: Any) -> int:
    amount = Decimal(str(value or '0')).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
    return int(amount * 100)


def _money(cents: int) -> str:
    sign = '−' if cents < 0 else ''
    cents = abs(cents)
    rubles = f'{cents // 100:,}'.replace(',', ' ')
    fraction = cents % 100
    return f'{sign}{rubles},{fraction:02d} руб.' if fraction else f'{sign}{rubles} руб.'


def _manual_time(value: Any) -> str:
    if not value:
        return ''
    if isinstance(value, time):
        return value.strftime('%H:%M')
    return str(value)[:5]


def _manual_datetime(work_date: date, value: Any) -> datetime | None:
    raw = _manual_time(value)
    if not raw:
        return None
    try:
        hour, minute = [int(part) for part in raw.split(':', 1)]
        return datetime.combine(work_date, time(hour, minute))
    except (TypeError, ValueError):
        return None


def _round_arrival(value: datetime | None) -> datetime | None:
    if not value:
        return None
    start = value.replace(minute=0, second=0, microsecond=0)
    return start if value.minute <= 15 else start + timedelta(hours=1)


def _round_departure(value: datetime | None) -> datetime | None:
    if not value:
        return None
    return value.replace(minute=0, second=0, microsecond=0)


def _has_departure(arrival: datetime | None, departure: datetime | None, marks_count: int) -> bool:
    return bool(arrival and departure and marks_count >= 2 and departure > arrival)


def _worked_hours(arrival: datetime | None, departure: datetime | None, lunch_minutes: int, marks_count: int) -> int | None:
    if not _has_departure(arrival, departure, marks_count):
        return None
    start = _round_arrival(arrival)
    end = _round_departure(departure)
    if not start or not end or end <= start:
        return None
    minutes = max(0, int((end - start).total_seconds() // 60) - max(0, lunch_minutes))
    return minutes // 60


def _shift_start(work_dt: datetime, shift_start: str | None) -> datetime:
    value = (shift_start or DEFAULT_SHIFT_START)[:5]
    try:
        hour, minute = [int(part) for part in value.split(':', 1)]
    except (TypeError, ValueError):
        hour, minute = 9, 0
    return work_dt.replace(hour=hour, minute=minute, second=0, microsecond=0)


def _is_late(arrival: datetime | None, shift_start: str | None) -> bool:
    return bool(arrival and arrival > _shift_start(arrival, shift_start) + timedelta(minutes=LATE_GRACE_MINUTES))


def _is_early_departure(departure: datetime | None, marks_count: int) -> bool:
    return bool(departure and marks_count >= 2 and (departure.minute > 0 or departure.second > 0))


def _report_date(value: date) -> str:
    weekdays = ['пн.', 'вт.', 'ср.', 'чт.', 'пт.', 'сб.', 'вс.']
    return f'{weekdays[value.weekday()]} {value:%d.%m}'


def _time_text(value: datetime | None) -> str:
    return value.strftime('%H:%M') if value else '—'


def _employee_name(row: dict[str, Any]) -> str:
    return (str(row.get('full_name') or '').strip()
            or str(row.get('terminal_user_name') or '').strip()
            or str(row.get('terminal_user_id') or ''))


def _best_candidate(value: Any) -> str:
    items = [item.strip() for item in str(value or '').split('\n') if item.strip()]
    if not items:
        return ''

    def score(item: str) -> int:
        return sum(6 for char in item if '\u0400' <= char <= '\u04ff') - item.count('�') * 200 + min(len(item), 100)

    return sorted(items, key=score, reverse=True)[0]


def _normalize_employee_row(row: dict[str, Any]) -> dict[str, Any]:
    if not str(row.get('employee_number') or '').strip():
        row['employee_number'] = _best_candidate(row.get('employee_number_candidates'))
    if not str(row.get('full_name') or '').strip():
        row['full_name'] = _best_candidate(row.get('full_name_candidates'))
    if not str(row.get('terminal_user_name') or '').strip():
        row['terminal_user_name'] = _best_candidate(row.get('terminal_user_name_candidates'))
    row['status'] = row.get('status') or 'active'
    return row


def _reportable_on_date(employee: dict[str, Any], work_date: date) -> bool:
    if (employee.get('status') or 'active') == 'hidden':
        return False
    fired = _to_date(employee.get('fired_date'))
    return not ((employee.get('status') or 'active') == 'fired' and fired and work_date > fired)


def _dict_rows(cursor) -> list[dict[str, Any]]:
    columns = [column[0] for column in cursor.description]
    return [dict(zip(columns, row)) for row in cursor.fetchall()]


def _payroll_terms_for_date(history: list[dict[str, Any]], work_date: date, fallback: dict[str, Any]) -> dict[str, Any]:
    chosen = None
    for terms in history:
        effective = _to_date(terms.get('effective_from')) or date.min
        if effective <= work_date and (chosen is None or effective > (_to_date(chosen.get('effective_from')) or date.min)):
            chosen = terms
    return chosen or fallback


def _calculate_payroll(row: dict[str, Any], terms: dict[str, Any]) -> tuple[int | None, int, int]:
    arrival = row.get('effective_arrival_time') or row.get('arrival_time')
    departure = row.get('effective_departure_time') or row.get('departure_time')
    marks = int(row.get('marks_count') or 0)
    if row.get('manual_arrival_time') or row.get('manual_departure_time'):
        marks = max(2, marks)
    hours = _worked_hours(arrival, departure, int(terms.get('lunch_minutes') or 0), marks)
    has_departure = _has_departure(arrival, departure, marks)
    penalty = (
        MISSING_MARK_PENALTY_CENTS
        if str(row.get('terminal_user_id')) == MISSING_MARK_PENALTY_EMPLOYEE
        and (row.get('work_date') or date.min) >= MISSING_MARK_PENALTY_FROM
        and not has_departure
        else 0
    )
    if row.get('penalty_amount') is not None:
        penalty = -_money_cents(row['penalty_amount'])
    return hours, (hours or 0) * _money_cents(terms.get('hourly_rate')) + penalty, penalty


def _workdays(start: date, end: date) -> list[date]:
    days = []
    cursor = start
    while cursor <= end:
        if cursor.weekday() <= 4:
            days.append(cursor)
        cursor += timedelta(days=1)
    return days


def _employees(cursor, terminal_id=None):
    filter_sql = ' WHERE terminal_user_id=%s' if terminal_id is not None else ''
    cursor.execute(
        f"""SELECT ids.terminal_user_id,
                          s.employee_number,
                          s.full_name,
                          GROUP_CONCAT(DISTINCT NULLIF(r.employee_number, '') SEPARATOR '\n') AS employee_number_candidates,
                          GROUP_CONCAT(DISTINCT NULLIF(r.full_name, '') SEPARATOR '\n') AS full_name_candidates,
                          GROUP_CONCAT(DISTINCT NULLIF(r.terminal_user_name, '') SEPARATOR '\n') AS terminal_user_name_candidates,
                          COALESCE(s.shift_start_time, %s) AS shift_start_time,
                          COALESCE(s.lunch_minutes, 0) AS lunch_minutes,
                          COALESCE(s.hourly_rate, 0) AS hourly_rate,
                          COALESCE(s.status, 'active') AS status,
                          s.fired_date
                   FROM (
                       SELECT terminal_user_id FROM biotime_raw_events{filter_sql}
                       UNION SELECT terminal_user_id FROM biotime_employee_settings{filter_sql}
                       UNION SELECT terminal_user_id FROM biotime_payroll_payments{filter_sql}
                       UNION SELECT terminal_user_id FROM biotime_payroll_days{filter_sql}
                   ) ids
                   LEFT JOIN biotime_raw_events r ON r.terminal_user_id=ids.terminal_user_id
                   LEFT JOIN biotime_employee_settings s ON s.terminal_user_id=ids.terminal_user_id
                   GROUP BY ids.terminal_user_id, s.employee_number, s.full_name, s.shift_start_time,
                            s.lunch_minutes, s.hourly_rate, s.status, s.fired_date""",
        [DEFAULT_SHIFT_START + ':00'] + ([terminal_id] * 4 if terminal_id is not None else []),
    )
    return [_normalize_employee_row(row) for row in _dict_rows(cursor)]


@method_decorator(never_cache, name='dispatch')
class AttendanceView(APIView):
    permission_classes = [IsAuthenticated]


class IsAttendanceAdministrator(BasePermission):
    def has_permission(self, request, view):
        return request.user.is_superuser


class AttendanceEmployeesView(AttendanceView):
    permission_classes = [IsAuthenticated, IsAttendanceAdministrator]

    def get(self, request):
        if 'attendance' not in connections:
            return Response({'error': 'Учёт рабочего времени временно недоступен.'}, status=503)
        with connections['attendance'].cursor() as cursor:
            employees = _employees(cursor)
        return Response([
            {
                'terminalUserId': str(employee['terminal_user_id']),
                'label': f"{_employee_name(employee)} · BioTime {employee['terminal_user_id']}",
            }
            for employee in sorted(employees, key=lambda row: _employee_name(row).casefold())
        ])


class AttendanceUserLinkView(AttendanceView):
    permission_classes = [IsAuthenticated, IsAttendanceAdministrator]

    def patch(self, request, pk):
        terminal_id = request.data.get('attendanceTerminalUserId') if isinstance(request.data, dict) else None
        if not isinstance(terminal_id, str) or len(terminal_id.strip()) > 50:
            return Response({'error': 'Выберите сотрудника BioTime.'}, status=400)
        terminal_id = terminal_id.strip() or None
        user = get_user_model().objects.filter(pk=pk).first()
        if user is None:
            return Response({'error': 'Пользователь не найден.'}, status=404)
        if terminal_id:
            if 'attendance' not in connections:
                return Response({'error': 'Учёт рабочего времени временно недоступен.'}, status=503)
            with connections['attendance'].cursor() as cursor:
                employees = _employees(cursor, terminal_id)
            if not employees:
                return Response({'error': 'Сотрудник BioTime не найден.'}, status=400)
            terminal_id = str(employees[0]['terminal_user_id'])
        try:
            with transaction.atomic():
                UserProfile.objects.update_or_create(
                    user=user, defaults={'attendance_terminal_user_id': terminal_id},
                )
        except IntegrityError:
            if not UserProfile.objects.filter(attendance_terminal_user_id=terminal_id).exclude(user=user).exists():
                raise
            return Response({'error': 'Этот сотрудник BioTime уже привязан к другой учётной записи.'}, status=409)
        return Response({'attendanceTerminalUserId': terminal_id or ''})


class PersonalAttendanceView(AttendanceView):
    """Return only the BioTime employee attached to the current Proplan user."""

    def get(self, request):
        profile = getattr(request.user, 'profile', None)
        terminal_id = str(getattr(profile, 'attendance_terminal_user_id', '') or '').strip()
        if not terminal_id:
            return Response({'error': 'Для вашей учётной записи ещё не настроена привязка к BioTime. Обратитесь к администратору.'}, status=409)

        today = _business_today()
        try:
            start = _parse_date(request.query_params.get('from'), today.replace(day=1))
            end = _parse_date(request.query_params.get('to'), today)
        except ValueError as error:
            return Response({'error': str(error)}, status=400)
        if start > end:
            start, end = end, start

        if end > today or (end - start).days > 366 or start.year < 1000:
            return Response({'error': 'Укажите период до сегодняшнего дня включительно, не длиннее года.'}, status=400)
        if 'attendance' not in connections:
            return Response({'error': 'Учёт рабочего времени временно недоступен.'}, status=503)

        default_shift_sql = DEFAULT_SHIFT_START + ':00'
        with connections['attendance'].cursor() as cursor:
            employees = _employees(cursor, terminal_id)
            if not employees:
                return Response({'error': 'Привязанный сотрудник BioTime не найден. Обратитесь к администратору.'}, status=409)
            employee = employees[0]
            cursor.execute(
                """SELECT DATE(r.event_time) AS work_date, r.terminal_user_id,
                          s.employee_number,
                          s.full_name,
                          GROUP_CONCAT(DISTINCT NULLIF(r.employee_number, '') SEPARATOR '\n') AS employee_number_candidates,
                          GROUP_CONCAT(DISTINCT NULLIF(r.full_name, '') SEPARATOR '\n') AS full_name_candidates,
                          GROUP_CONCAT(DISTINCT NULLIF(r.terminal_user_name, '') SEPARATOR '\n') AS terminal_user_name_candidates,
                          MIN(event_time) AS arrival_time, MAX(event_time) AS departure_time, COUNT(*) AS marks_count,
                          COALESCE(s.shift_start_time, %s) AS shift_start_time,
                          COALESCE(s.lunch_minutes, 0) AS lunch_minutes,
                          COALESCE(s.hourly_rate, 0) AS hourly_rate,
                          COALESCE(s.status, 'active') AS status,
                          s.fired_date
                   FROM biotime_raw_events r
                   LEFT JOIN biotime_employee_settings s ON s.terminal_user_id=r.terminal_user_id
                   WHERE r.terminal_user_id=%s AND DATE(r.event_time) BETWEEN %s AND %s
                   GROUP BY DATE(r.event_time), r.terminal_user_id, s.employee_number, s.full_name,
                            s.shift_start_time, s.lunch_minutes, s.hourly_rate, s.status, s.fired_date
                   ORDER BY work_date DESC, r.terminal_user_id""",
                [default_shift_sql, terminal_id, start, end],
            )
            raw_rows = [_normalize_employee_row(row) for row in _dict_rows(cursor)]

            cursor.execute(
                """SELECT terminal_user_id, work_date, manual_arrival_time, manual_departure_time, penalty_amount
                   FROM biotime_attendance_overrides
                   WHERE terminal_user_id=%s AND work_date BETWEEN %s AND %s""",
                [terminal_id, start, end],
            )
            overrides = {
                (str(row['terminal_user_id']), _to_date(row['work_date'])): row
                for row in _dict_rows(cursor)
            }

            cursor.execute(
                """SELECT * FROM biotime_payroll_terms WHERE terminal_user_id=%s""",
                [terminal_id],
            )
            terms_history = _dict_rows(cursor)
            cursor.execute(
                """SELECT * FROM biotime_payroll_days
                   WHERE terminal_user_id=%s AND work_date BETWEEN %s AND %s""",
                [terminal_id, start, end],
            )
            snapshots = {
                _to_date(row['work_date']): row
                for row in _dict_rows(cursor)
            }
            cursor.execute(
                """SELECT * FROM biotime_payroll_payments
                   WHERE terminal_user_id=%s AND period_to BETWEEN %s AND %s AND cancelled_at IS NULL""",
                [terminal_id, start, end],
            )
            payments = _dict_rows(cursor)

        rows_by_day: dict[date, dict[str, Any]] = {}
        for row in raw_rows:
            work_date = _to_date(row.get('work_date'))
            if work_date:
                rows_by_day[work_date] = row

        for (_, work_date), override in overrides.items():
            if work_date and work_date not in rows_by_day:
                rows_by_day[work_date] = {
                    **employee,
                    'work_date': work_date,
                    'arrival_time': None,
                    'departure_time': None,
                    'marks_count': 0,
                    'is_absent': True,
                }

        for work_date in _workdays(start, end):
            if work_date not in rows_by_day and _reportable_on_date(employee, work_date):
                rows_by_day[work_date] = {
                    **employee,
                    'work_date': work_date,
                    'arrival_time': None,
                    'departure_time': None,
                    'marks_count': 0,
                    'is_absent': True,
                }

        result = []
        total_hours = 0
        accrued_cents = 0
        late_count = 0
        early_count = 0
        for work_date in sorted(rows_by_day.keys(), reverse=True):
            row = rows_by_day[work_date]
            row['work_date'] = work_date
            row['terminal_user_id'] = terminal_id
            row['arrival_time'] = _to_datetime(row.get('arrival_time'))
            row['departure_time'] = _to_datetime(row.get('departure_time'))
            override = overrides.get((terminal_id, work_date))
            row['manual_arrival_time'] = override.get('manual_arrival_time') if override else None
            row['manual_departure_time'] = override.get('manual_departure_time') if override else None
            row['penalty_amount'] = override.get('penalty_amount') if override else None
            row['effective_arrival_time'] = _manual_datetime(work_date, row['manual_arrival_time']) or row['arrival_time']
            row['effective_departure_time'] = _manual_datetime(work_date, row['manual_departure_time']) or row['departure_time']

            snapshot = snapshots.get(work_date)
            terms = snapshot or _payroll_terms_for_date(terms_history, work_date, row)
            row['hourly_rate'] = terms.get('hourly_rate', row.get('hourly_rate', 0))
            row['lunch_minutes'] = int(terms.get('lunch_minutes') or 0)
            row['shift_start_time'] = str(terms.get('shift_start_time') or row.get('shift_start_time') or default_shift_sql)
            calc_hours, calc_cents, penalty_cents = _calculate_payroll(row, terms)
            payroll_hours = int(snapshot['worked_hours']) if snapshot else calc_hours
            payroll_cents = _money_cents(snapshot['amount']) if snapshot else calc_cents
            payroll_frozen = snapshot is not None

            calc_arrival = row['effective_arrival_time']
            calc_departure = row['effective_departure_time']
            calc_marks = max(2, int(row.get('marks_count') or 0)) if calc_arrival and calc_departure else int(row.get('marks_count') or 0)
            has_short_day = payroll_hours is not None and payroll_hours < STANDARD_HOURS
            is_late = has_short_day and _is_late(calc_arrival, row['shift_start_time'])
            is_early = has_short_day and _is_early_departure(calc_departure, calc_marks)
            missing = []
            if row.get('is_absent'):
                missing.append('Пропуск')
            else:
                if not _round_arrival(row['arrival_time']):
                    missing.append('Нет прихода')
                if not _round_departure(row['departure_time']) or not _has_departure(row['arrival_time'], row['departure_time'], int(row.get('marks_count') or 0)):
                    missing.append('Нет ухода')
            if row.get('manual_arrival_time') or row.get('manual_departure_time'):
                missing.append('Ручной ввод')
            if penalty_cents < 0:
                missing.append(f'Штраф {_money(penalty_cents)}')

            if payroll_hours is not None:
                total_hours += payroll_hours
            accrued_cents += payroll_cents
            if is_late:
                late_count += 1
            if is_early:
                early_count += 1

            result.append({
                'date': work_date.isoformat(),
                'dateLabel': _report_date(work_date),
                'employee': _employee_name(row),
                'arrival': _time_text(row['arrival_time']),
                'departure': _time_text(row['departure_time']),
                'manualArrival': _manual_time(row.get('manual_arrival_time')),
                'manualDeparture': _manual_time(row.get('manual_departure_time')),
                'hours': payroll_hours,
                'rate': str(row['hourly_rate']),
                'rateText': _money(_money_cents(row['hourly_rate'])),
                'amountCents': payroll_cents,
                'amountText': _money(payroll_cents),
                'marks': int(row.get('marks_count') or 0),
                'warnings': missing,
                'isLate': bool(is_late),
                'isEarlyDeparture': bool(is_early),
                'isFrozen': bool(payroll_frozen),
                'statusText': 'Зафиксировано' if payroll_frozen else ('Предварительно' if work_date == today else ''),
            })

        paid_cents = sum(_money_cents(payment.get('amount')) for payment in payments)
        return Response({
            'from': start.isoformat(),
            'to': end.isoformat(),
            'employee': {'terminalUserId': terminal_id, 'name': _employee_name(employee)},
            'rows': result,
            'totals': {
                'hours': total_hours,
                'accruedCents': accrued_cents,
                'accruedText': _money(accrued_cents),
                'paidCents': paid_cents,
                'paidText': _money(paid_cents),
                'balanceCents': accrued_cents - paid_cents,
                'balanceText': _money(accrued_cents - paid_cents),
                'late': late_count,
                'early': early_count,
            },
        })
