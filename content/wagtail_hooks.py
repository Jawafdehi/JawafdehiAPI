from wagtail import hooks

from .chooser import case_chooser_viewset


@hooks.register("register_admin_viewset")
def register_case_chooser_viewset():
    return case_chooser_viewset
