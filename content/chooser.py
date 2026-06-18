from wagtail.admin.viewsets.chooser import ChooserViewSet


class CaseChooserViewSet(ChooserViewSet):
    """Chooser-only modal for picking an existing corruption Case.

    Cases remain owned by the DRF/Jazzmin admin; this viewset deliberately
    defines no creation form, so Wagtail can browse and select cases but never
    create or edit them.
    """

    model = "cases.Case"
    icon = "doc-full"
    choose_one_text = "Choose a case"
    choose_another_text = "Choose another case"
    edit_item_text = "Edit this case"


case_chooser_viewset = CaseChooserViewSet("case_chooser")
