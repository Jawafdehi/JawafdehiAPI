import json

from django.core.exceptions import ValidationError
from django.forms.fields import Field
from django.forms.widgets import Textarea, Widget
from django.template.loader import render_to_string
from django.utils.safestring import mark_safe
from jawafdehi_shared.entities.ids import is_valid_entity_iri


class EasyMDEWidget(Textarea):
    class Media:
        css = {
            "all": ("https://cdn.jsdelivr.net/npm/easymde@2.19.0/dist/easymde.min.css",)
        }
        js = ("https://cdn.jsdelivr.net/npm/easymde@2.19.0/dist/easymde.min.js",)

    def __init__(self, attrs=None):
        default_attrs = {
            "cols": 80,
            "rows": 25,
            "data-easymde": "true",
        }
        if attrs:
            default_attrs.update(attrs)
        super().__init__(default_attrs)

    def render(self, name, value, attrs=None, renderer=None):
        textarea_html = super().render(name, value, attrs, renderer)
        final_attrs = self.build_attrs(self.attrs, attrs)
        widget_id = final_attrs.get("id", f"id_{name}")
        init_script = f"""
<script>
(function() {{
    var el = document.getElementById({json.dumps(widget_id)});
    if (el && !el._easymde_initialized) {{
        el._easymde_initialized = true;
        new EasyMDE({{
            element: el,
            spellChecker: false,
            status: ["lines", "words", "cursor"],
            toolbar: [
                "bold", "italic", "strikethrough", "heading", "|",
                "quote", "unordered-list", "ordered-list", "|",
                "link", "image", "table", "horizontal-rule", "|",
                "preview", "side-by-side", "fullscreen", "|",
                "guide"
            ],
            renderingConfig: {{
                singleLineBreaks: false,
                codeSyntaxHighlighting: true,
            }},
            placeholder: "Write in Markdown...",
            autosave: {{
                enabled: false,
            }},
            minHeight: "400px",
            maxHeight: "600px",
        }});
    }}
}})();
</script>
"""
        return mark_safe(textarea_html + init_script)


class ToastUIEditorWidget(Textarea):
    """
    Rich text Markdown editor powered by Toast UI Editor (TOAST UI Editor 3.x).
    Renders a WYSIWYG editing surface that auto-syncs Markdown to a hidden
    <textarea> on form submit. Drop-in replacement for EasyMDEWidget with
    native Word / Office copy-paste handling.
    """

    class Media:
        css = {
            "all": (
                "https://uicdn.toast.com/editor/3.2.2/toastui-editor.min.css",
                "https://uicdn.toast.com/editor-plugin-table-merged-cell/3.0.2/toastui-editor-plugin-table-merged-cell.min.css",
            )
        }
        js = (
            "https://uicdn.toast.com/editor/3.2.2/toastui-editor-all.min.js",
            "https://uicdn.toast.com/editor-plugin-table-merged-cell/3.0.2/toastui-editor-plugin-table-merged-cell.min.js",
        )

    def __init__(self, attrs=None):
        default_attrs = {
            "rows": 1,
            "data-toastui-editor": "true",
        }
        if attrs:
            default_attrs.update(attrs)
        super().__init__(default_attrs)

    def render(self, name, value, attrs=None, renderer=None):
        textarea_html = super().render(name, value, attrs, renderer)
        final_attrs = self.build_attrs(self.attrs, attrs)
        widget_id = final_attrs.get("id", f"id_{name}")
        editor_container_id = f"{widget_id}_editor"
        initial_md = json.dumps(value if value else "")

        init_script = f"""
<div id="{editor_container_id}"></div>
<script>
(function() {{
    var containerEl = document.getElementById({json.dumps(editor_container_id)});
    var textareaEl = document.getElementById({json.dumps(widget_id)});
    if (containerEl && textareaEl && !containerEl._tui_initialized) {{
        containerEl._tui_initialized = true;

        var editor = new toastui.Editor({{
            el: containerEl,
            height: '500px',
            minHeight: '400px',
            initialEditType: 'wysiwyg',
            previewStyle: 'vertical',
            initialValue: {initial_md},
            usageStatistics: false,
            toolbarItems: [
                ['heading', 'bold', 'italic', 'strike'],
                ['hr', 'quote'],
                ['ul', 'ol', 'task', 'indent', 'outdent'],
                ['table', 'image', 'link'],
                ['code', 'codeblock'],
                ['scrollSync'],
            ],
            plugins: [toastui.Editor.plugin.tableMergedCell],
            placeholder: 'Write in Markdown\u2026',
            autofocus: false,
            previewHighlight: true,
        }});

        textareaEl.style.display = 'none';

        var form = textareaEl.closest('form');
        if (form) {{
            form.addEventListener('submit', function() {{
                textareaEl.value = editor.getMarkdown();
            }});
        }}
    }}
}})();
</script>
"""
        return mark_safe(textarea_html + init_script)


class BaseMultiWidget(Widget):
    template_name = None

    class Media:
        css = {"all": ("cases/css/widgets.css",)}
        js = ("cases/js/widgets.js",)

    def get_context(self, name, value, attrs):
        if value is None:
            value = []
        elif isinstance(value, str):
            value = json.loads(value) if value else []

        final_attrs = self.build_attrs(self.attrs, attrs)
        widget_id = final_attrs.get("id", name)

        return {
            "widget_id": widget_id,
            "name": name,
            "values": value,
            "values_json": json.dumps(value),
        }

    def render(self, name, value, attrs=None, renderer=None):
        context = self.get_context(name, value, attrs)
        return mark_safe(render_to_string(self.template_name, context))

    def value_from_datadict(self, data, files, name):
        value = data.get(name, "[]")
        if isinstance(value, list):
            return value
        try:
            return json.loads(value) if value else []
        except (json.JSONDecodeError, TypeError, ValueError):
            return []


class MultiEntityIDWidget(BaseMultiWidget):
    template_name = "cases/widgets/multi_entity_widget.html"


class MultiEntityIDField(Field):
    widget = MultiEntityIDWidget

    def to_python(self, value):
        if value in self.empty_values:
            return []
        if isinstance(value, list):
            return value
        try:
            return json.loads(value) if value else []
        except (json.JSONDecodeError, TypeError, ValueError):
            return []

    def validate(self, value):
        super().validate(value)
        for entity_id in value:
            if not is_valid_entity_iri(entity_id):
                raise ValidationError(
                    f"Invalid entity @id IRI: {entity_id!r}. Must be of the "
                    "form 'https://<authority>/entity/<prefix>/<slug>'."
                )


class MultiTextWidget(BaseMultiWidget):
    template_name = "cases/widgets/multi_text_widget.html"

    def __init__(self, attrs=None, button_label=None):
        super().__init__(attrs)
        self.button_label = button_label or "Add Item"

    def get_context(self, name, value, attrs):
        context = super().get_context(name, value, attrs)
        context["button_label"] = self.button_label
        return context


class MultiTextField(Field):
    def __init__(self, *args, button_label="Add Item", **kwargs):
        self.button_label = button_label
        super().__init__(*args, **kwargs)
        self.widget = MultiTextWidget(button_label=button_label)

    def to_python(self, value):
        if value in self.empty_values:
            return []
        if isinstance(value, list):
            return value
        try:
            return json.loads(value) if value else []
        except (json.JSONDecodeError, TypeError, ValueError):
            return []

    def validate(self, value):
        super().validate(value)
        # Only validate non-empty if field is required
        if self.required and (not value or len(value) < 1):
            raise ValidationError("This field is required.")


class MultiTimelineWidget(BaseMultiWidget):
    template_name = "cases/widgets/multi_timeline_widget.html"


class MultiTimelineField(Field):
    widget = MultiTimelineWidget

    def to_python(self, value):
        if value in self.empty_values:
            return []
        if isinstance(value, list):
            return value
        try:
            return json.loads(value) if value else []
        except (json.JSONDecodeError, TypeError, ValueError):
            return []



