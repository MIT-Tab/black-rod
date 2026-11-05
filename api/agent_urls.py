from django.urls import path

from .agent_views import (
    AgentBatchesView,
    AgentDiscoveryView,
    AgentGuideView,
    AgentRecordsView,
    AgentUndoView,
)

urlpatterns = [
    path("", AgentDiscoveryView.as_view(), name="agent_discovery"),
    path("guide/", AgentGuideView.as_view(), name="agent_guide"),
    path("records/<str:resource>/", AgentRecordsView.as_view(), name="agent_records"),
    path(
        "records/<str:resource>/<int:pk>/",
        AgentRecordsView.as_view(),
        name="agent_record",
    ),
    path("batches/", AgentBatchesView.as_view(), name="agent_batches"),
    path("batches/<int:pk>/", AgentBatchesView.as_view(), name="agent_batch"),
    path("batches/<int:pk>/undo/", AgentUndoView.as_view(), name="agent_undo"),
]
