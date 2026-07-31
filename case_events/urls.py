# SPDX-License-Identifier: Hippocratic-3.0
"""Signal-filing endpoints.

Only one, and only one is expected: every other producer watches something
rather than being called. See :mod:`case_events.views`.
"""

from django.urls import path

from case_events.views import ManualNoteView

urlpatterns = [
    path("signals/manual-note/", ManualNoteView.as_view(), name="manual-note"),
]
