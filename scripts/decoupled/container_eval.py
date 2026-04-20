import argparse
import asyncio
import json
import os
from typing import Any, Dict, Optional

from utils.data_structures.task_config import TaskConfig
from utils.evaluation.evaluator import TaskEvaluator
from utils.status_manager import TaskStatusManager


def read_json_file(path: str) -> Dict[str, Any]:
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def write_json_file(path: str, data: Dict[str, Any]) -> None:
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def evaluation_status_from_pass_value(pass_value: Optional[bool]) -> Optional[str]:
    if pass_value is True:
        return "pass"
    if pass_value is False:
        return "fail"
    return None


def synthesize_dump_line_from_bundle(bundle: Dict[str, Any]) -> Dict[str, Any]:
    """Rebuild the minimal ``dump_line`` the evaluator needs when no
    trajectory log exists (v2 client-driven mode).

    In v1 the trajectory log is written by the in-container agent loop and
    contains a serialized ``TaskConfig`` plus a run status.  In v2 the
    client drives the loop on the host, so no trajectory is produced inside
    the container.  The evaluator only reads two keys — ``config`` (to
    rebuild the TaskConfig) and ``status`` (must be SUCCESS to grade).  We
    rebuild the TaskConfig from disk exactly the way
    ``container_preprocess.build_task_bundle`` does, then force-align its
    paths + ``launch_time`` to the preprocess-time values captured in the
    bundle so the per-task evaluator sees the same workspace the agent did.

    The client calling ``/grade`` is an implicit SUCCESS signal.
    """
    eval_cfg = bundle["eval_config"]
    container_paths = bundle["container_paths"]

    tc = TaskConfig.build(
        bundle["task_dir"],
        eval_cfg["agent"]["model"]["short_name"],
        eval_cfg["global_task_config"],
        single_turn_mode=bundle.get("single_turn_mode", True),
        cn_mode=bundle.get("cn_mode", False),
    )
    tc.task_root = container_paths["task_root"]
    tc.agent_workspace = container_paths["agent_workspace"]
    tc.log_file = container_paths["log_file"]
    tc.launch_time = bundle.get("launch_time", tc.launch_time)

    return {"config": tc.to_dict(), "status": "success"}


def remap_dump_line_paths_to_container(
    dump_line: Dict[str, Any], bundle: Dict[str, Any]
) -> Dict[str, Any]:
    container_paths = bundle["container_paths"]
    config = dump_line.get("config")
    if not isinstance(config, dict):
        return dump_line

    config = dict(config)
    config["task_root"] = container_paths["task_root"]
    config["agent_workspace"] = container_paths["agent_workspace"]
    config["log_file"] = container_paths["log_file"]

    updated = dict(dump_line)
    updated["config"] = config
    return updated


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Container-side evaluation for decoupled pipeline")
    parser.add_argument("--bundle_file", default="/workspace/dumps/task_bundle.json")
    parser.add_argument("--allow_resume", action="store_true")
    return parser.parse_args()


async def run_eval(bundle_file: str, allow_resume: bool = False) -> Dict[str, Any]:
    bundle = read_json_file(bundle_file)
    log_file = bundle["container_paths"]["log_file"]
    task_root = bundle["container_paths"]["task_root"]
    eval_file_path = os.path.join(os.path.dirname(log_file), "eval_res.json")

    if allow_resume and os.path.exists(eval_file_path):
        eval_res = read_json_file(eval_file_path)
    else:
        if os.path.exists(log_file):
            # v1 path: trajectory log exists, read config + status from it.
            dump_line = read_json_file(log_file)
            dump_line = remap_dump_line_paths_to_container(dump_line, bundle)
        else:
            # v2 path: no trajectory log (client drove the loop on the host).
            # Rebuild the minimal dump_line from the bundle.
            dump_line = synthesize_dump_line_from_bundle(bundle)
        eval_res = await TaskEvaluator.evaluate_one(dump_line)
        write_json_file(eval_file_path, eval_res)


    status_manager = TaskStatusManager(task_root)
    status_manager.update_evaluation(evaluation_status_from_pass_value(eval_res.get("pass")))

    return eval_res


def main() -> None:
    args = parse_args()
    eval_res = asyncio.run(run_eval(args.bundle_file, allow_resume=args.allow_resume))

    print("Evaluation finished.")
    print(f"Pass: {eval_res.get('pass')}")
    print(f"Details: {eval_res.get('details')}")
    if eval_res.get("failure") is not None:
        print(f"Failure: {eval_res.get('failure')}")

    raise SystemExit(0 if eval_res.get("pass") is True else 1)


if __name__ == "__main__":
    main()
