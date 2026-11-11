"""Пересчёт StockItem при любом изменении реестра резерва (создание/удаление, вкл. каскад)."""
from django.db.models.signals import post_save, post_delete
from django.dispatch import receiver

from .models import Reservation
from .services import recalc_stock


@receiver(post_save, sender=Reservation)
def _reservation_saved(sender, instance, **kwargs):
    recalc_stock(instance.product_id)


@receiver(post_delete, sender=Reservation)
def _reservation_deleted(sender, instance, **kwargs):
    recalc_stock(instance.product_id)


from django.db.models.signals import pre_delete
from .models import ProductionJob


@receiver(pre_delete, sender=ProductionJob)
def _protect_production_deletion(sender, instance, **kwargs):
    from .deletion import protect_uncompensated_delete
    protect_uncompensated_delete(instance)
