"""Продажи: заказ клиента (SalesOrder) и его позиции (OrderLine).

Источник — docs/data-model.md. Статусы стадий (ingredients/production/stock) —
вычисляемые бэком, поэтому храним как поля, обновляемые логикой «Произвести и Закупить».
"""
from django.db import models


class StageStatus(models.TextChoices):
    """Цветовые статусы стадий заказа (Комплектующие/Производство/Склад)."""
    NONE = 'none', '—'
    MISSING = 'missing', 'Отсутствуют'          # красный
    OVERDUE = 'overdue', 'Просрочено'           # красный
    NOT_READY = 'not_ready', 'Не готово'        # жёлтый
    IN_STOCK = 'in_stock', 'В наличии'          # зелёный
    READY = 'ready', 'Готово'                   # зелёный
    USED = 'used', 'Использованы'               # зелёный
    PACKED = 'packed', 'Укомплектовано'         # зелёный


class SalesOrder(models.Model):
    """Заказ на продажу."""
    name = models.CharField(max_length=128, help_text='Номер заказа')
    customer = models.ForeignKey('directory.Customer', null=True, blank=True, on_delete=models.SET_NULL)
    manager = models.ForeignKey('auth.User', null=True, blank=True, on_delete=models.SET_NULL)
    created_by = models.ForeignKey('auth.User', null=True, blank=True, related_name='created_sales_orders',
                                   on_delete=models.SET_NULL)
    # денормализованные поля из proplan (customerName/managerName/subtotal — без FK)
    customer_name = models.CharField(max_length=255, blank=True)
    manager_name = models.CharField(max_length=255, blank=True)
    total_amount = models.DecimalField(max_digits=14, decimal_places=2, default=0)
    finish_by_date = models.DateField(null=True, blank=True, help_text='Дата отгрузки')
    order_ready_date = models.DateField(null=True, blank=True)
    shipping_address = models.CharField(max_length=512, blank=True)
    billing_address = models.CharField(max_length=512, blank=True)
    info = models.TextField(blank=True, help_text='Описание')
    archived = models.BooleanField(default=False)
    # Вычисляемые статусы стадий (обновляются логикой)
    ingredients_status = models.CharField(max_length=16, choices=StageStatus.choices, default=StageStatus.NONE)
    production_status = models.CharField(max_length=16, choices=StageStatus.choices, default=StageStatus.NONE)
    stock_status = models.CharField(max_length=16, choices=StageStatus.choices, default=StageStatus.NONE)
    display_status = models.CharField(max_length=64, blank=True, help_text='Пилюля отгрузки')
    rank = models.IntegerField(default=0, help_text='Порядок строк (drag)')
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['rank', 'id']

    @property
    def subtotal(self):
        return sum((l.subtotal for l in self.lines.all()), 0)

    def __str__(self):
        return self.name


class OrderLine(models.Model):
    """Позиция заказа на продажу."""
    order = models.ForeignKey(SalesOrder, related_name='lines', on_delete=models.CASCADE)
    product = models.ForeignKey('catalog.Product', on_delete=models.PROTECT)
    quantity = models.DecimalField(max_digits=14, decimal_places=3, default=1)
    price_per_unit = models.DecimalField(max_digits=14, decimal_places=2, default=0)
    # Построчные статусы (колонки Склад/Комплектующие/Производство в карточке)
    stock_status = models.CharField(max_length=16, choices=StageStatus.choices, default=StageStatus.NONE)
    ingredients_status = models.CharField(max_length=16, choices=StageStatus.choices, default=StageStatus.NONE)
    production_status = models.CharField(max_length=16, choices=StageStatus.choices, default=StageStatus.NONE)

    @property
    def subtotal(self):
        return self.quantity * self.price_per_unit

    def __str__(self):
        return f'{self.product} × {self.quantity}'
