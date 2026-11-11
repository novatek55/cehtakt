"""Склад: остатки по продуктам (ProductItem) и движения/корректировки.

Источник — docs/data-model.md (ProductItem: available/committed/expected/minLimit).
"""
from django.db import models


class StockItem(models.Model):
    """Складская позиция — агрегат остатков по одному продукту."""
    product = models.OneToOneField('catalog.Product', related_name='stock', on_delete=models.CASCADE)
    available_quantity = models.DecimalField(max_digits=16, decimal_places=3, default=0, help_text='В наличии')
    committed_quantity = models.DecimalField(max_digits=16, decimal_places=3, default=0, help_text='Зарезервировано (Потребитель)')
    expected_quantity = models.DecimalField(max_digits=16, decimal_places=3, default=0, help_text='Ожидается')

    def __str__(self):
        return f'{self.product}: {self.available_quantity}'


class StockMovement(models.Model):
    """Движение склада / ручная корректировка остатка (вкладка Корректировки)."""
    class Kind(models.TextChoices):
        CORRECTION = 'correction', 'Корректировка'
        RECEIPT = 'receipt', 'Поступление'
        CONSUMPTION = 'consumption', 'Списание'
        PRODUCTION = 'production', 'Выпуск'

    product = models.ForeignKey('catalog.Product', related_name='movements', on_delete=models.CASCADE)
    # Задание, породившее движение (выпуск годных и списание входов в finish_operation).
    #
    # NULL — это осознанное состояние, а не забытое заполнение. Связи нет по существу у:
    #   * ручной корректировки остатка (kind=correction);
    #   * приёмки закупки (kind=receipt) — там связь с заказом закупки, а не с заданием;
    #   * списания при отгрузке (kind=consumption из apps.shipments) — отгрузка не задание;
    #   * записей, созданных до появления поля: старые записи не бэкфиллились,
    #     номер задания в них лежит только текстом в note, парсинг ненадёжен.
    #
    # ВАЖНО для потребителей: по kind=CONSUMPTION НЕЛЬЗЯ предполагать «это производство» —
    # такие же движения создаёт отгрузка. Сверку выпуска с заданиями строить на
    # job__isnull=False, а не на виде движения.
    #
    # SET_NULL: движение обязано пережить удаление задания — остаток уже изменён,
    # и движение является финансово значимым фактом. PROTECT заблокировал бы
    # существующий сценарий (delete_jobs не проверяет статус, поэтому через массовое
    # удаление заданий в UI можно снести и завершённое задание с движениями склада).
    # Ограничение: связь живёт, пока живёт задание; после удаления задания движение
    # остаётся, а связь теряется безвозвратно.
    job = models.ForeignKey('production.ProductionJob', null=True, blank=True,
                            related_name='stock_movements', on_delete=models.SET_NULL)
    kind = models.CharField(max_length=16, choices=Kind.choices, default=Kind.CORRECTION)
    quantity_delta = models.DecimalField(max_digits=16, decimal_places=3, help_text='+/− к остатку')
    quantity_before = models.DecimalField(max_digits=16, decimal_places=3, null=True, blank=True, help_text='Было')
    quantity_after = models.DecimalField(max_digits=16, decimal_places=3, null=True, blank=True, help_text='Стало')
    note = models.CharField(max_length=255, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    created_by = models.ForeignKey('auth.User', null=True, blank=True, on_delete=models.SET_NULL)

    class Meta:
        ordering = ['-created_at']

    def __str__(self):
        return f'{self.get_kind_display()} {self.product}: {self.quantity_delta}'


class MarketplaceStock(models.Model):
    """FBS-остаток товара на конкретном складе маркетплейса."""

    class Source(models.TextChoices):
        OZON = 'ozon', 'Ozon'
        WB = 'wb', 'Wildberries'

    product = models.ForeignKey('catalog.Product', related_name='marketplace_stocks',
                                on_delete=models.CASCADE)
    source = models.CharField(max_length=16, choices=Source.choices)
    warehouse_id = models.CharField(max_length=64)
    quantity = models.DecimalField(max_digits=16, decimal_places=3, default=0)
    updated_at = models.DateTimeField(auto_now=True)
    updated_by = models.ForeignKey('auth.User', null=True, blank=True, on_delete=models.SET_NULL)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=['product', 'source', 'warehouse_id'],
                                    name='uniq_marketplace_stock_product_source_wh'),
        ]

    def __str__(self):
        return f'{self.source}:{self.warehouse_id} {self.product_id} = {self.quantity}'
