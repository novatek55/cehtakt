"""Copy a catalog card, retaining references to existing component products."""
from copy import deepcopy

from django.db import transaction

from .models import Product, Process, Ingredient, IngredientOption, Operation


def _fields(instance, **overrides):
    values = {
        field.attname: deepcopy(getattr(instance, field.attname))
        for field in instance._meta.concrete_fields if not field.primary_key
    }
    values.update(overrides)
    return values


@transaction.atomic
def copy_product(product):
    suffix = ' — копия'
    name = product.name[:255 - len(suffix)] + suffix
    process = Process.objects.create(**_fields(product.process, name=name))
    copied = Product.objects.create(**_fields(product, process_id=process.pk, name=name))
    for ingredient in product.process.ingredients.prefetch_related('options').all():
        new_ingredient = Ingredient.objects.create(**_fields(ingredient, process_id=process.pk))
        for option in ingredient.options.all():
            IngredientOption.objects.create(**_fields(option, ingredient_id=new_ingredient.pk))
    for operation in product.process.operations.all():
        Operation.objects.create(**_fields(operation, process_id=process.pk))
    return copied
