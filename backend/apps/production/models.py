"""Производство: задание (ProductionJob) и его операции по участкам (JobOperation).

Источник — docs/data-model.md (ProductionPlanRow + areaCells). Дерево Плана строится
через parent (заказ-группа → под-задания). JobOperation = карточки канбана по участкам.
"""
from django.db import models


class ProductionJob(models.Model):
    """Производственное задание (строка Плана / узел дерева)."""
    class Status(models.TextChoices):
        WAITING = 'waiting', 'В ожидании'
        IN_PROGRESS = 'in_progress', 'В работе'
        OVERDUE = 'overdue', 'Просрочено'
        DONE = 'done', 'Готово'

    name = models.CharField(max_length=255, help_text='Номер задания (иерархический)')
    parent = models.ForeignKey('self', null=True, blank=True, related_name='children', on_delete=models.CASCADE)
    product = models.ForeignKey('catalog.Product', on_delete=models.PROTECT)
    process = models.ForeignKey('catalog.Process', null=True, blank=True, on_delete=models.SET_NULL)
    sales_order = models.ForeignKey('sales.SalesOrder', null=True, blank=True, related_name='jobs', on_delete=models.SET_NULL)
    batch = models.CharField(max_length=128, blank=True, help_text='Партийный номер')
    customers = models.CharField(max_length=255, blank=True, help_text='Получатель')
    quantity = models.DecimalField(max_digits=14, decimal_places=3, default=0)
    made_quantity = models.DecimalField(max_digits=14, decimal_places=3, default=0)
    finish_by_date = models.DateField(null=True, blank=True)
    scheduled_finish_date = models.DateField(null=True, blank=True)
    started_at = models.DateTimeField(null=True, blank=True)
    description = models.CharField(max_length=512, blank=True)
    status = models.CharField(max_length=16, choices=Status.choices, default=Status.WAITING)
    is_simple = models.BooleanField(default=True)
    ingredients_status = models.CharField(max_length=16, default='none')
    rank = models.IntegerField(default=0)
    created_at = models.DateTimeField(auto_now_add=True)
    rework = models.ForeignKey('DefectRecord', null=True, blank=True,
                               related_name='jobs', on_delete=models.PROTECT)

    class Meta:
        ordering = ['rank', 'id']

    def __str__(self):
        return self.name


class JobOperation(models.Model):
    """Операция задания на конкретном участке — карточка канбана (Задания)."""
    class Status(models.TextChoices):
        WAITING = 'waiting', 'В ожидании'
        IN_PROGRESS = 'in_progress', 'В работе'
        DONE = 'done', 'Готово'

    job = models.ForeignKey(ProductionJob, related_name='operations', on_delete=models.CASCADE)
    area = models.ForeignKey('directory.Area', related_name='job_operations', on_delete=models.PROTECT)
    operation = models.ForeignKey('catalog.Operation', null=True, blank=True, on_delete=models.SET_NULL)
    name = models.CharField(max_length=255, blank=True)
    quantity = models.DecimalField(max_digits=14, decimal_places=3, default=0)
    started_quantity = models.DecimalField(max_digits=14, decimal_places=3, default=0,
                                           help_text='Взято в работу (кнопка «Начать»)')
    done_quantity = models.DecimalField(max_digits=14, decimal_places=3, default=0,
                                        help_text='Годных выпущено')
    defect_quantity = models.DecimalField(max_digits=14, decimal_places=3, default=0,
                                          help_text='Брак (материал списан, изделие не вышло)')
    sequence = models.PositiveIntegerField(default=0)
    status = models.CharField(max_length=16, choices=Status.choices, default=Status.WAITING)
    started_at = models.DateTimeField(null=True, blank=True)
    finished_at = models.DateTimeField(null=True, blank=True)
    completion_snapshot = models.JSONField(null=True, blank=True, editable=False)
    executor = models.ForeignKey('auth.User', null=True, blank=True, on_delete=models.SET_NULL)

    class Meta:
        ordering = ['sequence', 'id']
        indexes = [
            # Внешний API выпуска (/api/v1/production/output/) регулярно опрашивает
            # завершённые операции с ORDER BY finished_at, id — без индекса это
            # filesort по всей таблице на каждом запросе.
            models.Index(fields=['finished_at', 'id'], name='joboperation_finished_idx'),
        ]

    def __str__(self):
        return f'{self.name or self.job} @ {self.area}'


class OperationCancellation(models.Model):
    """Immutable record of an undone completion, retained if work is redistributed."""
    job = models.ForeignKey(ProductionJob, null=True, on_delete=models.SET_NULL, related_name='cancellations')
    operation = models.ForeignKey(JobOperation, null=True, on_delete=models.SET_NULL)
    area = models.ForeignKey('directory.Area', null=True, on_delete=models.SET_NULL)
    created_by = models.ForeignKey('auth.User', null=True, on_delete=models.SET_NULL)
    created_at = models.DateTimeField(auto_now_add=True)

    reason = models.CharField(max_length=500)
    snapshot = models.JSONField()

    class Meta:
        ordering = ['-created_at', '-id']


class DefectRecord(models.Model):
    """An immutable defect origin; names survive reassignment and source deletion."""
    source_job = models.ForeignKey(ProductionJob, null=True, on_delete=models.SET_NULL,
                                   related_name='defect_records')
    source_operation = models.ForeignKey(JobOperation, null=True, on_delete=models.SET_NULL,
                                         related_name='defect_records')
    quantity = models.DecimalField(max_digits=14, decimal_places=3)
    created_at = models.DateTimeField(auto_now_add=True)
    source = models.JSONField(default=dict)
    generated_jobs = models.JSONField(default=list)
    added_reservations = models.JSONField(default=list)
    cancelled_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ['-created_at', '-id']


class Reservation(models.Model):
    """Строка реестра резерва: consume (взято со склада) / produce (будет изготовлено).

    Источник правды для StockItem.committed/expected (см. services.recalc_stock).
    """
    class Kind(models.TextChoices):
        CONSUME = 'consume', 'Потребление'
        PRODUCE = 'produce', 'Выпуск'

    product = models.ForeignKey('catalog.Product', related_name='reservations', on_delete=models.PROTECT)
    sales_order = models.ForeignKey('sales.SalesOrder', related_name='reservations', on_delete=models.CASCADE)
    job = models.ForeignKey(ProductionJob, null=True, blank=True, related_name='reservations', on_delete=models.CASCADE)
    ingredient = models.ForeignKey('catalog.Ingredient', null=True, blank=True,
                                   related_name='reservations', on_delete=models.SET_NULL,
                                   help_text='Строка состава (для гашения резерва по группе ИЛИ)')
    kind = models.CharField(max_length=8, choices=Kind.choices)
    quantity = models.DecimalField(max_digits=16, decimal_places=4, default=0)
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f'{self.get_kind_display()} {self.product} × {self.quantity}'
