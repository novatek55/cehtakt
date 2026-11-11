"""Отгрузки: документ отгрузки (Shipment) и его состав (ShipmentLine).

Источник — docs/superpowers/specs/2026-07-22-shipments-module-design.md.
Отгрузка не продажный документ: цен и сумм нет, только номенклатура и количество.
Поля sku_raw/name_raw/external_posting/manually_edited/deleted нужны импорту из
Ozon/WB (этап 2) — заводим сразу, чтобы не плодить миграции.
"""
from django.db import models


class Shipment(models.Model):
    """Заявка на отгрузку."""
    class Source(models.TextChoices):
        MANUAL = 'manual', 'Вручную'
        OZON = 'ozon', 'Ozon'
        WB = 'wb', 'Wildberries'

    class Status(models.TextChoices):
        IN_WORK = 'in_work', 'В работе'
        SHIPPED = 'shipped', 'Отгружено'

    name = models.CharField(max_length=128, help_text='Номер заявки')
    source = models.CharField(max_length=16, choices=Source.choices, default=Source.MANUAL)
    status = models.CharField(max_length=16, choices=Status.choices, default=Status.IN_WORK)
    note = models.TextField(blank=True)
    external_supply_id = models.CharField(
        max_length=64, blank=True,
        help_text='Номер поставки Wildberries, созданной при сборке')
    shipped_at = models.DateTimeField(null=True, blank=True)
    handed_over_at = models.DateTimeField(
        null=True, blank=True,
        help_text='Момент передачи поставки в доставку WB (шаг «Передать в доставку»)')
    ozon_unaccepted_postings = models.JSONField(
        default=list, blank=True,
        help_text='Номера закрытых в Proplan Ozon-отправлений, которые Ozon всё ещё ждёт')
    scan_finished_at = models.DateTimeField(
        null=True, blank=True,
        help_text='Момент завершения сверки отгрузки сканером')
    created_at = models.DateTimeField(auto_now_add=True)
    created_by = models.ForeignKey('auth.User', null=True, blank=True, on_delete=models.SET_NULL)

    class Meta:
        ordering = ['-created_at', '-id']

    def __str__(self):
        return self.name


class ShipmentLine(models.Model):
    """Позиция отгрузки. product=None — артикул не нашёлся в каталоге."""
    shipment = models.ForeignKey(Shipment, related_name='lines', on_delete=models.CASCADE)
    product = models.ForeignKey('catalog.Product', null=True, blank=True, on_delete=models.PROTECT)
    sku_raw = models.CharField(max_length=128, blank=True, help_text='Артикул из маркетплейса')
    name_raw = models.CharField(max_length=255, blank=True, help_text='Название из маркетплейса')
    quantity = models.DecimalField(max_digits=14, decimal_places=3, default=1)
    external_posting = models.CharField(max_length=128, blank=True, help_text='Номер отправления')
    external_status = models.CharField(
        max_length=32, blank=True,
        help_text='Стадия отправления на маркетплейсе (awaiting_packaging/awaiting_deliver/new)')
    external_trbx_id = models.CharField(
        max_length=64, blank=True,
        help_text='id грузоместа WB, в котором лежит это отправление (связь 1:1)')
    external_label_barcode = models.CharField(max_length=255, blank=True)
    external_label_code = models.CharField(
        max_length=64, blank=True,
        help_text='Крупные 4 цифры с этикетки WB (partB)')
    external_trbx_barcode = models.CharField(max_length=255, blank=True)
    external_product_id = models.CharField(
        max_length=64, blank=True,
        help_text='Числовой id товара на маркетплейсе — нужен сборке Ozon')
    external_warehouse = models.CharField(
        max_length=128, blank=True,
        help_text='Склад маркетплейса, с которого должно собираться отправление')
    manually_edited = models.BooleanField(default=False)
    deleted = models.BooleanField(default=False, help_text='Мягкое удаление — помнит дедуп импорта')

    class Meta:
        ordering = ['id']

    def __str__(self):
        return f'{self.product or self.sku_raw} × {self.quantity}'


class ShipmentScan(models.Model):
    """Факт сканирования кода коробки/отправления при сверке отгрузки."""
    class Kind(models.TextChoices):
        ITEM = 'item', 'Товар'
        CARGO = 'cargo', 'Грузоместо'

    shipment = models.ForeignKey(Shipment, related_name='scans', on_delete=models.CASCADE)
    line = models.ForeignKey(ShipmentLine, related_name='scans', on_delete=models.CASCADE)
    kind = models.CharField(max_length=16, choices=Kind.choices, default=Kind.ITEM)
    code = models.CharField(max_length=255)
    normalized_code = models.CharField(max_length=255)
    scanned_at = models.DateTimeField(auto_now_add=True)
    scanned_by = models.ForeignKey('auth.User', null=True, blank=True, on_delete=models.SET_NULL)

    class Meta:
        ordering = ['-scanned_at', '-id']
        constraints = [
            models.UniqueConstraint(fields=['shipment', 'normalized_code'],
                                    name='uniq_shipment_scan_code'),
            models.UniqueConstraint(fields=['shipment', 'line', 'kind'],
                                    name='uniq_shipment_scan_line_kind'),
        ]

    def __str__(self):
        return f'{self.shipment_id}: {self.code}'

    def save(self, *args, **kwargs):
        if not self.normalized_code:
            self.normalized_code = ' '.join(str(self.code or '').strip().split()).casefold()
        super().save(*args, **kwargs)


class MarketplaceSettings(models.Model):
    """Ключи доступа к API маркетплейсов (одна запись на систему).

    Наружу отдаются только маской — см. MarketplaceSettingsView.
    """
    ozon_client_id = models.CharField(max_length=128, blank=True)
    ozon_api_key = models.CharField(max_length=255, blank=True)
    ozon_warehouse_id = models.CharField(max_length=64, blank=True)
    ozon_warehouse_ids = models.JSONField(default=list, blank=True)
    wb_token = models.TextField(blank=True, help_text='Токен Wildberries Marketplace API')
    wb_warehouse_id = models.CharField(max_length=64, blank=True)

    class Meta:
        verbose_name = 'настройки маркетплейсов'
        verbose_name_plural = 'настройки маркетплейсов'

    @classmethod
    def load(cls):
        """Единственная запись настроек; создаётся при первом обращении."""
        return cls.objects.first() or cls.objects.create()

    @property
    def ozon_configured(self):
        return bool(self.ozon_client_id and self.ozon_api_key)

    @property
    def wb_configured(self):
        return bool(self.wb_token)

    def __str__(self):
        return 'Настройки маркетплейсов'


class MarketplaceImportRetry(models.Model):
    """Единственная отложенная попытка импорта конкретного источника."""

    source = models.CharField(max_length=32, unique=True)
    next_attempt_at = models.DateTimeField()
    last_error = models.TextField(blank=True)
    attempts = models.PositiveIntegerField(default=0)
    last_succeeded_at = models.DateTimeField(null=True, blank=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['next_attempt_at', 'id']


class MarketplaceWarehouse(models.Model):
    """Склад маркетплейса, загруженный через API для выбора в настройках."""

    source = models.CharField(max_length=16, choices=Shipment.Source.choices)
    warehouse_id = models.CharField(max_length=64)
    name = models.CharField(max_length=255, blank=True)
    status = models.CharField(max_length=64, blank=True)
    is_rfbs = models.BooleanField(default=False)
    warehouse_type = models.CharField(max_length=64, blank=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=['source', 'warehouse_id'],
                                    name='uniq_marketplace_warehouse_source_id'),
        ]
        ordering = ['source', 'name', 'warehouse_id']

    def __str__(self):
        return f'{self.source}:{self.name or self.warehouse_id}'


class MarketplaceProductAlias(models.Model):
    """Привязка артикула маркетплейса к товару каталога.

    Нужна, когда offer_id на маркетплейсе не совпадает с артикулом товара
    (Product.sku) и нормализация пробелов не помогает. Пользователь один раз
    выбирает товар для несопоставленной строки — привязка запоминается, и
    следующий импорт находит товар сам. Product.sku при этом не трогаем: он
    остаётся артикулом товара для продаж/закупок/производства.

    offer_id хранится нормализованным (см. services._norm_sku).
    """
    source = models.CharField(max_length=16, choices=Shipment.Source.choices)
    offer_id = models.CharField(max_length=128, help_text='Артикул маркетплейса (нормализованный)')
    product = models.ForeignKey('catalog.Product', related_name='marketplace_aliases',
                                on_delete=models.CASCADE)
    created_at = models.DateTimeField(auto_now_add=True)
    created_by = models.ForeignKey('auth.User', null=True, blank=True, on_delete=models.SET_NULL)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=['source', 'offer_id'],
                                    name='uniq_marketplace_alias_source_offer'),
        ]

    def __str__(self):
        return f'{self.source}:{self.offer_id} → {self.product_id}'
