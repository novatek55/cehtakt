from rest_framework import serializers
from django.contrib.auth.models import User

from .models import Area


class AreaWriteSerializer(serializers.ModelSerializer):
    """Создание/редактирование участка."""
    responsibles = serializers.PrimaryKeyRelatedField(
        many=True, queryset=User.objects.all(), required=False)
    executors = serializers.PrimaryKeyRelatedField(
        many=True, queryset=User.objects.all(), required=False)

    class Meta:
        model = Area
        fields = ['id', 'name', 'weekly_limit', 'cost_per_hour',
                  'distribute_jobs', 'job_restriction_by_ingredients', 'sort_index',
                  'responsibles', 'executors']


class AreaListSerializer(serializers.ModelSerializer):
    """Строка «Список участков» — поля под фронт (camelCase)."""
    weeklyLimit = serializers.SerializerMethodField()
    costPerHour = serializers.SerializerMethodField()
    distributeJobs = serializers.BooleanField(source='distribute_jobs')
    jobWithoutIngredients = serializers.BooleanField(source='job_restriction_by_ingredients')
    responsibles = serializers.SerializerMethodField()
    executors = serializers.SerializerMethodField()
    responsibleIds = serializers.SerializerMethodField()
    executorIds = serializers.SerializerMethodField()
    weeklySchedule = serializers.SerializerMethodField()

    class Meta:
        model = Area
        fields = [
            'id', 'name', 'weeklyLimit', 'costPerHour',
            'distributeJobs', 'jobWithoutIngredients',
            'responsibles', 'executors', 'responsibleIds', 'executorIds',
            'weeklySchedule',
        ]

    def get_weeklyLimit(self, obj):
        return obj.weekly_limit or 0

    def get_costPerHour(self, obj):
        return float(obj.cost_per_hour)

    def _users(self, qs):
        return ', '.join(u.get_full_name() or u.username for u in qs.all())

    def get_responsibles(self, obj):
        return self._users(obj.responsibles)

    def get_executors(self, obj):
        return self._users(obj.executors)

    def get_responsibleIds(self, obj):
        return list(obj.responsibles.values_list('id', flat=True))

    def get_executorIds(self, obj):
        return list(obj.executors.values_list('id', flat=True))

    def get_weeklySchedule(self, obj):
        return obj.weekly_schedule if isinstance(obj.weekly_schedule, str) else ''
