"""Regression tests for MCP tool input schemas."""

from jsonschema import Draft202012Validator

from jawafdehi_mcp.server import TOOLS


def test_all_tool_schemas_are_valid_draft_2020_12():
    for tool in TOOLS:
        Draft202012Validator.check_schema(tool.input_schema)


def test_all_tool_schemas_have_top_level_object_type():
    for tool in TOOLS:
        schema = tool.input_schema
        assert isinstance(schema, dict), f"{tool.name} schema must be a dict"
        assert schema.get("type") == "object", (
            f"{tool.name} schema must declare top-level type=object"
        )


def test_empty_properties_object_schemas_have_required_list():
    for tool in TOOLS:
        schema = tool.input_schema
        if schema.get("type") != "object":
            continue

        properties = schema.get("properties")
        if properties == {}:
            assert "required" in schema, (
                f"{tool.name} empty-properties schema must include required"
            )
            assert schema["required"] == [], (
                f"{tool.name} empty-properties schema must set required to []"
            )
