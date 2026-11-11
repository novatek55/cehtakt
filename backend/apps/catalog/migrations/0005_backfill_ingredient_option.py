from django.db import migrations


def backfill(apps, schema_editor):
    Ingredient = apps.get_model('catalog', 'Ingredient')
    IngredientOption = apps.get_model('catalog', 'IngredientOption')
    for ing in Ingredient.objects.all().iterator():
        if not IngredientOption.objects.filter(ingredient=ing).exists():
            IngredientOption.objects.create(
                ingredient=ing, source_product_id=ing.source_product_id, priority=0)


def unbackfill(apps, schema_editor):
    IngredientOption = apps.get_model('catalog', 'IngredientOption')
    IngredientOption.objects.all().delete()


class Migration(migrations.Migration):
    dependencies = [('catalog', '0004_ingredientoption')]
    operations = [migrations.RunPython(backfill, unbackfill)]
