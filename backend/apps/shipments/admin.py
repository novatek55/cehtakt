from django import forms
from django.contrib import admin

from .models import Shipment, ShipmentLine


class ShipmentLineInline(admin.TabularInline):
    model = ShipmentLine
    extra = 0


class ShipmentAdminForm(forms.ModelForm):
    class Meta:
        model = Shipment
        fields = '__all__'
        help_texts = {
            'ozon_unaccepted_postings': 'Номера закрытых в системе «Цехтакт» Ozon-отправлений, которые Ozon всё ещё ждёт',
        }


@admin.register(Shipment)
class ShipmentAdmin(admin.ModelAdmin):
    form = ShipmentAdminForm
    list_display = ('name', 'source', 'status', 'created_at', 'shipped_at')
    list_filter = ('source', 'status')
    inlines = [ShipmentLineInline]
