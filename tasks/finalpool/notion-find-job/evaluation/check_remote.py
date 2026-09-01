import requests
import sys
import os
from typing import Callable, Dict, List, Tuple, Optional


def _normalize_page_id(value: str) -> str:
    """Return a canonical Notion page ID, rejecting non-ID input."""
    if not isinstance(value, str):
        raise ValueError("Notion page ID is not a string")

    compact = value.strip().replace('-', '').lower()
    if len(compact) != 32 or any(
        character not in '0123456789abcdef' for character in compact
    ):
        raise ValueError("Notion page ID is malformed")

    return (
        f"{compact[:8]}-{compact[8:12]}-{compact[12:16]}-"
        f"{compact[16:20]}-{compact[20:]}"
    )


def _get_page_title(page: Dict) -> str:
    """Extract a normal Notion page title from an API page response."""
    properties = page.get('properties', {})
    if not isinstance(properties, dict):
        return ''

    for prop in properties.values():
        if not isinstance(prop, dict) or prop.get('type') != 'title':
            continue
        title_parts = prop.get('title', [])
        if not isinstance(title_parts, list):
            return ''
        return ''.join(
            part.get('plain_text', part.get('text', {}).get('content', ''))
            for part in title_parts
            if isinstance(part, dict)
        )
    return ''


def _load_duplicated_page_id(task_root: str) -> str:
    page_id_path = os.path.join(task_root, 'files', 'duplicated_page_id.txt')
    try:
        with open(page_id_path, 'r', encoding='utf-8') as page_id_file:
            return _normalize_page_id(page_id_file.read())
    except OSError as exc:
        raise ValueError("Duplicated Notion page ID file is unavailable") from exc


def _get_attributed_job_finder_page(
    task_root: str, notion_token: str, eval_page_id: str
) -> Dict:
    """Retrieve and validate the task-local Job Finder page and its parent."""
    task_page_id = _load_duplicated_page_id(task_root)
    expected_parent_id = _normalize_page_id(eval_page_id)

    task_page = get_notion_page_properties(task_page_id, notion_token)
    if _get_page_title(task_page) != 'Job Finder':
        raise ValueError("Duplicated Notion page title is not exactly 'Job Finder'")

    parent = task_page.get('parent', {})
    if not isinstance(parent, dict) or parent.get('type') != 'page_id':
        raise ValueError("Duplicated Notion page does not have a page parent")

    try:
        actual_parent_id = _normalize_page_id(parent.get('page_id'))
    except ValueError as exc:
        raise ValueError("Duplicated Notion page parent ID is malformed") from exc
    if actual_parent_id != expected_parent_id:
        raise ValueError("Duplicated Notion page parent does not match the configured eval page")

    parent_page = get_notion_page_properties(expected_parent_id, notion_token)
    if _get_page_title(parent_page) != 'Notion Eval Page':
        raise ValueError("Configured Notion eval page title is not exactly 'Notion Eval Page'")

    return {'id': task_page_id, 'title': 'Job Finder'}


def _collect_paginated_results(
    fetch_page: Callable[[Optional[str]], Dict], resource_name: str
) -> Dict:
    """Collect a complete Notion list response and guard cursor loops."""
    all_results = []
    cursor = None
    seen_cursors = set()
    latest_page = {}

    while True:
        page = fetch_page(cursor)
        if not isinstance(page, dict):
            raise ValueError(f"Notion {resource_name} response is not an object")

        page_results = page.get('results', [])
        if not isinstance(page_results, list):
            raise ValueError(f"Notion {resource_name} results is not a list")
        all_results.extend(page_results)
        latest_page = page

        if not page.get('has_more', False):
            break

        next_cursor = page.get('next_cursor')
        if not next_cursor:
            raise ValueError(
                f"Notion {resource_name} response has_more=True without next_cursor"
            )
        if next_cursor in seen_cursors:
            raise ValueError(
                f"Notion {resource_name} repeated next_cursor: {next_cursor}"
            )
        seen_cursors.add(next_cursor)
        cursor = next_cursor

    return {
        **latest_page,
        'results': all_results,
        'has_more': False,
        'next_cursor': None,
    }

def get_notion_workspace_pages(token, query=None):
    """Get all pages in Notion workspace"""
    url = "https://api.notion.com/v1/search"
    headers = {
        "Authorization": f"Bearer {token}",
        "Notion-Version": "2022-06-28",
        "Content-Type": "application/json"
    }
    
    # Search all pages
    base_payload = {
        "filter": {
            "value": "page",
            "property": "object"
        },
        "sort": {
            "direction": "descending",
            "timestamp": "last_edited_time"
        },
        "page_size": 100,
    }
    if query:
        base_payload['query'] = query

    def fetch_page(cursor):
        payload = dict(base_payload)
        if cursor:
            payload['start_cursor'] = cursor
        response = requests.post(url, headers=headers, json=payload)
        response.raise_for_status()
        return response.json()

    try:
        return _collect_paginated_results(fetch_page, "workspace search")
    except requests.exceptions.RequestException as e:
        raise Exception(f"Failed to get workspace pages: {e}")

def get_notion_page_properties(page_id, token):
    """Get page properties from Notion page"""
    url = f"https://api.notion.com/v1/pages/{page_id}"
    headers = {
        "Authorization": f"Bearer {token}",
        "Notion-Version": "2022-06-28",
        "Content-Type": "application/json"
    }
    
    try:
        response = requests.get(url, headers=headers)
        response.encoding = 'utf-8'
        response.raise_for_status()
        return response.json()
    except requests.exceptions.RequestException as e:
        raise Exception(f"Failed to get Notion page properties: {e}")

def find_page_by_title(token, target_title, partial_match=True):
    """Find page by title"""
    try:
        pages_data = get_notion_workspace_pages(token, target_title)
        matching_pages = []
        print(f"----- Searching for page: '{target_title}' (partial_match={partial_match}) -----")
        print(f"Found {len(pages_data.get('results', []))} total pages")
        
        for page in pages_data.get('results', []):
            page_title = ""
            
            # Get page title
            if 'properties' in page and 'title' in page['properties']:
                title_prop = page['properties']['title']
                if title_prop['type'] == 'title':
                    title_parts = title_prop['title']
                    page_title = ''.join([part.get('text', {}).get('content', '') for part in title_parts])
            
            # Print each page for debugging
            print(f"Checking page: '{page_title}' (ID: {page['id']})")
            
            # Check title match
            if partial_match:
                if target_title.lower() in page_title.lower():
                    print(f"✅ MATCH FOUND: '{page_title}'")
                    matching_pages.append({
                        'id': page['id'],
                        'title': page_title,
                        'url': page.get('url', ''),
                        'last_edited_time': page.get('last_edited_time', '')
                    })
            else:
                if target_title.lower() == page_title.lower():
                    print(f"✅ EXACT MATCH FOUND: '{page_title}'")
                    matching_pages.append({
                        'id': page['id'],
                        'title': page_title,
                        'url': page.get('url', ''),
                        'last_edited_time': page.get('last_edited_time', '')
                    })
        
        print(f"----- Search completed. Found {len(matching_pages)} matching pages -----")
        return matching_pages
    except Exception as e:
        raise Exception(f"Failed to find page: {e}")

def get_notion_page_blocks(page_id, token):
    """Get all block content from Notion page"""
    url = f"https://api.notion.com/v1/blocks/{page_id}/children"
    headers = {
        "Authorization": f"Bearer {token}",
        "Notion-Version": "2022-06-28",
        "Content-Type": "application/json"
    }
    
    def fetch_page(cursor):
        params = {'page_size': 100}
        if cursor:
            params['start_cursor'] = cursor
        response = requests.get(url, headers=headers, params=params)
        response.raise_for_status()
        return response.json()

    try:
        return _collect_paginated_results(fetch_page, f"block children for {page_id}")
    except requests.exceptions.RequestException as e:
        raise Exception(f"Failed to get page blocks: {e}")

def get_database_details(database_id, token):
    """Get database details including title"""
    url = f"https://api.notion.com/v1/databases/{database_id}"
    headers = {
        "Authorization": f"Bearer {token}",
        "Notion-Version": "2022-06-28",
        "Content-Type": "application/json"
    }
    
    try:
        response = requests.get(url, headers=headers)
        response.raise_for_status()
        return response.json()
    except requests.exceptions.RequestException as e:
        raise Exception(f"Failed to get database details: {e}")

def find_database_in_page(page_id, token, target_db_title):
    """Find database within a specific page"""
    try:
        # Get page blocks
        blocks_data = get_notion_page_blocks(page_id, token)
        
        print(f"Searching for '{target_db_title}' database in page blocks...")
        print(f"Found {len(blocks_data.get('results', []))} blocks in the page")
        
        def search_blocks_recursively(blocks, level=0):
            """Recursively search through blocks and their children"""
            indent = "  " * level
            
            for block in blocks:
                block_type = block.get('type', '')
                block_id = block.get('id', '')
                print(f"{indent}Checking block type: {block_type}, ID: {block_id}")
                
                # Check if this block is a child database
                if block_type == 'child_database':
                    # Get database title
                    db_title = block.get('child_database', {}).get('title', '')
                    print(f"{indent}Found child database: '{db_title}'")
                    
                    if target_db_title.lower() in db_title.lower():
                        print(f"{indent}✅ Found matching database: '{db_title}' in page")
                        return {
                            'id': block_id,
                            'title': db_title,
                            'type': 'child_database'
                        }
                
                # Also check if block itself is a database (inline databases)
                elif block_type == 'database':
                    try:
                        # Query the database to get its title
                        db_details = get_database_details(block_id, token)
                        db_title = ''.join([part.get('text', {}).get('content', '') for part in db_details.get('title', [])])
                        print(f"{indent}Found inline database: '{db_title}'")
                        
                        if target_db_title.lower() in db_title.lower():
                            print(f"{indent}✅ Found matching inline database: '{db_title}' in page")
                            return {
                                'id': block_id,
                                'title': db_title,
                                'type': 'inline_database'
                            }
                    except Exception as e:
                        print(f"{indent}Error getting database details for {block_id}: {e}")
                
                # For container blocks, recursively check their children
                elif block_type in ['column_list', 'column', 'table', 'table_row', 'toggle', 'callout', 'quote']:
                    print(f"{indent}Searching children of {block_type} block...")
                    try:
                        # Get children blocks of this container
                        children_data = get_notion_page_blocks(block_id, token)
                        children_blocks = children_data.get('results', [])
                        if children_blocks:
                            print(f"{indent}Found {len(children_blocks)} children in {block_type}")
                            result = search_blocks_recursively(children_blocks, level + 1)
                            if result:
                                return result
                        else:
                            print(f"{indent}No children found in {block_type}")
                    except Exception as e:
                        print(f"{indent}Error getting children of {block_type}: {e}")
            
            return None
        
        # Start recursive search
        result = search_blocks_recursively(blocks_data.get('results', []))
        
        if result:
            return result
        else:
            print(f"No '{target_db_title}' database found in this page")
            return None
        
    except Exception as e:
        print(f"Error searching for database in page: {e}")
        return None

def get_database_entries(database_id, token):
    """Get all entries from a database"""
    url = f"https://api.notion.com/v1/databases/{database_id}/query"
    headers = {
        "Authorization": f"Bearer {token}",
        "Notion-Version": "2022-06-28",
        "Content-Type": "application/json"
    }
    
    def fetch_page(cursor):
        payload = {'page_size': 100}
        if cursor:
            payload['start_cursor'] = cursor
        response = requests.post(url, headers=headers, json=payload)
        response.raise_for_status()
        return response.json()

    try:
        return _collect_paginated_results(
            fetch_page, f"database entries for {database_id}"
        )
    except requests.exceptions.RequestException as e:
        raise Exception(f"Failed to get database entries: {e}")

def extract_job_information_from_database(database_entries: List[Dict]) -> List[Dict]:
    """Extract job application information from database entries"""
    jobs = []
    
    for entry in database_entries:
        job_info = {
            'company': '',
            'position': '',
            'location': '',
            'flexibility': '',
            'status': '',
            'salary_range': '',
            'interview_date': '',
            'connect_email': ''
        }
        
        # Extract properties
        properties = entry.get('properties', {})
        
        for prop_name, prop_data in properties.items():
            prop_type = prop_data.get('type', '')
            
            if prop_type == 'title':
                # Usually the company name
                title_parts = prop_data.get('title', [])
                text = ''.join([part.get('text', {}).get('content', '') for part in title_parts])
                if 'company' in prop_name.lower() or prop_name.lower() in ['title']:
                    job_info['company'] = text.strip()
            
            elif prop_type == 'rich_text':
                rich_text = prop_data.get('rich_text', [])
                text = ''.join([part.get('text', {}).get('content', '') for part in rich_text])
                text = text.strip()
                
                if 'position' in prop_name.lower():
                    job_info['position'] = text
                elif 'location' in prop_name.lower():
                    job_info['location'] = text
                elif 'salary' in prop_name.lower():
                    job_info['salary_range'] = text
                elif 'email' in prop_name.lower():
                    job_info['connect_email'] = text
            
            elif prop_type == 'select':
                select_value = prop_data.get('select', {})
                if select_value:
                    text = select_value.get('name', '').strip()
                    if 'status' in prop_name.lower():
                        job_info['status'] = text
                    elif 'flexibility' in prop_name.lower():
                        job_info['flexibility'] = text
            
            elif prop_type == 'email':
                email_value = prop_data.get('email', '').strip()
                if email_value:
                    job_info['connect_email'] = email_value
                    
            elif prop_type == 'date':
                date_value = prop_data.get('date', {})
                if date_value and date_value.get('start'):
                    if 'interview' in prop_name.lower():
                        job_info['interview_date'] = date_value.get('start', '')
        
        # Only add if we have essential information
        if job_info['company']:
            jobs.append(job_info)
    
    return jobs

def check_job_applications_status(jobs: List[Dict]) -> Tuple[bool, List[str]]:
    """Check if HCD and AHC job applications have status 'Applied'"""
    # Expected companies that should have 'Applied' status
    expected_companies = ['HCD', 'AHC']
    
    issues = []
    
    # Find HCD and AHC entries
    found_companies = {}
    for job in jobs:
        company_name = job.get('company', '').strip()
        if company_name in expected_companies:
            found_companies[company_name] = job
    
    # Check if both companies are found
    for expected_company in expected_companies:
        if expected_company not in found_companies:
            issues.append(f"Company '{expected_company}' not found in Job Tracker database")
        else:
            job = found_companies[expected_company]
            status = job.get('status', '').strip()
            if status.lower() != 'applied':
                issues.append(f"Company '{expected_company}' has status '{status}' instead of 'Applied'")
            else:
                print(f"✅ Company '{expected_company}' has correct status: {status}")
    
    return len(issues) == 0, issues

def check_remote(agent_workspace: str, groundtruth_workspace: str, res_log: dict) -> Tuple[bool, str]:
    """
    Remote check for job search task completion - validates Notion database updates
    """
    try:
        # Try to get Notion token from config
        notion_token = None

        current_file_path = os.path.abspath(__file__)
        current_dir = os.path.dirname(current_file_path)
        parent_dir = os.path.dirname(current_dir)
        grandparent_dir = os.path.dirname(parent_dir)
        sys.path.insert(0, os.path.dirname(os.path.dirname(grandparent_dir)))
        print(f"Added directory to sys.path: {grandparent_dir}")

        import configs.token_key_session as configs
        from utils.app_specific.notion.notion_page_protector import (
            NotionPageProtector,
        )

        notion_token = getattr(configs.all_token_key_session, 'notion_integration_key', None)
        eval_page_url = getattr(configs.all_token_key_session, 'eval_notion_page_url', None)
        
        if not notion_token:
            return False, "No Notion token available for remote check"
        if not eval_page_url:
            return False, "No Notion eval page URL available for remote check"
        
        print("=== Starting Notion Database Remote Check ===")
        task_root = parent_dir
        eval_page_id = NotionPageProtector.extract_page_id_from_url(
            eval_page_url
        )
        job_finder_page = _get_attributed_job_finder_page(
            task_root, notion_token, eval_page_id
        )
        print(f"Using Job Finder page: {job_finder_page['title']} (ID: {job_finder_page['id']})")
        
        # Find the Job Tracker database within the Job Finder page
        print("Searching for 'Job Tracker' database within Job Finder page...")
        job_tracker_db = find_database_in_page(job_finder_page['id'], notion_token, "Job Tracker")
        if not job_tracker_db:
            return False, "No 'Job Tracker' database found within the Job Finder page"
        
        print(f"Found Job Tracker database: {job_tracker_db['title']} (ID: {job_tracker_db['id']}) in Job Finder page")
        
        # Get job tracker database entries
        print("Getting job tracker database entries...")
        job_entries = get_database_entries(job_tracker_db['id'], notion_token)
        print(f"Found {len(job_entries.get('results', []))} job entries")
        
        # Extract job information
        jobs = extract_job_information_from_database(job_entries.get('results', []))
        print(f"Extracted {len(jobs)} jobs from database")
        
        # Print job information for debugging
        print("=== Current Jobs in Database ===")
        for job in jobs:
            print(f"- {job['company']}: Position={job['position']}, Status={job['status']}, Location={job['location']}")
        
        # Check if HCD and AHC have 'Applied' status
        status_match, status_issues = check_job_applications_status(jobs)
        
        if not status_match:
            return False, "Job applications status check failed: " + " | ".join(status_issues)
        
        return True, "Job Tracker database status check passed for HCD and AHC"
        
    except Exception as e:
        return False, f"Failed to check remote Notion database: {str(e)}"
