"""Аутентификация: вход, текущий пользователь, выход (DRF token-auth)."""
from django.contrib.auth import authenticate
from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework.authtoken.models import Token

from .permissions import user_permissions


def _me(user):
    role = getattr(getattr(user, 'profile', None), 'role', None)
    return {
        'id': user.id,
        'name': user.get_full_name() or user.username,
        'username': user.username,
        'email': user.email,
        'isSuperuser': user.is_superuser,
        'role': role.name if role else ('Администратор' if user.is_superuser else None),
        'roleId': role.id if role else None,
        # суперпользователю — все права (фронт не прячет ничего)
        'permissions': ('*' if user.is_superuser else user_permissions(user)),
    }


class LoginView(APIView):
    def post(self, request):
        username = (request.data.get('username') or '').strip()
        password = request.data.get('password') or ''
        user = authenticate(username=username, password=password)
        if not user:
            return Response({'error': 'Неверный логин или пароль'}, status=400)
        if not user.is_active:
            return Response({'error': 'Пользователь отключён'}, status=403)
        token, _ = Token.objects.get_or_create(user=user)
        return Response({'token': token.key, 'user': _me(user)})


class MeView(APIView):
    def get(self, request):
        if not request.user or not request.user.is_authenticated:
            return Response({'error': 'не авторизован'}, status=401)
        return Response(_me(request.user))


class LogoutView(APIView):
    def post(self, request):
        Token.objects.filter(user=request.user).delete()
        return Response(status=204)
