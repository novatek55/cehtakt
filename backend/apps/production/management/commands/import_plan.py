"""Импорт производственного Плана из proplan (запрос jobsForTasksPage).

orderLines → ProductionJob (задание), jobs → JobOperation (операция на участке).
job.name = имя участка (пила/лазер/…). Идемпотентно (полный replace плана).

Запуск (креды из env, пароль из файла):
  python manage.py import_plan
"""
import os

from django.core.management.base import BaseCommand, CommandError

from apps.catalog.management.commands.import_from_proplan import _gql, LOGIN_MUT
from apps.catalog.models import Product
from apps.directory.models import Area
from apps.production.models import ProductionJob, JobOperation

PLAN_Q = '''query Plan($filter: JobFilter, $barcode: String) {
  jobsForTasksPage(filter: $filter, barcode: $barcode) {
    orderLines { id name quantity finishByDate productId batch description }
    products { id name sku processId }
    jobs { id name state quantity orderLineId }
  }
}'''

_STATE = {
    'AVAILABLE': ProductionJob.Status.IN_PROGRESS,
    'DONE': ProductionJob.Status.DONE,
    'WAITING': ProductionJob.Status.WAITING,
}


class Command(BaseCommand):
    help = 'Импортирует производственный План из proplan'

    def handle(self, *args, **opts):
        login = os.environ.get('PROPLAN_LOGIN', '')
        password = os.environ.get('PROPLAN_PASSWORD', '')
        if not login or not password:
            raise CommandError('Задай env PROPLAN_LOGIN и PROPLAN_PASSWORD.')
        token = _gql(LOGIN_MUT, {'login': login, 'password': password})['login']['accessToken']

        data = _gql(PLAN_Q, {'filter': {}, 'barcode': None}, token)['jobsForTasksPage']
        order_lines, jobs, products = data['orderLines'], data['jobs'], data['products']
        prod_by_id = {p['id']: p for p in products}

        # полный replace плана (идемпотентно)
        JobOperation.objects.all().delete()
        ProductionJob.objects.all().delete()
        area_cache = {}
        by_ol = {}
        skipped = 0
        for ol in order_lines:
            info = prod_by_id.get(ol.get('productId')) or {}
            product = Product.objects.filter(name=info.get('name')).first() if info.get('name') else None
            if not product:
                skipped += 1
                continue
            job = ProductionJob.objects.create(
                name=ol.get('name') or '',
                batch=ol.get('batch') or '',
                product=product,
                process=product.process,
                quantity=ol.get('quantity') or 0,
                description=ol.get('description') or '',
                status=ProductionJob.Status.IN_PROGRESS,
            )
            by_ol[ol['id']] = job

        n_ops = 0
        for j in jobs:
            job = by_ol.get(j.get('orderLineId'))
            if not job:
                continue
            aname = (j.get('name') or '').strip()
            if not aname:
                continue
            if aname not in area_cache:
                area_cache[aname] = Area.objects.get_or_create(name=aname)[0]
            JobOperation.objects.create(
                job=job, area=area_cache[aname], name=aname,
                quantity=j.get('quantity') or 0,
                status=JobOperation.Status.IN_PROGRESS,
            )
            n_ops += 1

        self.stdout.write(self.style.SUCCESS(
            f'План импортирован: заданий {ProductionJob.objects.count()} '
            f'(пропущено без продукта {skipped}), операций {n_ops}'))
