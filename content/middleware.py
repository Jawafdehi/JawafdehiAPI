"""Close the Wagtail admin's authenticated-but-unauthorized redirect loop."""

from django.http import HttpResponseForbidden
from django.urls import NoReverseMatch, reverse
from django.utils.functional import cached_property

# Deliberately dependency-free (no template, no DB) so it still renders when the
# user has no CMS access and the admin's own template machinery is off-limits.
_FORBIDDEN_BODY = """<!doctype html>
<html lang="en">
<meta charset="utf-8">
<title>No Newsroom access</title>
<h1>You are signed in, but this account cannot open the Newsroom.</h1>
<p>Your sign-in worked. The account simply has no CMS role attached, so there is
nothing to retry &mdash; ask an administrator to grant it the
<strong>Caseworker</strong> role, then sign in again.</p>
<p lang="ne">तपाईं साइन इन हुनुभयो, तर यो खाताबाट न्यूजरुम खोल्न मिल्दैन।
यो खातामा कुनै सीएमएस भूमिका छैन, त्यसैले फेरि प्रयास गर्नुको अर्थ छैन &mdash;
प्रशासकलाई <strong>Caseworker</strong> भूमिका दिन अनुरोध गर्नुहोस्।</p>
</html>
"""


class WagtailAdminAccessMiddleware:
    """Answer 403 when a signed-in user without CMS access hits the Newsroom.

    Wagtail routes an authenticated user who lacks ``wagtailadmin.access_admin``
    down the SAME branch as an anonymous one: ``require_admin_access`` falls
    through to ``reject_request``, which calls ``redirect_to_login``
    (``wagtail/admin/auth.py``). Behind SSO that redirect is not a dead end but
    a cycle — the IdP session is still live, so it re-authenticates instantly and
    returns to the same forbidden page. The browser spins forever, the user is
    told nothing, and the only trace is a wall of 302s plus eventual
    ``oidc_states`` 400s once concurrent auth attempts evict each other.

    Returning 403 before Wagtail can redirect turns that into one readable
    answer. Anonymous users are untouched: for them the redirect to login is
    correct, and this middleware ignores them.
    """

    # Escape hatches that must keep working for a signed-in user with no access,
    # or they would have no way to switch accounts.
    _EXEMPT_URL_NAMES = ("wagtailadmin_login", "wagtailadmin_logout")

    def __init__(self, get_response):
        self.get_response = get_response

    @cached_property
    def _admin_prefix(self) -> str | None:
        try:
            return reverse("wagtailadmin_home")
        except NoReverseMatch:  # CMS not mounted (partial test URLConfs)
            return None

    @cached_property
    def _exempt_paths(self) -> frozenset[str]:
        paths = set()
        for name in self._EXEMPT_URL_NAMES:
            try:
                paths.add(reverse(name))
            except NoReverseMatch:
                continue
        return frozenset(paths)

    def __call__(self, request):
        if self._blocks(request):
            return HttpResponseForbidden(
                _FORBIDDEN_BODY, content_type="text/html; charset=utf-8"
            )
        return self.get_response(request)

    def _blocks(self, request) -> bool:
        prefix = self._admin_prefix
        if prefix is None or not request.path.startswith(prefix):
            return False
        if request.path in self._exempt_paths:
            return False
        user = getattr(request, "user", None)
        if user is None or not user.is_authenticated:
            return False
        # Superusers bypass Wagtail's permission checks, and has_perms honours
        # that, so they are never blocked here.
        return not user.has_perms(["wagtailadmin.access_admin"])
