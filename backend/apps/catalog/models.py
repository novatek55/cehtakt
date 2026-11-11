"""Каталог: изделия (Process), варианты (Product), состав (Ingredient), операции (Operation).

Источник — docs/data-model.md. Ключевое:
- Process = мастер-изделие с флагами make/buy, составом (ingredients) и техпроцессом (граф/операции)
- Product = вариант изделия (sku, цены, себестоимость = ingredients_cost + operations_cost)
- Ingredient = строка BOM (ссылается на другой Product → рекурсия)
- Operation = шаг техпроцесса на участке (Area); complexity=complex хранит граф в Process.graph
"""
from django.db import models


class Process(models.Model):
    """Изделие (мастер-запись каталога)."""
    class Complexity(models.TextChoices):
        SIMPLE = 'simple', 'Простой'
        COMPLEX = 'complex', 'Сложный'

    name = models.CharField(max_length=255)
    archived = models.BooleanField(default=False)
    unit = models.ForeignKey('directory.Unit', null=True, blank=True, on_delete=models.PROTECT)
    category = models.ForeignKey('directory.Category', null=True, blank=True, on_delete=models.SET_NULL)
    default_supplier = models.ForeignKey('directory.Supplier', null=True, blank=True, on_delete=models.SET_NULL)
    # Способ пополнения
    make = models.BooleanField(default=True, help_text='Производство')
    buy = models.BooleanField(default=False, help_text='Закупка')
    make_by_default = models.BooleanField(default=True)
    # Техпроцесс
    complexity = models.CharField(max_length=16, choices=Complexity.choices, default=Complexity.SIMPLE)
    graph = models.JSONField(default=dict, blank=True, help_text='DAG операций для сложного техпроцесса')
    description = models.TextField(blank=True)

    class Meta:
        verbose_name_plural = 'processes'

    def __str__(self):
        return self.name


class CurrentProductManager(models.Manager):
    def get_queryset(self):
        return super().get_queryset().filter(merged_into__isnull=True)


class Product(models.Model):
    """Вариант изделия — то, что продаётся/закупается/лежит на складе."""
    process = models.ForeignKey(Process, related_name='products', on_delete=models.CASCADE)
    name = models.CharField(max_length=255)
    sku = models.CharField(max_length=128, blank=True)
    archived = models.BooleanField(default=False)
    merged_into = models.ForeignKey('self', null=True, blank=True, editable=False,
                                   related_name='merged_duplicates', on_delete=models.PROTECT)
    objects = CurrentProductManager()
    all_objects = models.Manager()
    default_sell_price = models.DecimalField(max_digits=14, decimal_places=2, default=0)
    default_buy_price = models.DecimalField(max_digits=14, decimal_places=2, default=0)
    inner_barcode = models.CharField(max_length=128, blank=True)
    min_limit = models.DecimalField(max_digits=14, decimal_places=3, default=0, help_text='Несниж. остаток')
    options = models.JSONField(default=dict, blank=True, help_text='Характеристики варианта')
    # Кэш вычисляемой себестоимости (пересчёт по BOM+операциям)
    ingredients_cost = models.DecimalField(max_digits=14, decimal_places=2, default=0)
    operations_cost = models.DecimalField(max_digits=14, decimal_places=2, default=0)
    total_production_duration = models.PositiveIntegerField(default=0, help_text='сек')

    def __str__(self):
        return self.name


class ProductMerge(models.Model):
    """Permanent record of a material merge; source ID remains a tombstone."""
    source = models.OneToOneField(Product, related_name='merge_record', on_delete=models.PROTECT)
    target = models.ForeignKey(Product, related_name='merge_history', on_delete=models.PROTECT)
    created_by = models.ForeignKey('auth.User', null=True, on_delete=models.SET_NULL)
    created_at = models.DateTimeField(auto_now_add=True)
    revision = models.CharField(max_length=64)
    details = models.JSONField(default=dict)

    class Meta:
        ordering = ['-created_at', '-id']


class ProductAttachment(models.Model):
    """Файл техкарты, привязанный к конкретному изделию/детали."""
    class Kind(models.TextChoices):
        IMAGE = 'image', 'Картинка'
        FILE = 'file', 'Файл'

    product = models.ForeignKey(Product, null=True, blank=True, related_name='attachments', on_delete=models.CASCADE)
    kind = models.CharField(max_length=16, choices=Kind.choices)
    file = models.FileField(upload_to='techcards/%Y/%m/')
    original_name = models.CharField(max_length=255)
    content_type = models.CharField(max_length=128, blank=True)
    content_hash = models.CharField(max_length=64)
    size = models.PositiveIntegerField(default=0)
    uploaded_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['kind', 'id']

    def __str__(self):
        return self.original_name


class Ingredient(models.Model):
    """Строка спецификации (BOM): компонент в составе изделия."""
    process = models.ForeignKey(Process, related_name='ingredients', on_delete=models.CASCADE)
    source_product = models.ForeignKey(Product, related_name='used_in', on_delete=models.PROTECT)
    quantity = models.DecimalField(max_digits=14, decimal_places=4, default=1)
    notes = models.CharField(max_length=255, blank=True)

    def __str__(self):
        return f'{self.source_product} × {self.quantity}'


class IngredientOption(models.Model):
    """Взаимозаменяемый материал в составе слота BOM (Ingredient).

    priority=0 — основной вариант; порядок определяет waterfall при
    разузловании: сначала выбираем остаток варианта с меньшим priority,
    затем следующего. Дефицит закупается как основной вариант.
    """
    ingredient = models.ForeignKey(Ingredient, related_name='options', on_delete=models.CASCADE)
    source_product = models.ForeignKey(Product, related_name='used_as_option', on_delete=models.PROTECT)
    priority = models.PositiveIntegerField(default=0)

    class Meta:
        ordering = ['priority', 'id']

    def __str__(self):
        return f'#{self.priority} {self.source_product}'


class Operation(models.Model):
    """Операция техпроцесса на участке (для простого техпроцесса — упорядоченный список)."""
    process = models.ForeignKey(Process, related_name='operations', on_delete=models.CASCADE)
    area = models.ForeignKey('directory.Area', on_delete=models.PROTECT)
    name = models.CharField(max_length=255)
    sequence = models.PositiveIntegerField(default=0)
    duration_seconds = models.PositiveIntegerField(default=0, help_text='Ожидаемая длительность')
    cost_per_hour_override = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True)
    is_parallel = models.BooleanField(default=False, help_text='Выполнять параллельно')

    class Meta:
        ordering = ['sequence', 'id']

    def __str__(self):
        return f'{self.name} @ {self.area}'
