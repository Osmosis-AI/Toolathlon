# PR54 Branch-vs-PR Task Reconciliation Tracker

Tracks the tasks under `tasks/` that differ between our verified branch and PR #54,
so every difference is a recorded decision (adopt PR / keep ours / reject PR) rather
than an accidental divergence.

- **Our branch:** `toolathlon-tasks-verified-toreview`
- **PR #54 branch:** `claude/task-fixes-2026-06-24` (head `bd8609b7`, a.k.a. `origin/pr/54`)
- **Snapshot:** 2026-06-25. Originally 14 differing tasks under `tasks/`.

This is separate from `UpdateLogs_CommonIssues.md` (which tracks PR54 per-commit review).
When a decision here reverses an entry there, this file is the authoritative one.

## Decision legend

- `[PR]`  adopt PR's version (our branch made equal to PR)
- `[OURS]` keep our version (PR's change not adopted, but we changed the files ourselves)
- `[REJECT]` reject PR's change (we keep the pre-PR version on purpose)

`你改 / PR改` = did each side change the task relative to the merge-base `acb8e54a`.

## Tasks

| # | Decision | Task | 你改 | PR改 | Differing files (ours↔PR) | Notes |
|---|---|---|---|---|---|---|
| 1 | `[PR]` ✅ applied | `imagenet` | no | yes | `docs/task.md`, `docs/task_cn.md` | **Adopt PR per user decision (2026-06-25) — reverses the earlier reject in `UpdateLogs` (rows 49/152/217).** PR reverts the prompt from the strict "must exactly follow `format.tex`" wording back to the softer "example structure". Now aligned to PR. |
| 2 | `[PR]` ✅ applied | `sync-todo-to-readme` | no | yes | `docs/task.md`, `docs/task_cn.md`, `groundtruth_workspace/README.md` | **Adopt PR per user decision (2026-06-25) — reverses the earlier reject in `UpdateLogs` (rows 56/111/149).** PR narrows the task to a main→dev TODO diff (215-entry README GT) instead of the full-scan口径 (240). Now aligned to PR. |
| 3 | `[PR]` ✅ applied | `canvas-arrange-exam` | yes | yes | `docs/task.md`, `evaluation/check_local.py`, `files/course_config.json`, `groundtruth_workspace/exam_schedule.xlsx` | **Adopt PR per user decision (2026-06-25) — reverses our earlier "Black Smith" choice (`a95e67d7` / `2d7f0cdd`).** PR keeps the CS301 replacement proctor as "Professor Smith" (no fabricated first name): announcement says "Professor Smith", GT Proctor cell `[7,2]` = "Smith", and the prompt drops the "fill in full names" requirement. grader logic (token-subset compare, strip "Professor") is unchanged between versions. Now aligned to PR. |
| 4 | `[PR]` ✅ applied | `canvas-do-quiz` | yes | yes | `evaluation/check_remote.py` | **Adopt PR per user decision (2026-06-25).** Comment-only difference — both versions share the identical `kept_score`-over-`score` grading logic (our `67c44db9`); PR just has a more detailed comment (DB101 attempt-2 `untaken`/`score=null` example). No behavioral change. Now aligned to PR. |
| 5 | `[PR]` ✅ applied | `email-paper-homepage` | yes | yes | `docs/task.md`, `evaluation/main.py` | **Adopt PR per user decision (2026-06-25) — reverses the earlier reject `d170d91b`.** PR broadens the task scope from "newly-accepted papers only" to "all accepted/published papers": Enhancing LLMs now requires its codeurl, Optimizing LLMs becomes `to_be_released` (no codeurl), the Workshop check is dropped, and the allowed-modify file set expands. Now aligned to PR. |
| 6 | `[OURS]` | `k8s-mysql` | yes | yes | `groundtruth_workspace/gtq2.csv` | Our verified fix `7b5c257c` (gtq2 regeneration). |
| 7 | `[OURS]` | `notion-hr` | yes | yes | `evaluation/main.py` | Our verified fix `f9eaf3b8` (degree case comparison). |
| 8 | `[OURS]` | `notion-personal-website` | yes | yes | `evaluation/check_remote.py` | Our verified fix `a1c9c087` (shared helper / pagination). |
| 9 | `[OURS]` | `student-interview` | yes | yes | `evaluation/main.py` | Our verified fix `36bb920d` (conflict message formatting / timezone). |
| 10 | `[OURS]` | `woocommerce-product-recall` | yes | no | `evaluation/check_remote_recall.py`, `initial_workspace/recall_form_template.json` | Our change `943be37d` (align recall form with Google Forms MCP limits). PR did not touch this task — difference is entirely ours. |
| 11 | `[REJECT]` | `detect-revised-terms` | no | yes | `docs/task.md`, `evaluation/check_content.py`, `groundtruth_workspace/revised_terms.csv` | Rejected (`UpdateLogs` `d8973618` + rows 77/119/151): PR would loosen the clarified "quoted original text + complete new-law provisions + one-to-many separate rows" requirement and roll back the exact-normalized GT. |
| 12 | `[REJECT]` | `nvidia-stock-analysis` | no | yes | `evaluation/main.py`, `initial_workspace/data.txt`, `initial_workspace/tips.txt`, `task_config.json` | Rejected (`UpdateLogs` `ba1fd321` + rows 64/122/157): PR removes the saved 2026-06-12 Basic-Trend snapshot / live-fallback, loosens top-holder matching, shrinks Sheet 3 instructions, and drops tools. |
| 13 | `[REJECT]` | `search-ca-school` | no | yes | `docs/task.md`, `evaluation/main.py`, `groundtruth_workspace/AI_univ_LA_500miles_Top30.json`, `…_2024.json` | Rejected (`UpdateLogs` rows 85/147): PR swaps to a static 2016–2026 GT (`+UCR`, `-ASU`) without `cs_ranking_rank`, inconsistent with the task's CSRankings口径. |
| 14 | `[REJECT]` | `wandb-shortest-length` | no | yes | `evaluation/main.py` | Rejected / already covered (`UpdateLogs` `02f1eb1a` + rows 74/118): current evaluator already accepts both final-step `499` and strict every-100-steps ending `400`; no PR change needed. |

## Summary

- **5** adopt PR (`imagenet`, `sync-todo-to-readme`, `email-paper-homepage`, `canvas-arrange-exam`, `canvas-do-quiz`) — applied 2026-06-25, now aligned to PR.
- **5** keep ours (own verified fix differs from PR).
- **4** reject PR (keep pre-PR version on purpose).
- After the 5 adoptions, **9** tasks under `tasks/` still differ from PR (all intentional).
- Non-task files that also differ (out of scope here): `UpdateLogs_CommonIssues.md`, `scripts/run_single_containerized.sh`, `scripts/run_single_decoupled.sh`, `utils/app_specific/notion/ops.py`.
