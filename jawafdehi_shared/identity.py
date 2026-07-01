"""Chat service-account identity helpers.

The DRF-token-based ``ChatServiceAccountAuthentication`` that used to live here
was REMOVED in phase5 (the OIDC-only migration): it subclassed
``rest_framework.authentication.TokenAuthentication`` and so recognised the
legacy ``Authorization: Token <key>`` scheme, which is exactly what the
migration retires. The API is now OIDC-only.

The end-user impersonation (``X-Jawafdehi-User-Id`` header ->
``ChatUserIdentity`` -> real Django user) is an application-layer concern that
layers on top of OIDCAuthentication. ``cases.api_views.MeView`` performs that
resolution after authentication using the helpers below; the Zitadel service
account is recognised out-of-band via ``settings.OIDC_SERVICE_ACCOUNT_SUBJECTS``
(see ``jawafdehi_shared.auth.oidc``).
"""

JAWAFDEHI_USER_ID_HEADER = "HTTP_X_JAWAFDEHI_USER_ID"
JAWAFDEHI_USER_NAME_HEADER = "HTTP_X_JAWAFDEHI_USER_NAME"


def resolve_or_create_identity(owui_user_id, request):
    """Get or create a ChatUserIdentity for the given OpenWebUI user ID."""
    from cases.models import ChatUserIdentity

    owui_user_name = (request.META.get(JAWAFDEHI_USER_NAME_HEADER) or "").strip()

    identity, created = ChatUserIdentity.objects.get_or_create(
        owui_user_id=owui_user_id,
        defaults={"owui_user_name": owui_user_name},
    )

    if not created and owui_user_name:
        identity.owui_user_name = owui_user_name
        identity.save(update_fields=["owui_user_name"])

    return identity
