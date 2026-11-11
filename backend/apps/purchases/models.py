"""Закупки: заявка (PurchaseOrder) и её позиции (PurchaseOrderLine).

Поставщик и статус — на ПОЗИЦИИ (в заявке материалы разных поставщиков);
статус заявки — вычисляемый свод по позициям. См. docs/superpowers/specs/
2026-07-14-purchases-design.md.
"""
from django.db import models


class LineStatus(models.TextChoices):
    REQUEST = 'request', 'Заявка'
    ORDERED = 'ordered', 'Заказано'
    RECEIVED = 'received', 'Получено'


class PurchaseOrder(models.Model):
    """Заявка на закупку (потребность). Статус — вычисляемый из позиций."""
    class Kind(models.TextChoices):
        ORDER = 'order', 'Под заказ'
        STOCK = 'stock', 'Пополнение склада'
        MANUAL = 'manual', 'Вручную'

    class Status(models.TextChoices):
        REQUEST = 'request', 'Заявка'
        ORDERED = 'ordered', 'Заказано'
        RECEIVED = 'received', 'Получено'

    name = models.CharField(max_length=128, help_text='Номер заявки')
    kind = models.CharField(max_length=8, choices=Kind.choices, default=Kind.MANUAL)
    sales_order = models.ForeignKey('sales.SalesOrder', null=True, blank=True,
                                    related_name='purchase_requests', on_delete=models.SET_NULL)
    # legacy-поля шапки (используются старым UI создания); supplier/дата теперь на позиции
    supplier = models.ForeignKey('directory.Supplier', null=True, blank=True, on_delete=models.SET_NULL)
    supplier_name = models.CharField(max_length=255, blank=True)
    total_amount = models.DecimalField(max_digits=14, decimal_places=2, default=0)
    info = models.TextField(blank=True)
    status = models.CharField(max_length=16, choices=Status.choices, default=Status.REQUEST)
    expected_arrival_date = models.DateField(null=True, blank=True, help_text='Дата поставки')
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-created_at']

    @property
    def subtotal(self):
        return sum((l.subtotal for l in self.lines.all()), 0)

    def computed_status(self):
        """(key, label) — сводный статус заявки по статусам позиций."""
        ss = [l.status for l in self.lines.all()]
        if not ss:
            return ('request', 'Заявка')
        if all(s == 'received' for s in ss):
            return ('received', 'Получено')
        if any(s == 'received' for s in ss):
            return ('partial_received', 'Частично получено')
        if all(s in ('ordered', 'received') for s in ss):
            return ('ordered', 'Заказано')
        if any(s == 'ordered' for s in ss):
            return ('partial_ordered', 'Частично заказано')
        return ('request', 'Заявка')

    def __str__(self):
        return self.name


class PurchaseOrderLine(models.Model):
    """Позиция заявки: материал, поставщик, статус, частичный приход."""
    order = models.ForeignKey(PurchaseOrder, related_name='lines', on_delete=models.CASCADE)
    product = models.ForeignKey('catalog.Product', on_delete=models.PROTECT)
    quantity = models.DecimalField(max_digits=14, decimal_places=3, default=1, help_text='Нужно')
    price_per_unit = models.DecimalField(max_digits=14, decimal_places=2, default=0)
    supplier = models.ForeignKey('directory.Supplier', null=True, blank=True, on_delete=models.SET_NULL)
    status = models.CharField(max_length=8, choices=LineStatus.choices, default=LineStatus.REQUEST)
    received_quantity = models.DecimalField(max_digits=14, decimal_places=3, default=0,
                                            help_text='Принято (частичный приход)')
    expected_arrival_date = models.DateField(null=True, blank=True, help_text='Дата поставки')

    @property
    def subtotal(self):
        return self.quantity * self.price_per_unit

    def __str__(self):
        return f'{self.product} × {self.quantity}'
