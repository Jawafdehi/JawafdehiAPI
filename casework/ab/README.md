# Local three-database sqlite runbook

This runbook brings up a **local-only** Django instance backed by three separate
sqlite files (`default` / `nes` / `ngm`), matching prod's per-service database
isolation (`config/db_router.py::ServiceDatabaseRouter`) without touching any
remote database. It is the safe write target for A/B-testing the enrichment
pipeline — no test in this stack ever reaches production.

Run everything from the repo root (`services/JawafdehiAPI/`, or the equivalent
worktree root) with `uv`, never poetry/pip.

## Step 1: Migrate all three databases

```bash
unset DATABASE_URL
uv run python manage.py migrate --database=default
uv run python manage.py migrate --database=nes
uv run python manage.py migrate --database=ngm
```

Each of the three commands is a **separate migration run** against a distinct
sqlite file. `config/settings.py:441-473` is what makes this possible: when
`DATABASE_URL` is unset, `default` falls back to `sqlite:///db.sqlite3`, and via
`_sqlite_alias()` the `nes` and `ngm` aliases fall back to their own
`db_nes.sqlite3` / `db_ngm.sqlite3` files (each with a distinct `TEST["NAME"]`
too — see `config/settings_test.py`). Distinct files are mandatory: Django
treats two sqlite connections that share a `NAME` as the *same* database
connection, which would make `config/db_router.py` isolation a fiction —
a write routed to `nes` would silently become visible on `default`.

Each run applies **every** app's migrations (`migrate` with no app label walks
the whole migration graph), but `ServiceDatabaseRouter.allow_migrate` decides,
per app + alias, whether tables are actually created there:

- `entities` → `nes`
- `courts` + `materials` → `ngm`
- everything else (`cases`, `review`, `jobs`, `django.contrib.*`, wagtail, …) → `default`

So all three runs end `OK`, but the resulting files are different sizes because
each only actually materializes the tables the router assigns to it:

```
db.sqlite3      1,269,760 bytes   (default: cases/review/jobs/contrib/wagtail/...)
db_nes.sqlite3    106,496 bytes   (nes: entities)
db_ngm.sqlite3    221,184 bytes   (ngm: courts/materials)
```

## Step 2: Verify router isolation holds on sqlite

```bash
unset DATABASE_URL
DEBUG=True uv run python -c "
import django, os
os.environ.setdefault('DJANGO_SETTINGS_MODULE','config.settings')
django.setup()
from django.conf import settings
for a in ('default','nes','ngm'):
    print(a, settings.DATABASES[a]['NAME'])
"
```

Expected (and observed) output — three *distinct* file paths:

```
default db.sqlite3
nes db_nes.sqlite3
ngm db_ngm.sqlite3
```

If any two ever match, stop: isolation is broken and nothing routed through the
DB router can be trusted.

**Note:** this one-off script (unlike `manage.py migrate`, which is in
`config/settings.py`'s `_BUILD_TIME_COMMANDS` allowlist) is not a recognized
build command, so the settings module's fail-closed `SECRET_KEY` guard applies
unless `DEBUG` or `TESTING` is set. The block above already prefixes the
command with `DEBUG=True` (or use `TESTING=true` instead) for exactly this
reason. This does not change any database target — it only satisfies the same
guard that `manage.py runserver` (Step 4) also needs.

## Step 3: Create a caseworker user for API writes

Writes require superuser or `Caseworker` group membership
(`cases/rules/predicates.py:88-98`). This creates (or resets) a **local-only**
superuser, `abgen`, with the throwaway password `local-dev-only` — never used
against any non-local database.

```bash
unset DATABASE_URL
DEBUG=True uv run python manage.py shell -c "
from django.contrib.auth import get_user_model
U = get_user_model()
u, _ = U.objects.get_or_create(username='abgen', defaults={'is_superuser': True, 'is_staff': True})
u.is_superuser = True; u.is_staff = True; u.set_password('local-dev-only'); u.save()
print('user ready:', u.username, 'superuser:', u.is_superuser)
"
```

Expected: `user ready: abgen superuser: True`.

## Step 4: Start the server and confirm it answers

```bash
unset DATABASE_URL
DEBUG=True uv run python manage.py runserver 0.0.0.0:48010 &
sleep 5
curl -s -o /dev/null -w '%{http_code}\n' http://127.0.0.1:48010/api/cases/
```

Expected: `200` or `403` (either proves the app is serving on the local three-
database sqlite stack; a connection refusal means the server never came up).
Stop the background server (`kill %1` or the PID it printed) once you're done —
don't leave it orphaned.

**Port 48010 is the standard port for this runbook — do not default to
`48000`.** On this shared host, port `48000` is persistently bound by an
unrelated, long-running process owned by a different OS user, and this has
already caused a real false-positive during Task 2: curling `48000` returned a
misleading `200`, but that response came from the other user's foreign
process, not from anything this runbook started. A `200` on any port proves
nothing by itself about *whose* server answered.

Before trusting a `200` from this (or any) port on a shared host — and
**before running any later `--apply` write operation against that base
URL** — confirm the responding server is actually yours:

- Check that the request shows up in *your own* dev server's access log
  (the `manage.py runserver` process above prints a `django.server` log line
  — including the HTTP method, path, and status code — for every request it
  serves; if your `curl` doesn't appear there, it didn't hit your server),
  and/or
- Check the listening PID with `ss -ltnp | grep <port>` and confirm it matches
  the PID your `manage.py runserver` command printed/returned.

Do not attempt to free `48000` or otherwise touch another user's process —
just use a different port (`48010`) and verify ownership as above. Skipping
this check risks silently writing enrichment data into a stranger's Django
instance during later A/B-test tasks, which would be a serious error.

## Step 5: authenticate `CaseworkApi` writes locally (`DEV_AUTH`)

**Why `Authorization: Bearer <anything>` does not work locally:** DRF's
authenticator chain always tries `jawafdehi_shared.auth.oidc.OIDCAuthentication`
FIRST (`config/settings.py:704-715`). It claims any request carrying a
`Bearer` header for itself and immediately raises
`AuthenticationFailed("OIDC authentication is not configured (OIDC_JWKS_URI)")`
when `OIDC_JWKS_URI`/`OIDC_ISSUER` are unset — which is the default local
state (no `.env` in this repo; nothing stands up a real Zitadel). Because
`OIDCAuthentication` raises before DRF ever tries the next authenticator in
the list, it doesn't matter what session/basic auth is configured — a
`Bearer` header is always routed to OIDC and always 401s locally. This is not
a bug to route around per-task; it's why `CaseworkApi` supports a second,
explicit auth mode below.

Start the server with `DEV_AUTH=1` (additive to `DEBUG`/`TESTING` per
`config/settings.py:693-715`; never honored in production) so DRF ALSO
accepts `SessionAuthentication`/`BasicAuthentication`:

```bash
unset DATABASE_URL
DEV_AUTH=1 DEBUG=True uv run python manage.py runserver 0.0.0.0:48010 &
```

Then use `CaseworkApi` in **Basic** mode — never Bearer — as the `abgen`
superuser created in Step 3 (`local-dev-only`; throwaway local-only password):

```python
from casework.common.api import CaseworkApi

api = CaseworkApi(
    "http://127.0.0.1:48010",
    basic=("abgen", "local-dev-only"),
)
api.patch_field("some-slug", "bigo", 500)
```

`CaseworkApi.__init__` requires exactly one of `token=` (Bearer, the
production default, unchanged) or `basic=(username, password)` (HTTP Basic,
local-only) — passing both or neither raises `ValueError`, so it is not
possible to accidentally send both headers on one request. `role`/group
membership (Caseworker Django group, `is_superuser`) is checked identically
in both modes — DEV_AUTH only skips JWT verification, not authorization.

## sqlite divergences from prod Postgres (`docs/testing.md:19-26`)

A few behaviors differ from prod Postgres under this local sqlite stack.
None of them affect enrichment correctness — they're guarded by
`connection.vendor == "postgresql"` checks that fall back to sqlite-safe code
paths, and are covered by targeted tests rather than by the engine itself:

- **`cases` tags filter** (`cases/api_views.py:417`): Postgres uses the
  `tags__contains` JSONB-containment lookup; sqlite doesn't support it, so the
  fallback filters in Python instead.
- **Courts raw-SQL query timeout** (`courts/views.py:338`): Postgres sets
  `SET statement_timeout = %s` on the cursor before running raw SQL against the
  `ngm` alias; sqlite has no equivalent and skips it.
- **NES entity JSONB containment / keyword aggregation**
  (`entities/persistence.py:66`, `entities/persistence.py:322`): Postgres uses
  `@>` containment and a `LATERAL jsonb_array_elements_text` query for
  `all_keywords()`; sqlite (no JSONB support) takes the Python-side fallback
  path instead.
- **DRF throttling is off under `TESTING`** — irrelevant to enrichment writes,
  which are not rate-limit-sensitive in this local stack.

None of these change what gets written to, or read from, the three sqlite
databases — only which code path computes the same result.
