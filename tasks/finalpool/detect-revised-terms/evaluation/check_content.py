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


# Chinese-digit → Arabic conversion for legal article numbers.  Article
# numbers in Chinese law citations are written as ``第一百五十五条`` /
# ``第七条`` / etc.  We need to compare them numerically because agents
# may write the same article with different specificity (``第七条`` vs
# ``第七条第一款第一项``) or list multiple in one cite (``第一百五十五条、
# 第一百五十六条``).
_CN_DIGIT = {
    "零": 0, "〇": 0, "○": 0,
    "一": 1, "二": 2, "两": 2, "三": 3, "四": 4,
    "五": 5, "六": 6, "七": 7, "八": 8, "九": 9,
}
_CN_UNIT = {"十": 10, "百": 100, "千": 1000, "万": 10000}


def _cn_to_int(s: str) -> int:
    """Convert a Chinese-numeral string (e.g. ``一百五十五``, ``七``,
    ``二十``) to int.  Returns -1 if the input has any unknown chars.

    Handles the standard 0–9999 range, which is sufficient for article
    numbers (民法典 tops out at 第一千二百六十条).
    """
    if not s:
        return -1
    if all(c.isdigit() for c in s):
        # Already Arabic
        return int(s)
    total = 0
    section = 0
    for c in s:
        if c in _CN_DIGIT:
            section = _CN_DIGIT[c]
        elif c in _CN_UNIT:
            unit = _CN_UNIT[c]
            if section == 0:
                section = 1  # bare 十 = 10, 百 = 100, etc.
            if unit == 10000:
                total = (total + section) * 10000
                section = 0
            else:
                total += section * unit
                section = 0
        else:
            return -1
    return total + section


_LAW_NAME_RE = re.compile(r"《\s*(?:中华人民共和国)?\s*([^》]+?)\s*》")
_ARTICLE_RE = re.compile(r"第\s*([零〇○一二三四五六七八九十百千万0-9]+)\s*条")


def parse_clause_cite(text: str):
    """Extract (law_name, frozenset_of_article_numbers) from a citation
    string like ``《中华人民共和国合同法》第一百五十五条、第一百五十六条``.

    A cite may contain multiple article numbers separated by 、 or , — we
    extract them all.  Sub-article qualifiers like ``第一款第一项`` are
    intentionally ignored, so ``第七条`` and ``第七条第一款第一项`` parse
    to the same article set ``{7}``.

    Returns ``("", frozenset())`` on parse failure so callers can detect
    junk gracefully.
    """
    if not text or pd.isna(text):
        return "", frozenset()
    s = str(text)
    name_match = _LAW_NAME_RE.search(s)
    law_name = name_match.group(1).strip() if name_match else ""
    article_nums = set()
    for m in _ARTICLE_RE.finditer(s):
        n = _cn_to_int(m.group(1))
        if n > 0:
            article_nums.add(n)
    return law_name, frozenset(article_nums)


def clause_cite_matches(gt_cite: str, agent_cite: str) -> bool:
    """Check whether an agent cite is compatible with a GT cite.

    A match requires:
      * same law name (e.g. both ``合同法``)
      * GT's article numbers ⊆ agent's article numbers

    This makes the grader tolerant of agents that:
      * cite a more granular sub-article (``第七条第一款第一项``) when
        GT just says ``第七条`` — same article number set
      * list multiple new-law articles when GT picks one (e.g. agent
        ``第一百五十五条、第一百五十六条`` covers GT's ``第一百五十五条``)
    """
    gt_name, gt_nums = parse_clause_cite(gt_cite)
    a_name, a_nums = parse_clause_cite(agent_cite)
    if not gt_nums or not a_nums:
        # Fall back to normalized exact match when we can't extract numbers
        return normalize_legal_clause(gt_cite) == normalize_legal_clause(agent_cite)
    if gt_name and a_name and gt_name != a_name:
        return False
    return gt_nums.issubset(a_nums)


def content_text_matches(gt_text: str, agent_text: str) -> bool:
    """Bidirectional substring match after normalization.

    Returns True if the GT text is a contiguous substring of the agent's
    text, OR the agent's text is a contiguous substring of the GT's text.
    Both directions are needed in practice:

      * Agent quoted MORE than GT: e.g. for 合同法第五十六条 the case PDF
        cites two sentences; the agent quotes both, GT only quoted the
        first.  Here GT ⊆ agent.
      * Agent quoted LESS than GT: e.g. for 物权法第二十条第二款 the agent
        quoted only the actually-revised 第二款 from 民法典第二百二十一条,
        while GT included the full 3-sentence text of 第221条.  Here
        agent ⊆ GT.
    """
    g = normalize_content_text(gt_text)
    a = normalize_content_text(agent_text)
    if not g and not a:
        return True
    if not g or not a:
        return False
    return g in a or a in g


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

    # Recall-only check with semantic loosening.  For each GT row we
    # require some agent row to:
    #   - share the same case file name
    #   - cite a compatible old-law clause (law name + article-number
    #     superset; see ``clause_cite_matches``)
    #   - cite a compatible new-law clause (same logic)
    #   - have content text that's a bidirectional substring of GT's
    #     (see ``content_text_matches``)
    #
    # This handles the common cases where the agent's answer is
    # semantically correct but more granular or differently-formatted
    # than our hand-curated GT.  Extra agent rows are not penalized.

    def find_match(gt_row, agent_rows) -> bool:
        for _, ar in agent_rows.iterrows():
            if str(gt_row["案件文件名称"]).strip() != str(ar["案件文件名称"]).strip():
                continue
            if not clause_cite_matches(gt_row["判决文书中的原始条款"], ar["判决文书中的原始条款"]):
                continue
            if not clause_cite_matches(gt_row["新法条款"], ar["新法条款"]):
                continue
            if not content_text_matches(gt_row["原始引用内容"], ar["原始引用内容"]):
                continue
            if not content_text_matches(gt_row["新法条款内容"], ar["新法条款内容"]):
                continue
            return True
        return False

    missing = []
    for _, gt_row in groundtruth_df.iterrows():
        if not find_match(gt_row, agent_df):
            missing.append((
                str(gt_row["案件文件名称"]),
                str(gt_row["判决文书中的原始条款"]),
                str(gt_row["新法条款"]),
            ))

    if missing:
        sample = missing[0]
        return False, (
            f"Recall failed: {len(missing)} of {len(groundtruth_df)} "
            f"required entries are missing from the agent output. "
            f"Example missing entry: case={sample[0]} old={sample[1]} new={sample[2]}"
        )

    return True, None
