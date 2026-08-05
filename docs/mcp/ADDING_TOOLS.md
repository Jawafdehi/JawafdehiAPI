# Adding MCP Tools

Tools live in `jawafdehi_mcp/tools/` and subclass `BaseTool`.

1. Add the tool module and implement `name`, `description`, `input_schema`, and
   async `execute`.
2. Export the class from `jawafdehi_mcp/tools/__init__.py`.
3. Add an instance to `TOOLS` in `jawafdehi_mcp/server.py`.
4. Add focused tests under `tests/mcp/`.

For a normal Jawafdehi control-plane endpoint, use the shared fixed-path client:

```python
from jawafdehi_mcp.control_plane import request_control_plane

result = await request_control_plane(
    "POST",
    "/api/example/",
    json_body={"value": 1},
)
```

The client accepts only `/api/*` paths, forwards request auth, returns a stable
status/data envelope, uses the bounded in-memory Django transport for embedded
HTTP, and uses normal network HTTP for stdio. API permission checks remain the
authorization boundary. Use raw `embedded_api_client_kwargs()` only for a
protocol that the shared JSON client cannot represent, such as multipart file
upload.
