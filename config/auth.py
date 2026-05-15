from rest_framework.authentication import TokenAuthentication

SERVICE_ACCOUNT_USERNAME = "chat-jawafdehi-org"
JAWAFDEHI_USER_ID_HEADER = "HTTP_X_JAWAFDEHI_USER_ID"


class ChatServiceAccountAuthentication(TokenAuthentication):
    """
    DRF authentication class extending TokenAuthentication for service account
    impersonation.

    When a request is authenticated with the chat-jawafdehi-org service account
    token AND includes an X-Jawafdehi-User-Id header, resolves the header value
    to a real Django user via ChatUserIdentity and returns that user.

    Downstream permission checks (django-rules, DRF permissions) then operate
    against the real user rather than the service account.
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

        from cases.models import ChatUserIdentity

        try:
            identity = ChatUserIdentity.objects.get(owui_user_id=owui_user_id)
        except ChatUserIdentity.DoesNotExist:
            return None

        if not identity.user.is_active:
            return None

        return (identity.user, token)
