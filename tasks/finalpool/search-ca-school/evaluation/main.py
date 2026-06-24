"""Grader for `search-ca-school`.

The task asks the agent to find US AI top-30 universities (excluding
Information Retrieval) within 500 mi of the LA Natural History Museum,
for the year range 2016-2026.

This grader compares the agent's submission against a STATIC ground
truth file committed alongside the task at
``groundtruth_workspace/AI_univ_LA_500miles_Top30.json``.  Previously the
grader recomputed the ranking from a live CSRankings fetch at grade time;
that proved unreliable because CSRankings's ``generated-author-info.csv``
regenerates nightly from DBLP, and individual late-year-2024 papers
flipping the order at the rank-30 boundary caused agents who fetched the
data earlier to disagree with the grader.  The 11-year window
(2016-2026) gives ~2-point gaps between consecutive ranks at the
boundary, so the static snapshot is stable.
"""

from argparse import ArgumentParser
import asyncio
import os
from utils.general.helper import read_json, normalize_str


# University name abbreviations mapping
UNIVERSITY_ABBREVIATIONS = {
    # UCB
    "ucb": "university of california berkeley",
    "ucberkeley": "university of california berkeley",

    # UCLA
    "ucla": "university of california los angeles",
    "uclosangeles": "university of california los angeles",

    # UCSD
    "ucsd": "university of california san diego",
    "ucsandiego": "university of california san diego",

    # USC
    "usc": "university of southern california",

    # Caltech
    "caltech": "california institute of technology",
    "cit": "california institute of technology",

    # Stanford
    "stanford": "stanford university",

    # UCI
    "uci": "university of california irvine",
    "ucirvine": "university of california irvine",

    # UCSB
    "ucsb": "university of california santa barbara",
    "ucsantabarbara": "university of california santa barbara",

    # UCSC
    "ucsc": "university of california santa cruz",
    "ucscsantacruz": "university of california santa cruz",

    # UCR
    "ucr": "university of california riverside",
    "ucriverside": "university of california riverside",

    # ASU
    "asu": "arizona state university",
    "arizonastateuniversity": "arizona state university",
}


def check(needed_info, groundtruth_info, allow_adjacent_swap_if_close: bool = True):
    """Compare agent's submission list against the expected list.

    car_drive_miles: 10% / ±2mi tolerance (Google Maps routing noise).
    city / university: normalize_str compare, with abbreviation table.

    ``allow_adjacent_swap_if_close``: if True, when two adjacent entries
    in expected have driving distances within ≤5 mi, permit the agent
    to swap them.  Routing noise at very close distances can flip their
    order regardless of the canonical sort, so allowing this avoids a
    false negative.

    Note: cs_ranking_rank is no longer required in the agent's output
    (task simplified to drop CSRankings rank from the schema).  Extra
    fields in the agent's entries are ignored.
    """
    # Build alternate expected lists with permissible adjacent swaps
    candidates = [list(groundtruth_info)]
    if allow_adjacent_swap_if_close:
        for i in range(len(groundtruth_info) - 1):
            if abs(groundtruth_info[i]["car_drive_miles"] -
                   groundtruth_info[i + 1]["car_drive_miles"]) <= 5:
                alt = list(groundtruth_info)
                alt[i], alt[i + 1] = alt[i + 1], alt[i]
                candidates.append(alt)

    def _try(expected_list):
        for idx, (given_school, gt_school) in enumerate(zip(needed_info, expected_list)):
            tol = max(gt_school['car_drive_miles'] * 0.1, 2)
            if abs(given_school['car_drive_miles'] - gt_school['car_drive_miles']) > tol:
                return False, (f"position {idx+1}: distance mismatch — "
                               f"agent {given_school['car_drive_miles']} vs "
                               f"expected {gt_school['car_drive_miles']} "
                               f"({gt_school['university']})")
            given_city = given_school['city'].replace("city of", "").replace("the", "").replace("city", "").strip()
            if normalize_str(gt_school['city']) not in normalize_str(given_city):
                return False, (f"position {idx+1}: city mismatch — "
                               f"agent {given_city!r} vs expected {gt_school['city']!r} "
                               f"({gt_school['university']})")
            # Expand "Univ." → "University" on BOTH sides so the GT's
            # CSRankings dept names (which use "Univ.") and any "University"
            # variant from the agent compare equal after normalize_str.
            def _expand(s):
                return s.replace("Univ.", "University").replace("univ.", "university")
            agent_uni = _expand(given_school['university'])
            expected_uni = _expand(gt_school['university'])
            if normalize_str(agent_uni) != normalize_str(expected_uni):
                expanded_abbrev = UNIVERSITY_ABBREVIATIONS.get(normalize_str(agent_uni))
                if expanded_abbrev is None or normalize_str(expanded_abbrev) != normalize_str(expected_uni):
                    return False, (f"position {idx+1}: university name mismatch — "
                                   f"agent {agent_uni!r} vs expected {expected_uni!r}")
        return True, None

    last_err = None
    for cand in candidates:
        ok, err = _try(cand)
        if ok:
            return True
        last_err = err
    if last_err:
        print(last_err)
    return False


async def main(args):
    needed_info_file = os.path.join(args.agent_workspace, "AI_univ_LA_500miles_Top30.json")
    if not os.path.exists(needed_info_file):
        print(f"File {needed_info_file} does not exist")
        return False
    needed_info = read_json(needed_info_file)

    if not args.groundtruth_workspace:
        print("--groundtruth_workspace is required")
        return False
    gt_file = os.path.join(args.groundtruth_workspace, "AI_univ_LA_500miles_Top30.json")
    if not os.path.exists(gt_file):
        print(f"Ground truth file {gt_file} does not exist")
        return False
    groundtruth_info = read_json(gt_file)

    print(f"\nExpected {len(groundtruth_info)} schools (CSRankings 2016-2026, US AI ex-IR, ≤500 mi from LA NHM):")
    for s in groundtruth_info:
        print(f"  {s['car_drive_miles']:3d}mi | {s['university']}")
    print(f"\nAgent submitted {len(needed_info)} schools.")

    if len(needed_info) != len(groundtruth_info):
        print(f"The number of universities differs: "
              f"agent has {len(needed_info)}, expected {len(groundtruth_info)}")
        return False

    return check(needed_info, groundtruth_info)


if __name__ == "__main__":
    parser = ArgumentParser()
    parser.add_argument("--agent_workspace", required=False)
    parser.add_argument("--groundtruth_workspace", required=False)
    parser.add_argument("--res_log_file", required=False)
    parser.add_argument("--launch_time", required=False, help="Launch time")
    args = parser.parse_args()

    if args.res_log_file:
        try:
            read_json(args.res_log_file)
        except Exception:
            pass

    res = asyncio.run(main(args))
    if not res:
        print("Failed to pass tests!")
        exit(1)
    print("Pass all tests!")
    exit(0)
