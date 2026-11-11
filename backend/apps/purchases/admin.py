"""Авто-регистрация всех моделей приложения в админке (бесплатный CRUD)."""
from django.apps import apps as django_apps
from django.contrib import admin

for _model in django_apps.get_app_config('purchases').get_models():
    try:
        admin.site.register(_model)
    except admin.sites.AlreadyRegistered:
        pass
