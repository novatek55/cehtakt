"""Справочники: компания, участки, роли, контрагенты, единицы, категории.

Источник модели — docs/data-model.md (сущности Company, Area, UserRole, Supplier, Unit).
"""
from django.db import models


class Company(models.Model):
    """Настройки компании (экран Настройки)."""
    name = models.CharField(max_length=255)
    code = models.CharField(max_length=64, blank=True)
    # Основные настройки
    reserve_days = models.PositiveIntegerField(default=0, help_text='Резерв в днях')
    default_delivery_days = models.PositiveIntegerField(default=0, help_text='Время поставки по умолчанию')
    currency = models.CharField(max_length=8, default='RUB')
    timezone = models.CharField(max_length=64, default='Europe/Moscow')
    inner_barcode_enabled = models.BooleanField(default=False, help_text='Внутренний штрих-код продукта')
    # Производство: план и задания
    allow_free_order_work = models.BooleanField(default=False, help_text='Работы в произвольном порядке')
    show_ship_date_in_kanban = models.BooleanField(default=False)
    default_production_task_type = models.CharField(max_length=64, blank=True)
    webhook_url = models.URLField(blank=True)
    settings = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name_plural = 'companies'

    def __str__(self):
        return self.name


class Unit(models.Model):
    """Единица измерения (шт., м3 и т.д.)."""
    name = models.CharField(max_length=64, unique=True)

    def __str__(self):
        return self.name


class Category(models.Model):
    """Категория изделий/материалов."""
    name = models.CharField(max_length=255)

    class Meta:
        verbose_name_plural = 'categories'

    def __str__(self):
        return self.name


class Customer(models.Model):
    """Клиент (заказчик)."""
    name = models.CharField(max_length=255)
    archived = models.BooleanField(default=False)

    def __str__(self):
        return self.name


class Supplier(models.Model):
    """Поставщик."""
    name = models.CharField(max_length=255)
    archived = models.BooleanField(default=False)

    def __str__(self):
        return self.name


class Area(models.Model):
    """Участок / рабочий центр (пила, лазер, сварка…). Колонки канбана и Плана."""
    name = models.CharField(max_length=255)
    archived = models.BooleanField(default=False)
    sort_index = models.IntegerField(default=0)
    cost_per_hour = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    weekly_limit = models.PositiveIntegerField(null=True, blank=True, help_text='Мощность/неделя')
    weekly_schedule = models.JSONField(default=dict, blank=True)
    default_parallel = models.PositiveIntegerField(default=1)
    distribute_jobs = models.BooleanField(default=False, help_text='Разрешить распределение')
    job_restriction_by_ingredients = models.BooleanField(default=False, help_text='Работа без комплектующих')
    block_period = models.JSONField(default=dict, blank=True)
    # Пользователи участка (выявлено с экрана «Список участков»)
    responsibles = models.ManyToManyField('auth.User', related_name='responsible_areas', blank=True, help_text='Ответственные')
    executors = models.ManyToManyField('auth.User', related_name='executor_areas', blank=True, help_text='Исполнители')

    class Meta:
        ordering = ['sort_index', 'id']

    def __str__(self):
        return self.name


class UserRole(models.Model):
    """Роль с гранулярными правами (read/create/update/delete по доменам)."""
    name = models.CharField(max_length=255)
    # Права храним матрицей: {"sales": {"read": true, ...}, ...}
    permissions = models.JSONField(default=dict, blank=True)
    restrict_to_areas = models.BooleanField(
        default=False,
        help_text='Видит задания только своих участков (responsibles/executors)')
    own_tasks_only = models.BooleanField(
        default=False,
        help_text='На «Заданиях» видит только карточки, где он исполнитель (иначе — все карточки участка)')

    def __str__(self):
        return self.name


class UserProfile(models.Model):
    """Профиль пользователя: привязка к роли (для проверки прав)."""
    user = models.OneToOneField('auth.User', related_name='profile', on_delete=models.CASCADE)
    role = models.ForeignKey(UserRole, null=True, blank=True, related_name='users', on_delete=models.SET_NULL)

    attendance_terminal_user_id = models.CharField(
        max_length=50, unique=True, blank=True, null=True,
        help_text='ID сотрудника BioTime для личного отчёта посещений',
    )

    def __str__(self):
        return f'{self.user} → {self.role}'
