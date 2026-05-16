from rest_framework.authentication import TokenAuthentication

SERVICE_ACCOUNT_USERNAME = "chat-jawafdehi-org"
JAWAFDEHI_USER_ID_HEADER = "HTTP_X_JAWAFDEHI_USER_ID"
JAWAFDEHI_USER_NAME_HEADER = "HTTP_X_JAWAFDEHI_USER_NAME"


class ChatServiceAccountAuthentication(TokenAuthentication):
    """
    DRF authentication class extending TokenAuthentication for service account
    impersonation.

    When a request is authenticated with the chat-jawafdehi-org service account
    token AND includes an X-Jawafdehi-User-Id header, get-or-creates a
    ChatUserIdentity record and — if the identity is mapped to a real Django
    user — returns that user for downstream permission checks.

    If the identity exists but is not yet mapped to a Django user, returns None
    to deny authorization.
    """

    def authenticate(self, request):
        auth_result = super().authenticate(request)
        if auth_result is None:
            return None

        user, token = auth_result

        if user.username != SERVICE_ACCOUNT_USERNAME:
            return auth_result

        owui_user_id = (request.META.get(JAWAFDEHI_USER_ID_HEADER) or "").strip()
        if not owui_user_id:
            return auth_result

        identity = resolve_or_create_identity(owui_user_id, request)
        if identity is None:
            return None

        if identity.user is None or not identity.user.is_active:
            return None

        return (identity.user, token)


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
