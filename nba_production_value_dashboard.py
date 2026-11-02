import re
import io
import unicodedata
from datetime import date, datetime
from zoneinfo import ZoneInfo

import pandas as pd
import requests
import streamlit as st
from rapidfuzz import fuzz, process

# Optional PDF parser used by the official NBA injury-report page.
try:
    import pdfplumber
except ImportError:
    pdfplumber = None


# =====================================================
# PAGE CONFIGURATION
# =====================================================

st.set_page_config(
    page_title="NBA Production Value Dashboard",
    page_icon="🏀",
    layout="wide",
)
st.title("NBA Production Value Dashboard")

ET = ZoneInfo("America/New_York")
NBA_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 Chrome/125.0 Safari/537.36"
    ),
    "Referer": "https://www.nba.com/",
    "Origin": "https://www.nba.com",
}


# =====================================================
# LOAD LOCAL PLAYER METRICS
# =====================================================

@st.cache_data
def load_data():
    wins = pd.read_csv("productionvalue.csv")
    vorp = pd.read_csv("vorp.csv")
    wins.columns = wins.columns.str.strip()
    vorp.columns = vorp.columns.str.strip()

    required_wins = {"Team", "Player", "Wins Added"}
    required_vorp = {"Team", "Name", "Rating"}
    if not required_wins.issubset(wins.columns):
        raise ValueError(f"productionvalue.csv needs columns: {required_wins}")
    if not required_vorp.issubset(vorp.columns):
        raise ValueError(f"vorp.csv needs columns: {required_vorp}")

    for df, player_col in [(wins, "Player"), (vorp, "Name")]:
        df[player_col] = df[player_col].fillna("").astype(str).str.strip()
        df["Team"] = df["Team"].fillna("").astype(str).str.strip()
        df.drop(
            df[(df[player_col] == "") | (df["Team"] == "")].index,
            inplace=True,
        )

    wins["Wins Added"] = pd.to_numeric(wins["Wins Added"], errors="coerce")
    vorp["Rating"] = pd.to_numeric(vorp["Rating"], errors="coerce")
    wins = wins.dropna(subset=["Wins Added"]).copy()
    vorp = vorp.dropna(subset=["Rating"]).copy()

    # Keep optional numeric columns numeric where possible.
    for col in wins.columns:
        if col not in ("Team", "Player"):
            wins[col] = pd.to_numeric(wins[col], errors="ignore")
    return wins, vorp


try:
    wins_df, vorp_df = load_data()
except Exception as error:
    st.error(f"Error loading local CSV data: {error}")
    st.stop()


# =====================================================
# NORMALIZATION AND PLAYER MATCHING
# =====================================================

def normalize_name(name):
    """Normalize accents/punctuation and convert 'Last, First' to 'First Last'."""
    name = unicodedata.normalize("NFKD", str(name))
    name = "".join(c for c in name if not unicodedata.combining(c))
    name = name.lower().strip()
    if "," in name:
        parts = [part.strip() for part in name.split(",", 1)]
        if len(parts) == 2 and parts[0] and parts[1]:
            name = f"{parts[1]} {parts[0]}"
    name = name.replace("’", "'").replace("‘", "'")
    name = re.sub(r"[^a-z0-9\s]", " ", name)
    return re.sub(r"\s+", " ", name).strip()


def compact_name(name):
    """Remove punctuation and whitespace so apostrophe/hyphen variants match."""
    return re.sub(r"[^a-z0-9]", "", normalize_name(name))


def normalize_team(team):
    return normalize_name(team)


wins_names = sorted(wins_df["Player"].unique())
vorp_names = sorted(vorp_df["Name"].unique())
name_map = {("wins", name): name for name in wins_names}
wins_by_normalized = {}
for name in wins_names:
    wins_by_normalized.setdefault(normalize_name(name), []).append(name)
wins_by_compact = {}
for name in wins_names:
    wins_by_compact.setdefault(compact_name(name), []).append(name)

match_suggestions = []
for vorp_name in vorp_names:
    normalized = normalize_name(vorp_name)
    exact = wins_by_normalized.get(normalized, [])
    if len(exact) == 1:
        name_map[("vorp", vorp_name)] = exact[0]
        continue
    if len(exact) > 1:
        name_map[("vorp", vorp_name)] = f"VORP only: {vorp_name}"
        match_suggestions.append({
            "VORP Name": vorp_name, "Suggested Match": "",
            "Score": 100, "Status": "Ambiguous exact match",
        })
        continue

    vorp_teams = set(
        vorp_df.loc[vorp_df["Name"] == vorp_name, "Team"].map(normalize_team)
    )
    same_team_candidates = []
    for wins_name in wins_names:
        wins_teams = set(
            wins_df.loc[wins_df["Player"] == wins_name, "Team"].map(normalize_team)
        )
        if vorp_teams & wins_teams:
            same_team_candidates.append(wins_name)
    candidates = same_team_candidates or wins_names
    results = process.extract(vorp_name, candidates, scorer=fuzz.WRatio, limit=2)
    if not results:
        name_map[("vorp", vorp_name)] = f"VORP only: {vorp_name}"
        continue
    best_name, best_score, _ = results[0]
    second_score = results[1][1] if len(results) > 1 else 0
    if best_score >= 96 and best_score - second_score >= 5:
        name_map[("vorp", vorp_name)] = best_name
    else:
        name_map[("vorp", vorp_name)] = f"VORP only: {vorp_name}"
        match_suggestions.append({
            "VORP Name": vorp_name, "Suggested Match": best_name,
            "Score": round(best_score, 1), "Status": "Review required",
        })

st.header("Player Name Matching")
if "name_overrides" not in st.session_state:
    st.session_state["name_overrides"] = {}

with st.expander("Review possible name differences"):
    st.caption(
        "Automatic matches require high confidence. Review uncertain matches. "
        "Choose 'Keep separate' if these are different players."
    )
    if match_suggestions:
        with st.form("name_matching_form"):
            new_overrides = {}
            for i, item in enumerate(match_suggestions):
                vorp_name = item["VORP Name"]
                suggested = item["Suggested Match"]
                st.write(
                    f"**{vorp_name}** — suggestion: "
                    f"{suggested or 'No safe suggestion'} "
                    f"(score {item['Score']}; {item['Status']})"
                )
                options = ["Keep separate"] + wins_names
                saved = st.session_state["name_overrides"].get(vorp_name, suggested)
                index = options.index(saved) if saved in options else 0
                selected = st.selectbox(
                    f"Match {vorp_name} to", options, index=index, key=f"match_{i}"
                )
                new_overrides[vorp_name] = selected if selected != "Keep separate" else None
            if st.form_submit_button("Save name matches"):
                st.session_state["name_overrides"] = new_overrides
                st.rerun()
    else:
        st.success("All player names matched automatically.")

for vorp_name, wins_name in st.session_state["name_overrides"].items():
    name_map[("vorp", vorp_name)] = (
        wins_name if wins_name else f"VORP only: {vorp_name}"
    )


def canonical_player(source, name):
    return name_map.get((source, name), name)


# =====================================================
# TEAM NORMALIZATION / NBA TEAM ABBREVIATIONS
# =====================================================

TEAM_ALIASES = {
    "atl": "atlanta hawks", "atlanta hawks": "atlanta hawks",
    "bos": "boston celtics", "boston celtics": "boston celtics",
    "bkn": "brooklyn nets", "brk": "brooklyn nets", "brooklyn nets": "brooklyn nets",
    "cha": "charlotte hornets", "cho": "charlotte hornets", "charlotte hornets": "charlotte hornets",
    "chi": "chicago bulls", "chicago bulls": "chicago bulls",
    "cle": "cleveland cavaliers", "cleveland cavaliers": "cleveland cavaliers",
    "dal": "dallas mavericks", "dallas mavericks": "dallas mavericks",
    "den": "denver nuggets", "denver nuggets": "denver nuggets",
    "det": "detroit pistons", "detroit pistons": "detroit pistons",
    "gs": "golden state warriors", "gsw": "golden state warriors", "golden state warriors": "golden state warriors",
    "hou": "houston rockets", "houston rockets": "houston rockets",
    "ind": "indiana pacers", "indiana pacers": "indiana pacers",
    "lac": "los angeles clippers", "la clippers": "los angeles clippers", "los angeles clippers": "los angeles clippers",
    "lal": "los angeles lakers", "la lakers": "los angeles lakers", "los angeles lakers": "los angeles lakers",
    "mem": "memphis grizzlies", "memphis grizzlies": "memphis grizzlies",
    "mia": "miami heat", "miami heat": "miami heat",
    "mil": "milwaukee bucks", "milwaukee bucks": "milwaukee bucks",
    "min": "minnesota timberwolves", "minnesota timberwolves": "minnesota timberwolves",
    "no": "new orleans pelicans", "nop": "new orleans pelicans", "new orleans pelicans": "new orleans pelicans",
    "ny": "new york knicks", "nyk": "new york knicks", "new york knicks": "new york knicks",
    "okc": "oklahoma city thunder", "oklahoma city thunder": "oklahoma city thunder",
    "orl": "orlando magic", "orlando magic": "orlando magic",
    "phi": "philadelphia 76ers", "phila": "philadelphia 76ers", "philadelphia 76ers": "philadelphia 76ers",
    "phx": "phoenix suns", "pho": "phoenix suns", "phoenix suns": "phoenix suns",
    "por": "portland trail blazers", "portland trail blazers": "portland trail blazers",
    "sac": "sacramento kings", "sacramento kings": "sacramento kings",
    "sa": "san antonio spurs", "sas": "san antonio spurs", "san antonio spurs": "san antonio spurs",
    "tor": "toronto raptors", "toronto raptors": "toronto raptors",
    "utah": "utah jazz", "uta": "utah jazz", "utah jazz": "utah jazz",
    "was": "washington wizards", "wsh": "washington wizards", "washington wizards": "washington wizards",
}


def team_key(team):
    raw = normalize_team(team)
    return TEAM_ALIASES.get(raw, raw)


wins_teams = sorted(wins_df["Team"].unique())
vorp_teams = sorted(vorp_df["Team"].unique())


def get_wins_team(team_name):
    key = team_key(team_name)
    candidates = [t for t in wins_teams if team_key(t) == key]
    return candidates[0] if len(candidates) == 1 else None


def get_vorp_team(wins_team):
    key = team_key(wins_team)
    candidates = [t for t in vorp_teams if team_key(t) == key]
    return candidates[0] if len(candidates) == 1 else None


# =====================================================
# AUTOMATIC SCHEDULE FETCH (NBA CDN SCOREBOARD)
# =====================================================

@st.cache_data(ttl=900, show_spinner=False)
def fetch_schedule(selected_date: date):
    """Fetch NBA games for a selected date from ESPN's public scoreboard."""
    url = "https://site.api.espn.com/apis/site/v2/sports/basketball/nba/scoreboard"
    response = requests.get(
        url,
        params={"dates": selected_date.strftime("%Y%m%d")},
        headers={"User-Agent": "Mozilla/5.0"},
        timeout=20,
    )
    response.raise_for_status()
    payload = response.json()
    rows = []

    for event in payload.get("events", []):
        competitions = event.get("competitions", [])
        if not competitions:
            continue
        competition = competitions[0]
        competitors = competition.get("competitors", [])
        home = next((c for c in competitors if c.get("homeAway") == "home"), None)
        away = next((c for c in competitors if c.get("homeAway") == "away"), None)
        if not home or not away:
            continue

        rows.append({
            "Game ID": event.get("id", ""),
            "Away Team": away.get("team", {}).get("displayName", ""),
            "Home Team": home.get("team", {}).get("displayName", ""),
            "Tip-off (ET)": event.get("date", ""),
            "Status": competition.get("status", {}).get("type", {}).get("shortDetail", ""),
        })

    return pd.DataFrame(
        rows,
        columns=["Game ID", "Away Team", "Home Team", "Tip-off (ET)", "Status"],
    )

# =====================================================
# OFFICIAL INJURY REPORT FETCH / PDF PARSING
# =====================================================

NBA_INJURY_URL = "https://official.nba.com/nba-injury-report-2026-27-season/"


@st.cache_data(ttl=300, show_spinner=False)
def fetch_injury_report(selected_date: date):
    """Read ESPN's public structured injury feed and flatten its nested JSON.

    ESPN's feed is not consistently shaped like a list of {athlete, team,
    injury} objects. Depending on the response, athlete names and injury
    statuses can be nested under display_name/displayName and injuries.
    This walker carries player/team context down through the JSON tree.
    """
    url = "https://site.api.espn.com/apis/site/v2/sports/basketball/nba/injuries"
    response = requests.get(url, headers=NBA_HEADERS, timeout=25)
    response.raise_for_status()
    payload = response.json()

    records = []
    seen = set()

    def first_text(value):
        if isinstance(value, str):
            return value.strip()
        if isinstance(value, dict):
            for key in ("displayName", "display_name", "fullName", "name", "abbreviation"):
                if isinstance(value.get(key), str):
                    return value[key].strip()
        return ""

    def detail_text(node):
        parts = []
        for key in ("shortComment", "longComment", "comment", "detail", "details", "description", "summary", "type"):
            value = node.get(key)
            if isinstance(value, str) and value.strip():
                parts.append(value.strip())
            elif isinstance(value, dict):
                label = first_text(value)
                if label:
                    parts.append(label)
        return " — ".join(dict.fromkeys(parts))

    def walk(node, player="", team=""):
        if isinstance(node, list):
            for child in node:
                walk(child, player, team)
            return
        if not isinstance(node, dict):
            return

        team = first_text(node.get("team")) or team
        athlete = node.get("athlete")
        if athlete is not None:
            player = first_text(athlete) or player
        # Athlete objects carry display_name, but injury objects generally do not.
        if not player and ("injuries" in node or "athlete" in node):
            player = first_text(node.get("display_name") or node.get("displayName") or node.get("fullName"))
        elif not player and "display_name" in node:
            player = first_text(node["display_name"])

        status = first_text(node.get("gameStatus") or node.get("game_status") or node.get("status"))
        reason = detail_text(node)
        injury = node.get("injury")
        if isinstance(injury, dict):
            status = first_text(injury.get("gameStatus") or injury.get("status")) or status
            extra = detail_text(injury)
            reason = " — ".join(filter(None, (reason, extra)))

        # Do not emit container/team nodes as player injuries. Injury entries
        # carry either status plus context or a nested injury object.
        if player and (status or reason) and ("status" in node or "gameStatus" in node or "game_status" in node or "injury" in node):
            key = (team.casefold(), player.casefold(), status.casefold(), reason.casefold())
            if key not in seen:
                seen.add(key)
                records.append({"Team": team, "Player": player, "Status": status, "Reason": reason})

        for key, value in node.items():
            if isinstance(value, (dict, list)):
                walk(value, player, team)

    walk(payload)

    if not records:
        top_keys = ", ".join(list(payload.keys())[:20]) if isinstance(payload, dict) else type(payload).__name__
        raise RuntimeError(
            "ESPN returned JSON, but no player/status records could be extracted. "
            f"Top-level structure: {top_keys}. The dashboard will not silently "
            "claim that injuries were applied."
        )

    result = pd.DataFrame(records)
    return result, url, f"ESPN structured injury feed (latest available; selected schedule date: {selected_date:%B %d, %Y})"


# =====================================================
# CALCULATIONS
# =====================================================

def calculate_wins_added(team, excluded_canonical):
    data = wins_df[wins_df["Team"] == team].copy()
    data["_canonical"] = data["Player"].map(lambda n: canonical_player("wins", n))
    data = data[~data["_canonical"].isin(excluded_canonical)]
    return float(data["Wins Added"].sum())


def calculate_vorp(team, excluded_canonical):
    vorp_team = get_vorp_team(team)
    if vorp_team is None:
        return None
    data = vorp_df[vorp_df["Team"] == vorp_team].copy()
    data["_canonical"] = data["Name"].map(lambda n: canonical_player("vorp", n))
    data = data[~data["_canonical"].isin(excluded_canonical)]
    return float(data["Rating"].sum())


def prediction(team_a, team_b, score_a, score_b, metric):
    if score_a is None or score_b is None:
        return f"{metric}: unable to match both teams."
    if abs(score_a - score_b) < 1e-9:
        return f"{metric}: Even matchup."
    favored = team_a if score_a > score_b else team_b
    return f"{metric}: {favored} favored by {abs(score_a - score_b):.2f}."


def injury_status_key(status):
    """Find the actual game availability designation, including ESPN variants."""
    raw = str(status or "").lower()
    value = normalize_name(raw)
    # Do not treat ESPN's broad 'day-to-day' label as definitive when an
    # explicit game designation (e.g. Doubtful) appears in the same field.
    for candidate in ("out", "doubtful", "questionable", "probable", "available"):
        if re.search(r"(?<![a-z])" + candidate + r"(?![a-z])", raw):
            return candidate
    if value in {"day to day", "day today", "dtd", "game time decision", "gtd"}:
        return "questionable"
    if value in {"injured reserve", "inactive", "suspended"}:
        return "out"
    return value


def effective_injury_status(status, reason):
    """Interpret game designation from ESPN status AND its injury notes.

    ESPN often says Day-To-Day at the top level. Notes may contain both
    'originally questionable' and 'now doubtful'; choose the latest current
    designation rather than the first keyword found in the paragraph.
    """
    raw_status = str(status or "")
    key = injury_status_key(raw_status)
    if key in {"out", "doubtful", "questionable", "probable", "available"} and normalize_name(raw_status) not in {"day to day", "day today", "dtd", "gtd"}:
        return key

    notes = str(reason or "").lower()
    # Prioritize the current designation over historic mentions.
    current_patterns = [
        r"(?:now|currently|has been|was|is|listed as|listed|downgraded to|upgraded to|status is|designation is)\s+(?:listed\s+as\s+)?(out|doubtful|questionable|probable|available)\b",
        r"\b(out|doubtful|questionable|probable|available)\s+(?:for|against|tonight|today|on monday|on tuesday|on wednesday|on thursday|on friday|on saturday|on sunday)\b",
    ]
    matches = []
    for pattern in current_patterns:
        for match in re.finditer(pattern, notes):
            prefix = notes[max(0, match.start()-32):match.start()]
            if re.search(r"\b(originally|previously|initially|earlier)\s*$", prefix):
                continue
            matches.append((match.start(), match.group(1)))
    if matches:
        return max(matches, key=lambda item: item[0])[1]

    # Single unambiguous designation in notes is useful; if multiple are
    # present without temporal context, do not invent which one is latest.
    mentions = set(re.findall(r"\b(out|doubtful|questionable|probable|available)\b", notes))
    if len(mentions) == 1:
        return next(iter(mentions))
    return key


def player_metric_teams(player_name):
    """Return normalized teams for a canonical player in the Wins Added dataset."""
    rows = wins_df.loc[wins_df["Player"] == player_name, "Team"]
    return {team_key(team) for team in rows if str(team).strip()}


def match_injury_to_player(injury_player, team_name=None):
    """Match Last, First names and punctuation variants, using team to disambiguate."""
    normalized = normalize_name(injury_player)
    exact = wins_by_normalized.get(normalized, [])
    if len(exact) == 1:
        return exact[0], "Exact match"
    if len(exact) > 1:
        return None, "Ambiguous exact match"

    compact_matches = wins_by_compact.get(compact_name(injury_player), [])
    if len(compact_matches) == 1:
        return compact_matches[0], "Match ignoring punctuation/spacing"
    if len(compact_matches) > 1 and team_name:
        key = team_key(team_name)
        on_team = [
            player for player in compact_matches
            if any(
                team_key(team) == key
                for team in wins_df.loc[wins_df["Player"] == player, "Team"]
            )
        ]
        if len(on_team) == 1:
            return on_team[0], "Punctuation-insensitive match + team"
    if len(compact_matches) > 1:
        return None, "Ambiguous punctuation-insensitive match"

    candidates = wins_names
    if team_name:
        key = team_key(team_name)
        team_candidates = [
            p for p in wins_names
            if any(team_key(t) == key for t in wins_df.loc[wins_df["Player"] == p, "Team"])
        ]
        if team_candidates:
            candidates = team_candidates
    results = process.extract(injury_player, candidates, scorer=fuzz.WRatio, limit=2)
    if not results:
        return None, "No match"
    best, score, _ = results[0]
    second = results[1][1] if len(results) > 1 else 0
    if score >= 96 and score - second >= 5:
        return best, f"Fuzzy match ({score:.0f})"
    return None, f"Needs review ({score:.0f})"


# =====================================================
# DAILY AUTOMATED PICKS
# =====================================================

st.header("Daily Automated Picks")
st.caption(
    "Schedule and injury reports are fetched online. Picks are comparisons of "
    "the team totals in your CSVs, not calibrated win probabilities."
)

selected_date = st.date_input("Game date", value=datetime.now(ET).date(), key="daily_game_date")
status_policy = st.radio(
    "Injury exclusion rule",
    [
        "Exclude Out only",
        "Exclude Out + Doubtful",
        "Exclude Out + Doubtful + Questionable",
    ],
    horizontal=True,
)
excluded_statuses = {
    "Exclude Out only": {"out"},
    "Exclude Out + Doubtful": {"out", "doubtful"},
    "Exclude Out + Doubtful + Questionable": {"out", "doubtful", "questionable"},
}[status_policy]

# ESPN sometimes reports Day-to-Day instead of the actual game designation.
# Let users force an exclusion without altering their source CSVs.
manual_excluded_players = st.multiselect(
    "Additional players to exclude (manual injury overrides)",
    options=sorted(wins_names),
    default=[],
    help="Use when ESPN reports Day-to-Day but the game report says Doubtful or Out. "
         "These players are removed from both Wins Added and VORP totals.",
)

if st.button("Refresh schedule and injury report", type="primary"):
    fetch_schedule.clear()
    fetch_injury_report.clear()
    st.rerun()

schedule_df = None
injuries_df = None
injury_source_url = None
injury_label = None
schedule_error = None
injury_error = None

with st.spinner("Loading NBA schedule..."):
    try:
        schedule_df = fetch_schedule(selected_date)
    except Exception as e:
        schedule_error = str(e)

with st.spinner("Loading NBA injury feed..."):
    try:
        injuries_df, injury_source_url, injury_label = fetch_injury_report(selected_date)
    except Exception as e:
        injury_error = str(e)

if schedule_error:
    st.error(f"Schedule unavailable: {schedule_error}")
elif schedule_df is None or schedule_df.empty:
    st.info(f"No games were returned for {selected_date:%B %d, %Y}.")
else:
    st.caption(f"{len(schedule_df)} game(s) scheduled for {selected_date:%B %d, %Y}.")

if injury_error:
    st.warning(
        f"Injury feed unavailable: {injury_error} "
        "Daily picks will not be labelled injury-adjusted."
    )
elif injuries_df is not None:
    st.caption(f"Injury source: {injury_source_url}")
    if injury_label:
        st.caption(f"Report link label: {injury_label}")
    st.caption(f"Parsed {len(injuries_df)} player status row(s).")

if schedule_df is not None and not schedule_df.empty:
    injury_rows = []
    unmatched_injuries = []
    if injuries_df is not None and not injuries_df.empty:
        for _, row in injuries_df.iterrows():
            player = str(row["Player"]).strip()
            status = str(row["Status"]).strip()
            team = str(row["Team"]).strip()
            canonical, match_note = match_injury_to_player(player, team)
            norm_status = effective_injury_status(status, str(row.get("Reason", "")))
            injury_rows.append({
                "Team": team,
                "Player": player,
                "Status": status,
                "Normalized Status": norm_status,
                "Reason": str(row.get("Reason", "")),
                "Matched Player": canonical or "",
                "Match Result": match_note,
                "Excluded by rule": norm_status in excluded_statuses,
            })
            if canonical is None:
                unmatched_injuries.append({
                    "Team": team, "Player": player, "Status": status,
                    "Match Result": match_note,
                })

    mapped_injuries = pd.DataFrame(injury_rows)
    excluded_canonical = set()
    if not mapped_injuries.empty:
        for _, row in mapped_injuries.iterrows():
            if row["Excluded by rule"] and row["Matched Player"]:
                excluded_canonical.add(row["Matched Player"])

    # Manual overrides use the same canonical player names as the model totals.
    # They are additive to the automatic injury exclusions.
    excluded_canonical.update(manual_excluded_players)

    pick_rows = []
    for _, game in schedule_df.iterrows():
        away_raw, home_raw = game["Away Team"], game["Home Team"]
        away_team, home_team = get_wins_team(away_raw), get_wins_team(home_raw)
        if away_team is None or home_team is None:
            pick_rows.append({
                "Away Team": away_raw, "Home Team": home_raw,
                "Away Wins Added": None, "Home Wins Added": None,
                "Wins Added Pick": "Team mapping required",
                "Wins Added Differential": None,
                "Away VORP": None, "Home VORP": None,
                "VORP Pick": "Team mapping required",
                "VORP Differential": None,
                "Combined Pick": "Team mapping required",
                "Injury Adjusted": injuries_df is not None,
            })
            continue

        away_wins = calculate_wins_added(away_team, excluded_canonical)
        home_wins = calculate_wins_added(home_team, excluded_canonical)
        away_vorp = calculate_vorp(away_team, excluded_canonical)
        home_vorp = calculate_vorp(home_team, excluded_canonical)

        wins_text = prediction(away_team, home_team, away_wins, home_wins, "Wins Added")
        vorp_text = prediction(away_team, home_team, away_vorp, home_vorp, "VORP")

        wins_pick = (
            away_team if away_wins > home_wins else
            home_team if home_wins > away_wins else "Even"
        )
        vorp_pick = (
            away_team if away_vorp is not None and home_vorp is not None and away_vorp > home_vorp else
            home_team if away_vorp is not None and home_vorp is not None and home_vorp > away_vorp else
            "Even/Unavailable"
        )
        if wins_pick == vorp_pick and wins_pick not in ("Even", "Even/Unavailable"):
            combined = wins_pick
        elif wins_pick == "Even" and vorp_pick not in ("Even/Unavailable", "Even"):
            combined = vorp_pick
        elif vorp_pick == "Even" and wins_pick not in ("Even/Unavailable", "Even"):
            combined = wins_pick
        else:
            combined = "Split / no clear pick"

        game_exclusions = []
        matchup_team_keys = {team_key(away_team), team_key(home_team)}
        for player in manual_excluded_players:
            if matchup_team_keys.intersection(player_metric_teams(player)):
                game_exclusions.append(f"{player} (manual override)")
        if not mapped_injuries.empty:
            matchup_team_keys = {team_key(away_team), team_key(home_team)}
            for _, inj in mapped_injuries.iterrows():
                canonical = str(inj["Matched Player"]).strip()
                if canonical and canonical in excluded_canonical:
                    # Prefer the player's team in productionvalue.csv; use the injury
                    # report's team field only as a fallback when metric data lacks it.
                    metric_teams = player_metric_teams(canonical)
                    injury_team_key = team_key(inj["Team"])
                    player_team_keys = metric_teams or ({injury_team_key} if injury_team_key else set())
                    if matchup_team_keys.intersection(player_team_keys):
                        game_exclusions.append(f"{inj['Player']} ({inj['Status']})")

        pick_rows.append({
            "Away Team": away_team,
            "Home Team": home_team,
            "Away Wins Added": round(away_wins, 2),
            "Home Wins Added": round(home_wins, 2),
            "Wins Added Pick": wins_pick,
            "Wins Added Differential": round(abs(away_wins - home_wins), 2),
            "Away VORP": None if away_vorp is None else round(away_vorp, 2),
            "Home VORP": None if home_vorp is None else round(home_vorp, 2),
            "VORP Pick": vorp_pick,
            "VORP Differential": (
                None if away_vorp is None or home_vorp is None
                else round(abs(away_vorp - home_vorp), 2)
            ),
            "Combined Pick": combined,
            "Excluded players": ", ".join(sorted(set(game_exclusions))) if game_exclusions else "No matched eligible injuries for this team",
            "Injury Adjusted": injuries_df is not None,
        })

    picks_df = pd.DataFrame(pick_rows)
    st.subheader("Today's Picks")
    st.caption(
        "Differentials are the absolute differences between the teams' summed metric "
        "totals after the selected injury exclusions. They are not projected point spreads."
    )
    if injury_error:
        st.warning("These picks are NOT verified as injury-adjusted because the injury report failed to load.")
    else:
        st.caption(f"Excluded statuses under current rule: {', '.join(sorted(excluded_statuses))}.")
    if injuries_df is not None:
        total_injury_rows = len(injuries_df)
        matched_count = (
            int(mapped_injuries["Matched Player"].fillna("").astype(str).str.strip().ne("").sum())
            if not mapped_injuries.empty else 0
        )
        eligible_count = (
            int(mapped_injuries["Excluded by rule"].sum())
            if not mapped_injuries.empty else 0
        )
        excluded_count = len(excluded_canonical)
        st.caption(
            f"Injury feed check: {total_injury_rows} rows parsed · "
            f"{matched_count} matched to your player data · "
            f"{eligible_count} statuses fit the selected rule · "
            f"{excluded_count} unique player(s) removed from the metric totals "
            f"(including {len(manual_excluded_players)} manual override(s))."
        )
    display_columns = [
        "Away Team", "Home Team",
        "Away Wins Added", "Home Wins Added", "Wins Added Pick", "Wins Added Differential",
        "Away VORP", "Home VORP", "VORP Pick", "VORP Differential",
        "Combined Pick", "Excluded players",
    ]
    display_columns = [column for column in display_columns if column in picks_df.columns]
    st.dataframe(picks_df[display_columns], use_container_width=True, hide_index=True)

    if not mapped_injuries.empty:
        st.subheader("Parsed Injury Report")
        st.dataframe(
            mapped_injuries[
                ["Team", "Player", "Status", "Normalized Status", "Reason", "Matched Player", "Match Result", "Excluded by rule"]
            ],
            use_container_width=True,
            hide_index=True,
        )
    if injuries_df is not None and not injuries_df.empty:
        with st.expander("Inspect raw ESPN injury feed"):
            st.caption("These are the player/status rows received from ESPN before matching them to productionvalue.csv.")
            st.dataframe(injuries_df, use_container_width=True, hide_index=True)

    if unmatched_injuries:
        with st.expander(f"Review {len(unmatched_injuries)} injury name(s) that were not confidently matched"):
            st.caption("These players were not automatically excluded from the metrics. Verify names/teams before using the picks.")
            st.dataframe(pd.DataFrame(unmatched_injuries), use_container_width=True, hide_index=True)


# =====================================================
# MANUAL MATCHUP SELECTOR (RETAINED)
# =====================================================

st.header("Manual Team Win Prediction (Wins Added & VORP)")
col1, col2 = st.columns(2)
with col1:
    team_1 = st.selectbox("Select Team 1", wins_teams, key="team_1")
with col2:
    available_teams = [team for team in wins_teams if team != team_1]
    team_2 = st.selectbox("Select Team 2", available_teams, key="team_2")

def get_team_player_options(team):
    options = set()
    for player in wins_df.loc[wins_df["Team"] == team, "Player"].unique():
        options.add(canonical_player("wins", player))
    vorp_team = get_vorp_team(team)
    if vorp_team:
        for player in vorp_df.loc[vorp_df["Team"] == vorp_team, "Name"].unique():
            options.add(canonical_player("vorp", player))
    return sorted(options)

st.subheader("Player Availability")
st.caption("Exclude a player once; the exclusion affects both metrics when the identity is matched.")
col1, col2 = st.columns(2)
with col1:
    excluded_1 = st.multiselect(
        f"Exclude players from {team_1}", get_team_player_options(team_1), key="excluded_1"
    )
with col2:
    excluded_2 = st.multiselect(
        f"Exclude players from {team_2}", get_team_player_options(team_2), key="excluded_2"
    )

wins_1 = calculate_wins_added(team_1, set(excluded_1))
wins_2 = calculate_wins_added(team_2, set(excluded_2))
vorp_1 = calculate_vorp(team_1, set(excluded_1))
vorp_2 = calculate_vorp(team_2, set(excluded_2))

col1, col2 = st.columns(2)
with col1:
    st.subheader(team_1)
    st.metric("Wins Added", f"{wins_1:.2f}")
    st.metric("VORP", "Team mapping unavailable" if vorp_1 is None else f"{vorp_1:.2f}")
with col2:
    st.subheader(team_2)
    st.metric("Wins Added", f"{wins_2:.2f}")
    st.metric("VORP", "Team mapping unavailable" if vorp_2 is None else f"{vorp_2:.2f}")

if st.button("Predict Manual Matchup"):
    st.subheader("Manual Matchup Predictions")
    st.write(prediction(team_1, team_2, wins_1, wins_2, "Wins Added"))
    st.write(prediction(team_1, team_2, vorp_1, vorp_2, "VORP"))


# =====================================================
# TEAM RANKINGS
# =====================================================

st.header("Top Teams by Total Wins Added")
num_teams = st.slider(
    "Number of teams to display", min_value=1,
    max_value=min(30, len(wins_teams)), value=min(10, len(wins_teams)),
)
team_rankings = (
    wins_df.groupby("Team", as_index=False)["Wins Added"]
    .sum().sort_values("Wins Added", ascending=False)
    .head(num_teams).reset_index(drop=True)
)
team_rankings.index += 1
team_rankings["Wins Added"] = team_rankings["Wins Added"].map(lambda x: f"{x:.2f}")
st.table(team_rankings)


# =====================================================
# PLAYER PROFILE LOOKUP
# =====================================================

st.header("Player Profile Lookup")
selected_player = st.selectbox("Select a Player", sorted(wins_df["Player"].unique()))
player_rows = wins_df[wins_df["Player"] == selected_player]
player_data = player_rows.iloc[0]
canonical = canonical_player("wins", selected_player)
matching_vorp_names = [
    name for name in vorp_names if canonical_player("vorp", name) == canonical
]
player_vorp = None
if matching_vorp_names:
    rows = vorp_df[vorp_df["Name"] == matching_vorp_names[0]]
    if not rows.empty:
        player_vorp = rows.iloc[0]["Rating"]

st.subheader(selected_player)
col1, col2, col3 = st.columns(3)
with col1:
    st.metric("Team", player_data["Team"])
    if "Production Value (Millions)" in wins_df.columns and pd.notna(player_data["Production Value (Millions)"]):
        st.metric("Production Value", f"${player_data['Production Value (Millions)']:.2f}M")
    if "Possesions" in wins_df.columns and pd.notna(player_data["Possesions"]):
        st.metric("Possessions", f"{player_data['Possesions']:.0f}")
with col2:
    if "RAPM over replacement" in wins_df.columns and pd.notna(player_data["RAPM over replacement"]):
        st.metric("RAPM Over Replacement", f"{player_data['RAPM over replacement']:.2f}")
    if "AAV" in wins_df.columns and pd.notna(player_data["AAV"]):
        st.metric("AAV", f"${player_data['AAV']:.2f}M")
    st.metric("VORP", "Not found" if player_vorp is None else f"{player_vorp:.2f}")
with col3:
    st.metric("Wins Added", f"{player_data['Wins Added']:.2f}")
    if "Net Value" in wins_df.columns and pd.notna(player_data["Net Value"]):
        st.metric("Net Value", f"{player_data['Net Value']:.2f}M")


# =====================================================
# PLAYER LEADERBOARDS
# =====================================================

st.header("Player Leaderboards")
available_stats = [
    stat for stat in [
        "RAPM over replacement", "Wins Added",
        "Production Value (Millions)", "Net Value",
    ] if stat in wins_df.columns
]
if available_stats:
    stat_choice = st.selectbox("Select Stat to Rank Players", available_stats)
    num_players = st.slider(
        "Number of players to display", min_value=1,
        max_value=len(wins_df), value=min(10, len(wins_df)),
    )
    leaderboard = wins_df.sort_values(stat_choice, ascending=False).head(num_players).reset_index(drop=True)
    leaderboard.index += 1
    leaderboard_display = leaderboard[["Player", "Team", stat_choice]].copy()
    leaderboard_display[stat_choice] = leaderboard_display[stat_choice].map(
        lambda x: f"{x:.2f}" if pd.notna(x) else ""
    )
    st.table(leaderboard_display)
else:
    st.info("No supported player leaderboard stats found in productionvalue.csv.")

st.subheader("VORP Player Leaderboard")
num_vorp_players = st.slider(
    "Number of VORP players to display", min_value=1,
    max_value=len(vorp_df), value=min(10, len(vorp_df)), key="vorp_slider",
)
vorp_leaderboard = vorp_df.sort_values("Rating", ascending=False).head(num_vorp_players).reset_index(drop=True)
vorp_leaderboard.index += 1
vorp_display = vorp_leaderboard[["Name", "Team", "Rating"]].copy()
vorp_display["Rating"] = vorp_display["Rating"].map(lambda x: f"{x:.2f}")
st.table(vorp_display)
