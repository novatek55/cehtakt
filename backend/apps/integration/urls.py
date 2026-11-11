from django.urls import path

from .views import PingView, ProductionOutputView, StockView

urlpatterns = [
    path('ping/', PingView.as_view()),
    path('stock/', StockView.as_view()),
    path('production/output/', ProductionOutputView.as_view()),
]
