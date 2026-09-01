import os
import tempfile
import unittest
from unittest.mock import Mock, patch

from check_remote import (
    _collect_paginated_results,
    _get_attributed_job_finder_page,
    get_database_entries,
    get_notion_page_blocks,
    get_notion_workspace_pages,
)


EVAL_PAGE_ID = 'aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa'
TASK_PAGE_ID = 'bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb'
FOREIGN_PAGE_ID = 'cccccccc-cccc-cccc-cccc-cccccccccccc'


def _response(payload):
    response = Mock()
    response.raise_for_status.return_value = None
    response.json.return_value = payload
    return response


def _page(title, parent_id=None):
    page = {
        'properties': {
            'title': {
                'type': 'title',
                'title': [{'plain_text': title}],
            }
        }
    }
    if parent_id is not None:
        page['parent'] = {'type': 'page_id', 'page_id': parent_id}
    return page


class SlotAttributionTests(unittest.TestCase):
    def _task_root(self, page_id=TASK_PAGE_ID):
        temp_dir = tempfile.TemporaryDirectory()
        files_dir = os.path.join(temp_dir.name, 'files')
        os.makedirs(files_dir)
        with open(
            os.path.join(files_dir, 'duplicated_page_id.txt'),
            'w',
            encoding='utf-8',
        ) as page_id_file:
            page_id_file.write(page_id)
        self.addCleanup(temp_dir.cleanup)
        return temp_dir.name

    @patch('check_remote.find_page_by_title')
    @patch('check_remote.get_notion_page_properties')
    def test_uses_task_local_page_even_if_foreign_match_would_be_first(
        self, get_page, find_page
    ):
        find_page.return_value = [
            {'id': 'foreign-job-finder', 'title': 'Job Finder'},
            {'id': TASK_PAGE_ID, 'title': 'Job Finder'},
        ]
        pages_by_id = {
            TASK_PAGE_ID: _page('Job Finder', EVAL_PAGE_ID),
            EVAL_PAGE_ID: _page('Notion Eval Page'),
        }
        get_page.side_effect = lambda page_id, _token: pages_by_id[page_id]

        page = _get_attributed_job_finder_page(
            self._task_root(), 'token', EVAL_PAGE_ID
        )

        self.assertEqual(page, {'id': TASK_PAGE_ID, 'title': 'Job Finder'})
        self.assertEqual(
            [call.args[0] for call in get_page.call_args_list],
            [TASK_PAGE_ID, EVAL_PAGE_ID],
        )
        find_page.assert_not_called()

    @patch('check_remote.get_notion_page_properties')
    def test_parent_id_mismatch_fails_closed(self, get_page):
        get_page.return_value = _page('Job Finder', FOREIGN_PAGE_ID)

        with self.assertRaisesRegex(ValueError, 'parent does not match'):
            _get_attributed_job_finder_page(
                self._task_root(), 'token', EVAL_PAGE_ID
            )

        get_page.assert_called_once_with(TASK_PAGE_ID, 'token')

    @patch('check_remote.get_notion_page_properties')
    def test_parent_title_mismatch_fails_closed(self, get_page):
        get_page.side_effect = [
            _page('Job Finder', EVAL_PAGE_ID),
            _page('Benchmark Worker 1'),
        ]

        with self.assertRaisesRegex(ValueError, 'title is not exactly'):
            _get_attributed_job_finder_page(
                self._task_root(), 'token', EVAL_PAGE_ID
            )

    @patch('check_remote.get_notion_page_properties')
    def test_task_page_title_mismatch_fails_closed(self, get_page):
        get_page.return_value = _page('Job Finder Copy', EVAL_PAGE_ID)

        with self.assertRaisesRegex(ValueError, "not exactly 'Job Finder'"):
            _get_attributed_job_finder_page(
                self._task_root(), 'token', EVAL_PAGE_ID
            )

    def test_missing_task_local_page_id_fails_closed(self):
        temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(temp_dir.cleanup)

        with self.assertRaisesRegex(ValueError, 'file is unavailable'):
            _get_attributed_job_finder_page(
                temp_dir.name, 'token', EVAL_PAGE_ID
            )

    def test_malformed_task_local_page_id_fails_closed(self):
        for malformed_id in (
            '',
            'not-a-page-id',
            'https://www.notion.so/' + EVAL_PAGE_ID.replace('-', ''),
        ):
            with self.subTest(malformed_id=malformed_id):
                with self.assertRaisesRegex(ValueError, 'page ID is malformed'):
                    _get_attributed_job_finder_page(
                        self._task_root(malformed_id), 'token', EVAL_PAGE_ID
                    )


class PaginationTests(unittest.TestCase):
    @patch('check_remote.requests.post')
    def test_workspace_search_collects_pages_after_first_hundred(self, post):
        first_hundred = [{'id': f'page-{index}'} for index in range(100)]
        job_finder = {'id': 'job-finder', 'title': 'Job Finder'}
        post.side_effect = [
            _response(
                {
                    'results': first_hundred,
                    'has_more': True,
                    'next_cursor': 'search-cursor',
                }
            ),
            _response(
                {
                    'results': [job_finder],
                    'has_more': False,
                    'next_cursor': None,
                }
            ),
        ]

        result = get_notion_workspace_pages('token', 'Job Finder')

        self.assertEqual(len(result['results']), 101)
        self.assertEqual(result['results'][-1], job_finder)
        self.assertEqual(post.call_args_list[0].kwargs['json']['page_size'], 100)
        self.assertEqual(
            post.call_args_list[0].kwargs['json']['query'],
            'Job Finder',
        )
        self.assertNotIn('start_cursor', post.call_args_list[0].kwargs['json'])
        self.assertEqual(
            post.call_args_list[1].kwargs['json']['start_cursor'],
            'search-cursor',
        )

    @patch('check_remote.requests.get')
    def test_block_children_pagination_uses_query_cursor(self, get):
        get.side_effect = [
            _response(
                {
                    'results': [{'id': 'block-1'}],
                    'has_more': True,
                    'next_cursor': 'block-cursor',
                }
            ),
            _response(
                {
                    'results': [{'id': 'block-2'}],
                    'has_more': False,
                    'next_cursor': None,
                }
            ),
        ]

        result = get_notion_page_blocks('page-id', 'token')

        self.assertEqual(
            [block['id'] for block in result['results']],
            ['block-1', 'block-2'],
        )
        self.assertEqual(
            get.call_args_list[1].kwargs['params']['start_cursor'],
            'block-cursor',
        )

    @patch('check_remote.requests.post')
    def test_database_query_pagination_uses_body_cursor(self, post):
        post.side_effect = [
            _response(
                {
                    'results': [{'id': 'job-1'}],
                    'has_more': True,
                    'next_cursor': 'database-cursor',
                }
            ),
            _response(
                {
                    'results': [{'id': 'job-2'}],
                    'has_more': False,
                    'next_cursor': None,
                }
            ),
        ]

        result = get_database_entries('database-id', 'token')

        self.assertEqual(
            [job['id'] for job in result['results']],
            ['job-1', 'job-2'],
        )
        self.assertEqual(
            post.call_args_list[1].kwargs['json']['start_cursor'],
            'database-cursor',
        )

    def test_missing_cursor_is_rejected(self):
        with self.assertRaisesRegex(ValueError, 'without next_cursor'):
            _collect_paginated_results(
                lambda _cursor: {'results': [], 'has_more': True},
                'test resource',
            )

    def test_repeated_cursor_is_rejected(self):
        with self.assertRaisesRegex(ValueError, 'repeated next_cursor'):
            _collect_paginated_results(
                lambda _cursor: {
                    'results': [],
                    'has_more': True,
                    'next_cursor': 'same-cursor',
                },
                'test resource',
            )


if __name__ == '__main__':
    unittest.main()
