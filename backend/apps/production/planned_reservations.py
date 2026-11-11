from collections import defaultdict
from decimal import Decimal

from django.db.models import Exists, OuterRef, Prefetch, Q

from apps.catalog.models import Ingredient
from apps.stock.models import StockItem

from .models import JobOperation, ProductionJob, Reservation
from .rework import _pending


def uncovered_job_reservations(stock_items=None):
    """Return (job, product ID, quantity) for work missing from the stored reserve.

    Count open quantities once per job, including split operations and standalone
    jobs without route cards. Existing holds keep priority over new allocations.
    """
    operations = JobOperation.objects.filter(job_id=OuterRef('pk'))
    ingredients = Ingredient.objects.order_by('id').prefetch_related('options')
    jobs = (
        ProductionJob.objects
        .annotate(has_operations=Exists(operations),
                  has_open_operations=Exists(operations.exclude(status=JobOperation.Status.DONE)))
        .filter(Q(has_open_operations=True) |
                (Q(has_operations=False) & ~Q(status=ProductionJob.Status.DONE)))
        .select_related('sales_order', 'product__process')
        .prefetch_related('operations', Prefetch('product__process__ingredients', queryset=ingredients))
        .order_by('rank', 'id')
    )

    job_holds, order_holds = defaultdict(Decimal), defaultdict(Decimal)
    for job_id, order_id, ingredient_id, product_id, quantity in (
        Reservation.objects.filter(kind=Reservation.Kind.CONSUME, quantity__gt=0)
        .values_list('job_id', 'sales_order_id', 'ingredient_id', 'product_id', 'quantity')
    ):
        if job_id is None:
            order_holds[order_id, ingredient_id, product_id] += quantity
        else:
            job_holds[job_id, ingredient_id, product_id] += quantity

    if stock_items is None:
        stock_items = StockItem.objects.all()
    free = defaultdict(Decimal, {
        stock.product_id: max(stock.available_quantity - stock.committed_quantity, Decimal(0))
        for stock in stock_items
    })
    requirements = []
    for job in jobs:
        units = _pending(job) if job.has_operations else job.quantity - job.made_quantity
        if units <= 0:
            continue
        for ingredient in job.product.process.ingredients.all():
            quantity = units * ingredient.quantity
            if quantity <= 0:
                continue
            options = tuple(dict.fromkeys(option.source_product_id for option in ingredient.options.all()))
            requirements.append((job, ingredient.id, options or (ingredient.source_product_id,), quantity))

    # Cover exact ingredients before allocating legacy, untagged holds.
    # An order-level hold can be shared by several jobs, but consumed only once.
    for holds, by_job, exact in (
        (job_holds, True, True), (job_holds, True, False),
        (order_holds, False, True), (order_holds, False, False),
    ):
        remaining = []
        for job, ingredient_id, options, quantity in requirements:
            owner = job.pk if by_job else job.sales_order_id
            for product_id in options:
                key = (owner, ingredient_id if exact else None, product_id)
                take = min(quantity, holds[key])
                holds[key] -= take
                quantity -= take
            if quantity > 0:
                remaining.append((job, ingredient_id, options, quantity))
        requirements = remaining

    additions = []
    for job, _, options, quantity in requirements:
        # Reserve available substitutes in priority order and put the shortage
        # on the primary component, as in order planning.
        for product_id in options:
            take = min(quantity, free[product_id])
            if take:
                additions.append((job, product_id, take))
                free[product_id] -= take
                quantity -= take
        if quantity:
            additions.append((job, options[0], quantity))
    return additions
