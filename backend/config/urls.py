"""
URL configuration for config project.

The `urlpatterns` list routes URLs to views. For more information please see:
    https://docs.djangoproject.com/en/6.0/topics/http/urls/
Examples:
Function views
    1. Add an import:  from my_app import views
    2. Add a URL to urlpatterns:  path('', views.home, name='home')
Class-based views
    1. Add an import:  from other_app.views import Home
    2. Add a URL to urlpatterns:  path('', Home.as_view(), name='home')
Including another URLconf
    1. Import the include() function: from django.urls import include, path
    2. Add a URL to urlpatterns:  path('blog/', include('blog.urls'))
"""
from django.contrib import admin
from django.conf import settings
from django.conf.urls.static import static
from django.urls import include, path
from apps.catalog.filters import FinalAreaOptionsView
from apps.production.output_report import OutputReportView
from apps.production.custom_positions import CustomPositionView
from apps.production.batch_actions import TaskBatchView, TaskAssignView, TaskAssignExecutorsView
from apps.catalog.merge_views import MaterialMergeView, MaterialMergePreviewView, MaterialMergeHistoryView

from apps.catalog.views import (
    import_techcard, ProductListView, ProductTreeView, ProductBulkDeleteView,
    IngredientDetailView, ProcessIngredientCreateView, ProductUsedInView, ProductCopyView,
    ProductAttachmentUploadView, ProductAttachmentFileView,
)
from apps.directory.views import (
    AreaListView, AreaDetailView, CompanyView, UserListView, UserDetailView,
    ReferenceListView, ReferenceDetailView, RoleListView, RoleDetailView,
)
from apps.directory.attendance import PersonalAttendanceView, AttendanceEmployeesView, AttendanceUserLinkView
from apps.directory.auth_views import LoginView, MeView, LogoutView
from apps.stock.views import (
    StockListView, StockCorrectionView, StockCorrectionsListView, StockMovementListView,
    StockCorrectionsBulkDeleteView,
    MarketplaceStockBindView, MarketplaceStockPushView, MarketplaceStockSyncView, MarketplaceStockUpdateView,
)
from apps.production.views import (
    ProductionPlanView, ProductionReportView, TasksKanbanView, JobOperationUpdateView, ProduceView, ProduceBulkView,
    JobMoveView, JobsReorderView, JobsBulkDeleteView, TaskStartView, TaskFinishView, TaskCancelStartView, TaskCancelFinishView,
    TaskGroupView, TaskGroupEditView,
    ProductReservationsView, ProductJobsSyncPreviewView, ProductJobsSyncView,
)
from apps.sales.views import (
    SalesOrderListView, SalesOrderDetailView, SalesOrderBulkView, OrderLinesView, OrderLineDetailView,
    OrderExplodeView, OrderCreateJobsView, OrderJobStatusView, OrderMaterialReservationView, SalesOrderBulkDeleteView,
)
from apps.purchases.views import (
    PurchaseOrderListView, PurchaseOrderDetailView, PurchaseOrderBulkView,
    PurchaseLinesView, PurchaseLineDetailView, PurchaseLineReceiveView,
    PurchaseFromStockView, PurchaseAssortmentView, PurchaseBulkOrderView,
    PurchaseBulkDeleteView, PurchaseDocumentOcrView,
)
from apps.shipments.views import (
    ShipmentListView, ShipmentDetailView, ShipmentLinesView, ShipmentShipView,
    MarketplaceSettingsView, ShipmentImportView, ShipmentLabelsCheckView, ShipmentLabelsView,
    ShipmentAssembleView, ShipmentHandoverView, ShipmentLineBindView, ShipmentPickingListView,
    ShipmentOzonSyncView, ShipmentPickingListOptionsView, ShipmentSupplyView, ShipmentTrbxView,
    ShipmentTrbxStickersView, ShipmentScanView, ShipmentScanFinishView, ShipmentReturnPickupCodeView,
    ShipmentReturnReceiveView,
    OzonWarehouseListView, OzonDeliveryCheckView,
)

urlpatterns = [
    path('admin/', admin.site.urls),
    # Внешний сервисный API — контракт зафиксирован, живёт отдельно от /api/…
    path('api/v1/', include('apps.integration.urls')),
    path('api/auth/login/', LoginView.as_view()),
    path('api/auth/me/', MeView.as_view()),
    path('api/auth/logout/', LogoutView.as_view()),
    path('api/attendance/', PersonalAttendanceView.as_view()),
    path('api/attendance/employees/', AttendanceEmployeesView.as_view()),
    path('api/attendance/users/<int:pk>/', AttendanceUserLinkView.as_view()),
    path('api/import-techcard/', import_techcard),
    path('api/products/', ProductListView.as_view()),
    path('api/products/merge-preview/', MaterialMergePreviewView.as_view()),
    path('api/products/merge/', MaterialMergeView.as_view()),
    path('api/products/merge-history/', MaterialMergeHistoryView.as_view()),
    path('api/products/final-areas/', FinalAreaOptionsView.as_view()),
    path('api/stock/final-areas/', FinalAreaOptionsView.as_view()),
    path('api/products/bulk-delete/', ProductBulkDeleteView.as_view()),
    path('api/products/<int:pk>/copy/', ProductCopyView.as_view()),
    path('api/products/<int:pk>/tree/', ProductTreeView.as_view()),
    path('api/product-attachments/', ProductAttachmentUploadView.as_view()),
    path('api/product-attachments/<int:pk>/file/', ProductAttachmentFileView.as_view(), name='product-attachment-file'),
    path('api/products/<int:pk>/used-in/', ProductUsedInView.as_view()),
    path('api/products/<int:pk>/reservations/', ProductReservationsView.as_view()),
    path('api/products/<int:pk>/jobs-sync-preview/', ProductJobsSyncPreviewView.as_view()),
    path('api/products/<int:pk>/jobs-sync/', ProductJobsSyncView.as_view()),
    path('api/ingredients/<int:pk>/', IngredientDetailView.as_view()),
    path('api/processes/<int:process_id>/ingredients/', ProcessIngredientCreateView.as_view()),
    path('api/areas/', AreaListView.as_view()),
    path('api/areas/<int:pk>/', AreaDetailView.as_view()),
    path('api/company/', CompanyView.as_view()),
    path('api/users/', UserListView.as_view()),
    path('api/users/<int:pk>/', UserDetailView.as_view()),
    path('api/roles/', RoleListView.as_view()),
    path('api/roles/<int:pk>/', RoleDetailView.as_view()),
    path('api/reference/<str:kind>/', ReferenceListView.as_view()),
    path('api/reference/<str:kind>/<int:pk>/', ReferenceDetailView.as_view()),
    path('api/stock/', StockListView.as_view()),
    path('api/stock/corrections/', StockCorrectionsListView.as_view()),
    path('api/stock/corrections/bulk-delete/', StockCorrectionsBulkDeleteView.as_view()),
    path('api/stock/marketplaces/sync/', MarketplaceStockSyncView.as_view()),
    path('api/stock/marketplaces/push/', MarketplaceStockPushView.as_view()),
    path('api/stock/marketplaces/bind/', MarketplaceStockBindView.as_view()),
    path('api/stock/<int:product_id>/marketplace-stock/', MarketplaceStockUpdateView.as_view()),
    path('api/stock/<int:product_id>/movements/', StockMovementListView.as_view()),
    path('api/stock/<int:product_id>/correction/', StockCorrectionView.as_view()),
    path('api/production/', ProductionPlanView.as_view()),
    path('api/production/report/', ProductionReportView.as_view()),
    path('api/reports/output/', OutputReportView.as_view()),
    path('api/production/jobs/reorder/', JobsReorderView.as_view()),
    path('api/production/jobs/bulk-delete/', JobsBulkDeleteView.as_view()),
    path('api/production/jobs/<int:job_id>/move/', JobMoveView.as_view()),
    path('api/tasks/', TasksKanbanView.as_view()),
    path('api/tasks/batch/', TaskBatchView.as_view()),
    path('api/tasks/assign/', TaskAssignView.as_view()),
    path('api/tasks/assign/executors/', TaskAssignExecutorsView.as_view()),
    path('api/tasks/group/', TaskGroupView.as_view()),
    path('api/tasks/group/edit/', TaskGroupEditView.as_view()),
    path('api/tasks/<int:pk>/start/', TaskStartView.as_view()),
    path('api/tasks/<int:pk>/finish/', TaskFinishView.as_view()),
    path('api/tasks/<int:pk>/cancel-start/', TaskCancelStartView.as_view()),
    path('api/tasks/<int:pk>/cancel-finish/', TaskCancelFinishView.as_view()),
    path('api/tasks/<int:pk>/', JobOperationUpdateView.as_view()),
    path('api/produce/', ProduceView.as_view()),
    path('api/produce/bulk/', ProduceBulkView.as_view()),
    path('api/sales/custom-position/', CustomPositionView.as_view()),
    path('api/sales/', SalesOrderListView.as_view()),
    path('api/sales/bulk/', SalesOrderBulkView.as_view()),
    path('api/sales/bulk-delete/', SalesOrderBulkDeleteView.as_view()),
    path('api/sales/lines/<int:pk>/', OrderLineDetailView.as_view()),
    path('api/sales/<int:order_id>/lines/', OrderLinesView.as_view()),
    path('api/sales/<int:order_id>/explode/', OrderExplodeView.as_view()),
    path('api/sales/<int:order_id>/create-jobs/', OrderCreateJobsView.as_view()),
    path('api/sales/<int:order_id>/job-status/', OrderJobStatusView.as_view()),
    path('api/sales/<int:order_id>/material-reservation/', OrderMaterialReservationView.as_view()),
    path('api/sales/<int:pk>/', SalesOrderDetailView.as_view()),
    path('api/shipments/', ShipmentListView.as_view()),
    path('api/shipments/settings/', MarketplaceSettingsView.as_view()),
    path('api/shipments/settings/ozon-warehouses/', OzonWarehouseListView.as_view()),
    path('api/shipments/import/', ShipmentImportView.as_view()),
    path('api/shipments/ozon-delivery-check/', OzonDeliveryCheckView.as_view()),
    path('api/shipments/<int:pk>/labels/check/', ShipmentLabelsCheckView.as_view()),
    path('api/shipments/<int:pk>/labels/', ShipmentLabelsView.as_view()),
    path('api/shipments/<int:pk>/picking-list/options/', ShipmentPickingListOptionsView.as_view()),
    path('api/shipments/<int:pk>/picking-list/', ShipmentPickingListView.as_view()),
    path('api/shipments/<int:pk>/sync/', ShipmentOzonSyncView.as_view()),
    path('api/shipments/<int:pk>/return-pickup-code/', ShipmentReturnPickupCodeView.as_view()),
    path('api/shipments/<int:pk>/return-receive/', ShipmentReturnReceiveView.as_view()),
    path('api/shipments/<int:pk>/assemble/', ShipmentAssembleView.as_view()),
    path('api/shipments/<int:pk>/handover/', ShipmentHandoverView.as_view()),
    path('api/shipments/<int:pk>/supply/', ShipmentSupplyView.as_view()),
    path('api/shipments/<int:pk>/trbx/', ShipmentTrbxView.as_view()),
    path('api/shipments/<int:pk>/trbx/stickers/', ShipmentTrbxStickersView.as_view()),
    path('api/shipments/<int:pk>/scan/finish/', ShipmentScanFinishView.as_view()),
    path('api/shipments/<int:pk>/scan/', ShipmentScanView.as_view()),
    path('api/shipments/<int:pk>/lines/<int:line_id>/bind/', ShipmentLineBindView.as_view()),
    path('api/shipments/<int:pk>/lines/', ShipmentLinesView.as_view()),
    path('api/shipments/<int:pk>/ship/', ShipmentShipView.as_view()),
    path('api/shipments/<int:pk>/', ShipmentDetailView.as_view()),
    path('api/purchases/', PurchaseOrderListView.as_view()),
    path('api/purchases/bulk/', PurchaseOrderBulkView.as_view()),
    path('api/purchases/ocr/', PurchaseDocumentOcrView.as_view()),
    path('api/purchases/from-stock/', PurchaseFromStockView.as_view()),
    path('api/purchases/assortment/', PurchaseAssortmentView.as_view()),
    path('api/purchases/bulk-order/', PurchaseBulkOrderView.as_view()),
    path('api/purchases/bulk-delete/', PurchaseBulkDeleteView.as_view()),
    path('api/purchases/lines/<int:pk>/receive/', PurchaseLineReceiveView.as_view()),
    path('api/purchases/lines/<int:pk>/', PurchaseLineDetailView.as_view()),
    path('api/purchases/<int:order_id>/lines/', PurchaseLinesView.as_view()),
    path('api/purchases/<int:pk>/', PurchaseOrderDetailView.as_view()),
]

if settings.DEBUG:
    urlpatterns += static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT)
