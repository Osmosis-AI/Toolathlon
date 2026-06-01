from argparse import ArgumentParser
import asyncio
import csv
import io
import os
import time
import urllib.error
import urllib.request
from collections import defaultdict
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

    # ASU
    "asu": "arizona state university",
    "arizonastateuniversity": "arizona state university",
}


# ── Live-CSRankings grader ──────────────────────────────────────────────
#
# Why "live": this task asks the agent to read live web sources (CSRankings)
# and produce a list that reflects current 2024 AI rankings.  CSRankings's
# "2024" data isn't immutable — it keeps shifting as DBLP indexes more
# late-2024 papers throughout 2025/2026, so any static groundtruth becomes
# stale within months.  An agent that honestly queries the source today
# would deterministically produce different numbers than a snapshot frozen
# even a few months ago.
#
# Solution: at grade time, we fetch the same primary source CSRankings'
# own JS uses (``generated-author-info.csv``) and recompute the ranking
# with the same geomean algorithm, then apply our fixed distance and
# top-30 filters.  Agent and grader see the same CSRankings state within
# minutes of each other → exact rank match is sound, no artificial
# tolerance needed.

# CSRankings AI venue → area mapping (per csrankings.org source).
# Excludes "Information Retrieval" (sigir/www) per the task prompt.
VENUE2AREA = {
    "aaai": "ai", "ijcai": "ai",
    "cvpr": "vision", "eccv": "vision", "iccv": "vision",
    "icml": "mlmining", "kdd": "mlmining", "nips": "mlmining", "iclr": "mlmining",
    "acl": "nlp", "emnlp": "nlp", "naacl": "nlp",
}
CSR_AREAS = ("ai", "vision", "mlmining", "nlp")
CSR_TARGET_YEAR = 2024
CSR_AUTHORS_URL = "https://csrankings.org/generated-author-info.csv"

# Departments CSRankings tags as US institutions that could plausibly land
# in top-30 AI.  Sourced from the CSRankings region filter (us=1).  Adding
# a new US institution to this set is a one-time edit.
US_INSTITUTIONS = {
    "Carnegie Mellon University",
    "Univ. of Illinois at Urbana-Champaign",
    "Univ. of Maryland - College Park",
    "Univ. of California - San Diego",
    "Georgia Institute of Technology",
    "Stanford University",
    "Johns Hopkins University",
    "Massachusetts Inst. of Technology",
    "Univ. of California - Berkeley",
    "University of Texas at Austin",
    "University of Wisconsin - Madison",
    "University of Pennsylvania",
    "Cornell University",
    "Purdue University",
    "University of North Carolina",
    "Columbia University",
    "University of Michigan",
    "New York University",
    "University of Central Florida",
    "University of Southern California",
    "University of Washington",
    "Pennsylvania State University",
    "Univ. of California - Los Angeles",
    "University of Virginia",
    "Arizona State University",
    "Princeton University",
    "Texas A&M University",
    "Univ. of California - Irvine",
    "Univ. of California - Santa Barbara",
    "Boston University",
    "Univ. of California - Davis",
    "Univ. of California - Santa Cruz",
    "Univ. of California - Riverside",
    "Yale University",
    "Harvard University",
    "California Institute of Technology",
    "Univ. of California - Merced",
    "University of Chicago",
    "University of Minnesota",
    "University of Pittsburgh",
    "Brown University",
    "Rice University",
    "Northwestern University",
    "Duke University",
    "Univ. of Massachusetts Amherst",
    "Univ. of California - San Francisco",
    "Rutgers University",
    "Ohio State University",
}

# Driving distances (miles) from the Los Angeles Natural History Museum,
# plus the canonical city name CSRankings uses, for every US institution
# that could plausibly be within 500 mi of LA.  Schools don't move, so
# these values are stable; integer miles match the task's "integers only"
# requirement.  An institution missing from this dict is treated as "out
# of range" (i.e. not included in the expected list even if it makes
# top-30) — extend the dict if a new near-LA school enters top-30.
DISTANCES_MILES = {
    "University of Southern California": ("Los Angeles", 1),
    "Univ. of California - Los Angeles": ("Los Angeles", 14),
    "California Institute of Technology": ("Pasadena", 18),
    "Univ. of California - Irvine": ("Irvine", 40),
    "Univ. of California - Riverside": ("Riverside", 55),
    "Univ. of California - Santa Barbara": ("Santa Barbara", 109),
    "Univ. of California - San Diego": ("La Jolla", 110),
    "Univ. of California - Merced": ("Merced", 296),
    "Stanford University": ("Stanford", 362),
    "Univ. of California - Berkeley": ("Berkeley", 376),
    "Univ. of California - Santa Cruz": ("Santa Cruz", 380),
    "Arizona State University": ("Tempe", 384),
    "Univ. of California - San Francisco": ("San Francisco", 384),
    "Univ. of California - Davis": ("Davis", 388),
}

CSR_FETCH_BUDGET_S = 90.0
CSR_FETCH_POLL_INTERVAL_S = 5.0


def _fetch_csrankings_csv_with_retry() -> str:
    """Fetch generated-author-info.csv from csrankings.org with retry budget.

    csrankings.org occasionally serves slow / 5xx; the grader cannot run
    without this file, so we poll up to CSR_FETCH_BUDGET_S total.  Any
    successful 200 response with non-trivial body short-circuits.
    """
    deadline = time.time() + CSR_FETCH_BUDGET_S
    attempt = 0
    last_err = None
    while time.time() < deadline:
        attempt += 1
        try:
            req = urllib.request.Request(
                CSR_AUTHORS_URL,
                headers={"User-Agent": "Toolathlon-grader/1.0"},
            )
            with urllib.request.urlopen(req, timeout=30) as r:
                body = r.read().decode("utf-8", errors="ignore")
            if len(body) > 1_000_000:  # sanity: real file is ~30 MB; reject obvious truncation
                print(f"Fetched CSRankings CSV ({len(body)} bytes) on attempt {attempt}")
                return body
            last_err = f"unexpectedly small body ({len(body)} bytes)"
        except (urllib.error.URLError, urllib.error.HTTPError, OSError, TimeoutError) as e:
            last_err = repr(e)
        remaining = deadline - time.time()
        if remaining <= 0:
            break
        sleep_for = min(CSR_FETCH_POLL_INTERVAL_S, remaining)
        print(f"  attempt {attempt}: {last_err}; sleeping {sleep_for:.1f}s")
        time.sleep(sleep_for)
    raise RuntimeError(
        f"Failed to fetch CSRankings CSV after {attempt} attempt(s): {last_err}"
    )


def _compute_us_top30_ranks(csv_body: str) -> list:
    """Return [(dept, rank)] for US top-30 in AI ex-IR for CSR_TARGET_YEAR.

    Uses CSRankings' own scoring algorithm: per (dept, area) sum of
    adjustedcount within the year, then geometric mean of (1 + count)
    across the 4 AI areas minus 1.  Ties broken by department name
    ascending (same convention CSRankings uses).
    """
    counts = defaultdict(lambda: defaultdict(float))
    reader = csv.DictReader(io.StringIO(csv_body))
    for row in reader:
        venue = row.get("area")
        if venue not in VENUE2AREA:
            continue
        try:
            if int(row["year"]) != CSR_TARGET_YEAR:
                continue
        except (ValueError, KeyError):
            continue
        try:
            counts[row["dept"]][VENUE2AREA[venue]] += float(row["adjustedcount"])
        except (ValueError, KeyError):
            continue

    ranked = []
    for dept, by_area in counts.items():
        prod = 1.0
        for a in CSR_AREAS:
            prod *= 1.0 + by_area.get(a, 0.0)
        geo = prod ** (1.0 / len(CSR_AREAS)) - 1.0
        ranked.append((geo, dept))
    # Sort by geomean desc, then department name asc
    ranked.sort(key=lambda x: (-x[0], x[1]))

    us_top30 = []
    for geo, dept in ranked:
        if dept in US_INSTITUTIONS:
            us_top30.append((dept, len(us_top30) + 1))
            if len(us_top30) == 30:
                break
    return us_top30


def compute_expected_live() -> list:
    """Deterministic expected output for the current CSRankings state.

    Returns the list of universities the agent should produce, sorted by
    (miles asc, rank asc) as the task prompt requires.  Every value
    (rank, distance, city) is derived from primary sources or the fixed
    distance table; no human-curated frozen file.
    """
    csv_body = _fetch_csrankings_csv_with_retry()
    us_top30 = _compute_us_top30_ranks(csv_body)
    print(f"CSRankings live US top-30 for {CSR_TARGET_YEAR} AI (ex-IR):")
    for dept, rank in us_top30:
        in_range = "  ≤500mi" if dept in DISTANCES_MILES else ""
        print(f"  rank {rank:2d}: {dept}{in_range}")

    # Fail-loud safety net: warn when a top-30 school is missing from the
    # hardcoded DISTANCES_MILES table.  Most missing schools are legitimately
    # far from LA (East Coast, Midwest, etc.) and will be correctly dropped,
    # but if a NEAR-LA school newly enters the top-30 and isn't in the
    # table, the grader would silently miss it.  The warning makes that
    # gap visible so operators can extend DISTANCES_MILES on the next pass.
    missing = [dept for dept, _ in us_top30 if dept not in DISTANCES_MILES]
    if missing:
        print(f"\n⚠ {len(missing)} top-30 school(s) have no entry in DISTANCES_MILES "
              f"(treated as out-of-range — extend the table if any are actually ≤500 mi from LA):")
        for dept in missing:
            print(f"    {dept}")
        print()

    expected = []
    for dept, rank in us_top30:
        if dept not in DISTANCES_MILES:
            continue
        city, miles = DISTANCES_MILES[dept]
        if miles > 500:
            continue
        expected.append({
            "university": dept,
            "city": city,
            "cs_ranking_rank": rank,
            "car_drive_miles": miles,
        })
    expected.sort(key=lambda x: (x["car_drive_miles"], x["cs_ranking_rank"]))
    return expected


# ── Comparison ──────────────────────────────────────────────────────────


def check(needed_info, groundtruth_info, allow_adjacent_swap_if_close: bool = True):
    """Compare agent's submission list against the expected list.

    cs_ranking_rank: exact (live grader → no drift).
    car_drive_miles: 10% / ±2mi tolerance (Google Maps routing noise).
    city / university: normalize_str compare, with abbreviation table.

    ``allow_adjacent_swap_if_close``: if True, when two adjacent entries
    in expected have driving distances within ≤5 mi (e.g. UCSB at 109 and
    UCSD at 110), permit the agent to swap them.  Routing noise at very
    close distances can flip their order regardless of the canonical
    sort, so allowing this avoids a false negative.
    """
    # Build an alternate expected list with permissible adjacent swaps
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
            if given_school['cs_ranking_rank'] != gt_school['cs_ranking_rank']:
                return False, (f"position {idx+1}: rank mismatch — "
                               f"agent {given_school['cs_ranking_rank']} vs "
                               f"expected {gt_school['cs_ranking_rank']} "
                               f"({gt_school['university']})")
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
            # Expand "Univ." → "University" on BOTH sides so the live grader's
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
    needed_info_file = os.path.join(args.agent_workspace, "AI_univ_LA_500miles_Top30_2024.json")
    if not os.path.exists(needed_info_file):
        print(f"File {needed_info_file} does not exist")
        return False
    needed_info = read_json(needed_info_file)

    try:
        groundtruth_info = compute_expected_live()
    except Exception as e:
        print(f"Failed to compute live groundtruth from CSRankings: {e}")
        return False

    print(f"\nExpected {len(groundtruth_info)} schools at this CSRankings snapshot:")
    for s in groundtruth_info:
        print(f"  rank {s['cs_ranking_rank']:2d} | {s['car_drive_miles']:3d}mi | {s['university']}")
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
