from django.core.management.base import BaseCommand

from apps.shipments.services import run_pending_wb_returns_retry


class Command(BaseCommand):
    help = 'Выполняет наступившую отложенную загрузку возвратов Wildberries.'

    def handle(self, *args, **options):
        result = run_pending_wb_returns_retry()
        self.stdout.write(result['status'])
