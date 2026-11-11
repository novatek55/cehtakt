"""Area membership is independent of role visibility and administrator rights."""
from django.contrib.auth import get_user_model
from django.db.models import Q
from apps.directory.models import Area


def area_staff(area_id):
    return get_user_model().objects.filter(is_active=True).filter(
        Q(executor_areas__pk=area_id) | Q(responsible_areas__pk=area_id)).distinct()


def validate_executor(executor_id, area_id):
    try:
        if isinstance(executor_id, bool):
            raise ValueError
        executor_id = int(executor_id)
    except (TypeError, ValueError, OverflowError):
        raise ValueError('Выберите сотрудника участка.')
    if not 0 < executor_id <= 9223372036854775807 or not area_staff(area_id).filter(pk=executor_id).exists():
        raise ValueError('Исполнитель должен быть действующим сотрудником этого участка.')
    return executor_id


def is_area_member(user, area_id):
    return bool(user and user.is_authenticated and area_staff(area_id).filter(pk=user.pk).exists())
