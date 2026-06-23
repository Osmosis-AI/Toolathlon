import os
import re
from argparse import ArgumentParser

def find_package_import(tex_content: str) -> bool:
    # Detect if there is \n\usepackage[xxx]{tcolorbox} or \n\usepackage{tcolorbox}
    # Approach 1: Use raw string and correct escaping
    pattern1 = '\n'+r'\\usepackage\[.*?\]\{tcolorbox\}'
    pattern2 = '\n'+r'\\usepackage\{tcolorbox\}'
    
    # Combine both patterns (optional square brackets)
    pattern = '\n'+r'\\usepackage(?:\[.*?\])?\{tcolorbox\}'
    
    return re.search(pattern, tex_content) is not None

def find_color_definition(tex_content: str):
    # M-STaR's example_paper.tex defines proxYellow TWICE — the first as HTML
    # ffbb00, the second as HTML ff9100, and the second overrides.  Agents
    # following the rendered paper legitimately pick ff9100; agents reading
    # the first definition pick ffbb00.  Accept either.
    #
    # Returns the variable name (str) bound to the yellow HTML colour, or
    # — if a \colorlet chain derives a "light" variant from that yellow
    # (e.g. \colorlet{lightProxYellow}{proxYellow!50}) — returns the
    # derived variant's name, since that's what the box style uses.
    base = None
    for hex_target in ("ffbb00", "ff9100"):
        m = re.search(
            rf'\\definecolor\{{(\w+)\}}\{{HTML\}}\{{{hex_target}\}}',
            tex_content,
            re.IGNORECASE,
        )
        if m:
            base = m.group(1)
            break
    if not base:
        return None
    # Trace \colorlet{X}{base!N} → return X (the "light" derived variant).
    m = re.search(
        rf'\\colorlet\{{(\w+)\}}\{{{re.escape(base)}!\d+\}}',
        tex_content,
    )
    return m.group(1) if m else base

def find_desired_tcolorbox_remove_blanks(tex_content: str, title: str, color_var: str = "lightProxYellow") -> str:
    # Remove all white space characters
    removed_blanks_tex_content = re.sub(r'\s+', '', tex_content)
    # print(removed_blanks_tex_content)

    # Build the pattern to match (also removes whitespace)
    first_part = r"\begin{tcolorbox}[colback=" + color_var + r"!10,colframe=" + color_var + r",left=2mm,right=2mm,title=\textcolor{black}{\textbf{<<<<title>>>>}}]\begin{small}"
    first_part = first_part.replace("<<<<title>>>>", title)
    first_part = re.sub(r'\s+', '', first_part)
    # print(first_part)
    second_part = r"\end{small}\end{tcolorbox}"

    firstpartposition = removed_blanks_tex_content.rfind(first_part)
    if firstpartposition == -1:
        print(f"Not found first part in {title}")
        return None
    secondpartposition = removed_blanks_tex_content.find(second_part, firstpartposition+len(first_part))
    if secondpartposition == -1:
        print(f"Not found second part in {title}")
        return None
    needed_content = removed_blanks_tex_content[firstpartposition+len(first_part):secondpartposition]
    return needed_content

def read_file(file_path: str) -> str:
    with open(file_path, 'r', encoding='utf-8') as file:
        return file.read()

GT_SIMPLE_PROMPT = r"Question:\\\{input\}\\Answer:\\Let's think step by step."
GT_COMPLEX_PROMPT = r"<|im\_start|>system\\You are a helpful assistant.<|im\_end|>\\<|im\_start|>user\\\{input\}\\Please reason step by step, and put your final answer within \textbackslash\textbackslash boxed\{\}.\\<|im\_end|>\\<|im\_start|>assistant"


def _normalize_text_commands(s: str) -> str:
    """Treat LaTeX text-mode commands as equivalent to their bare chars.

    Inside a tcolorbox body, ``<`` ``>`` ``|`` are NOT LaTeX-reserved in
    standard text mode, so agents may write them either as literal chars
    or via the ``\\textless`` / ``\\textgreater`` / ``\\textbar`` kernel
    commands.  Both render identically; we treat them as equivalent.
    """
    s = s.replace(r'\textless', '<')
    s = s.replace(r'\textgreater', '>')
    s = s.replace(r'\textbar', '|')
    return s


GT_MAPPING = {"Simple Prompt": re.sub(r'\s+', '', GT_SIMPLE_PROMPT), "Complex Prompt": re.sub(r'\s+', '', GT_COMPLEX_PROMPT)}

def main():
    parser = ArgumentParser()
    parser.add_argument("--agent_workspace", required=False)
    parser.add_argument("--res_log_file", required=False)
    parser.add_argument("--groundtruth_workspace", required=False)
    parser.add_argument("--launch_time", required=False, help="Launch time")
    args = parser.parse_args()

    tex_file_list = [
        "Appendix.tex",
        "main.tex",
        "introduction.tex",
        "Zero_Result.tex"
    ]

    foundpackageimport = False
    main_text_content = read_file(os.path.join(args.agent_workspace, "simplerlcolm25", "main.tex"))
    if find_package_import(main_text_content):
        print("Found package import in main.tex")
        foundpackageimport = True
    if not foundpackageimport:
        print("Not found package import in any tex file")
        return False

    color_var = None
    for tex_file in tex_file_list:
        tex_content = read_file(os.path.join(args.agent_workspace, "simplerlcolm25",tex_file))
        cv = find_color_definition(tex_content)
        if cv:
            print(f"Found color definition '{cv}' (HTML ffbb00) in {tex_file}")
            color_var = cv
            break
    if not color_var:
        print("Not found color definition (any name) bound to HTML ffbb00 in any tex file")
        return False
    

    founddesiredtcolorbox_dict = {'Simple Prompt': False, 'Complex Prompt': False}
    appendix_text_content = read_file(os.path.join(args.agent_workspace, "simplerlcolm25", "Appendix.tex"))

    # Find the section after \section{Model Prompt}
    if r"\section{Model Prompt}" not in appendix_text_content:
        print("Fail to find section Model Prompt in Appendix.tex")
        return False
    needed_appendix_text_content = appendix_text_content.split(r"\section{Model Prompt}")[-1]

    for title, gt in GT_MAPPING.items():
        content = find_desired_tcolorbox_remove_blanks(needed_appendix_text_content, title, color_var=color_var)
        if content is None:
            print(f"Not found desired tcolorbox in {title}")
            founddesiredtcolorbox_dict[title] = False
            break
        filled_content = content
        # print("===== FILLED ======\n",filled_content)
        # print(gt)
        # check if the filled_content startswith gt (after normalizing the
        # interchangeable \textless / \textgreater / \textbar kernel commands
        # to their bare-char equivalents on both sides).
        normalized_filled = _normalize_text_commands(filled_content.strip())
        normalized_gt = _normalize_text_commands(gt.strip())
        if not normalized_filled.startswith(normalized_gt):
            print(f"Filled content does not start with {gt}")
            founddesiredtcolorbox_dict[title] = False
            break
        founddesiredtcolorbox_dict[title]= True
        print(f"√ Found desired tcolorbox in {title}")
    
    if not founddesiredtcolorbox_dict['Simple Prompt'] or not founddesiredtcolorbox_dict['Complex Prompt']:
        return False
    
    print("√ All test passed!")
    return True


if __name__ == "__main__":
    success = main()
    if success:
        exit(0)
    else:
        exit(1)