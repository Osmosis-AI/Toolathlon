import os
import re
from collections import Counter
from utils.general.helper import read_json
import pandas as pd


def normalize_legal_clause(clause_text):
    """
    标准化法律条款编号 去除空格和换行符 (Normalize legal clause number by removing spaces and line breaks)

    例如： (Examples:)
    - "《中华人民共和国物权法》第二十条第二款" -> "《中华人民共和国物权法》第二十条第二款"
    - "第二十条第二款" -> "第二十条第二款"
    """
    if not clause_text or pd.isna(clause_text):
        return ""

    clause_text = str(clause_text).strip()

    # 去除多余的空格 (Remove extra spaces)
    clause_text = re.sub(r'\s+', ' ', clause_text)
    clause_text = clause_text.strip()

    return clause_text


def normalize_content_text(content_text):
    """
    Normalize legal clause content by stripping punctuation/whitespace at
    BOTH ends and collapsing internal whitespace.  This tolerates the
    natural variation between what the case PDF quotes (often ending in
    '。' or trailing ellipsis '……') and what an agent might write
    (possibly with leading/trailing whitespace, missing terminal period,
    or different ellipsis form like '…' / '...').

    Examples:
        "预告登记失效。"      -> "预告登记失效"
        "   预告登记失效"     -> "预告登记失效"
        "……禁止结婚的亲属关系的；……" -> "禁止结婚的亲属关系的"
        "重婚的；…"           -> "重婚的"
    """
    if not content_text or pd.isna(content_text):
        return ""

    s = str(content_text)
    # Collapse all whitespace (Chinese legal text doesn't depend on
    # interior spacing; PDF extraction often injects stray whitespace).
    s = re.sub(r'\s+', '', s)
    # Strip leading AND trailing punctuation — Chinese full-width set +
    # Latin equivalents + ellipsis forms ('……'/'…'/'...').
    PUNCT_CLASS = r'[。！？；，、：……\.,;:!?　"“”‘’\']*'
    s = re.sub(r'^' + PUNCT_CLASS, '', s)
    s = re.sub(PUNCT_CLASS + r'$', '', s)
    return s

def check_content(agent_workspace: str, groundtruth_workspace: str):
    agent_needed_file = os.path.join(agent_workspace, "revised_terms.csv")
    groundtruth_needed_file = os.path.join(groundtruth_workspace, "revised_terms.csv")

    if not os.path.exists(agent_needed_file):
        return False, f"Agent workspace is missing the file: {agent_needed_file}"
    if not os.path.exists(groundtruth_needed_file):
        return False, f"Groundtruth workspace is missing the file: {groundtruth_needed_file}"

    agent_df = pd.read_csv(agent_needed_file)
    groundtruth_df = pd.read_csv(groundtruth_needed_file)

    # Check if the agent's revised terms file has the required columns
    required_columns = ["案件文件名称", "判决文书中的原始条款", "原始引用内容", "新法条款", "新法条款内容"]
    if not all(col in agent_df.columns for col in required_columns):
        return False, f"Agent's revised terms file is missing required columns: {required_columns}"

    # Exact match after normalization. We accept either the full
    # groundtruth or the groundtruth with its final row removed because
    # the final row is intentionally treated as optional/ambiguous.
    check_columns = required_columns

    def make_key(row):
        vals = []
        for col in check_columns:
            v = str(row[col]).strip()
            if col in ["判决文书中的原始条款", "新法条款"]:
                v = normalize_legal_clause(v)
            elif col in ["原始引用内容", "新法条款内容"]:
                v = normalize_content_text(v)
            vals.append(v)
        return tuple(vals)

    def format_sample(entry):
        return tuple((v[:80] + "…") if len(v) > 80 else v for v in entry)

    def compare_against(candidate_df, label):
        if len(agent_df) != len(candidate_df):
            return False, f"{label}: row count mismatch, agent has {len(agent_df)} rows, groundtruth has {len(candidate_df)} rows"

        gt_entries = Counter(make_key(r) for _, r in candidate_df.iterrows())
        agent_entries = Counter(make_key(r) for _, r in agent_df.iterrows())
        if agent_entries == gt_entries:
            return True, None

        missing = gt_entries - agent_entries
        extra = agent_entries - gt_entries
        parts = [f"{label}: normalized content mismatch."]
        if missing:
            sample = next(iter(missing))
            parts.append(f"Missing example: {format_sample(sample)}")
        if extra:
            sample = next(iter(extra))
            parts.append(f"Extra example: {format_sample(sample)}")
        return False, " ".join(parts)

    candidates = [("full groundtruth", groundtruth_df)]
    if len(groundtruth_df) > 0:
        candidates.append(("groundtruth without final row", groundtruth_df.iloc[:-1]))

    errors = []
    for label, candidate_df in candidates:
        ok, msg = compare_against(candidate_df, label)
        if ok:
            return True, None
        errors.append(msg)

    return False, "Agent output did not exactly match any accepted groundtruth version. " + " | ".join(errors)
