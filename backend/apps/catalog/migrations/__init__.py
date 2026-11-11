def _run_paint_backfill(apps):
    """Проставить options.paint_area_per_unit всем трубам и листам по названию.

    Вынесено сюда (а не в модуль миграции) ради переиспользования и тестируемости.
    apps — реестр моделей (historical в миграции, django.apps.apps в тесте).
    Возвращает число обновлённых записей.
    """
    from apps.catalog.services import compute_material_paint_area
    Product = apps.get_model('catalog', 'Product')
    updated = 0
    for p in Product.objects.select_related('process', 'process__unit').all():
        # Коэффициент — только на материалах (закуп), не на производимых сборках:
        # фронт показывает поле лишь на !isManufactured, и иначе калькулятор задвоил бы
        # площадь (сборка + её деталь).
        if getattr(p.process, 'make', False):
            continue
        unit_name = p.process.unit.name if getattr(p.process, 'unit_id', None) else None
        coeff = compute_material_paint_area(p.name, unit_name)
        if coeff is None:
            continue
        opts = dict(p.options or {})
        if opts.get('paint_area_per_unit') == coeff:
            continue
        opts['paint_area_per_unit'] = coeff
        p.options = opts
        p.save(update_fields=['options'])
        updated += 1
    return updated
