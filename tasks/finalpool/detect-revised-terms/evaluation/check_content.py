import os
import re
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

    # Recall-only check: the grader requires that every row in the
    # groundtruth is present somewhere in the agent's output, but does
    # NOT penalize the agent for producing extra rows.
    #
    # Rationale: the task prompt asks for "ANY clauses cited that
    # conflict with, are inconsistent with, have been revised by, or
    # repealed under the new law", which legitimately admits multiple
    # revised provisions per case.  A faithful agent will often surface
    # more rows than the curated GT subset (for example, additional
    # revised cites the GT leaves out because of mapping ambiguity, like
    # 民法总则→民法典 same-text moves).  Requiring exact set equality
    # would penalize broader-but-correct answers; requiring strict
    # supersetness still ensures every concretely-revised provision we
    # care about is captured.

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

    groundtruth_entries = {make_key(r) for _, r in groundtruth_df.iterrows()}
    agent_entries = {make_key(r) for _, r in agent_df.iterrows()}

    missing_in_agent = groundtruth_entries - agent_entries
    if missing_in_agent:
        sample = next(iter(missing_in_agent))
        sample_short = tuple((v[:80] + "…") if len(v) > 80 else v for v in sample)
        return False, (
            f"Recall failed: {len(missing_in_agent)} of {len(groundtruth_entries)} "
            f"required entries are missing from the agent output. "
            f"Example missing entry: {sample_short}"
        )

    return True, None
