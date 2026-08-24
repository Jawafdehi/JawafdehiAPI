from django.urls import path

from case_tags.views import VocabularyView

urlpatterns = [
    path("case-tags/", VocabularyView.as_view(), name="case-tags"),
]
