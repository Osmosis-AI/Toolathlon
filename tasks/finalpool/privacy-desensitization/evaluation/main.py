"""Privacy desensitization grader.

Replaced the previous exact-content equality check (after whitespace
strip) with an F1-based comparison of redaction events:

  * The grader reads each agent file, the corresponding GT file, and the
    matching original from ``initial_workspace/files.tar.gz``.
  * It diffs ORIGINAL→AGENT and ORIGINAL→GT to derive each side's list
    of redaction spans (ranges in the original that were replaced —
    typically with ``/hidden/``).
  * Two redactions match if their original-text spans overlap.
  * Precision, recall, and F1 are computed micro-averaged across all
    files (sum TP / FP / FN, then one F1).
  * The task passes if F1 >= 0.95 and all expected files are present.

This replaces a brittle exact-equality grader that punished agents who
redacted MORE thoroughly than the GT — a real failure mode when the
curated GT had its own gaps.  The 0.95 threshold absorbs small
disagreements (e.g. a single boundary character) without letting
through agents that miss whole categories.
"""
from argparse import ArgumentParser
import difflib
import os
import re
import shutil
import tarfile
from pathlib import Path


F1_THRESHOLD = 0.95


def _redactions(original: str, redacted: str):
    """Return list of (start, end) spans in ``original`` that differ from
    ``redacted``.  Uses sequence-matcher opcodes; ``replace`` and
    ``delete`` opcodes both count as redactions.
    """
    matcher = difflib.SequenceMatcher(a=original, b=redacted, autojunk=False)
    spans = []
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag in ("replace", "delete"):
            if i2 > i1:
                spans.append((i1, i2))
    return spans


def _spans_overlap(a, b):
    return not (a[1] <= b[0] or b[1] <= a[0])


def _match_spans(gt_spans, agent_spans):
    """Greedy match by overlap.  Returns (TP, matched_gt_idx_set,
    matched_agent_idx_set)."""
    matched_gt = set()
    matched_agent = set()
    for i, gs in enumerate(gt_spans):
        for j, as_ in enumerate(agent_spans):
            if j in matched_agent:
                continue
            if _spans_overlap(gs, as_):
                matched_gt.add(i)
                matched_agent.add(j)
                break
    return len(matched_gt), matched_gt, matched_agent


def _extract_tar(path, dest):
    with tarfile.open(path, "r:gz") as tar:
        tar.extractall(path=dest, filter="data")


def main(args):
    agent_root = Path(args.agent_workspace)
    gt_root = Path(args.groundtruth_workspace)
    agent_dir = agent_root / "desensitized_documents"

    if not agent_dir.exists():
        print(f"× Desensitized documents directory not found: {agent_dir}")
        return 1

    # Extract GT
    gt_tar = gt_root / "gt_files.tar.gz"
    gt_tmp = gt_root / "tmp"
    if gt_tmp.exists():
        shutil.rmtree(gt_tmp)
    gt_tmp.mkdir(parents=True, exist_ok=True)
    _extract_tar(gt_tar, gt_tmp)
    gt_dir = gt_tmp / "desensitized_documents"

    # Extract originals from initial_workspace/ (not stashed by v3, so
    # accessible at grade time).
    task_root = Path(__file__).parent.parent
    orig_tar = task_root / "initial_workspace" / "files.tar.gz"
    orig_tmp = gt_root / "_orig_tmp"
    if orig_tmp.exists():
        shutil.rmtree(orig_tmp)
    orig_tmp.mkdir(parents=True, exist_ok=True)
    _extract_tar(orig_tar, orig_tmp)

    agent_files = sorted(p.name for p in agent_dir.iterdir() if p.is_file())
    gt_files = sorted(p.name for p in gt_dir.iterdir() if p.is_file())

    if len(agent_files) != len(gt_files):
        print(f"× File count mismatch: agent has {len(agent_files)}, GT has {len(gt_files)}")
        return 1

    missing = set(gt_files) - set(agent_files)
    extra = set(agent_files) - set(gt_files)
    if missing or extra:
        print(f"× File-name mismatch.  Missing: {sorted(missing)}  Extra: {sorted(extra)}")
        return 1

    total_tp = total_fp = total_fn = 0
    per_file = []

    for fname in agent_files:
        stem, _, ext = fname.rpartition(".")
        if stem.endswith("_desensitized"):
            orig_name = stem[: -len("_desensitized")] + "." + ext
        else:
            orig_name = fname

        orig_path = orig_tmp / orig_name
        agent_path = agent_dir / fname
        gt_path = gt_dir / fname

        if not orig_path.exists():
            print(f"× No matching original for {fname} (looked for {orig_name})")
            return 1

        original = orig_path.read_text(encoding="utf-8", errors="replace")
        agent_text = agent_path.read_text(encoding="utf-8", errors="replace")
        gt_text = gt_path.read_text(encoding="utf-8", errors="replace")

        gt_spans = _redactions(original, gt_text)
        agent_spans = _redactions(original, agent_text)
        tp, _, matched_agent = _match_spans(gt_spans, agent_spans)
        fp = len(agent_spans) - len(matched_agent)
        fn = len(gt_spans) - tp

        total_tp += tp
        total_fp += fp
        total_fn += fn
        per_file.append((fname, tp, fp, fn, len(gt_spans), len(agent_spans)))

    if total_tp == 0 and total_fp == 0 and total_fn == 0:
        f1 = 1.0
        precision = recall = 1.0
    else:
        precision = total_tp / (total_tp + total_fp) if (total_tp + total_fp) > 0 else 0.0
        recall = total_tp / (total_tp + total_fn) if (total_tp + total_fn) > 0 else 0.0
        f1 = (2 * precision * recall / (precision + recall)) if (precision + recall) > 0 else 0.0

    print("\n=== Privacy-desensitization grader ===")
    print(f"Files: {len(agent_files)} (matches GT)")
    print(f"Aggregate redaction TP={total_tp}  FP={total_fp}  FN={total_fn}")
    print(f"Precision={precision:.4f}  Recall={recall:.4f}  F1={f1:.4f}")
    print(f"Threshold: F1 >= {F1_THRESHOLD}")

    if f1 < F1_THRESHOLD:
        print("\nPer-file diagnostics (worst F1 first):")
        scored = []
        for name, tp, fp, fn, gtc, agtc in per_file:
            if tp + fp + fn == 0:
                continue
            p = tp / (tp + fp) if (tp + fp) > 0 else 0.0
            r = tp / (tp + fn) if (tp + fn) > 0 else 0.0
            file_f1 = (2 * p * r / (p + r)) if (p + r) > 0 else 0.0
            scored.append((file_f1, name, tp, fp, fn, gtc, agtc))
        scored.sort()
        for file_f1, name, tp, fp, fn, gtc, agtc in scored[:10]:
            print(f"  {name:50s} F1={file_f1:.3f}  TP={tp} FP={fp} FN={fn} (gt={gtc} agent={agtc})")

    if f1 >= F1_THRESHOLD:
        print(f"\n√ Passed (F1={f1:.4f})")
        return 0
    else:
        print(f"\n× Failed (F1={f1:.4f} below {F1_THRESHOLD})")
        return 1


if __name__ == "__main__":
    parser = ArgumentParser()
    parser.add_argument("--agent_workspace", required=False, default=".")
    parser.add_argument("--groundtruth_workspace", required=False, default=".")
    parser.add_argument("--res_log_file", required=False)
    parser.add_argument("--launch_time", required=False, help="Launch time")
    args = parser.parse_args()
    raise SystemExit(main(args))
