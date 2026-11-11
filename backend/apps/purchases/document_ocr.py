import io
import re
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation

from django.core.files.uploadedfile import UploadedFile

from apps.catalog.models import Product


NUMBER_RE = re.compile(r"(?<!\d)(\d[\d OoОо\s.,\-–—]*\d|\d)(?!\d)")
CODE_RE = re.compile(r"\[?\d{1,2}[-–]\d{4,}")
OCR_DIGIT_TRANS = str.maketrans({
    "O": "0",
    "o": "0",
    "О": "0",
    "о": "0",
    "З": "3",
    "з": "3",
    "б": "6",
    "\u00a0": "",
    " ": "",
})


@dataclass
class OcrCell:
    row: int
    col: int
    text: str
    score: float | None = None


class OcrUnavailable(RuntimeError):
    pass


_OCR_LETTER_TRANS = str.maketrans({
    "O": "0", "o": "0", "О": "0", "о": "0", "З": "3", "з": "3", "б": "6",
})


def normalize_number(value):
    raw = str(value or "").strip()
    if not raw:
        return None
    # Буквы-ошибки → цифры, но пробелы пока сохраняем, чтобы видеть группы разрядов.
    text = raw.translate(_OCR_LETTER_TRANS)
    text = re.sub(r"(?<=\d)[-–—](?=\d{1,2}\D*$)", ".", text)
    text = re.sub(r"(?<=\d)\.\s+(?=\d{1,2}\D*$)", ".", text)
    text = re.sub(r"(?<=\d)\s+(?=\d{1,2}\D*$)", ".", text) if re.fullmatch(r"\d{1,3}\s+\d{1,2}", text) else text
    has_separator = "," in text or "." in text
    text = text.replace(",", ".")
    if not has_separator:
        # OCR часто теряет запятую копеек, оставляя пробел: «33 600 00» → 33600.00.
        # Разряды в рублях группируются по 3 цифры, поэтому 2-значная последняя
        # группа без разделителя — это копейки, а не тысячи.
        groups = [g for g in re.split(r"[\s ]+", text.strip()) if g]
        if len(groups) >= 2 and all(g.isdigit() for g in groups):
            if len(groups[-1]) == 2:
                text = "".join(groups[:-1]) + "." + groups[-1]
            elif len(groups[-1]) > 3:
                joined = "".join(groups)
                text = joined[:-2] + "." + joined[-2:]
    cleaned = re.sub(r"[^0-9.]", "", text)
    if not cleaned:
        return None
    if cleaned.count(".") > 1:
        head, *tail = cleaned.split(".")
        cleaned = head + "." + "".join(tail)
    try:
        return Decimal(cleaned)
    except InvalidOperation:
        return None


def numbers_in_text(text):
    values = []
    for match in NUMBER_RE.findall(text or ""):
        value = normalize_number(match)
        if value is not None:
            values.append(value)
    return values


def row_status(quantity, price, amount):
    warnings = []
    if quantity is None or price is None or amount is None:
        return "warning", ["missing_numeric_value"]
    expected = (quantity * price).quantize(Decimal("0.01"))
    tolerance = max(Decimal("5.00"), abs(expected) * Decimal("0.015"))
    if abs(expected - amount) > tolerance:
        warnings.append("amount_mismatch")
    return ("warning" if warnings else "ok"), warnings


def _cell_text(cell):
    return " ".join(str(cell.get("text", "")).split())


def _row_cells(cells):
    rows = {}
    for cell in cells:
        text = _cell_text(cell)
        if not text:
            continue
        rows.setdefault(int(cell["row"]), []).append({**cell, "text": text})
    return {row: sorted(items, key=lambda item: int(item["col"])) for row, items in rows.items()}


def _is_product_name(cell):
    text = cell["text"]
    if not text:
        return False
    if CODE_RE.fullmatch(text.strip()):
        return False
    if int(cell["col"]) not in {2, 3, 4}:
        return False
    if any(word in text.casefold() for word in (
        "наименование",
        "товара",
        "акциза",
        "налога",
        "налоговая",
        "услуг",
        "регистрационный",
    )):
        return False
    letters = len(re.findall(r"[A-Za-zА-Яа-яЁё]", text))
    digits = len(re.findall(r"\d", text))
    return letters >= 4 and letters >= digits


def _pick_name(cells):
    candidates = [cell for cell in cells if _is_product_name(cell)]
    if not candidates:
        return ""
    candidates.sort(key=lambda cell: (-len(cell["text"]), int(cell["col"])))
    return candidates[0]["text"]


def _numeric_cells(cells):
    items = []
    for cell in cells:
        values = numbers_in_text(cell["text"])
        if values:
            items.append({"col": int(cell["col"]), "text": cell["text"], "values": values})
    return items


def _has_item_code(cells):
    for cell in cells:
        if int(cell["col"]) != 0:
            continue
        cleaned = str(cell["text"]).translate(OCR_DIGIT_TRANS).replace("–", "-").replace("—", "-")
        if CODE_RE.search(cleaned):
            return True
    return False


def _pick_quantity_price_amount(cells):
    nums = _numeric_cells(cells)
    quantity = price = amount = check_amount = None
    by_col = {}
    for item in nums:
        by_col.setdefault(item["col"], []).extend(item["values"])

    if by_col.get(6):
        quantity = by_col[6][-1]
    if by_col.get(7):
        price = by_col[7][-1]
    if by_col.get(8):
        check_amount = by_col[8][-1]
    if by_col.get(13):
        amount = by_col[13][-1]
    elif by_col.get(19):
        amount = by_col[19][-1]
    elif check_amount is not None:
        amount = check_amount

    for item in nums:
        col = item["col"]
        value = item["values"][-1]
        if quantity is None and 5 <= col <= 7 and value >= 1:
            quantity = value
        if price is None and 7 <= col <= 10 and Decimal("0") < value < Decimal("100000"):
            price = value

    amount_candidates = [
        item["values"][-1] for item in nums
        if item["col"] >= 11 and item["values"][-1] >= Decimal("1")
    ]
    if amount_candidates:
        amount = amount_candidates[-1]

    all_values = [value for item in nums for value in item["values"]]
    if quantity is None and len(all_values) >= 3:
        quantity = all_values[-3]
    if price is None and len(all_values) >= 2:
        price = all_values[-2]
    if amount is None and all_values:
        amount = all_values[-1]
    if check_amount is None:
        check_amount = amount
    return quantity, price, amount, check_amount


# Ключевые слова шапки — терпимы к типичным ошибкам OCR (е→с/о и т.п.):
# сравниваем по коротким подстрокам-корням, а не по полному слову.
_HEADER_NAME_WORDS = ("наименование", "номенклатур", "товар", "работ", "услуг")
_HEADER_QTY_WORDS = ("кол",)  # кол-во / количество / «количсство»
_HEADER_PRICE_WORDS = ("цен", "цон", "тариф")  # цена / «цона»
_HEADER_SUM_WORDS = ("сумм", "стоим")


def _is_item_name_text(text):
    if not text:
        return False
    low = text.casefold()
    if any(word in low for word in ("наименование", "итого", "всего", "работы, услуги", "работы; услуги")):
        return False
    letters = len(re.findall(r"[A-Za-zА-Яа-яЁё]", text))
    return letters >= 4


def _detect_header_columns(grouped, sorted_rows):
    """Найти строку-шапку и вернуть (cols, header_row_index).

    cols: физические колонки name/quantity/price/amount (amount — самая правая
    «Сумма/Стоимость»). Возвращает None, если шапка не найдена.
    """
    best = None
    for row_index in sorted_rows:
        cols = {"name": None, "quantity": None, "price": None, "amount": None}
        for cell in grouped[row_index]:
            low = cell["text"].casefold()
            col = int(cell["col"])
            if cols["name"] is None and any(word in low for word in _HEADER_NAME_WORDS):
                cols["name"] = col
            if cols["quantity"] is None and any(word in low for word in _HEADER_QTY_WORDS):
                cols["quantity"] = col
            if cols["price"] is None and any(word in low for word in _HEADER_PRICE_WORDS):
                cols["price"] = col
            if any(word in low for word in _HEADER_SUM_WORDS):
                if cols["amount"] is None or col > cols["amount"]:
                    cols["amount"] = col
        score = sum(1 for value in cols.values() if value is not None)
        # Шапка обязана иметь колонку наименования и хотя бы одну «сумму».
        if cols["name"] is not None and cols["amount"] is not None and score >= 2:
            if best is None or score > best[0]:
                best = (score, row_index, cols)
    return (best[2], best[1]) if best else None


def _name_at_col(cells, name_col):
    parts = [
        cell["text"]
        for cell in sorted(cells, key=lambda item: int(item["col"]))
        if int(cell["col"]) == name_col and cell["text"]
    ]
    text = " ".join(parts).strip()
    return text if _is_item_name_text(text) else ""


def _assemble_invoice_rows(grouped, sorted_rows, header_cols, header_row):
    """Позиционная сборка строк счёта.

    Номера строк и точные индексы колонок в фото-OCR ненадёжны (номер часто
    теряется или слипается с названием, колонки шапки и данных смещены).
    Поэтому: наименование берём из name_col, а числа — по порядку слева направо
    правее названия: первое = кол-во, второе = цена, последнее = сумма.
    """
    name_col = header_cols["name"]
    start_rows = [
        row_index
        for row_index in sorted_rows
        if row_index > header_row and _name_at_col(grouped[row_index], name_col)
    ]

    rows = []
    for index, row_index in enumerate(start_rows):
        next_start = start_rows[index + 1] if index + 1 < len(start_rows) else None
        end_row = next_start if next_start is not None else row_index + 2
        combined_cells = []
        for source_row in sorted_rows:
            if row_index <= source_row < end_row:
                combined_cells.extend(grouped[source_row])

        name = _name_at_col(grouped[row_index], name_col)
        if not name:
            continue

        nums = []
        for cell in sorted(combined_cells, key=lambda item: int(item["col"])):
            if int(cell["col"]) <= name_col:
                continue
            values = numbers_in_text(cell["text"])
            if values:
                nums.append(values[-1])
        if not nums:
            continue

        amount = nums[-1]
        quantity = nums[0] if len(nums) >= 2 else None
        price = nums[1] if len(nums) >= 3 else None
        status, warnings = row_status(quantity, price, amount)
        rows.append({
            "supplier_name": name,
            "quantity": quantity,
            "price": price,
            "amount": amount,
            "status": status,
            "warnings": warnings,
            "source_row": row_index,
            "product_id": None,
            "product_name": "",
            "match_score": 0,
        })
    return rows


def _group_text_items(items, y_tolerance=14):
    groups = []
    for item in sorted(items, key=lambda value: (float(value["y"]), float(value["x"]))):
        target = next((group for group in groups if abs(group["y"] - float(item["y"])) <= y_tolerance), None)
        if target is None:
            target = {"y": float(item["y"]), "items": []}
            groups.append(target)
        target["items"].append(item)
        target["y"] = sum(float(value["y"]) for value in target["items"]) / len(target["items"])
    return sorted(groups, key=lambda group: group["y"])


def _text_item_header_columns(grouped_rows):
    best = None
    for row in grouped_rows:
        cols = {"name": None, "quantity": None, "price": None, "amount": None}
        for item in row["items"]:
            text = str(item["text"]).casefold()
            x = float(item["x"])
            if cols["name"] is None and any(word in text for word in _HEADER_NAME_WORDS):
                cols["name"] = x
            if cols["quantity"] is None and any(word in text for word in _HEADER_QTY_WORDS):
                cols["quantity"] = x
            if cols["price"] is None and any(word in text for word in _HEADER_PRICE_WORDS):
                cols["price"] = x
            if any(word in text for word in _HEADER_SUM_WORDS):
                if cols["amount"] is None or x > cols["amount"]:
                    cols["amount"] = x
        score = sum(1 for value in cols.values() if value is not None)
        if cols["name"] is not None and cols["quantity"] is not None and cols["amount"] is not None and score >= 3:
            if best is None or score > best[0]:
                best = (score, row["y"], cols)
    return (best[2], best[1]) if best else None


def _item_number(item):
    values = numbers_in_text(item["text"])
    return values[-1] if values else None


def _pick_nearest_number(items, x, left=None, right=None):
    candidates = []
    for item in items:
        item_x = float(item["x"])
        if left is not None and item_x < left:
            continue
        if right is not None and item_x > right:
            continue
        value = _item_number(item)
        if value is not None:
            candidates.append((abs(item_x - x), item_x, value))
    if not candidates:
        return None
    candidates.sort(key=lambda value: (value[0], -value[1]))
    return candidates[0][2]


def _clean_text_item_name(text):
    text = re.sub(r"^\s*\d+\s+", "", str(text or "")).strip()
    return " ".join(text.split())


def assemble_purchase_rows_from_text_items(items):
    grouped_rows = _group_text_items(items)
    header = _text_item_header_columns(grouped_rows)
    if not header:
        return []

    header_cols, header_y = header
    quantity_x = header_cols["quantity"]
    price_x = header_cols["price"] or (quantity_x + 160)
    amount_x = header_cols["amount"]
    name_right = quantity_x - 20

    rows = []
    for group in grouped_rows:
        if group["y"] <= header_y + 12:
            continue
        row_text = " ".join(str(item["text"]) for item in group["items"])
        if any(word in row_text.casefold() for word in ("итого", "всего", "ндс (22")):
            break

        name_parts = [
            _clean_text_item_name(item["text"])
            for item in sorted(group["items"], key=lambda value: float(value["x"]))
            if float(item["x"]) < name_right and _is_item_name_text(_clean_text_item_name(item["text"]))
        ]
        name = " ".join(part for part in name_parts if part).strip()
        if not name:
            continue

        quantity = _pick_nearest_number(
            group["items"],
            quantity_x,
            left=quantity_x - 80,
            right=(quantity_x + price_x) / 2,
        )
        price = _pick_nearest_number(
            group["items"],
            price_x,
            left=(quantity_x + price_x) / 2,
            right=(price_x + amount_x) / 2,
        )
        amount = _pick_nearest_number(
            group["items"],
            amount_x,
            left=amount_x - 120,
        )
        if quantity is None and price and amount:
            quantity = (amount / price).quantize(Decimal("0.001"))
        status, warnings = row_status(quantity, price, amount)
        rows.append({
            "supplier_name": name,
            "quantity": quantity,
            "price": price,
            "amount": amount,
            "status": status,
            "warnings": warnings,
            "source_row": int(round(group["y"])),
            "product_id": None,
            "product_name": "",
            "match_score": 0,
        })
    return rows


def assemble_purchase_rows(cells):
    grouped = _row_cells(cells)
    rows = []
    sorted_rows = sorted(grouped)

    is_stock_code_doc = any(_has_item_code(grouped[row_index]) for row_index in sorted_rows)
    if not is_stock_code_doc:
        header = _detect_header_columns(grouped, sorted_rows)
        if header:
            header_cols, header_row = header
            return _assemble_invoice_rows(grouped, sorted_rows, header_cols, header_row)

    start_rows = []
    for row_index in sorted_rows:
        cells_in_row = grouped[row_index]
        if _has_item_code(cells_in_row) and _pick_name(cells_in_row):
            start_rows.append(row_index)

    for index, row_index in enumerate(start_rows):
        next_start = start_rows[index + 1] if index + 1 < len(start_rows) else None
        end_row = next_start if next_start is not None else row_index + 3
        combined_cells = []
        for source_row in sorted_rows:
            if row_index <= source_row < end_row:
                combined_cells.extend(grouped[source_row])

        name = _pick_name(grouped[row_index])
        if not name:
            continue
        quantity, price, amount, check_amount = _pick_quantity_price_amount(combined_cells)
        status, warnings = row_status(quantity, price, check_amount)
        rows.append({
            "supplier_name": name,
            "quantity": quantity,
            "price": price,
            "amount": amount,
            "status": status,
            "warnings": warnings,
            "source_row": row_index,
            "product_id": None,
            "product_name": "",
            "match_score": 0,
        })
    return rows


def serialize_decimal(value):
    return None if value is None else format(value, "f")


def serialize_rows(rows):
    return [
        {
            **row,
            "quantity": serialize_decimal(row["quantity"]),
            "price": serialize_decimal(row["price"]),
            "amount": serialize_decimal(row["amount"]),
        }
        for row in rows
    ]


def _load_cv_runtime():
    try:
        import cv2
        import easyocr
        import numpy as np
        from PIL import Image
    except Exception as exc:  # noqa: BLE001
        raise OcrUnavailable(
            "Local OCR is not installed. Install opencv-python-headless, easyocr, numpy and pillow."
        ) from exc
    return cv2, easyocr, np, Image


def _bytes_to_bgr(file: UploadedFile):
    cv2, _easyocr, np, Image = _load_cv_runtime()
    from PIL import ImageOps

    raw = file.read()
    image = ImageOps.exif_transpose(Image.open(io.BytesIO(raw))).convert("RGB")
    # Приводим к рабочему разрешению: мелкие фото увеличиваем (иначе OCR не
    # читает тонкий текст), большие — уменьшаем. Цель — длинная сторона ~2400
    # (эмпирически даёт лучшую читаемость таблицы, чем 1800).
    target = 2400
    scale = target / max(image.size)
    scale = min(scale, 3.0)  # не раздуваем сверх меры
    if abs(scale - 1) > 0.05:
        image = image.resize((max(int(image.width * scale), 1), max(int(image.height * scale), 1)))
    return cv2.cvtColor(np.array(image), cv2.COLOR_RGB2BGR)


def _run_table_probe(image, angle=None, crop_top_ratio=0.12):
    from .table_image import detect_cells, detect_orientation
    if angle is None:
        angle = detect_orientation(image)
    return detect_cells(image, angle=angle, crop_top_ratio=crop_top_ratio)


def _ocr_items_from_result(ocr_result):
    items = []
    for box, text, score in ocr_result:
        text = " ".join(str(text).split())
        if not text:
            continue
        xs = [float(point[0]) for point in box]
        ys = [float(point[1]) for point in box]
        items.append({
            "x": min(xs),
            "y": sum(ys) / len(ys),
            "text": text,
            "score": float(score),
        })
    return items


def _score_text_items(items):
    text = "\n".join(item["text"] for item in items).casefold()
    cyrillic_count = len(re.findall(r"[а-яё]", text))
    header_hits = sum(text.count(word) for word in (
        "счет", "поставщик", "покупатель", "товары", "кол", "цен", "сумм"
    ))
    product_hits = sum(text.count(word) for word in (
        "труба", "лист", "круг", "гост", "din", "гайка", "болт", "саморез"
    ))
    numeric_rich = sum(1 for item in items if len(numbers_in_text(item["text"])) >= 2)
    short_noise = sum(1 for item in items if len(item["text"]) <= 2)
    return cyrillic_count + header_hits * 40 + product_hits * 60 + numeric_rich * 8 - short_noise * 10


def _best_text_orientation(image):
    import cv2
    import easyocr
    import numpy as np
    from .table_image import _get_easyocr_reader, _rotate_image

    reader = _get_easyocr_reader(easyocr)
    best = None
    for angle in (0, 90, 180, 270):
        rotated = _rotate_image(cv2, image, angle)
        rgb = cv2.cvtColor(rotated, cv2.COLOR_BGR2RGB)
        items = _ocr_items_from_result(reader.readtext(np.array(rgb), paragraph=False, detail=1))
        option = (angle, rotated, items, _score_text_items(items))
        if best is None or option[3] > best[3]:
            best = option
    return best


def _table_bounds_from_text_items(items, image_shape):
    grouped = _group_text_items(items, y_tolerance=18)
    header = _text_item_header_columns(grouped)
    if not header:
        return None
    _cols, header_y = header
    footer_y = None
    for item in sorted(items, key=lambda value: float(value["y"])):
        if float(item["y"]) <= header_y:
            continue
        if any(word in str(item["text"]).casefold() for word in ("итого", "всего")):
            footer_y = float(item["y"])
            break
    height, width = image_shape[:2]
    top = max(int(header_y) - 28, 0)
    bottom = min(int(footer_y + 12) if footer_y else int(header_y + height * 0.35), height)
    if bottom <= top + 40:
        return None
    return 0, top, width, bottom


def _run_text_table_probe(image):
    import cv2
    import easyocr
    import numpy as np
    from .table_image import _get_easyocr_reader
    from .table_image import _rotate_image

    reader = _get_easyocr_reader(easyocr)
    height, width = image.shape[:2]
    candidate_angles = (0,) if height >= width else (90, 270)
    best = None

    for angle in candidate_angles:
        rotated = _rotate_image(cv2, image, angle)
        h, w = rotated.shape[:2]
        x1, y1, x2, y2 = int(w * 0.02), int(h * 0.36), int(w * 0.99), int(h * 0.60)
        crop = rotated[y1:y2, x1:x2]
        if crop.size == 0:
            continue
        crop = cv2.resize(crop, (crop.shape[1] * 2, crop.shape[0] * 2), interpolation=cv2.INTER_CUBIC)
        rgb = cv2.cvtColor(crop, cv2.COLOR_BGR2RGB)
        ocr_result = reader.readtext(
            np.array(rgb),
            paragraph=False,
            detail=1,
            decoder="beamsearch",
            mag_ratio=1.2,
        )
        items = _ocr_items_from_result(ocr_result)
        rows = assemble_purchase_rows_from_text_items(items)
        option = (len(rows), rows, {
            "angle": angle,
            "text_items": len(items),
            "grid_mode": "text-fallback",
            "crop": [x1, y1, x2, y2],
        })
        if best is None or option[0] > best[0]:
            best = option

    if best is None:
        return [], {"angle": 0, "text_items": 0, "grid_mode": "text-fallback"}
    return best[1], best[2]


def _match_products(rows):
    try:
        from rapidfuzz import fuzz
    except Exception:  # noqa: BLE001
        fuzz = None

    products = list(Product.objects.filter(archived=False, process__buy=True).select_related("process"))
    for row in rows:
        best_product = None
        best_score = 0
        needle = row["supplier_name"]
        for product in products:
            if fuzz:
                score = fuzz.token_set_ratio(needle, product.name)
            else:
                score = 100 if needle.casefold() == product.name.casefold() else 0
            if score > best_score:
                best_product = product
                best_score = score
        if best_product and best_score >= 85:
            row["product_id"] = best_product.id
            row["product_name"] = best_product.name
            row["match_score"] = int(best_score)
    return rows


def recognize_purchase_files(files):
    if not files:
        return {"documents": [], "rows": []}

    all_rows = []
    documents = []
    for file in files:
        image = _bytes_to_bgr(file)
        cells = []
        rows, meta = _run_text_table_probe(image)
        if not rows:
            cells, meta = _run_table_probe(image)
            rows = assemble_purchase_rows(cells)
        all_rows.extend(rows)
        documents.append({
            "name": file.name,
            "angle": meta["angle"],
            "cells": len(cells),
            "rows": len(rows),
            "grid_mode": meta.get("grid_mode", "straight"),
            "curve_vertical_lines": meta.get("curve_vertical_lines", 0),
            "curve_horizontal_lines": meta.get("curve_horizontal_lines", 0),
        })

    _match_products(all_rows)
    return {"documents": documents, "rows": serialize_rows(all_rows)}
