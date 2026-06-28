import unittest

from agents._run_impl import ResponseFunctionToolCall

from utils.openai_agents_monkey_patch.custom_mcp_util import coerce_mcp_arguments
from utils.openai_agents_monkey_patch.custom_run_impl import (
    _canonicalize_tool_call,
    _resolve_registered_tool_name,
)


class ToolNameResolverTests(unittest.TestCase):
    def test_exact_name_wins(self):
        self.assertEqual(
            _resolve_registered_tool_name(
                "notion_API_post_search",
                ["notion-API-post-search", "notion_API_post_search"],
            ),
            "notion_API_post_search",
        )

    def test_unique_underscore_alias_resolves_hyphenated_tool(self):
        self.assertEqual(
            _resolve_registered_tool_name(
                "notion_API_post_search",
                ["notion-API-post-search"],
            ),
            "notion-API-post-search",
        )

    def test_ambiguous_alias_is_rejected(self):
        self.assertIsNone(
            _resolve_registered_tool_name(
                "service_a_b",
                ["service-a_b", "service_a-b"],
            )
        )

    def test_tool_call_is_copied_with_canonical_registered_name(self):
        tool_call = ResponseFunctionToolCall(
            arguments='{}',
            call_id='call-id',
            name='local_claim_done',
            type='function_call',
        )

        canonical = _canonicalize_tool_call(tool_call, ['local-claim_done'])

        self.assertEqual(canonical.name, 'local-claim_done')
        self.assertEqual(tool_call.name, 'local_claim_done')

    def test_alias_collision_between_handoff_and_function_is_not_rewritten(self):
        tool_call = ResponseFunctionToolCall(
            arguments='{}',
            call_id='call-id',
            name='service_a_b',
            type='function_call',
        )

        canonical = _canonicalize_tool_call(
            tool_call,
            ['service-a_b', 'service_a-b'],
        )

        self.assertIs(canonical, tool_call)


class MCPArgumentCoercionTests(unittest.TestCase):
    def setUp(self):
        self.schema = {
            "type": "object",
            "properties": {
                "page_id": {"type": "string"},
                "properties": {"type": "object", "additionalProperties": True},
                "filter": {"type": "object"},
                "filter_properties": {
                    "type": "array",
                    "items": {"type": "string"},
                },
                "page_size": {"type": "integer"},
                "enabled": {"type": "boolean"},
            },
        }

    def test_notion_container_and_scalar_arguments_are_repaired(self):
        arguments = {
            "page_id": "1234",
            "properties": '{"Status":{"select":{"name":"Applied"}}}',
            "filter": '{"property":"Status","select":{"equals":"Checking"}}',
            "filter_properties": '["title","status"]',
            "page_size": "30",
            "enabled": "false",
        }

        self.assertEqual(
            coerce_mcp_arguments(arguments, self.schema),
            {
                "page_id": "1234",
                "properties": {"Status": {"select": {"name": "Applied"}}},
                "filter": {
                    "property": "Status",
                    "select": {"equals": "Checking"},
                },
                "filter_properties": ["title", "status"],
                "page_size": 30,
                "enabled": False,
            },
        )

    def test_string_fields_and_malformed_json_are_unchanged(self):
        arguments = {
            "page_id": '{"still":"a string"}',
            "filter": "{malformed",
            "page_size": "thirty",
        }

        self.assertEqual(coerce_mcp_arguments(arguments, self.schema), arguments)

    def test_schema_that_allows_string_is_not_coerced(self):
        schema = {
            "type": "object",
            "properties": {
                "value": {
                    "anyOf": [
                        {"type": "object"},
                        {"type": "string"},
                    ]
                }
            },
        }
        arguments = {"value": '{"keep":"string"}'}

        self.assertEqual(coerce_mcp_arguments(arguments, schema), arguments)

    def test_already_typed_values_are_preserved(self):
        arguments = {
            "properties": {"Status": {"select": {"name": "Applied"}}},
            "filter_properties": ["title"],
            "page_size": 20,
            "enabled": True,
        }

        self.assertEqual(coerce_mcp_arguments(arguments, self.schema), arguments)


if __name__ == "__main__":
    unittest.main()
