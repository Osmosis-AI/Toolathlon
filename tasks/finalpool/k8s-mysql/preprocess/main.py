import asyncio
import sys
from utils.general.helper import run_command
from argparse import ArgumentParser
from pathlib import Path
import os
import shutil

if __name__=="__main__":
    parser = ArgumentParser()
    parser.add_argument("--agent_workspace", required=False)
    parser.add_argument("--launch_time", required=False, help="Launch time")
    args = parser.parse_args()

    print("Starting the preprocess script, constructing the cluster...")
    script_path = os.path.join(os.path.dirname(__file__), "..","scripts","k8s_mysql.sh")
    # Capture the bash script's returncode and propagate.  Without this,
    # a non-zero bash exit (e.g. mysql rollout timeout, image pull
    # failure, kind create failure) would be silently swallowed and the
    # task wrapper would report preprocess "done" → client thinks
    # cluster ready when it isn't.  We propagate via sys.exit so
    # container_preprocess.py marks the phase as failed.
    _, _, rc = asyncio.run(run_command(
        f"bash {script_path} start {args.agent_workspace}",
        debug=True, show_output=True,
    ))
    if rc != 0:
        print(f"k8s_mysql.sh failed with returncode={rc}; aborting preprocess",
              file=sys.stderr)
        sys.exit(rc)
    print("Cluster constructed")

    # Delete k8s_configs in agent_workspace (only the local copy; the
    # k8s MCP server reads from the task-dir backup).  Guard with
    # ``ignore_errors`` so a missing dir from a half-failed bash run
    # doesn't itself raise (we'd already have exited above on bash
    # failure, but defensive in case the directory wasn't created).
    shutil.rmtree(
        os.path.join(args.agent_workspace, "k8s_configs"),
        ignore_errors=True,
    )
    print("Deleted local k8s_configs successfully! We will only use the k8s mcp in this task!","green")

    print("Initialization complete")