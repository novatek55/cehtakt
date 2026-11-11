from django.contrib.auth.models import User as AuthUser
from django.db.models import ProtectedError
from rest_framework import generics
from rest_framework.views import APIView
from rest_framework.response import Response

from .models import Area, Company, Unit, Category, Customer, Supplier, UserRole
from .serializers import AreaListSerializer, AreaWriteSerializer

# Справочники: ключ из URL → модель
_REF_MODELS = {
    'units': Unit,
    'categories': Category,
    'customers': Customer,
    'suppliers': Supplier,
    'roles': UserRole,
}


# Матрица прав ролей: страницы (строки) × действия (столбцы) — копия proplan /users
ROLE_PAGES = [
    ('sales', 'Заказы'),
    ('shipments', 'Отгрузки'),
    ('purchases', 'Закупки'),
    ('production_plan', 'Производство'),
    ('production_tasks', 'Задания'),
    ('stock', 'Склад'),
    ('catalog', 'Каталог'),
    ('company', 'Настройки компании'),
]
ROLE_PERMS = ['read', 'create', 'update', 'delete']


def _role_dict(r):
    return {'id': r.id, 'name': r.name, 'permissions': r.permissions or {},
            'restrictToAreas': r.restrict_to_areas, 'ownTasksOnly': r.own_tasks_only}


def _full_matrix(value):
    return {page: {perm: value for perm in ROLE_PERMS} for page, _ in ROLE_PAGES}


class RoleListView(APIView):
    """GET /api/roles/ — страницы, действия и роли с матрицей прав; POST {name} — создать роль."""

    def get(self, request):
        if not UserRole.objects.exists():
            UserRole.objects.create(name='Администратор', permissions=_full_matrix(True))
        return Response({
            'pages': [{'key': k, 'label': l} for k, l in ROLE_PAGES],
            'perms': ROLE_PERMS,
            'roles': [_role_dict(r) for r in UserRole.objects.order_by('id')],
        })

    def post(self, request):
        name = (request.data.get('name') or '').strip()
        if not name:
            return Response({'error': 'нужно название'}, status=400)
        r = UserRole.objects.create(name=name, permissions=_full_matrix(False))
        return Response(_role_dict(r), status=201)


class RoleDetailView(APIView):
    """PATCH /api/roles/<id>/ {permissions|name} — изменить; DELETE — удалить роль."""

    def patch(self, request, pk):
        r = UserRole.objects.filter(pk=pk).first()
        if not r:
            return Response({'error': 'не найдено'}, status=404)
        perms = request.data.get('permissions')
        if perms is not None:
            r.permissions = perms
        name = request.data.get('name')
        if name:
            r.name = name.strip()
        restrict = request.data.get('restrictToAreas')
        if restrict is not None:
            r.restrict_to_areas = bool(restrict)
        own_only = request.data.get('ownTasksOnly')
        if own_only is not None:
            r.own_tasks_only = bool(own_only)
        r.save()
        return Response(_role_dict(r))

    def delete(self, request, pk):
        deleted, _ = UserRole.objects.filter(pk=pk).delete()
        if not deleted:
            return Response({'error': 'не найдено'}, status=404)
        return Response(status=204)


class ReferenceListView(APIView):
    """GET /api/reference/<kind>/ — список; POST {name} — добавить запись справочника."""

    def get(self, request, kind):
        model = _REF_MODELS.get(kind)
        if not model:
            return Response({'error': 'неизвестный справочник'}, status=404)
        return Response([{'id': o.id, 'name': o.name} for o in model.objects.order_by('name')])

    def post(self, request, kind):
        model = _REF_MODELS.get(kind)
        if not model:
            return Response({'error': 'неизвестный справочник'}, status=404)
        name = (request.data.get('name') or '').strip()
        if not name:
            return Response({'error': 'нужно название'}, status=400)
        obj, created = model.objects.get_or_create(name=name)
        return Response({'id': obj.id, 'name': obj.name, 'created': created}, status=201 if created else 200)


class ReferenceDetailView(APIView):
    """PATCH /api/reference/<kind>/<id>/ {name} — переименовать;
    DELETE — удалить запись справочника."""

    def patch(self, request, kind, pk):
        model = _REF_MODELS.get(kind)
        if not model:
            return Response({'error': 'неизвестный справочник'}, status=404)
        obj = model.objects.filter(pk=pk).first()
        if not obj:
            return Response({'error': 'не найдено'}, status=404)
        name = (request.data.get('name') or '').strip()
        if not name:
            return Response({'error': 'нужно название'}, status=400)
        obj.name = name
        obj.save(update_fields=['name'])
        return Response({'id': obj.id, 'name': obj.name})

    def delete(self, request, kind, pk):
        model = _REF_MODELS.get(kind)
        if not model:
            return Response({'error': 'неизвестный справочник'}, status=404)
        obj = model.objects.filter(pk=pk).first()
        if not obj:
            return Response({'error': 'не найдено'}, status=404)
        try:
            obj.delete()
        except ProtectedError:
            return Response(
                {'error': 'Запись используется в других данных и не может быть удалена.'},
                status=409,
            )
        return Response(status=204)

_COMPANY_FIELDS = [
    'name', 'reserve_days', 'default_delivery_days', 'currency', 'timezone',
    'inner_barcode_enabled', 'allow_free_order_work', 'show_ship_date_in_kanban',
    'default_production_task_type',
]


def _user_row(u):
    role = getattr(getattr(u, 'profile', None), 'role', None)
    return {
        'id': u.id,
        'name': u.get_full_name() or u.username,
        'username': u.username,
        'email': u.email,
        'role': role.name if role else ('Администратор' if u.is_superuser else '—'),
        'roleId': role.id if role else None,
        'isSuperuser': u.is_superuser,
    }


def _set_role(user, role_id):
    from .models import UserProfile
    profile, _ = UserProfile.objects.get_or_create(user=user)
    if role_id in (None, '', 0):
        profile.role = None
    else:
        profile.role = UserRole.objects.filter(pk=role_id).first()
    profile.save()


class UserListView(APIView):
    """GET /api/users/ — пользователи; POST — создать (логин/пароль/роль)."""

    def get(self, request):
        users = AuthUser.objects.select_related('profile__role').order_by('id')
        rows = []
        for user in users:
            row = _user_row(user)
            if request.user.is_superuser:
                profile = getattr(user, 'profile', None)
                row['attendanceTerminalUserId'] = getattr(profile, 'attendance_terminal_user_id', None) or ''
            rows.append(row)
        return Response(rows)

    def post(self, request):
        d = request.data
        email = (d.get('email') or '').strip()
        name = (d.get('name') or '').strip()
        login = (d.get('username') or '').strip()
        base = login or email or name or f'user{AuthUser.objects.count() + 1}'
        username = base
        i = 1
        while AuthUser.objects.filter(username=username).exists():
            i += 1
            username = f'{base}-{i}'
        u = AuthUser(username=username, email=email, first_name=name)
        password = d.get('password')
        if password:
            u.set_password(password)
        u.save()
        if 'roleId' in d:
            _set_role(u, d.get('roleId'))
        return Response(_user_row(u), status=201)


class UserDetailView(APIView):
    """PATCH /api/users/<id>/ — сменить роль/пароль/имя; DELETE — удалить пользователя."""

    def patch(self, request, pk):
        u = AuthUser.objects.filter(pk=pk).first()
        if not u:
            return Response({'error': 'не найдено'}, status=404)
        d = request.data
        if 'username' in d:
            login = (d.get('username') or '').strip()
            if not login:
                return Response({'error': 'логин не может быть пустым'}, status=400)
            if AuthUser.objects.filter(username=login).exclude(pk=u.pk).exists():
                return Response({'error': 'логин уже занят'}, status=400)
            u.username = login
        if 'name' in d:
            u.first_name = (d.get('name') or '').strip()
        if 'email' in d:
            u.email = (d.get('email') or '').strip()
        if d.get('password'):
            u.set_password(d['password'])
        u.save()
        if 'roleId' in d:
            _set_role(u, d.get('roleId'))
        return Response(_user_row(u))

    def delete(self, request, pk):
        u = AuthUser.objects.filter(pk=pk).first()
        if not u:
            return Response({'error': 'не найдено'}, status=404)
        if u.is_superuser:
            return Response({'error': 'нельзя удалить администратора'}, status=400)
        u.delete()
        return Response(status=204)


class CompanyView(APIView):
    """GET/PATCH /api/company/ — настройки единственной компании."""

    def _obj(self):
        return Company.objects.first() or Company.objects.create(name='Моя компания')

    def get(self, request):
        c = self._obj()
        return Response({f: getattr(c, f) for f in _COMPANY_FIELDS})

    def patch(self, request):
        c = self._obj()
        for f in _COMPANY_FIELDS:
            if f in request.data:
                setattr(c, f, request.data[f])
        c.save()
        return Response({f: getattr(c, f) for f in _COMPANY_FIELDS})


class AreaListView(generics.ListCreateAPIView):
    """GET /api/areas/ — список; POST — создать участок."""
    pagination_class = None

    def get_serializer_class(self):
        return AreaWriteSerializer if self.request.method == 'POST' else AreaListSerializer

    def get_queryset(self):
        return Area.objects.filter(archived=False).order_by('sort_index', 'id')


class AreaDetailView(generics.RetrieveUpdateDestroyAPIView):
    """PATCH /api/areas/<id>/ — редактирование (в т.ч. ответственные/исполнители).

    DELETE = архивирование (участки защищены FK из операций).
    """
    queryset = Area.objects.all()
    serializer_class = AreaWriteSerializer

    def perform_destroy(self, instance):
        instance.archived = True
        instance.save(update_fields=['archived'])
