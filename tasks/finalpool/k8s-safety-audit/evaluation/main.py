from argparse import ArgumentParser
from pathlib import Path
from utils.general.helper import run_command, get_module_path
import asyncio
import sys
import os

FOLDER_ID_FILE = os.path.join(os.path.dirname(__file__), "..", "files", "folder_id.txt")
with open(FOLDER_ID_FILE, "r") as f: FOLDER_ID = f.read().strip()
CREDENTIALS_FILE = "configs/google_credentials.json"
task_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))


def _instance_suffix() -> str:
    """Read instance_suffix from configs/ports_config.yaml so the kubeconfig
    filename matches what the preprocess shell script wrote.  See the
    matching helper in token_key_session.py for the full rationale."""
    cfg_path = os.path.join(task_dir, "..", "..", "..", "configs", "ports_config.yaml")
    try:
        with open(cfg_path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line.startswith("instance_suffix:"):
                    val = line.split(":", 1)[1].strip()
                    return val.strip('"').strip("'")
    except OSError:
        pass
    return ""


KUBECONFIG_PATH = os.path.join(
    task_dir, "k8s_configs", f"cluster-safety-audit{_instance_suffix()}-config.yaml"
)

if __name__ == "__main__":
    parser = ArgumentParser()
    parser.add_argument("--agent_workspace", required=False)
    parser.add_argument("--launch_time", required=False)
    parser.add_argument("--res_log_file", required=False)
    parser.add_argument("--groundtruth_workspace", required=False)
    parser.add_argument("--credentials_file", required=False)
    args = parser.parse_args()
    
    print("Check Google Sheets...")
    stdout, stderr, return_code = asyncio.run(run_command(
                f"uv run -m {get_module_path('check_google_sheet')} --folder_id {FOLDER_ID} --credentials_file {CREDENTIALS_FILE} --kubeconfig_path {KUBECONFIG_PATH}",
                debug=True, show_output=True))
    if return_code != 0:
        print(f"[ERROR] Google Sheets check failed with return code {return_code}")
        print(f"stdout: {stdout}")
        print(f"stderr: {stderr}")
        sys.exit(return_code)
    print("Google Sheets check completed.")

    print("Check cluster...")
    stdout, stderr, return_code = asyncio.run(run_command(
        f"uv run -m {get_module_path('check_cluster')} --kubeconfig_path {KUBECONFIG_PATH}",
        debug=True, show_output=True))
    if return_code != 0:
        print(f"[ERROR] Cluster check failed with return code {return_code}")
        print(f"stdout: {stdout}")
        print(f"stderr: {stderr}")
        sys.exit(return_code)
    print("Cluster check completed.")

    print("All tests passed. Task finished.")
