"""Доменные сервисы каталога: классификация изделий и импорт техкарт-деревьев.

Классификация типа (material/semi/product) — единственный источник правды для
всех потребителей: склад (apps.stock), каталог (apps.catalog.views) и внешний
API (apps.integration). Правило доменное, а не деталь конкретного контракта,
поэтому живёт здесь, а не во вьюхах.

Импорт техкарты-дерева (формат proplan-editor autosave) в модели каталога.
Узел дерева: {name, isManufactured, isPurchased, quantity, unit,
              operations:[{name, station, duration, costPerHour, isParallel}],
              children:[...]}
Рекурсивно создаёт Process + Product на каждый узел, Ingredient на каждую связь
родитель→ребёнок, Operation на каждую операцию. Идемпотентно (upsert по имени).
"""
import re
from decimal import Decimal

from django.db import transaction

from apps.catalog.models import Process, Product, Ingredient, IngredientOption, Operation, ProductAttachment
from apps.directory.models import Area, Unit, Category

# Типы изделия в терминах пополнения склада.
TYPE_MATERIAL = 'material'
TYPE_SEMI = 'semi'
TYPE_PRODUCT = 'product'


def _money(value) -> Decimal:
    try:
        return Decimal(str(value or 0))
    except Exception:  # noqa: BLE001
        return Decimal('0')


def calculate_operations_cost(process: Process) -> Decimal:
    total = Decimal('0')
    for op in process.operations.select_related('area').all():
        cost_per_hour = (
            op.cost_per_hour_override
            if op.cost_per_hour_override is not None
            else op.area.cost_per_hour
        )
        total += _money(cost_per_hour) * Decimal(op.duration_seconds or 0) / Decimal(3600)
    return total


def calculate_product_cost(product: Product, visiting=None) -> Decimal:
    """Recursive unit cost: materials use purchase price; made items use BOM + operations."""
    if product is None:
        return Decimal('0')
    visiting = set(visiting or set())
    if product.id in visiting:
        return Decimal('0')
    process = product.process
    if process.buy:
        return _money(product.default_buy_price)

    visiting.add(product.id)
    total = calculate_operations_cost(process)
    ingredients = process.ingredients.select_related(
        'source_product',
        'source_product__process',
    )
    for ingredient in ingredients:
        total += (
            calculate_product_cost(ingredient.source_product, visiting)
            * _money(ingredient.quantity)
        )
    visiting.remove(product.id)
    return total.quantize(Decimal('0.01'))


def semi_product_ids() -> set:
    """Id изделий, которые входят в чей-либо состав, — это и есть полуфабрикаты.

    Явного признака «полуфабрикат» в данных нет: признак — участие в BOM.
    """
    return set(Ingredient.objects.values_list('source_product_id', flat=True).distinct())


def classify_product_type(process, product_id, semi_ids) -> str:
    """Тип изделия: material (закупаем) | semi (ПФ — входит в состав) | product (продукция).

    semi_ids — результат semi_product_ids(); передаётся снаружи, чтобы не делать
    запрос на каждую строку списка.
    """
    if process.buy:
        return TYPE_MATERIAL
    if process.make:
        return TYPE_SEMI if product_id in semi_ids else TYPE_PRODUCT
    # Ни «закупать», ни «производить» — вырожденная запись, считаем материалом.
    return TYPE_MATERIAL


def filter_products_by_type(qs, type_code, semi_ids=None):
    """Отфильтровать queryset изделий по типу. Неизвестный тип и None — без фильтра.

    Повторяет classify_product_type на уровне SQL с одним исключением: вырожденные
    записи (ни make, ни buy) классификатор зовёт материалом, а фильтр material их не
    выбирает — так было и до выноса логики, поведение сохранено намеренно.
    """
    if type_code not in (TYPE_MATERIAL, TYPE_SEMI, TYPE_PRODUCT):
        return qs
    if type_code == TYPE_MATERIAL:
        return qs.filter(process__buy=True)
    if semi_ids is None:
        semi_ids = semi_product_ids()
    made = qs.filter(process__make=True, process__buy=False)
    return made.filter(id__in=semi_ids) if type_code == TYPE_SEMI else made.exclude(id__in=semi_ids)


def _duration_seconds(v) -> int:
    if isinstance(v, (int, float)):
        return int(v)
    s = str(v or '').strip()
    if re.fullmatch(r'\d+(\.\d+)?', s):
        return int(float(s))
    total = 0.0
    for n, u in re.findall(r'(\d+(?:[.,]\d+)?)\s*(ч|мин|с)', s):
        total += float(n.replace(',', '.')) * {'ч': 3600, 'мин': 60, 'с': 1}[u]
    return int(total)


def _resolve_material_option(alt: dict) -> Product:
    """Find an existing OR material option by SKU or exact name."""
    name = (alt.get('name') or '').strip()
    if not name:
        raise ValueError('вариант ИЛИ без названия')
    sku = (alt.get('article') or '').strip()
    qs = Product.objects.select_related('process').filter(process__buy=True)
    product = qs.filter(sku=sku).first() if sku else None
    if product is None:
        product = qs.filter(name=name).first()
    if product is None:
        raise ValueError(f'вариант ИЛИ "{name}" не найден в базе материалов')
    return product


_DIMS_RE = re.compile(r'(\d+(?:[.,]\d+)?)\s*[×хxX*]\s*(\d+(?:[.,]\d+)?)')


def _parse_two_dims(text):
    """Первые два числа-габарита из названия («30*15», «30х15», «1250*2500*1» → (a, b), мм)."""
    m = _DIMS_RE.search(text or '')
    if not m:
        return None
    a = float(m.group(1).replace(',', '.'))
    b = float(m.group(2).replace(',', '.'))
    if a <= 0 or b <= 0:
        return None
    return a, b


def compute_material_paint_area(name, unit_name=None):
    """Коэффициент площади окраски (м² на 1 ед.) из названия материала.

    - Труба (в названии есть «труб»), ед. м.пог.: периметр внешней стороны = 2·(a+b)/1000 м².
    - Лист (в названии есть «лист»): красится с двух сторон.
      Ед. м²/м2 → 2 (на 1 м²); иначе (штуки) — размеры a×b мм из названия → 2·a·b/1e6 (на 1 лист).
    Иначе (не распознан тип/габариты) → None.
    """
    low = (name or '').lower()
    unit = (unit_name or '').strip().lower()
    if 'труб' in low:
        dims = _parse_two_dims(name)
        if not dims:
            return None
        a, b = dims
        return round(2 * (a + b) / 1000, 4)
    if 'лист' in low:
        if unit in ('м²', 'м2'):
            return 2.0
        dims = _parse_two_dims(name)
        if not dims:
            return None
        a, b = dims
        return round(2 * a * b / 1_000_000, 4)
    return None


def _ref_by_name(model, name):
    """Найти справочную запись по имени, терпимо к дублям в данных.

    get_or_create(name=...) внутри делает get() и падает с MultipleObjectsReturned,
    если в БД уже есть дубли по имени (напр. две категории «материалы» на проде —
    поле Category.name/Area.name не уникально). Берём первую существующую, иначе создаём.
    """
    return model.objects.filter(name=name).first() or model.objects.create(name=name)


def _import_node(node: dict, stats: dict, force_product: Product | None = None) -> Product:
    """Импортирует узел, возвращает его Product (для связи как ingredient).

    Если передан force_product — это переименование существующего изделия «на месте»
    (по id, а не поиск по имени): корневой Process/Product обновляются напрямую,
    поэтому смена названия не плодит дубликат и id изделия сохраняется.
    """
    name = (node.get('name') or '').strip()
    if not name:
        raise ValueError('узел без названия')

    # Редактор передаёт productId для уже существующих дочерних узлов. Имя —
    # редактируемое поле, поэтому искать по нему нельзя: переименование иначе
    # создаёт новый Product/Process и подменяет позицию в спецификации.
    if force_product is None and node.get('productId') not in (None, ''):
        try:
            product_id = int(node['productId'])
        except (TypeError, ValueError):
            raise ValueError('некорректный id изделия')
        force_product = Product.objects.select_related('process').filter(pk=product_id).first()
        if force_product is None:
            raise ValueError('изделие для обновления не найдено')

    unit = None
    uname = (node.get('unit') or '').strip()
    if uname:
        unit = _ref_by_name(Unit, uname)

    category = None
    cname = (node.get('category') or '').strip()
    if cname:
        category = _ref_by_name(Category, cname)

    def _num(v):
        try:
            return float(v) if v not in (None, '') else 0
        except (TypeError, ValueError):
            return 0

    proc_fields = {
        'make': bool(node.get('isManufactured', True)),
        'buy': bool(node.get('isPurchased', False)),
        'unit': unit,
        'category': category,
        'description': (node.get('note') or '').strip(),
    }
    def _paint_area(node_val):
        try:
            v = float(node_val) if node_val not in (None, '') else None
        except (TypeError, ValueError):
            v = None
        return v if v and v > 0 else None

    prod_fields = {
        'sku': (node.get('article') or '').strip(),
        'default_sell_price': _num(node.get('price')),
        'default_buy_price': _num(node.get('purchasePrice')),
        'min_limit': _num(node.get('minStock')),
    }

    if force_product is not None:
        # Переименование «на месте»: правим существующие Process/Product по id.
        process = force_product.process
        process.name = name
        for k, v in proc_fields.items():
            setattr(process, k, v)
        process.save()
        product = force_product
        product.name = name
        for k, v in prod_fields.items():
            setattr(product, k, v)
        product.save()
        is_new = False
    else:
        process, _ = Process.objects.update_or_create(name=name, defaults=proc_fields)
        product, is_new = Product.objects.update_or_create(
            process=process, name=name, defaults=prod_fields,
        )
    stats['products'] += 1
    stats['new_products'] += is_new

    paint_area = _paint_area(node.get('paintAreaPerUnit'))
    if paint_area is not None:
        opts = dict(product.options or {})
        opts['paint_area_per_unit'] = paint_area
        product.options = opts
        product.save(update_fields=['options'])

    image = node.get('image') or None
    image_id = image.get('id') if isinstance(image, dict) else None
    file_ids = [
        item.get('id') for item in (node.get('files') or [])
        if isinstance(item, dict) and item.get('id')
    ]
    keep_ids = [*([image_id] if image_id else []), *file_ids]
    if keep_ids:
        ProductAttachment.objects.filter(pk__in=keep_ids).update(product=product)
    ProductAttachment.objects.filter(product=product, kind=ProductAttachment.Kind.IMAGE).exclude(
        pk=image_id
    ).delete()
    ProductAttachment.objects.filter(product=product, kind=ProductAttachment.Kind.FILE).exclude(
        pk__in=file_ids
    ).delete()

    # BOM и операции — пересобираем набор для идемпотентности (чистый replace)
    Ingredient.objects.filter(process=process).delete()
    Operation.objects.filter(process=process).delete()
    for i, op in enumerate(node.get('operations') or []):
        station = (op.get('station') or op.get('name') or '').strip()
        if not station:
            continue
        area = _ref_by_name(Area, station)
        Operation.objects.create(
            process=process, area=area, name=(op.get('name') or station).strip(),
            sequence=i, duration_seconds=_duration_seconds(op.get('duration')),
            cost_per_hour_override=op.get('costPerHour') or None,
            is_parallel=bool(op.get('isParallel', False)),
        )
        stats['operations'] += 1

    # дети → рекурсия + спецификация
    for child in node.get('children') or []:
        child_product = _import_node(child, stats)
        ing_obj = Ingredient.objects.create(
            process=process, source_product=child_product,
            quantity=child.get('quantity') or 1,
        )
        IngredientOption.objects.filter(ingredient=ing_obj).delete()
        IngredientOption.objects.create(ingredient=ing_obj, source_product=child_product, priority=0)
        for i, alt in enumerate(child.get('alternatives') or [], start=1):
            alt_prod = _resolve_material_option(alt)
            IngredientOption.objects.create(ingredient=ing_obj, source_product=alt_prod, priority=i)
        stats['ingredients'] += 1

    return product


@transaction.atomic
def import_tech_tree(tree: dict, root_product: Product | None = None) -> dict:
    """Точка входа: принимает {root:{...}} или узел напрямую. Возвращает статистику.

    root_product — если задан, корень дерева переименовывается «на месте» (по id),
    что сохраняет id изделия и не создаёт дубликат при смене названия.
    """
    root = tree.get('root', tree) if isinstance(tree, dict) else tree
    stats = {'products': 0, 'new_products': 0, 'ingredients': 0, 'operations': 0}
    root_product_obj = _import_node(root, stats, force_product=root_product)
    stats['root'] = root.get('name')
    stats['root_id'] = root_product_obj.pk
    return stats
