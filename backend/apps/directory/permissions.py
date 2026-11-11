"""Проверка прав по роли пользователя — единая логика по URL-пути.

Каждый /api/* путь маппится на «страницу» (как в матрице ролей), метод — на действие.
Право берётся из role.permissions[page][action]. Суперпользователь — полный доступ.
"""
import re

# порядок важен: более специфичные раньше
_PATH_PAGE = [
    (re.compile(r'^/api/auth/'), None),          # логин/профиль — всегда доступно
    (re.compile(r'^/api/import-techcard'), None),  # инструмент миграции
    (re.compile(r'^/api/sales'), 'sales'),
    # Ключи маркетплейсов — административная настройка системы, как остальные
    # Настройки: право company, не shipments. Специфичнее общей записи ниже — раньше неё.
    (re.compile(r'^/api/shipments/settings'), 'company'),
    (re.compile(r'^/api/shipments'), 'shipments'),
    (re.compile(r'^/api/purchases'), 'purchases'),
    (re.compile(r'^/api/production'), 'production_plan'),
    (re.compile(r'^/api/(tasks|produce)'), 'production_tasks'),
    (re.compile(r'^/api/stock'), 'stock'),
    (re.compile(r'^/api/(products|ingredients|processes)'), 'catalog'),
    (re.compile(r'^/api/(areas|company|users|reference|roles)'), 'company'),
]

_METHOD_PERM = {
    'GET': 'read', 'HEAD': 'read', 'OPTIONS': 'read',
    'POST': 'create', 'PUT': 'update', 'PATCH': 'update', 'DELETE': 'delete',
}

# Точечные переопределения общего маппинга метод→право. Сборка и передача к
# отгрузке необратимо меняют состояние реального заказа в Ozon/WB — это не
# «создание» в обычном смысле, а изменение существующей заявки, поэтому
# требуют update, а не create (которое даёт обычный POST по умолчанию).
_FORCE_ACTION = [
    # Привязка товара к строке — правка существующей заявки, не «создание».
    (re.compile(r'^/api/shipments/\d+/lines/\d+/bind/$'), 'update'),
    (re.compile(r'^/api/shipments/\d+/assemble/$'), 'update'),
    (re.compile(r'^/api/shipments/\d+/handover/$'), 'update'),
    (re.compile(r'^/api/shipments/\d+/supply/$'), 'update'),
    (re.compile(r'^/api/shipments/\d+/trbx/$'), 'update'),
    (re.compile(r'^/api/shipments/\d+/scan/(finish/)?$'), 'update'),
    (re.compile(r'^/api/tasks/group/edit/$'), 'update'),
]


def _forced_action(path):
    for rx, action in _FORCE_ACTION:
        if rx.match(path):
            return action
    return None


def _page_for_path(path):
    for rx, page in _PATH_PAGE:
        if rx.match(path):
            return page, True
    return None, False


def user_permissions(user):
    """Матрица прав пользователя (для выдачи на фронт). Суперпользователь → всё True не нужен здесь."""
    role = getattr(getattr(user, 'profile', None), 'role', None)
    return role.permissions if role and role.permissions else {}


def visible_area_ids(user):
    """Множество id участков, видимых пользователю на канбане «Задания».

    None = ограничения нет (суперюзер или роль без restrict_to_areas).
    Иначе — объединение responsibles и executors по участкам (по правам это одно и то же).
    Пустое множество = роль ограничена, но участки не назначены → пустой канбан.
    """
    if not user or not user.is_authenticated:
        return set()
    if user.is_superuser:
        return None
    role = getattr(getattr(user, 'profile', None), 'role', None)
    if not role or not role.restrict_to_areas:
        return None
    from .models import Area
    from django.db.models import Q
    return set(Area.objects.filter(Q(responsibles=user) | Q(executors=user)).values_list('id', flat=True))


def own_tasks_only(user):
    """True — на «Заданиях» пользователь видит только карточки, где он исполнитель.

    Суперпользователь и роль без флага — видят все карточки своих участков.
    """
    if not user or not user.is_authenticated or user.is_superuser:
        return False
    role = getattr(getattr(user, 'profile', None), 'role', None)
    return bool(role and role.own_tasks_only)


from rest_framework.permissions import BasePermission


class RolePermission(BasePermission):
    message = 'Недостаточно прав для этого действия.'

    def has_permission(self, request, view):
        # path_info — путь без префикса монтирования (FORCE_SCRIPT_NAME=/zakup/PLAN),
        # именно его ожидают регэкспы _PATH_PAGE (^/api/...).
        page, matched = _page_for_path(request.path_info)
        if matched and page is None:
            return True  # открытые пути (auth, миграция)

        user = request.user
        if not user or not user.is_authenticated:
            return False
        if user.is_superuser:
            return True

        if request.method == 'PATCH' and re.match(r'^/api/sales/\d+/$', request.path_info):
            return True

        if request.method == 'POST' and re.match(r'^/api/stock/corrections/bulk-delete/$', request.path_info):
            role = getattr(getattr(user, 'profile', None), 'role', None)
            stock_perms = ((role.permissions or {}) if role else {}).get('stock', {})
            return bool(stock_perms.get('create') or stock_perms.get('delete'))

        if not matched or page is None:
            return True  # незамапленный путь — не ограничиваем

        perm = _forced_action(request.path_info) or _METHOD_PERM.get(request.method, 'read')
        role = getattr(getattr(user, 'profile', None), 'role', None)
        if not role or not role.permissions:
            return False
        return bool(role.permissions.get(page, {}).get(perm, False))
