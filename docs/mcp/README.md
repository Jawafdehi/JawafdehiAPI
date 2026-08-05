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
are routed to MCP. `X-MCP-Mode` selects the authentication door after routing;
it cannot turn another Django path into an MCP endpoint.

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
`OIDC_JWKS_TIMEOUT` bound. A trusted ingress may set:

- `X-MCP-Mode: public`: anonymous callers receive the restricted public set.
- `X-MCP-Mode: internal`: anonymous callers receive an OAuth challenge.

`MCP_DEFAULT_MODE=public` is the safe fallback when the ingress header is
absent. A header may raise that default to `internal`; an
`MCP_DEFAULT_MODE=internal` deployment cannot be downgraded by a request
header. Invalid values fail to the deployment's safe floor.

MCP rejects unrecognized `Host` and `Origin` headers. The host and origin from
`OIDC_RESOURCE` plus local loopback hosts are trusted automatically. A
deployment with additional public/internal DNS names must list them in
`MCP_ALLOWED_HOSTS` and browser origins in `MCP_ALLOWED_ORIGINS`.

Any verified bearer receives the full MCP tool catalog, independent of its
roles. Catalog visibility is not an authorization grant: tools forward that
bearer to Django, whose API permissions remain the final boundary for reads and
writes. Anonymous callers continue to receive only the catalog selected by the
request mode.

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
