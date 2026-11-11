"""Shared/exclusive lock across all local application workers, for any DB."""
from contextlib import contextmanager
from contextvars import ContextVar
from functools import wraps
import hashlib
import os
import tempfile
import time

from django.conf import settings
from rest_framework.exceptions import APIException

_held = ContextVar('material_merge_lock', default=None)


class MergeBusy(APIException):
    status_code = 409
    default_detail = 'Сейчас выполняется другая операция. Повторите действие через несколько секунд.'


@contextmanager
def material_lock(exclusive=False):
    held = _held.get()
    if held is not None:
        if exclusive and not held:
            raise MergeBusy('Объединение нельзя запускать внутри обычной операции.')
        yield
        return
    import fcntl  # Production runs on Linux; flock is independent of DB vendor.
    database = settings.DATABASES['default']
    identity = str(database.get('HOST', '')) + ':' + str(database.get('NAME', ''))
    name = hashlib.sha256(identity.encode()).hexdigest()[:20]
    path = os.path.join(tempfile.gettempdir(), f'proplan-material-merge-{name}.lock')
    descriptor = os.open(path, os.O_CREAT | os.O_RDWR | os.O_CLOEXEC | os.O_NOFOLLOW, 0o600)
    token = None
    try:
        deadline = time.monotonic() + 8
        while True:
            try:
                fcntl.flock(descriptor, (fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH) | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise MergeBusy()
                time.sleep(0.025)
        token = _held.set(exclusive)
        yield
    finally:
        if token is not None:
            _held.reset(token)
        os.close(descriptor)


def exclusive_merge(function):
    @wraps(function)
    def wrapped(*args, **kwargs):
        with material_lock(exclusive=True):
            return function(*args, **kwargs)
    return wrapped
