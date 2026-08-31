import sys
import os
from argparse import ArgumentParser
import asyncio
import shlex

# Add utils to path
sys.path.append(os.path.dirname(__file__))

from configs.token_key_session import all_token_key_session
# from utils.app_specific.notion_page_duplicator import NotionPageDuplicator
from utils.general.helper import run_command, print_color


async def main():
    parser = ArgumentParser(description="Example code for notion tasks preprocess")
    parser.add_argument("--duplicated_page_id_file", required=True, 
                       help="Duplicated page id file")
    parser.add_argument("--needed_subpage_name", required=True, 
                       help="Needed subpage name")
    args = parser.parse_args()

    notion_source_page_url = all_token_key_session.source_notion_page_url
    notion_eval_page_url = all_token_key_session.eval_notion_page_url
    notion_integration_key = all_token_key_session.notion_integration_key
    notion_integration_key_eval_only = all_token_key_session.notion_integration_key_eval

    print_color(f"Removing old page {args.needed_subpage_name} from {notion_eval_page_url}","cyan")
    _, _, cleanup_return_code = await run_command(
        f"uv run -m utils.app_specific.notion.notion_remove_page "
        f"--url {shlex.quote(notion_eval_page_url)} "
        f"--name {shlex.quote(args.needed_subpage_name)} "
        f"--token {shlex.quote(notion_integration_key_eval_only)} "
        f"--no-confirm",
        debug=False,
        show_output=True
    )
    if cleanup_return_code != 0:
        raise RuntimeError(
            f"Removing old Notion page failed with exit code {cleanup_return_code}"
        )

    print_color(f"Duplicating new page {args.needed_subpage_name} from {notion_source_page_url} to {notion_eval_page_url}","cyan")
    _, _, duplicate_return_code = await run_command(
        f"uv run -m utils.app_specific.notion.notion_page_duplicator "
        f"--source-parent {shlex.quote(notion_source_page_url)} "
        f"--child-name {shlex.quote(args.needed_subpage_name)} "
        f"--target-parent {shlex.quote(notion_eval_page_url)} "
        f"--notion-key {shlex.quote(notion_integration_key)} "
        f"--output-file {shlex.quote(args.duplicated_page_id_file)}",
        debug=False,
        show_output=True
    )
    if duplicate_return_code != 0:
        raise RuntimeError(f"Duplicating Notion page failed with exit code {duplicate_return_code}")
    # print("Preprocess done!")

if __name__ == "__main__":
    asyncio.run(main())
