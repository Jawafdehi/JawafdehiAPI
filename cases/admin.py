from django import forms
from django.contrib import admin
from django.contrib.auth import get_user_model
from django.contrib.auth.admin import UserAdmin as BaseUserAdmin
from django.core.exceptions import ValidationError
from django.forms.models import BaseInlineFormSet
from django.utils.html import format_html

from cases.widgets import ToastUIEditorWidget

from .models import (
    Case,
    CaseEntityRelationship,
    CaseState,
    ChatUserIdentity,
    Feedback,
    RelationshipType,
    requires_accused,
)
from .rules.predicates import (
    can_manage_user,
    can_transition_case_state,
    can_view_case,
    is_admin,
    is_admin_or_moderator,
    is_caseworker,
    is_moderator,
)
from .validators import validate_courtcase_iri
from .widgets import (
    MultiTextField,
    MultiTimelineField,
)

User = get_user_model()


class UserModelChoiceField(forms.ModelChoiceField):
    def label_from_instance(self, obj):
        full_name = obj.get_full_name()
        if full_name:
            return f"{full_name} ({obj.username})"
        return obj.username


class UserModelMultipleChoiceField(forms.ModelMultipleChoiceField):
    def label_from_instance(self, obj):
        full_name = obj.get_full_name()
        if full_name:
            return f"{full_name} ({obj.username})"
        return obj.username


class UserFullNameAdminMixin:
    def formfield_for_foreignkey(self, db_field, request, **kwargs):
        if db_field.remote_field.model == User:
            kwargs["form_class"] = UserModelChoiceField
        return super().formfield_for_foreignkey(db_field, request, **kwargs)

    def formfield_for_manytomany(self, db_field, request, **kwargs):
        if db_field.remote_field.model == User:
            kwargs["form_class"] = UserModelMultipleChoiceField
        return super().formfield_for_manytomany(db_field, request, **kwargs)


# ============================================================================
# Custom Admin Forms
# ============================================================================


class CaseAdminForm(forms.ModelForm):
    """
    Custom form for Case admin with rich text editor and custom widgets.
    """

    key_allegations = MultiTextField(
        required=False,
        button_label="Add Key Allegation",
        label="Key Allegations",
        help_text="List of key allegation statements",
    )

    tags = MultiTextField(
        required=False,
        button_label="Add Tag",
        label="Tags",
        help_text="Tags for categorization",
    )

    court_cases = MultiTextField(
        required=False,
        button_label="Add Court Case IRI",
        label="Court-case references",
        help_text=(
            "Canonical court-case @id IRIs "
            "(https://jawafdehi.org/courtcase/<court>/<case_number>)"
        ),
    )

    timeline = MultiTimelineField(
        required=False,
        label="Timeline",
        help_text="Timeline of events (add in reverse-chronological order: most recent first)",
    )

    # evidence is now the CaseMaterialReference join; edit via API/inline in a follow-up

    start_date_bs = forms.CharField(
        label="Case start date (BS)",
        required=False,
        widget=forms.TextInput(
            attrs={
                "placeholder": "YYYY-MM-DD",
                "class": "vTextField nepali-date-picker",
                "autocomplete": "off",
                "readonly": "readonly",
                "style": "cursor: pointer;",
            }
        ),
    )
    end_date_bs = forms.CharField(
        label="Case end date (BS)",
        required=False,
        widget=forms.TextInput(
            attrs={
                "placeholder": "YYYY-MM-DD",
                "class": "vTextField nepali-date-picker",
                "autocomplete": "off",
                "readonly": "readonly",
                "style": "cursor: pointer;",
            }
        ),
    )

    class Meta:
        model = Case
        fields = "__all__"
        exclude = [
            "unified_entities"
        ]  # Exclude unified_entities as it's managed through the inline
        widgets = {
            "description": ToastUIEditorWidget(),
            "notes": ToastUIEditorWidget(),
            "state": forms.RadioSelect(),
            "case_start_date": forms.DateInput(attrs={"type": "date"}),
            "case_end_date": forms.DateInput(attrs={"type": "date"}),
        }
        help_texts = {
            "state": "Current workflow state: DRAFT (editable), IN_REVIEW (pending approval), PUBLISHED (public), CLOSED (archived)",
        }

    def __init__(self, *args, **kwargs):
        self.request = kwargs.pop("request", None)
        super().__init__(*args, **kwargs)

        # evidence is now the CaseMaterialReference join (case.material_references);
        # inline editing of evidence in the admin was removed with the
        # DocumentSource model and will return via a CaseMaterialReference path in
        # a follow-up.

        # court_cases is the CaseCourtCaseReference join, surfaced through the
        # Case.court_cases property as canonical @id IRIs — the ONLY reference
        # format, in the form as everywhere else.
        if self.instance.pk and "court_cases" not in self.initial:
            self.initial["court_cases"] = self.instance.court_cases

        # Disable PUBLISHED and CLOSED states for Caseworkers
        if self.request:
            user = self.request.user
            if is_caseworker(user) and not is_admin_or_moderator(user):
                # Disable PUBLISHED and CLOSED options for caseworkers
                state_field = self.fields.get("state")
                if state_field:
                    # Create custom choices with disabled options
                    state_field.widget.attrs["class"] = "contributor-state-field"

    class Media:
        css = {
            "all": (
                "https://nepalidatepicker.sajanmaharjan.com.np/v5/nepali.datepicker/css/nepali.datepicker.v5.0.6.min.css",
            )
        }
        js = (
            "https://nepalidatepicker.sajanmaharjan.com.np/v5/nepali.datepicker/js/nepali.datepicker.v5.0.6.min.js",
            "cases/js/date_converter.js",
        )

    def clean_court_cases(self):
        """Each row must be a canonical court-case @id IRI (surface per-row
        errors here — the model property would raise the same ValidationError
        too late, inside save)."""
        # Drop empty/whitespace-only rows and strip the rest — stray blank
        # rows are routine in the multi-text widget and shouldn't surface as
        # "'' is not a valid court-case @id IRI".
        rows = [
            r.strip()
            for r in self.cleaned_data.get("court_cases") or []
            if r and r.strip()
        ]
        errors = []
        for row in rows:
            try:
                validate_courtcase_iri(row)
            except ValidationError as exc:
                errors.extend(exc.messages)
        if errors:
            raise ValidationError(errors)
        return rows

    def clean_missing_details(self):
        """
        Ensure empty missing_details is stored as null rather than an empty string.
        """
        value = self.cleaned_data.get("missing_details")
        return value if value and value.strip() else None

    def clean(self):
        """
        Validate state transitions, new case state requirements, and required fields.
        """
        cleaned_data = super().clean()
        errors = {}

        # Validate slug format explicitly with better error message
        slug = cleaned_data.get("slug")
        if slug:
            try:
                from .validators import validate_slug

                validate_slug(slug)
            except ValidationError as e:
                # Defensively extract error message to handle both list and string formats
                errors["slug"] = (
                    e.messages[0] if hasattr(e, "messages") and e.messages else str(e)
                )

        # For new cases, enforce DRAFT state
        if not self.instance.pk:
            new_state = cleaned_data.get("state")
            if new_state != CaseState.DRAFT:
                errors["state"] = (
                    f"New cases must be created in DRAFT state. Cannot create a new case with state {new_state}."
                )

        # Check state transitions for existing cases
        if self.instance.pk:
            old_state = Case.objects.get(pk=self.instance.pk).state
            new_state = cleaned_data.get("state")

            if old_state != new_state and self.request:
                if not can_transition_case_state(
                    self.request.user, self.instance, new_state
                ):
                    errors["state"] = (
                        f"You do not have permission to transition from {old_state} to {new_state}. Caseworkers can only transition between DRAFT and IN_REVIEW states."
                    )

        # Validate required fields based on state
        new_state = cleaned_data.get("state")

        # Always require title
        if not cleaned_data.get("title", "").strip():
            errors["title"] = "Title is required"

        # Strict validation for IN_REVIEW and PUBLISHED states
        if new_state in [CaseState.IN_REVIEW, CaseState.PUBLISHED]:
            # Note: Alleged entity validation is performed in CaseAdmin.save_related()
            # after inline formsets are saved, not here in clean()

            # Check key_allegations
            key_allegations = cleaned_data.get("key_allegations")
            if not key_allegations or len(key_allegations) == 0:
                errors["key_allegations"] = (
                    "At least one key allegation is required for IN_REVIEW or PUBLISHED state"
                )

            # Check description
            description = cleaned_data.get("description", "").strip()
            if not description:
                errors["description"] = (
                    "Description is required for IN_REVIEW or PUBLISHED state"
                )

        if errors:
            raise ValidationError(errors)

        return cleaned_data

    def save(self, commit=True):
        """Persist the court-case references alongside the model fields.

        ``court_cases`` is a form-declared field (not a model column), so
        ``construct_instance`` skips it. Rows are canonical @id IRIs
        (validated in ``clean_court_cases``); assign through the property —
        ``Case.save()`` syncs the CaseCourtCaseReference join (also on the
        admin's ``commit=False`` + later ``obj.save()`` path).
        """
        instance = super().save(commit=False)
        if "court_cases" in self.cleaned_data:
            iris = list(self.cleaned_data.get("court_cases") or [])
            # Assign only on an actual change: cleaned_data always carries the
            # field, and an unconditional assignment would rewrite the join
            # (churning row identity + audit entries) on every unrelated save.
            if instance.pk is None or iris != instance.court_cases:
                instance.court_cases = iris
        if commit:
            instance.save()
            self.save_m2m()
        return instance


# ============================================================================
# Case Entity Relationship Inline
# ============================================================================


class CaseEntityRelationshipInlineFormSet(BaseInlineFormSet):
    """
    Custom formset for CaseEntityRelationshipInline that validates alleged
    entity presence at form-validation time (instead of save_related),
    so errors are surfaced cleanly in the admin UI.
    """

    def clean(self):
        super().clean()
        if not hasattr(self, "instance") or self.instance is None:
            return
        if self.instance.state not in {CaseState.IN_REVIEW, CaseState.PUBLISHED}:
            return
        # CORRUPTION cases require an ACCUSED entity; other case types (e.g.
        # TAX_EVASION) only require a named subject — any non-location entity.
        # The accepted relationship types and the error message differ, but the
        # form-scanning loop is otherwise identical.
        if requires_accused(self.instance.case_type):

            def is_required_entity(rel_type):
                return rel_type == RelationshipType.ACCUSED

            error = (
                "At least one accused entity relationship is required for IN_REVIEW or PUBLISHED state. "
                "Please add accused entities using the 'Case Entity Relationships' section below."
            )
        else:

            def is_required_entity(rel_type):
                return rel_type != RelationshipType.LOCATION

            error = (
                "At least one non-location entity relationship is required for IN_REVIEW or PUBLISHED state. "
                "Please add entities using the 'Case Entity Relationships' section below."
            )
        has_required_entity = any(
            form.cleaned_data
            and not form.cleaned_data.get("DELETE")
            and form.cleaned_data.get("nes_id")
            and is_required_entity(form.cleaned_data.get("relationship_type"))
            for form in self.forms
        )
        if not has_required_entity:
            raise ValidationError(error)


class CaseEntityRelationshipInline(admin.TabularInline):
    """
    Inline admin for managing Case <-> NES-entity binds.

    Each row binds the case to one NES entity by its canonical id (``nes_id``).
    NES owns the entity record; Jawafdehi stores only the id here. The id is a
    plain validated text field (no FK / autocomplete) because the entity lives
    in another service.

    Features:
    - TabularInline for efficient bulk operations
    - Relationship type dropdown with all available choices
    - Editable notes field for additional context
    - Created timestamp display for relationship tracking
    - Support for bulk operations
    """

    model = CaseEntityRelationship
    formset = CaseEntityRelationshipInlineFormSet
    extra = 1
    fields = ["nes_id", "relationship_type", "outcome", "notes", "created_at"]
    readonly_fields = ["created_at"]
    verbose_name = "Entity"
    verbose_name_plural = "Entities"

    # Enable bulk operations
    can_delete = True
    show_change_link = False

    class Media:
        css = {"all": ("admin/css/entity_view_link_hide.css",)}

    # Customize the form widget for relationship_type to show all choices
    def formfield_for_choice_field(self, db_field, request, **kwargs):
        """Customize the relationship_type field to show all available choices."""
        if db_field.name == "relationship_type":
            kwargs["widget"] = forms.Select(choices=RelationshipType.choices)
        return super().formfield_for_choice_field(db_field, request, **kwargs)

    def get_extra(self, request, obj=None, **kwargs):
        """Show extra forms for new cases, fewer for existing cases with relationships."""
        if obj and obj.entity_relationships.exists():
            return 0  # Don't show extra forms if relationships already exist
        return 1  # Show 1 extra form for new cases or cases without relationships

    # Read-only inline: entity binds are edited through the SPA `/admin` panel,
    # not Django admin. No add/change/delete rows here (view-only).
    def has_add_permission(self, request, obj=None):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


# ============================================================================
# Case Admin
# ============================================================================


@admin.register(Case)
class CaseAdmin(UserFullNameAdminMixin, admin.ModelAdmin):
    """
    Django Admin configuration for Case model.

    Features:
    - Custom form with rich text editor
    - State transition controls with validation
    - Version history display
    - Contributor assignment
    - Role-based permissions
    - CaseEntityRelationship inline for unified entity management
    """

    form = CaseAdminForm
    inlines = [CaseEntityRelationshipInline]

    class Media:
        js = ("cases/js/widgets.js", "admin/js/case_admin.js")
        css = {"all": ("cases/css/widgets.css", "admin/css/case_admin.css")}

    list_display = [
        "title_with_view_link",
        "case_type",
        "state_badge",
        "contributors_list",
        "created_at",
        "updated_at",
    ]

    list_filter = [
        "state",
        "case_type",
        "created_at",
    ]

    search_fields = [
        "slug",
        "title",
        "description",
    ]

    list_display_links = ("title_with_view_link",)

    readonly_fields = [
        "created_at",
        "updated_at",
        "version_info_display",
        "public_case_url",
    ]

    fieldsets = (
        (
            "Basic Information",
            {
                "fields": (
                    "slug",
                    "public_case_url",
                    "title",
                    "short_description",
                    "thumbnail_url",
                    "banner_url",
                    "case_type",
                    "state",
                    "bigo",
                )
            },
        ),
        (
            "Dates",
            {
                "fields": (
                    "case_start_date",
                    "start_date_bs",
                    "case_end_date",
                    "end_date_bs",
                )
            },
        ),
        (
            "Content",
            {
                "fields": (
                    "key_allegations",
                    "timeline",
                    "description",
                    "tags",
                    "court_cases",
                    "missing_details",
                    "notes",
                )
            },
        ),
        ("Assignment", {"fields": ("contributors",)}),
        (
            "Metadata",
            {
                "fields": (
                    "created_at",
                    "updated_at",
                    "version_info_display",
                ),
                "classes": ("collapse",),
            },
        ),
    )

    filter_horizontal = [
        "contributors",
    ]

    def state_badge(self, obj):
        """Display state as a colored badge."""
        colors = {
            CaseState.DRAFT: "#6c757d",
            CaseState.IN_REVIEW: "#ffc107",
            CaseState.PUBLISHED: "#28a745",
            CaseState.CLOSED: "#dc3545",
        }
        color = colors.get(obj.state, "#6c757d")
        return format_html(
            '<span style="background-color: {}; color: white; padding: 3px 10px; border-radius: 3px; font-weight: bold;">{}</span>',
            color,
            obj.get_state_display(),
        )

    state_badge.short_description = "State"

    def contributors_list(self, obj):
        """Display contributors as a comma-separated list of full names."""
        contributors = obj.contributors.all()
        if not contributors:
            return "—"
        names = []
        for user in contributors:
            full_name = user.get_full_name()
            if full_name:
                names.append(f"{full_name} ({user.username})")
            else:
                names.append(user.username)
        return ", ".join(names)

    contributors_list.short_description = "Contributors"

    def title_with_view_link(self, obj):
        """Display the case title with a View on Site link right after it."""
        viewable_states = [CaseState.PUBLISHED, CaseState.IN_REVIEW]
        if obj.state in viewable_states and obj.slug:
            public_url = f"https://jawafdehi.org/case/{obj.slug}"
            return format_html(
                '{} <a href="{}" target="_blank" rel="noopener noreferrer" '
                'class="button case-action-button case-action-button--view">View on Site</a>',
                obj.title,
                public_url,
            )
        return obj.title

    title_with_view_link.short_description = "Title"

    def public_case_url(self, obj):
        """Display the public case URL with a note about accessibility."""
        if not obj.slug:
            return "—"
        public_url = f"https://jawafdehi.org/case/{obj.slug}"
        viewable_states = [CaseState.PUBLISHED, CaseState.IN_REVIEW]
        if obj.state in viewable_states:
            return format_html(
                '<a href="{}" target="_blank" rel="noopener noreferrer">{}</a>',
                public_url,
                public_url,
            )
        return format_html(
            '{}<br><small style="color: #999;">Accessible only when case is in review or published.</small>',
            public_url,
        )

    public_case_url.short_description = "Public URL"

    def version_info_display(self, obj):
        """Display version info in a readable format."""
        if not obj.versionInfo:
            return "No version info"

        info = obj.versionInfo
        html = "<div style='font-family: monospace;'>"

        if "action" in info:
            html += f"<strong>Action:</strong> {info['action']}<br>"

        if "datetime" in info:
            html += f"<strong>DateTime:</strong> {info['datetime']}<br>"

        if "user_id" in info:
            html += f"<strong>User ID:</strong> {info['user_id']}<br>"

        if "change_summary" in info:
            html += f"<strong>Summary:</strong> {info['change_summary']}<br>"

        html += "</div>"
        return format_html(html)

    version_info_display.short_description = "Version Info"

    def get_queryset(self, request):
        """
        Filter queryset based on user role.

        - Caseworkers: See all non-CLOSED cases (global read access)
        - Moderators/Admins: See all cases
        """
        qs = super().get_queryset(request)

        # Admins and Moderators see everything
        if is_admin_or_moderator(request.user):
            return qs

        # Caseworkers see all non-CLOSED cases (global read-only access)
        if is_caseworker(request.user):
            return qs.exclude(state=CaseState.CLOSED)

        # No role - see nothing
        return qs.none()

    def get_readonly_fields(self, request, obj=None):
        """
        Make slug editable when:
        - It hasn't been set yet, OR
        - The case is in DRAFT state

        Once set and case is not DRAFT, slug becomes read-only to prevent breaking external links.
        """
        readonly = list(super().get_readonly_fields(request, obj))
        if obj and obj.slug and obj.state != CaseState.DRAFT:
            if "slug" not in readonly:
                readonly.append("slug")
        return readonly

    def has_view_permission(self, request, obj=None):
        """
        Check if user can view a case.

        - Contributors: Can only view assigned cases
        - Moderators/Admins: Can view all cases
        """
        if obj is None:
            return True

        return can_view_case(request.user, obj)

    # ------------------------------------------------------------------
    # Read-only in Django admin. The SPA `/admin` panel is the sole case
    # *write* surface; Django admin here is view-only (browse/inspect). All
    # three mutation permissions return False, so Django renders every field
    # read-only, hides Save, and drops the "Add case" button. Viewing +
    # role-scoped queryset filtering (see get_queryset / has_view_permission)
    # are unaffected.
    # ------------------------------------------------------------------
    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False

    def get_form(self, request, obj=None, **kwargs):
        """Pass request to form for role-based field customization."""
        form_class = super().get_form(request, obj, **kwargs)

        class FormWithRequest(form_class):
            def __new__(cls, *args, **kwargs):
                kwargs["request"] = request
                return form_class(*args, **kwargs)

        return FormWithRequest

    def get_fieldsets(self, request, obj=None):
        """Return fieldsets for fieldsets for the admin form. All fields are visible to all roles."""
        return super().get_fieldsets(request, obj)

    def save_related(self, request, form, formsets, change):
        """
        Save related objects (including many-to-many relationships).
        Automatically adds the creator to contributors when creating a new case.
        Validates alleged entity requirement after inline formsets are saved.
        """
        # First save the form's many-to-many data and inline formsets
        super().save_related(request, form, formsets, change)

        # Then add creator to contributors for new cases
        if not change:
            form.instance.contributors.add(request.user)

    def get_actions(self, request):
        """No write actions — case state transitions happen through the SPA
        `/admin` panel (the sole write surface), not Django admin."""
        return {}



# ============================================================================
# User Admin (for moderator restrictions)
# ============================================================================


class CustomUserAdmin(BaseUserAdmin):
    """
    Custom User admin to prevent Moderators from managing other Moderators.

    Property 14: Moderators cannot manage other Moderators in Django Admin
    """

    def get_queryset(self, request):
        """
        Filter queryset based on user role.

        - Admins: See all users
        - Moderators: See all users except other Moderators
        - Others: See nothing
        """
        qs = super().get_queryset(request)

        # Admins see everything
        if is_admin(request.user):
            return qs

        # Moderators see all users except other Moderators
        if is_moderator(request.user):
            # Exclude users who are in the Moderator group
            moderator_group_users = User.objects.filter(
                groups__name="Moderator"
            ).values_list("id", flat=True)
            return qs.exclude(id__in=moderator_group_users)

        # Others see nothing
        return qs.none()

    def has_change_permission(self, request, obj=None):
        """
        Check if user can change another user.

        - Admins: Can change all users
        - Moderators: Cannot change other Moderators
        """
        if obj is None:
            return True

        return can_manage_user(request.user, obj)

    def has_delete_permission(self, request, obj=None):
        """
        Check if user can delete another user.

        - Admins: Can delete users
        - Moderators: Cannot delete other Moderators
        """
        if obj is None:
            return True

        return can_manage_user(request.user, obj)


# Unregister the default User admin and register our custom one
admin.site.unregister(User)
admin.site.register(User, CustomUserAdmin)


# ============================================================================
# Admin Site Configuration
# ============================================================================

admin.site.site_header = "Jawafdehi"
admin.site.site_title = "Jawafdehi Contributor Portal"
admin.site.index_title = "Welcome to Jawafdehi Contributor Portal"


@admin.register(Feedback)
class FeedbackAdmin(admin.ModelAdmin):
    """Admin interface for Feedback model."""

    list_display = [
        "id",
        "feedback_type",
        "subject",
        "status",
        "has_attachment",
        "has_contact_info",
        "submitted_at",
    ]
    list_filter = ["feedback_type", "status", "submitted_at"]
    search_fields = ["subject", "description", "related_page"]
    readonly_fields = [
        "attachment_link",
        "submitted_at",
        "updated_at",
        "ip_address",
        "user_agent",
    ]

    fieldsets = (
        (
            "Feedback Details",
            {
                "fields": (
                    "feedback_type",
                    "subject",
                    "description",
                    "related_page",
                    "attachment",
                    "attachment_link",
                )
            },
        ),
        (
            "Contact Information",
            {"fields": ("contact_info",), "classes": ("collapse",)},
        ),
        ("Status", {"fields": ("status", "admin_notes")}),
        (
            "Metadata",
            {
                "fields": ("ip_address", "user_agent", "submitted_at", "updated_at"),
                "classes": ("collapse",),
            },
        ),
    )

    def has_contact_info(self, obj):
        """Check if feedback has contact information."""
        return bool(obj.contact_info and obj.contact_info.get("contactMethods"))

    has_contact_info.boolean = True
    has_contact_info.short_description = "Has Contact"

    def has_attachment(self, obj):
        """Check if feedback includes an attachment."""
        return bool(obj.attachment)

    has_attachment.boolean = True
    has_attachment.short_description = "Has File"

    def attachment_link(self, obj):
        """Render a clickable link to the uploaded attachment."""
        if not obj.attachment:
            return "No attachment"
        return format_html(
            '<a href="{}" target="_blank" rel="noopener noreferrer">{}</a>',
            obj.attachment.url,
            obj.attachment.name,
        )

    attachment_link.short_description = "Attachment"


# ============================================================================
# Token Admin (DRF authtoken) — REMOVED in phase5 (OIDC-only migration).
# The rest_framework.authtoken app is no longer installed, so there is no Token
# model to register an admin for.
# ============================================================================


@admin.register(ChatUserIdentity)
class ChatUserIdentityAdmin(UserFullNameAdminMixin, admin.ModelAdmin):
    list_display = ("owui_user_id", "owui_user_name", "user", "created_at")
    search_fields = ("owui_user_id", "owui_user_name", "user__username", "user__email")
    list_filter = ("created_at",)
    autocomplete_fields = ("user",)
    readonly_fields = ("created_at",)

    fieldsets = (
        (
            "Identity Mapping",
            {
                "fields": (
                    "owui_user_id",
                    "owui_user_name",
                    "user",
                    "created_at",
                ),
            },
        ),
    )
