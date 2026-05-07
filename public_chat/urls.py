from django.urls import path

from .views import PublicChatStreamView, PublicChatView

urlpatterns = [
    path("public/", PublicChatView.as_view(), name="public-chat"),
    path("public/stream/", PublicChatStreamView.as_view(), name="public-chat-stream"),
]
