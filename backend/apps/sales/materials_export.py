"""Excel export of the materials calculation using the selected order/production mode."""
from io import BytesIO

from django.http import HttpResponse
from openpyxl import Workbook
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.formatting.rule import CellIsRule
from openpyxl.utils import get_column_letter


def materials_excel(order, calculation, mode, product=None, quantity=None):
    workbook = Workbook()
    sheet = workbook.active
    sheet.title = 'Материалы'

    def text(row, col, value):
        cell = sheet.cell(row, col, str(value or ''))
        cell.data_type = 's'
        return cell

    sheet.merge_cells('A1:F1')
    text(1, 1, f'Расчёт материалов — {order.name}').font = Font(size=16, bold=True, color='174A7E')
    sheet.row_dimensions[1].height = 28
    sheet.merge_cells('A2:F2')
    text(2, 1, 'Режим: ' + ('С 0' if mode == 'from_zero' else 'С учётом остатков'))
    sheet.merge_cells('A3:F3')
    text(3, 1, f'ПФ: {product.name} · количество: {quantity}' if product else 'Расчёт по всем позициям заказа')
    headers = ['Материал', 'Артикул', 'На складе', 'Резерв', 'Нужно', 'Дефицит']
    for col, title in enumerate(headers, 1):
        cell = text(5, col, title)
        cell.fill = PatternFill('solid', fgColor='174A7E')
        cell.font = Font(bold=True, color='FFFFFF')
    for row, material in enumerate(calculation['materials_summary'], 6):
        text(row, 1, material['name'])
        text(row, 2, material.get('sku', '')).number_format = '@'
        for col, key in enumerate(('available', 'committed', 'need', 'deficit'), 3):
            cell = sheet.cell(row, col, material[key])
            cell.number_format = '#,##0.####'
        for cell in sheet[row]:
            cell.alignment = Alignment(vertical='top', wrap_text=True)
            if row % 2 == 0:
                cell.fill = PatternFill('solid', fgColor='F1F6FB')
    for col, width in enumerate((56, 24, 18, 18, 18, 18), 1):
        sheet.column_dimensions[get_column_letter(col)].width = width
    sheet.freeze_panes = 'C6'
    sheet.auto_filter.ref = f'A5:F{max(sheet.max_row, 5)}'
    if sheet.max_row >= 6:
        sheet.conditional_formatting.add(f'F6:F{sheet.max_row}', CellIsRule(
            operator='greaterThan', formula=['0'], font=Font(color='B91C1C', bold=True)))
    sheet.sheet_view.showGridLines = False
    sheet.print_title_rows = '1:5'
    sheet.sheet_properties.pageSetUpPr.fitToPage = True
    sheet.page_setup.orientation = 'landscape'
    sheet.page_setup.paperSize = sheet.PAPERSIZE_A4
    sheet.page_setup.fitToWidth = 1
    sheet.page_setup.fitToHeight = 0
    output = BytesIO()
    workbook.save(output)
    response = HttpResponse(output.getvalue(), content_type='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet')
    response['Content-Disposition'] = f'attachment; filename="materials-order-{order.pk}.xlsx"'
    return response
