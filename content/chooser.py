from django import forms
from django.db.models import Q
from django.utils.translation import gettext_lazy as _
from wagtail.admin.forms.choosers import BaseFilterForm
from wagtail.admin.views.generic.chooser import ChooseResultsView, ChooseView
from wagtail.admin.viewsets.chooser import ChooserViewSet


class CaseSearchFilterForm(BaseFilterForm):
    """Adds a free-text search box to the case chooser modal.

    ``cases.Case`` is owned by the DRF/Jazzmin admin and is NOT registered with
    Wagtail's search backend, so the chooser's built-in ``SearchFilterMixin``
    (which only activates for indexed models) never kicks in. Rather than index
    the model into Wagtail search, we filter with a plain ``icontains`` over the
    two human-searchable columns — ``title`` and ``slug`` (the public case
    identifier; the legacy ``case_id`` column was dropped).
    """

    q = forms.CharField(
        label=_("Search"),
        widget=forms.TextInput(attrs={"placeholder": _("Search by title or slug")}),
        required=False,
    )

    def filter(self, objects):
        objects = super().filter(objects)
        query = self.cleaned_data.get("q")
        if query:
            objects = objects.filter(
                Q(title__icontains=query) | Q(slug__icontains=query)
            )
            self.is_searching = True
            self.search_query = query
        return objects


class CaseChooseView(ChooseView):
    filter_form_class = CaseSearchFilterForm


class CaseChooseResultsView(ChooseResultsView):
    filter_form_class = CaseSearchFilterForm


class CaseChooserViewSet(ChooserViewSet):
    """Chooser-only modal for picking an existing corruption Case.

    Cases remain owned by the DRF/Jazzmin admin; this viewset deliberately
    defines no creation form, so Wagtail can browse and select cases but never
    create or edit them. The modal lazy-loads a paginated, searchable list, so
    the article edit page no longer renders every case inline.
    """

    model = "cases.Case"
    icon = "doc-full"
    choose_one_text = "Choose a case"
    choose_another_text = "Choose another case"
    edit_item_text = "Edit this case"
    choose_view_class = CaseChooseView
    choose_results_view_class = CaseChooseResultsView


case_chooser_viewset = CaseChooserViewSet("case_chooser")
