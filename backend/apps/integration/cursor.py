"""Пагинация внешнего API: ограничение limit и курсор по (finished_at, id)."""

from datetime import timezone as dt_timezone

from django.utils.dateparse import parse_datetime

DEFAULT_LIMIT = 200
MAX_LIMIT = 1000
# «~» — unreserved-символ по RFC 3986: проходит query-строку и любой прокси без
# percent-encoding. Прежний «|» в URI недопустим и требовал кодирования от потребителя —
# ровно те грабли, ради устранения которых курсор уже отдаётся с «Z» вместо «+00:00».
CURSOR_SEPARATOR = '~'
# Разделитель прежнего формата. Курсоров с ним в природе, скорее всего, нет, но эндпоинт
# уже мог быть показан — разбирать их дальше стоит одной строки. Выпускаем только «~».
LEGACY_CURSOR_SEPARATOR = '|'


def clamp_limit(raw, default=DEFAULT_LIMIT, maximum=MAX_LIMIT):
    """Безопасный limit: мусор и отсутствие значения → default, потолок — maximum."""
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return default
    return max(1, min(value, maximum))


def parse_offset(raw):
    """Смещение: мусор и отрицательные значения → 0. Отдельно от limit — нуль здесь легален."""
    try:
        return max(0, int(raw))
    except (TypeError, ValueError):
        return 0


def encode_cursor(finished_at, op_id):
    """Курсор из метки завершения и id операции. Время нормализуется в UTC.

    Время без часового пояса → ValueError.

    Смещение отдаём как «Z», а не «+00:00»: «+» в query-строке декодируется в пробел,
    и потребитель, не сделавший percent-encoding, вернул бы нам испорченный курсор.
    Курсор всегда в UTC, поэтому «Z» — точная и URL-безопасная запись того же момента.

    decode_cursor по-прежнему понимает и запись со смещением «+00:00», но курсор,
    выпущенный до перехода на «Z», доедет обратно только если клиент передаст его
    percent-encoded: сырой «+» в query-строке до нас уже не доживёт.
    """
    # astimezone() истолковал бы naive как локальное время процесса (даже не как
    # Django TIME_ZONE) и выдал синтаксически валидный курсор с неверным моментом —
    # decode_cursor такое уже не отличит, там будет честный +00:00.
    if finished_at.tzinfo is None or finished_at.utcoffset() is None:
        raise ValueError(f'время без часового пояса: {finished_at!r}')
    ts = finished_at.astimezone(dt_timezone.utc).isoformat().replace('+00:00', 'Z')
    return f'{ts}{CURSOR_SEPARATOR}{op_id}'


def decode_cursor(raw):
    """Разобрать курсор в (datetime, id). Пустой → None, битый → ValueError."""
    if raw is None:
        return None
    if not isinstance(raw, str):
        raise ValueError(f'некорректный курсор: {raw!r}')
    if not raw:
        return None
    # Принимаем оба разделителя намеренно: курсор старого формата («|») должен доехать
    # обратно, иначе потребитель, сохранивший его до смены формата, потеряет прогресс.
    # Время в курсоре ни того, ни другого символа не содержит, так что выбор однозначен.
    if CURSOR_SEPARATOR in raw:
        separator = CURSOR_SEPARATOR
    elif LEGACY_CURSOR_SEPARATOR in raw:
        separator = LEGACY_CURSOR_SEPARATOR
    else:
        raise ValueError(f'некорректный курсор: {raw}')
    ts_raw, id_raw = raw.rsplit(separator, 1)
    try:
        op_id = int(id_raw)
    except (TypeError, ValueError):
        raise ValueError(f'некорректный курсор: {raw}')
    try:
        ts = parse_datetime(ts_raw)
    except ValueError:
        raise ValueError(f'некорректный курсор: {raw}')
    if ts is None:
        raise ValueError(f'некорректный курсор: {raw}')
    # Курсор выпускаем только мы сами и всегда со смещением. Время без пояса —
    # испорченный курсор: Django молча истолковал бы его в TIME_ZONE и сдвинул
    # границу страницы, что дало бы дубли на выдаче.
    if ts.tzinfo is None or ts.utcoffset() is None:
        raise ValueError(f'некорректный курсор: {raw}')
    return ts, op_id
