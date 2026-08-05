# Embedded MCP Server

The Jawafdehi MCP server is part of this repository and runs inside the same
Gunicorn ASGI deployment as Django. It is not a sidecar or separately deployed
service.

## Runtime

- Streamable HTTP endpoint: `POST /mcp`
- MCP readiness: `GET /mcp/health`
- OAuth protected-resource metadata:
  `GET /.well-known/oauth-protected-resource/mcp`
- Django APIs: unchanged under `/api/`

`config.asgi.application` routes protocol requests to `jawafdehi_mcp` and all
other traffic to Django. MCP uses stateless streamable HTTP, so requests can be
served by any Gunicorn worker or pod without session affinity.

Only the explicit MCP protocol, health, and protected-resource metadata paths
are routed to MCP; no other Django path can become an MCP endpoint.

There is **one** endpoint and one deployment — in production
`https://api.jawafdehi.org/mcp`. There is no separate public/internal hostname
and no ingress-injected mode header.

MCP tools that consume Jawafdehi APIs retain the existing API contracts,
authentication, permission checks, and serializers. In the embedded HTTP
runtime, a bounded `httpx.ASGITransport` wrapper dispatches those requests
directly to Django in memory. It enforces a wall-clock deadline and shared
concurrency limit that stock `ASGITransport` does not provide. The caller's
verified bearer is forwarded. The stdio module runner remains a remote API
client and uses `JAWAFDEHI_API_BASE_URL`.

Task-oriented control-plane tools cover unified search, entity history/delete,
materials, courts and ingestion, case-update proposals, casework reviews, and
the central jobs queue. They map only to fixed `/api/*` routes; no arbitrary
path proxy is exposed.

## Authentication

The embedded server reads the platform's `OIDC_ISSUER`, `OIDC_AUDIENCE`, and
derived JWKS/userinfo settings. JWKS network fetches use the shared
`OIDC_JWKS_TIMEOUT` bound.

One rule decides the catalog:

| Caller | Catalog |
|---|---|
| Anonymous (no bearer) | the read-only `ANONYMOUS_TOOL_NAMES` set (13 tools) |
| Any verified bearer, **any role including none** | all 26 tools |
| stdio with `JAWAFDEHI_API_TOKEN` | all 26 (the service token is authentication) |

An anonymous request is **not** challenged, because every anonymous tool wraps a
REST route that is already `AllowAny` — `/api/entities`, `/api/search/`,
`/api/materials/`, `/api/courts/`. Serving those through MCP grants nothing the
API does not already serve without a token. A bearer that is *present but
invalid* is still rejected with `401` and a `WWW-Authenticate` challenge; that is
a broken caller, not an anonymous one.

Two consequences of not challenging anonymous callers:

- MCP clients begin an OAuth flow only on a `401`, so an anonymous client is
  never prompted to log in — it simply sees the smaller catalog.
  `get_current_user` is how a caller tells which side it is on, and
  `GET /.well-known/oauth-protected-resource/mcp` is how it discovers where to
  authenticate. That metadata document is therefore always served, and
  `OIDC_RESOURCE` is **required** — the endpoint returns `503` without it.
- `convert_to_markdown` is excluded from the anonymous set. It is one of the few
  tools with no `/api/` route behind it, so the catalog is its only gate — and
  that gate is enforced on `tools/call`, not merely on `tools/list`. It needs
  authentication but no particular role.

Catalog visibility is not an authorization grant. Tools forward the caller's
verified bearer to Django, whose API permissions remain the final boundary for
reads and writes — including the rules `docs/security/authz-model.md` pins on
MCP as a principal: the `ngm.query` scope-only bypass and the service-account
subject allowlist on `/api/caseworker/me`.

`ngm_query_judicial` is the one anonymous tool that does not run as the caller:
with no bearer to forward it authenticates with `MCP_QUERY_API_TOKEN`, a shared
service account. Those requests therefore send `public_projection: true`, which
forces the API's narrow public plane no matter what roles that account holds.
The flag is one-way — it can only narrow — so the guarantee does not depend on
how the account is provisioned. A forwarded caller bearer is unaffected, and
local stdio keeps the internal plane.

MCP rejects unrecognized `Host` and `Origin` headers. The host and origin from
`OIDC_RESOURCE` plus local loopback hosts are trusted automatically; additional
DNS names go in `MCP_ALLOWED_HOSTS` and browser origins in
`MCP_ALLOWED_ORIGINS`.

`GET /mcp/health` reports lifespan readiness only. A missing `OIDC_RESOURCE`
does not make it unready: the anonymous read catalog still serves correctly, and
failing the probe would remove a pod that is doing its job. That
misconfiguration surfaces on the metadata endpoint and on every authenticated
call instead.

`JAWAFDEHI_API_TOKEN` enables service-authenticated stdio use. It is not
injected into the HTTP platform process, never authenticates or elevates an HTTP
caller, and general case/NES HTTP calls forward only the caller's verified OIDC
bearer. The intentionally public `ngm_query_judicial` tool uses the separate
`MCP_QUERY_API_TOKEN` fallback needed to reach the API's SELECT-gated public
court-data plane. That credential must carry only the `ngm.query` scope. A local
stdio process may fall back to its full `JAWAFDEHI_API_TOKEN` for this query when
no dedicated query token is configured. When neither credential is available,
the tool is omitted from anonymous catalogs rather than advertising an
operation that cannot authenticate.

## Resource budgets

The shared process keeps MCP work bounded with:

- `MCP_EMBEDDED_API_TIMEOUT` and `MCP_EMBEDDED_API_MAX_CONCURRENCY`
- `MCP_CONTROL_PLANE_TIMEOUT`, `MCP_QUERY_TIMEOUT`, and
  `MCP_PROXY_HTTP_TIMEOUT`
- `MCP_DOCUMENT_FETCH_TIMEOUT`, `MCP_DOCUMENT_CONVERT_TIMEOUT`,
  `MCP_DOCUMENT_MAX_CONCURRENCY`, `MCP_DOCUMENT_MAX_INPUT_BYTES`, and
  `MCP_DOCUMENT_MAX_OUTPUT_CHARS`
- `MCP_DOCUMENT_WORKER_MEMORY_BYTES`

Defaults are listed in `.env.example` and passed through by `docker-compose.yml`.
Remote and inline document bytes are parsed in a spawned, killable worker with
RSS, CPU, file-size, descriptor, output, and wall-clock limits. The parent and
worker exchange bounded raw bytes rather than pickled objects.

## Filesystem boundary

The embedded HTTP server cannot read or write server-local paths supplied by
MCP clients. `convert_to_markdown` accepts remote HTTP(S) and data URIs over
HTTP, but `file_path`, `file://`, and `output_path` are stdio-only. Remote
targets must resolve to public IP addresses on ports 80/443, redirects are
revalidated, and downloads/conversion output are bounded. Plugins are always
disabled for remote and data inputs. Installed plugins are opt-in only for
trusted local stdio files via `enable_plugins=true`.
`ngm_extract_case_data` and `upload_material_file` are also stdio-only because
their contracts write or read local files.

## Commands

Run the combined local ASGI application:

```bash
DJANGO_SETTINGS_MODULE=config.settings_test TESTING=true \
  uv run uvicorn config.asgi:application --host 0.0.0.0 --port 48000
```

Run the retained stdio transport directly as a Python module:

```bash
JAWAFDEHI_API_BASE_URL=https://api.jawafdehi.org \
  JAWAFDEHI_API_TOKEN=<oidc-access-token> \
  uv run python -m jawafdehi_mcp.server
```

Run MCP tests:

```bash
DJANGO_SETTINGS_MODULE=config.settings_test TESTING=true \
  uv run pytest -q tests/mcp
```

The production image starts `config.asgi:application` with
`config.asgi_worker.BoundedUvicornWorker`. There is no standalone MCP HTTP
command; the project does not install a separate MCP CLI entry point.

The migrated MCP component retains its Hippocratic License 3.0 terms in
[`LICENSE`](../../jawafdehi_mcp/LICENSE).
