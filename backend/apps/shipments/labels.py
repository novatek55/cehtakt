"""Этикетки отправлений: получение у маркетплейсов и сборка в один PDF.

Ozon формирует PDF по заданию. Готовые файлы скачиваем и при необходимости объединяем.
WB отдаёт стикеры по одному картинками, их сшиваем в PDF через fpdf2.
"""
import base64
import io
import re
import subprocess
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path
from urllib.parse import urlsplit

from fpdf import FPDF
from PIL import Image

from .marketplaces import MarketplaceError, _request

OZON_LABEL_CREATE_URL = 'https://api-seller.ozon.ru/v3/posting/fbs/package-label/create'
OZON_LABEL_GET_URL = 'https://api-seller.ozon.ru/v2/posting/fbs/package-label/get'
WB_STICKERS_URL = 'https://marketplace-api.wildberries.ru/api/v3/orders/stickers?type=png&width=58&height=40'

# Размер стикера WB в миллиметрах — совпадает с запрошенным у API.
STICKER_W_MM = 58
STICKER_H_MM = 40
OZON_LABEL_BATCH_SIZE = 20
OZON_LABEL_WAIT_SECONDS = 30


def fetch_ozon_label_codes(client_id, api_key, posting_numbers):
    """Связь номера отправления с полной надписью/QR на новой этикетке Ozon."""
    codes = {}
    headers = {'Client-Id': client_id, 'Api-Key': api_key}
    for number in dict.fromkeys(_text(value) for value in posting_numbers):
        if not number:
            raise MarketplaceError('В строке подбора отсутствует номер отправления Ozon')
        body = _request(
            'https://api-seller.ozon.ru/v3/posting/fbs/get', headers,
            {'posting_number': number},
        )
        posting = body.get('result') if isinstance(body, dict) else None
        if not isinstance(posting, dict) or _text(posting.get('posting_number')) != number:
            raise MarketplaceError('Ozon вернул неверное отправление для этикетки')
        code = _text(posting.get('scanit'))
        if not code:
            raise MarketplaceError(
                'Ozon ещё не выдал надпись на этикетке. Соберите отправления '
                'и повторите скачивание листа подбора.')
        codes[number] = code
    return codes


def _font_path():
    candidates = [
        Path('C:/Windows/Fonts/arial.ttf'),
        Path('/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf'),
        Path('/usr/share/fonts/dejavu-sans-fonts/DejaVuSans.ttf'),
    ]
    return next((p for p in candidates if p.exists()), None)


def _text(value):
    return str(value or '').replace('\r', ' ').replace('\n', ' ').strip()


def _label_code(posting_number, assembly_code=''):
    code = _text(assembly_code)
    if code:
        return code
    m = re.match(r'^(\d+)-', _text(posting_number))
    return m.group(1)[-4:] if m else ''


def _wb_visible_label_code(value):
    code = _text(value)
    if '/' in code:
        return _text(code.rsplit('/', 1)[-1])
    return code


def _posting_highlight_parts(posting_number):
    value = _text(posting_number)
    m = re.match(r'^(\d{4,})(-.*)$', value)
    if not m:
        return value, '', ''
    number, suffix = m.groups()
    return number[:-4], number[-4:], suffix


def _image_from_url(url):
    url = _text(url)
    if not url:
        return None
    try:
        with urllib.request.urlopen(url, timeout=5) as resp:
            data = resp.read(2_000_000)
        img = Image.open(io.BytesIO(data))
        img.load()
        return img
    except (urllib.error.URLError, OSError, ValueError):
        return None


def _wait_ozon_label(headers, task_id, deadline):
    while time.monotonic() < deadline:
        body = _request(OZON_LABEL_GET_URL, headers, {'task_id': task_id}) or {}
        status = body.get('status') or {}
        code = status.get('code')
        if body.get('error'):
            raise MarketplaceError('Ozon сообщил об ошибке формирования этикеток.')
        if code == 'completed':
            return body
        if code not in ('pending', 'in_progress'):
            raise MarketplaceError('Ozon не смог сформировать этикетки.')
        time.sleep(1)
    raise MarketplaceError('Ozon ещё готовит этикетки. Повторите скачивание чуть позже.')


def _ozon_label_files(headers, numbers, deadline):
    body = _request(OZON_LABEL_CREATE_URL, headers, {'posting_numbers': numbers}) or {}
    tasks = body.get('tasks') or []
    if not tasks or any(not task.get('task_id') for task in tasks):
        raise MarketplaceError('Ozon не вернул задание на формирование этикеток.')

    files = []
    printed = 0
    unprinted = False
    for task in tasks:
        result = _wait_ozon_label(headers, task['task_id'], deadline)
        status = result['status']
        count = status.get('printed_postings_count', 0)
        printed += count
        unprinted = unprinted or bool(status.get('unprinted_postings'))
        if count:
            files.append(result.get('file_url'))

    if unprinted or printed != len(numbers):
        raise MarketplaceError(
            f'Ozon сформировал {printed} из {len(numbers)} этикеток. '
            'Повторите скачивание чуть позже.')
    return files


def _download_ozon_label(url):
    file_url = urlsplit(url or '')
    host = file_url.hostname or ''
    if file_url.scheme != 'https' or not host.endswith(('.ozone.ru', '.ozon.ru')):
        raise MarketplaceError('Ozon не вернул корректную ссылку на PDF этикеток.')
    try:
        with urllib.request.urlopen(url, timeout=10) as response:
            data = response.read()
    except (OSError, ValueError) as error:
        raise MarketplaceError('Не удалось скачать PDF этикеток Ozon.') from error
    if not data.startswith(b'%PDF'):
        raise MarketplaceError('Ozon вернул файл этикеток в неверном формате.')
    return data


def fetch_ozon_label_pdf(client_id, api_key, posting_numbers):
    """PDF с этикетками указанных отправлений Ozon. Возвращает байты."""
    if not posting_numbers:
        raise MarketplaceError('нет отправлений с готовыми этикетками')
    normalized_numbers = map(_text, posting_numbers)
    numbers = list(dict.fromkeys(number for number in normalized_numbers if number))
    if not numbers:
        raise MarketplaceError('нет корректных номеров отправлений для этикеток')
    headers = {'Client-Id': client_id, 'Api-Key': api_key}
    deadline = time.monotonic() + OZON_LABEL_WAIT_SECONDS
    files = []
    for offset in range(0, len(numbers), OZON_LABEL_BATCH_SIZE):
        batch = numbers[offset:offset + OZON_LABEL_BATCH_SIZE]
        files.extend(_ozon_label_files(headers, batch, deadline))

    if len(files) == 1:
        return _download_ozon_label(files[0])

    with tempfile.TemporaryDirectory(prefix='proplan-ozon-labels-') as temp_dir:
        pdf_paths = []
        for index, url in enumerate(files, start=1):
            path = Path(temp_dir) / f'labels-{index}.pdf'
            path.write_bytes(_download_ozon_label(url))
            pdf_paths.append(str(path))
        output_path = str(Path(temp_dir) / 'labels-combined.pdf')
        try:
            subprocess.run(
                ['gs', '-q', '-dBATCH', '-dNOPAUSE', '-sDEVICE=pdfwrite',
                 f'-sOutputFile={output_path}', *pdf_paths],
                check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=60)
            combined = Path(output_path).read_bytes()
        except (OSError, subprocess.SubprocessError) as error:
            raise MarketplaceError(f'Не удалось объединить партии PDF этикеток: {error}') from error
        if not combined.startswith(b'%PDF'):
            raise MarketplaceError('Ozon: результат объединения партий не является PDF')
        return combined


def fetch_wb_stickers(token, order_ids):
    """Стикеры WB: {order_id: png_bytes}. Отсутствующие и битые пропускаются."""
    if not order_ids:
        raise MarketplaceError('нет заданий для стикеров')
    body = _request(WB_STICKERS_URL, {'Authorization': token},
                    {'orders': [int(o) for o in order_ids if str(o).isdigit()]})
    out = {}
    for s in (body or {}).get('stickers') or []:
        raw = s.get('file') or ''
        try:
            out[str(s.get('orderId'))] = base64.b64decode(raw, validate=True)
        except Exception:
            # Битый стикер не должен ронять всю пачку — просто не попадёт в PDF.
            continue
    return out


def fetch_wb_stickers_with_barcodes(token, order_ids):
    if not order_ids:
        raise MarketplaceError('нет заданий для стикеров')
    body = _request(WB_STICKERS_URL, {'Authorization': token},
                    {'orders': [int(o) for o in order_ids if str(o).isdigit()]})
    stickers, barcodes, label_codes = {}, {}, {}
    for s in (body or {}).get('stickers') or []:
        order_id = str(s.get('orderId') or '')
        raw = s.get('file') or ''
        try:
            stickers[order_id] = base64.b64decode(raw, validate=True)
        except Exception:
            continue
        barcode = str(s.get('barcode') or '').strip()
        if order_id and barcode:
            barcodes[order_id] = barcode
        part_b = _text(s.get('partB'))
        label_code = part_b
        if order_id and label_code:
            label_codes[order_id] = label_code
    return stickers, barcodes, label_codes


def fetch_wb_trbx_stickers(token, supply_id, trbx_ids):
    """QR-стикеры грузомест: {trbx_id: png_bytes}. Битые пропускаются.

    Грузоместа не привязаны к товару, поэтому печатаются одним файлом —
    сборку делает build_wb_pdf (страница на стикер)."""
    if not trbx_ids:
        raise MarketplaceError('нет грузомест для стикеров')
    url = (f'https://marketplace-api.wildberries.ru/api/v3/supplies/'
           f'{supply_id}/trbx/stickers?type=png')
    body = _request(url, {'Authorization': token}, {'trbxIds': list(trbx_ids)})
    out = {}
    requested = list(trbx_ids)
    for i, s in enumerate((body or {}).get('stickers') or []):
        raw = s.get('file') or ''
        try:
            key = str(s.get('barcode') or s.get('trbxId') or (
                requested[i] if i < len(requested) else i))
            out[key] = base64.b64decode(raw, validate=True)
        except Exception:
            continue
    return out


def build_wb_pdf(stickers):
    """Собрать PDF: одна страница на стикер, размер страницы под стикер.

    Валидный base64, который не является картинкой, проходит fetch_wb_stickers
    и падает только здесь, на вставке в PDF — поэтому каждую вставку страхуем
    отдельно: битый стикер пропускаем и идём дальше, а не роняем всю пачку.
    """
    if not stickers:
        raise MarketplaceError('нет стикеров для сборки PDF')
    pdf = FPDF(unit='mm', format=(STICKER_W_MM, STICKER_H_MM))
    pdf.set_auto_page_break(False)
    inserted = 0
    for _, png in sorted(stickers.items()):
        # Валидируем ДО add_page: fpdf2 не умеет удалять уже добавленную
        # страницу, поэтому страницу заводим только под подтверждённо
        # читаемую картинку — иначе битый стикер оставит пустой лист в PDF.
        try:
            img = Image.open(io.BytesIO(png))
            img.load()
        except Exception:
            continue
        pdf.add_page()
        pdf.image(img, x=0, y=0, w=STICKER_W_MM, h=STICKER_H_MM)
        inserted += 1
    if inserted == 0:
        raise MarketplaceError('не удалось собрать ни одной этикетки')
    return bytes(pdf.output())


def build_picking_list_pdf(shipment, lines):
    """PDF-лист подбора: одна строка на активную позицию отгрузки."""
    if not lines:
        raise MarketplaceError('нет строк для листа подбора')

    pdf = FPDF(unit='mm', format='A4')
    pdf.set_auto_page_break(auto=True, margin=12)
    font = _font_path()
    if font:
        pdf.add_font('PickingFont', '', str(font))
        pdf.add_font('PickingFont', 'B', str(font))
        family = 'PickingFont'
    else:
        family = 'Helvetica'

    pdf.add_page()
    pdf.set_font(family, 'B', 16)
    pdf.cell(0, 9, 'Лист подбора отгрузки', new_x='LMARGIN', new_y='NEXT')
    pdf.set_font(family, '', 10)
    pdf.cell(0, 6, f'Отгрузка: {_text(shipment.name)}', new_x='LMARGIN', new_y='NEXT')
    source = {'manual': 'Вручную', 'ozon': 'Ozon', 'wb': 'Wildberries'}.get(shipment.source, shipment.source)
    pdf.cell(0, 6, f'Источник: {source}', new_x='LMARGIN', new_y='NEXT')
    pdf.cell(0, 6, f'Позиций: {len(lines)}', new_x='LMARGIN', new_y='NEXT')
    pdf.ln(3)

    widths = [11, 31, 30, 36, 66, 14]
    headers = ['#', 'Отправление', 'Склад', 'Артикул', 'Товар', 'Кол-во']
    pdf.set_font(family, 'B', 8)
    for width, header in zip(widths, headers):
        pdf.cell(width, 7, header, border=1)
    pdf.ln()

    pdf.set_font(family, '', 8)
    for idx, line in enumerate(lines, start=1):
        product_name = line.product.name if line.product_id else line.name_raw
        sku = line.product.sku if line.product_id else line.sku_raw
        row = [
            str(idx),
            _text(line.external_posting) or '-',
            _text(line.external_warehouse) or '-',
            _text(sku) or '-',
            _text(product_name) or '-',
            format(line.quantity.normalize(), 'f'),
        ]
        y = pdf.get_y()
        if y > 270:
            pdf.add_page()
            y = pdf.get_y()
        x = pdf.get_x()
        line_h = 6
        for width, value in zip(widths, row):
            pdf.multi_cell(width, line_h, value[:90], border=1, new_x='RIGHT', new_y='TOP',
                           max_line_height=line_h)
        pdf.set_xy(x, y + line_h)

    return bytes(pdf.output())


def build_wb_picking_list_pdf(shipment, lines):
    """PDF-лист подбора WB: товар и грузоместо/штрих-код для наклейки."""
    if not lines:
        raise MarketplaceError('нет строк для листа подбора')

    pdf = FPDF(orientation='L', unit='mm', format='A4')
    pdf.set_auto_page_break(auto=True, margin=10)
    font = _font_path()
    if font:
        pdf.add_font('PickingFont', '', str(font))
        pdf.add_font('PickingFont', 'B', str(font))
        family = 'PickingFont'
    else:
        family = 'Helvetica'

    pdf.add_page()
    pdf.set_font(family, 'B', 18)
    pdf.cell(0, 9, 'Лист подбора Wildberries', new_x='LMARGIN', new_y='NEXT')
    pdf.set_font(family, '', 10)
    pdf.cell(0, 6, f'Поставка: {_text(shipment.name)}', new_x='LMARGIN', new_y='NEXT')
    if shipment.external_supply_id:
        pdf.cell(0, 6, f'Номер поставки WB: {_text(shipment.external_supply_id)}', new_x='LMARGIN', new_y='NEXT')
    pdf.cell(0, 6, f'Позиций: {len(lines)}', new_x='LMARGIN', new_y='NEXT')
    pdf.ln(4)

    widths = [34, 38, 40, 18, 96, 58]
    headers = ['Задание WB', 'Код этикетки', 'Артикул', 'Кол-во', 'Товар', 'Грузоместо']
    pdf.set_fill_color(245, 247, 250)
    pdf.set_draw_color(225, 230, 236)
    pdf.set_text_color(87, 105, 125)
    pdf.set_font(family, 'B', 8)
    for width, header in zip(widths, headers):
        pdf.cell(width, 8, header, border='B', fill=True)
    pdf.ln()

    pdf.set_text_color(0, 0, 0)
    pdf.set_font(family, '', 8)
    for line in lines:
        y = pdf.get_y()
        if y > 190:
            pdf.add_page()
            pdf.set_text_color(87, 105, 125)
            pdf.set_font(family, 'B', 8)
            for width, header in zip(widths, headers):
                pdf.cell(width, 8, header, border='B', fill=True)
            pdf.ln()
            pdf.set_text_color(0, 0, 0)
            pdf.set_font(family, '', 8)
            y = pdf.get_y()

        product_name = line.product.name if line.product_id else line.name_raw
        sku = line.product.sku if line.product_id else line.sku_raw
        quantity = (
            format(line.quantity.normalize(), 'f')
            if hasattr(line.quantity, 'normalize')
            else str(line.quantity)
        )
        trbx_id = _text(line.external_trbx_id) or '-'
        label_code = (
            _wb_visible_label_code(getattr(line, 'external_label_code', ''))
            or _text(line.external_label_barcode)
            or '-'
        )
        row = [
            _text(line.external_posting) or '-',
            label_code[:45],
            (_text(sku) or '-')[:45],
            quantity,
            (_text(product_name) or '-')[:100],
            trbx_id[:64],
        ]
        row_h = 8
        for width, value in zip(widths, row):
            pdf.cell(width, row_h, value, border='B', align='C' if width == widths[2] else 'L')
        pdf.ln(row_h)

    return bytes(pdf.output())


def build_ozon_assembly_picking_list_pdf(title, postings):
    """PDF-лист подбора Ozon: надпись на этикетке и товар для её наклеивания."""
    if not postings:
        raise MarketplaceError('Ozon не вернул строки для листа подбора')
    if any(not _text(posting.get('label_code')) for posting in postings):
        raise MarketplaceError('Не получена надпись на этикетке Ozon для листа подбора')

    pdf = FPDF(orientation='L', unit='mm', format='A4')
    pdf.set_auto_page_break(auto=True, margin=10)
    font = _font_path()
    if font:
        pdf.add_font('PickingFont', '', str(font))
        pdf.add_font('PickingFont', 'B', str(font))
        family = 'PickingFont'
    else:
        family = 'Helvetica'

    pdf.add_page()
    pdf.set_font(family, 'B', 18)
    pdf.cell(0, 9, 'Лист подбора Ozon', new_x='LMARGIN', new_y='NEXT')
    pdf.set_font(family, '', 10)
    pdf.cell(0, 6, _text(title), new_x='LMARGIN', new_y='NEXT')
    pdf.ln(4)

    widths = [68, 54, 25, 130]
    headers = ['Надпись на этикетке', 'Артикул', 'Кол-во', 'Товар']
    pdf.set_fill_color(245, 247, 250)
    pdf.set_draw_color(225, 230, 236)
    pdf.set_text_color(87, 105, 125)
    pdf.set_font(family, 'B', 9)
    for width, header in zip(widths, headers):
        pdf.cell(width, 9, header, border='B', fill=True)
    pdf.ln()

    pdf.set_text_color(0, 0, 0)
    pdf.set_font(family, '', 9)
    for posting in postings:
        label_code = _text(posting.get('label_code'))
        for product in posting.get('products') or []:
            y = pdf.get_y()
            if y > 190:
                pdf.add_page()
                pdf.set_text_color(87, 105, 125)
                pdf.set_font(family, 'B', 9)
                for width, header in zip(widths, headers):
                    pdf.cell(width, 9, header, border='B', fill=True)
                pdf.ln()
                pdf.set_text_color(0, 0, 0)
                pdf.set_font(family, '', 9)
                y = pdf.get_y()
            x = pdf.get_x()
            row_h = 10
            _draw_highlighted_label_cell(pdf, family, widths[0], row_h, label_code)
            values = [
                (_text(product.get('offer_id')) or '-')[:55],
                str(product.get('quantity') or 0),
                (_text(product.get('product_name')) or '-')[:120],
            ]
            for width, value in zip(widths[1:], values):
                pdf.cell(width, row_h, value, border='B', align='C' if width == widths[2] else 'L')
            pdf.ln(row_h)

    return bytes(pdf.output())


def _draw_highlighted_label_cell(pdf, family, width, row_h, label_code):
    x, y = pdf.get_x(), pdf.get_y()
    pdf.cell(width, row_h, '', border='B')
    prefix, code = label_code[:-4], label_code[-4:]
    pad = 1.4
    pdf.set_xy(x + pad, y)
    pdf.set_text_color(0, 0, 0)
    pdf.set_font(family, '', 11)
    prefix_w = pdf.get_string_width(prefix) + 1.4
    pdf.cell(prefix_w, row_h, prefix)
    pdf.set_font(family, 'B', 12)
    code_w = max(pdf.get_string_width(code) + 3, 13)
    pdf.set_fill_color(0, 0, 0)
    pdf.set_text_color(255, 255, 255)
    badge_h = min(row_h, 7)
    pdf.set_xy(x + pad + prefix_w, y + (row_h - badge_h) / 2)
    pdf.cell(code_w, badge_h, code, fill=True, align='C')
    pdf.set_text_color(0, 0, 0)
    pdf.set_font(family, '', 9)
    pdf.set_xy(x + width, y)


def _draw_highlighted_posting_cell(pdf, family, width, row_h, posting_number):
    x = pdf.get_x()
    y = pdf.get_y()
    pdf.cell(width, row_h, '', border='B')

    prefix, code, suffix = _posting_highlight_parts(posting_number)
    if not code:
        pdf.set_xy(x, y)
        pdf.cell(width, row_h, posting_number, align='L')
        pdf.set_xy(x + width, y)
        return

    pad = 1.4
    pdf.set_xy(x + pad, y)
    pdf.set_text_color(0, 0, 0)
    pdf.set_font(family, '', 9)
    prefix_w = pdf.get_string_width(prefix)
    pdf.cell(prefix_w, row_h, prefix)

    code_w = max(pdf.get_string_width(code) + 2.4, 11)
    pdf.set_fill_color(0, 0, 0)
    pdf.set_text_color(255, 255, 255)
    pdf.set_font(family, 'B', 9)
    pdf.cell(code_w, row_h, code, fill=True, align='C')

    pdf.set_text_color(0, 0, 0)
    pdf.set_font(family, '', 9)
    pdf.cell(width - pad - prefix_w - code_w, row_h, suffix)
    pdf.set_xy(x + width, y)
