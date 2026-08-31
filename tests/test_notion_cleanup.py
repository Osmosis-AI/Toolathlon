import asyncio
import importlib.util
import sys
import types
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import requests

from utils.app_specific.notion import notion_remove_page


PAGE_ID_COMPACT = "279c110d7fa180da9103e87ec667dd77"
PAGE_ID_DASHED = "279c110d-7fa1-80da-9103-e87ec667dd77"


def response(status_code, payload=None):
    result = Mock(status_code=status_code)
    result.json.return_value = payload or {}
    if not 200 <= status_code < 300:
        result.raise_for_status.side_effect = requests.HTTPError(str(status_code))
    return result


def load_coordinator():
    token_module = types.ModuleType("configs.token_key_session")
    token_module.all_token_key_session = SimpleNamespace(
        source_notion_page_url="https://notion.example/source",
        eval_notion_page_url="https://notion.example/eval",
        notion_integration_key="source-token",
        notion_integration_key_eval="eval-token",
    )
    helper_module = types.ModuleType("utils.general.helper")
    helper_module.run_command = AsyncMock()
    helper_module.print_color = Mock()
    module_path = (
        Path(__file__).parents[1]
        / "utils/app_specific/notion/notion_remove_and_duplicate.py"
    )
    spec = importlib.util.spec_from_file_location(
        "notion_remove_and_duplicate_under_test", module_path
    )
    module = importlib.util.module_from_spec(spec)
    with patch.dict(
        sys.modules,
        {
            "configs.token_key_session": token_module,
            "utils.general.helper": helper_module,
        },
    ):
        spec.loader.exec_module(module)
    return module


class PageIdParsingTests(unittest.TestCase):
    def test_compact_url(self):
        self.assertEqual(
            notion_remove_page.get_page_id_from_url(
                f"https://www.notion.so/{PAGE_ID_COMPACT}"
            ),
            PAGE_ID_DASHED,
        )

    def test_titled_compact_url(self):
        self.assertEqual(
            notion_remove_page.get_page_id_from_url(
                f"https://www.notion.so/Movies-{PAGE_ID_COMPACT}?v=abc"
            ),
            PAGE_ID_DASHED,
        )

    def test_dashed_url(self):
        self.assertEqual(
            notion_remove_page.get_page_id_from_url(
                f"https://www.notion.so/Movies-{PAGE_ID_DASHED}"
            ),
            PAGE_ID_DASHED,
        )

    def test_bare_id(self):
        self.assertEqual(
            notion_remove_page.get_page_id_from_url(PAGE_ID_COMPACT),
            PAGE_ID_DASHED,
        )

    def test_bare_dashed_id(self):
        self.assertEqual(
            notion_remove_page.get_page_id_from_url(PAGE_ID_DASHED),
            PAGE_ID_DASHED,
        )

    def test_invalid_url_raises(self):
        with self.assertRaises(ValueError):
            notion_remove_page.get_page_id_from_url(
                "https://www.notion.so/not-a-page-id"
            )


class PageRemovalTests(unittest.TestCase):
    def test_get_children_non_2xx_raises(self):
        for status_code in (302, 400):
            with self.subTest(status_code=status_code):
                with patch.object(
                    notion_remove_page.requests,
                    "get",
                    return_value=response(status_code),
                ) as get:
                    with self.assertRaises(requests.HTTPError):
                        notion_remove_page.get_child_pages(PAGE_ID_DASHED, {})
                    self.assertFalse(get.call_args.kwargs["allow_redirects"])

    def test_empty_children_response_is_valid(self):
        with patch.object(
            notion_remove_page.requests,
            "get",
            return_value=response(200, {"results": [], "has_more": False}),
        ):
            self.assertEqual(
                notion_remove_page.delete_pages_by_title(
                    PAGE_ID_DASHED, "Movies", {}, dry_run=True
                ),
                0,
            )

    def test_all_duplicate_named_children_are_deleted(self):
        children = {
            "results": [
                {"id": "page-1", "type": "child_page", "child_page": {"title": "Movies"}},
                {"id": "other", "type": "child_page", "child_page": {"title": "Other"}},
                {"id": "page-2", "type": "child_page", "child_page": {"title": "Movies"}},
            ],
            "has_more": False,
        }
        with (
            patch.object(
                notion_remove_page.requests, "get", return_value=response(200, children)
            ),
            patch.object(
                notion_remove_page.requests,
                "delete",
                side_effect=[response(200), response(200)],
            ) as delete,
            patch("builtins.input", return_value="yes"),
        ):
            deleted = notion_remove_page.delete_pages_by_title(
                PAGE_ID_DASHED, "Movies", {}
            )

        self.assertEqual(deleted, 2)
        self.assertEqual(
            [call.args[0] for call in delete.call_args_list],
            [
                "https://api.notion.com/v1/blocks/page-1",
                "https://api.notion.com/v1/blocks/page-2",
            ],
        )
        for call in delete.call_args_list:
            self.assertFalse(call.kwargs["allow_redirects"])

    def test_matching_children_across_pages_are_all_deleted(self):
        first_page = {
            "results": [
                {"id": "page-1", "type": "child_page", "child_page": {"title": "Movies"}}
            ],
            "has_more": True,
            "next_cursor": "next-page",
        }
        second_page = {
            "results": [
                {"id": "page-2", "type": "child_page", "child_page": {"title": "Movies"}}
            ],
            "has_more": False,
        }
        with (
            patch.object(
                notion_remove_page.requests,
                "get",
                side_effect=[response(200, first_page), response(200, second_page)],
            ) as get,
            patch.object(
                notion_remove_page.requests,
                "delete",
                side_effect=[response(200), response(200)],
            ) as delete,
            patch("builtins.input", return_value="yes"),
        ):
            deleted = notion_remove_page.delete_pages_by_title(
                PAGE_ID_DASHED, "Movies", {}
            )

        self.assertEqual(deleted, 2)
        self.assertEqual(get.call_args_list[1].kwargs["params"]["start_cursor"], "next-page")
        self.assertEqual(delete.call_count, 2)

    def test_delete_http_error_raises(self):
        children = {
            "results": [
                {"id": "page-1", "type": "child_page", "child_page": {"title": "Movies"}}
            ],
            "has_more": False,
        }
        with (
            patch.object(
                notion_remove_page.requests, "get", return_value=response(200, children)
            ),
            patch.object(
                notion_remove_page.requests, "delete", return_value=response(500)
            ),
            patch("builtins.input", return_value="yes"),
        ):
            with self.assertRaises(requests.HTTPError):
                notion_remove_page.delete_pages_by_title(
                    PAGE_ID_DASHED, "Movies", {}
                )


class CoordinatorTests(unittest.TestCase):
    def run_main(self, module):
        with patch.object(
            sys,
            "argv",
            [
                "notion_remove_and_duplicate",
                "--duplicated_page_id_file",
                "/tmp/page-id",
                "--needed_subpage_name",
                "Movies",
            ],
        ):
            return asyncio.run(module.main())

    def test_cleanup_failure_aborts_before_duplicate(self):
        module = load_coordinator()
        module.run_command = AsyncMock(return_value=("", "cleanup failed", 1))

        with self.assertRaisesRegex(RuntimeError, "Removing old Notion page failed"):
            self.run_main(module)

        module.run_command.assert_awaited_once()
        self.assertFalse(module.run_command.await_args.kwargs["debug"])

    def test_duplicate_failure_raises(self):
        module = load_coordinator()
        module.run_command = AsyncMock(
            side_effect=[("", "", 0), ("", "duplicate failed", 2)]
        )

        with self.assertRaisesRegex(RuntimeError, "Duplicating Notion page failed"):
            self.run_main(module)

        self.assertEqual(module.run_command.await_count, 2)
        for call in module.run_command.await_args_list:
            self.assertFalse(call.kwargs["debug"])


if __name__ == "__main__":
    unittest.main()
