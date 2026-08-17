"""
Core data prep and lineup-lock maths for the Sorare Kickoff Planner.

Kept free of Streamlit imports so the logic can be tested headlessly.

The central idea
----------------
A Sorare lineup locks at the kickoff of the EARLIEST game among the players in
it. Confirmed team sheets drop somewhere between ~90 and ~30 minutes before
each kickoff. So for any two teams F and B:

    lock            = min(kickoff_F, kickoff_B)
    news_time(X)    = kickoff_X - news_lead
    margin(X)       = lock - news_time(X) = news_lead - (kickoff_X - lock)

The binding constraint is always the later of the two kickoffs, so:

    margin_pair = news_lead - abs(kickoff_F - kickoff_B)

margin >= 0  -> the team sheet is out before you lose the ability to edit.
margin <  0  -> you are picking blind on the later team.
"""

from __future__ import annotations

import os

import numpy as np
import pandas as pd

POSITIONS = ["Goalkeeper", "Defender", "Midfielder", "Forward"]

# Sorare league slugs -> human labels. Falls back to a prettified slug.
LEAGUE_LABELS = {
    "1-hnl": "Croatia · HNL",
    "2-bundesliga": "Germany · 2. Bundesliga",
    "austrian-bundesliga": "Austria · Bundesliga",
    "bundesliga-de": "Germany · Bundesliga",
    "campeonato-brasileiro-serie-a": "Brazil · Série A",
    "chinese-super-league": "China · Super League",
    "eliteserien": "Norway · Eliteserien",
    "eredivisie": "Netherlands · Eredivisie",
    "football-league-championship": "England · Championship",
    "j1-league": "Japan · J1 League",
    "jupiler-pro-league": "Belgium · Pro League",
    "k-league-1": "South Korea · K League 1",
    "laliga-es": "Spain · LaLiga",
    "liga-mx": "Mexico · Liga MX",
    "liga-pro": "Ecuador · Liga Pro",
    "ligue-1-fr": "France · Ligue 1",
    "ligue-2-fr": "France · Ligue 2",
    "mlspa": "USA/Canada · MLS",
    "premier-league-gb-eng": "England · Premier League",
    "premiership-gb-sct": "Scotland · Premiership",
    "primeira-liga-pt": "Portugal · Primeira Liga",
    "primera-a": "Colombia · Primera A",
    "primera-division-cl": "Chile · Primera División",
    "primera-division-pe": "Peru · Liga 1",
    "russian-premier-league": "Russia · Premier League",
    "segunda-division-es": "Spain · Segunda División",
    "serie-a-it": "Italy · Serie A",
    "serie-b-it": "Italy · Serie B",
    "spor-toto-super-lig": "Türkiye · Süper Lig",
    "super-league-ch": "Switzerland · Super League",
    "superliga-argentina-de-futbol": "Argentina · Liga Profesional",
    "superliga-dk": "Denmark · Superliga",
}

GRADES = ["Very tough", "Tough", "Average", "Good", "Great"]
GRADE_COLOR = {
    "Very tough": "#a03048",
    "Tough": "#c9784f",
    "Average": "#8a8f98",
    "Good": "#4f9d78",
    "Great": "#1f7a52",
}


def league_label(slug: str) -> str:
    if slug in LEAGUE_LABELS:
        return LEAGUE_LABELS[slug]
    return slug.replace("-", " ").title()


# --------------------------------------------------------------------------
# Gameweek calendar
# --------------------------------------------------------------------------
#
# Sorare gameweeks alternate on fixed Eastern-time boundaries:
#
#   Friday 10:00 ET  -> Tuesday 09:59 ET   (weekend gameweek)
#   Tuesday 10:00 ET -> Friday 09:59 ET    (midweek gameweek)
#
# GW1 is anchored to Friday 31 July 2026 10:00 ET, so periods run 4 days,
# 3 days, 4 days, 3 days. Arithmetic is done on naive Eastern wall-clock times
# rather than tz-aware ones: the boundary is 10:00 on the clock all year, and
# adding fixed 7-day offsets to a tz-aware timestamp would slide it an hour
# when the clocks change in November.

GW_TZ = "America/New_York"
GW_EPOCH_ET = pd.Timestamp("2026-07-31 10:00:00")  # naive ET, start of GW1
_DAY = pd.Timedelta(days=1)


def _to_et_naive(utc_series: pd.Series) -> pd.Series:
    return utc_series.dt.tz_convert(GW_TZ).dt.tz_localize(None)


def assign_gameweek(kickoff_utc: pd.Series) -> pd.Series:
    """Gameweek number for each kickoff. Can be 0 or negative before GW1."""
    days = (_to_et_naive(kickoff_utc) - GW_EPOCH_ET) / _DAY
    week = np.floor(days / 7.0)
    within = days - week * 7.0
    index = 2.0 * week + (within >= 4.0).astype(float)
    return pd.Series(index + 1.0, index=kickoff_utc.index).astype("Int64")


def gameweek_bounds(gw: int) -> tuple[pd.Timestamp, pd.Timestamp]:
    """(start, end) of a gameweek in UTC. End is the next gameweek's start."""
    index = int(gw) - 1
    week, phase = divmod(index, 2)
    start_et = GW_EPOCH_ET + pd.Timedelta(days=7 * week + (4 if phase else 0))
    end_et = start_et + pd.Timedelta(days=3 if phase else 4)
    to_utc = lambda t: t.tz_localize(GW_TZ).tz_convert("UTC")  # noqa: E731
    return to_utc(start_et), to_utc(end_et)


def gameweek_kind(gw: int) -> str:
    """Gameweeks starting on a Friday run through the weekend."""
    return "Midweek" if (int(gw) - 1) % 2 else "Weekend"


def gameweek_label(gw: int, tz: str = GW_TZ) -> str:
    start, end = gameweek_bounds(gw)
    s = start.tz_convert(tz)
    e = (end - pd.Timedelta(minutes=1)).tz_convert(tz)
    return f"GW {int(gw)} · {s:%a %d %b} – {e:%a %d %b}"


# --------------------------------------------------------------------------
# Competition -> Sorare league bucket
# --------------------------------------------------------------------------

SORARE_COMPETITION_MAPPING = {
    "Austrian Bundesliga": "Contender",
    "First Division A": "Jupiler Pro League",
    "UEFA Champions League": "UEFA",
    "UEFA Europa League": "UEFA",
    "HNL": "Contender",
    "Superliga": "Contender",
    "EPL": "EPL",
    "Championship": "English Championship",
    "Ligue 1": "Ligue 1",
    "Ligue 2": "Contender",
    "Bundesliga": "Bundesliga",
    "2. Bundesliga": "Contender",
    "Serie A": "Contender",
    "Serie B": "Contender",
    "Eredivisie": "Eredivisie",
    "Primeira Liga": "Primeira Liga",
    "Premiership": "Premiership",
    "LaLiga": "LaLiga",
    "Segunda División": "Contender",
    "Süper Lig": "Contender",
    "MLS": "MLS",
    "Concacaf Champions Cup": "Concacaf Champions Cup",
    "Liga Profesional Argentina": "Contender",
    "CONMEBOL Libertadores": "CONMEBOL Libertadores",
    "Liga MX": "Contender",
    "K League 1": "K League 1",
    "AFC Champions League Elite": "AFC Champions League Elite",
    "Liga 1": "Contender",
    "Primera Division": "Contender",
    "Brasil Serie A": "Contender",
    "Super League": "Contender",
    "Primera A": "Contender",
    "J1 League": "J1 League",
    "UEFA Conference League": "UEFA",
    "Russian Premier League": "Contender",
    "Chinese Super League": "Contender",
    "Liga Pro": "Contender",
    "CONMEBOL Sudamericana": "CONMEBOL Sudamericana",
    "Eliteserien": "Contender",
}

UNMAPPED = "Unmapped"

# Continental competitions resolve on name alone — a club's card league says
# nothing about which continental cup it is in.
CONTINENTAL = {
    "UEFA Champions League",
    "UEFA Europa League",
    "UEFA Conference League",
    "CONMEBOL Libertadores",
    "CONMEBOL Sudamericana",
    "Concacaf Champions Cup",
    "AFC Champions League Elite",
}

# Competition names that are reused across countries. The club's Sorare card
# league slug is what tells them apart: an austrian-bundesliga club playing
# "Bundesliga" is in the Austrian one, a campeonato-brasileiro-serie-a club
# playing "Serie A" is in Brazil's, and so on.
AMBIGUOUS_BY_SLUG = {
    "Bundesliga": {
        "bundesliga-de": "Bundesliga",
        "austrian-bundesliga": "Austrian Bundesliga",
    },
    "Serie A": {
        "serie-a-it": "Serie A",
        "campeonato-brasileiro-serie-a": "Brasil Serie A",
    },
    "Serie B": {
        "serie-b-it": "Serie B",
        "campeonato-brasileiro-serie-a": "Brasil Serie A",
    },
    "Premier League": {
        "premier-league-gb-eng": "EPL",
        "russian-premier-league": "Russian Premier League",
    },
    "Primera División": {
        "laliga-es": "LaLiga",
        "primera-division-cl": "Primera Division",
    },
    "Primera Division": {
        "laliga-es": "LaLiga",
        "primera-division-cl": "Primera Division",
    },
    "Super League": {
        "super-league-ch": "Super League",
        "chinese-super-league": "Chinese Super League",
    },
    "Superliga": {
        "superliga-dk": "Superliga",
        "superliga-argentina-de-futbol": "Liga Profesional Argentina",
    },
}

# Names the export uses that differ from the mapping's keys.
NAME_ALIASES = {
    "Premier League": "EPL",
    "Primera División": "Primera Division",
    "Süper Lig": "Süper Lig",
}


def resolve_competition(competition: str, slug: str) -> str:
    """Map an export competition name onto a key in SORARE_COMPETITION_MAPPING."""
    if not isinstance(competition, str):
        return ""
    if competition in CONTINENTAL:
        return competition
    by_slug = AMBIGUOUS_BY_SLUG.get(competition)
    if by_slug:
        resolved = by_slug.get(slug)
        if resolved:
            return resolved
        return ""  # ambiguous name, unrecognised slug — better blank than wrong
    if competition in SORARE_COMPETITION_MAPPING:
        return competition
    return NAME_ALIASES.get(competition, "")


def sorare_league(competition: str, slug: str) -> str:
    key = resolve_competition(competition, slug)
    return SORARE_COMPETITION_MAPPING.get(key, UNMAPPED)


SORARE_LEAGUES = sorted(set(SORARE_COMPETITION_MAPPING.values())) + [UNMAPPED]

# A club's card-league slug implies its domestic competition, and therefore
# which Sorare league bucket its cards belong to. This is what the grouped
# competition picker runs on.
SLUG_TO_COMPETITION = {
    "1-hnl": "HNL",
    "2-bundesliga": "2. Bundesliga",
    "austrian-bundesliga": "Austrian Bundesliga",
    "bundesliga-de": "Bundesliga",
    "campeonato-brasileiro-serie-a": "Brasil Serie A",
    "chinese-super-league": "Chinese Super League",
    "eliteserien": "Eliteserien",
    "eredivisie": "Eredivisie",
    "football-league-championship": "Championship",
    "j1-league": "J1 League",
    "jupiler-pro-league": "First Division A",
    "k-league-1": "K League 1",
    "laliga-es": "LaLiga",
    "liga-mx": "Liga MX",
    "liga-pro": "Liga Pro",
    "ligue-1-fr": "Ligue 1",
    "ligue-2-fr": "Ligue 2",
    "mlspa": "MLS",
    "premier-league-gb-eng": "EPL",
    "premiership-gb-sct": "Premiership",
    "primeira-liga-pt": "Primeira Liga",
    "primera-a": "Primera A",
    "primera-division-cl": "Primera Division",
    "primera-division-pe": "Liga 1",
    "russian-premier-league": "Russian Premier League",
    "segunda-division-es": "Segunda División",
    "serie-a-it": "Serie A",
    "serie-b-it": "Serie B",
    "spor-toto-super-lig": "Süper Lig",
    "super-league-ch": "Super League",
    "superliga-argentina-de-futbol": "Liga Profesional Argentina",
    "superliga-dk": "Superliga",
}


def slug_group(slug: str) -> str:
    """Sorare league bucket a club's cards sit in, from its card-league slug."""
    return SORARE_COMPETITION_MAPPING.get(SLUG_TO_COMPETITION.get(slug, ""), UNMAPPED)


# --------------------------------------------------------------------------
# Loading
# --------------------------------------------------------------------------

FIXTURE_COLS = {
    "Coverage Status": "coverage",
    "Date": "kickoff_utc",
    "Game Week": "gameweek",
    "Id": "game_id",
    "Name": "team",
    "name (upcomingGames.awayTeam)": "away_team",
    "name (upcomingGames.competition)": "competition",
    "name (upcomingGames.homeTeam)": "home_team",
    "Slug": "slug",
    "Domestic League Ranking": "league_rank",
}


def load_fixtures(source) -> pd.DataFrame:
    """Tidy the upcoming-fixtures export into one row per (team, game)."""
    raw = pd.read_csv(source, encoding="utf-8-sig")
    missing = [c for c in FIXTURE_COLS if c not in raw.columns]
    if missing:
        raise ValueError(f"upcoming_fixtures is missing columns: {missing}")

    fx = raw[list(FIXTURE_COLS)].rename(columns=FIXTURE_COLS).copy()
    fx["kickoff_utc"] = pd.to_datetime(fx["kickoff_utc"], utc=True, errors="coerce")
    fx = fx.dropna(subset=["kickoff_utc", "team", "game_id"])

    fx = fx.rename(columns={"gameweek": "gameweek_export"})

    # Fixtures whose kickoff time is not yet set carry an exact midnight-UTC
    # placeholder. Some are genuine, though: an MLS or Brazilian game really can
    # kick off at 00:00Z. Three signals separate them.
    exact_midnight = (
        (fx["kickoff_utc"].dt.hour == 0)
        & (fx["kickoff_utc"].dt.minute == 0)
        & (fx["kickoff_utc"].dt.second == 0)
    )
    # 1. A competition that never otherwise kicks off near midnight UTC is not
    #    about to start one round at 00:00 — Portugal and Croatia play evenings
    #    local, which is late afternoon UTC.
    near_midnight_hours = {22, 23, 0, 1}
    real_times = fx.loc[~exact_midnight, ["competition", "kickoff_utc"]]
    plays_near_midnight = {
        comp
        for comp, hours in real_times.groupby("competition")["kickoff_utc"].apply(
            lambda s: set(s.dt.hour)
        ).items()
        if hours & near_midnight_hours
    }
    comp_implausible = ~fx["competition"].isin(plays_near_midnight)
    # 2. A whole round sitting at midnight is a round without times yet.
    round_all_midnight = exact_midnight.groupby(
        [fx["competition"], fx["gameweek_export"]]
    ).transform("all")
    # 3. No gameweek assigned at all means the fixture is not scheduled yet.
    unscheduled = fx["gameweek_export"].isna()

    fx["kickoff_tbd"] = exact_midnight & (
        comp_implausible | round_all_midnight.fillna(False) | unscheduled
    )

    # Midnight UTC is the previous evening in Eastern time, which would drop a
    # placeholder Friday fixture into the midweek gameweek that ends that
    # morning. Number those off a plausible afternoon slot instead.
    for_numbering = fx["kickoff_utc"].where(
        ~fx["kickoff_tbd"], fx["kickoff_utc"] + pd.Timedelta(hours=18)
    )
    fx["gameweek"] = assign_gameweek(for_numbering)
    fx["gw_kind"] = fx["gameweek"].map(lambda g: gameweek_kind(g) if pd.notna(g) else None)

    is_home = fx["team"] == fx["home_team"]
    fx["location"] = np.where(is_home, "Home", "Away")
    fx["opponent"] = np.where(is_home, fx["away_team"], fx["home_team"])
    fx["covered"] = fx["coverage"].fillna("").str.upper().eq("FULL")

    # Relegated / promoted clubs appear under two Sorare league slugs. Keep the
    # team->slug mapping separately and store one row per (team, game).
    team_leagues = (
        fx[["team", "slug", "league_rank"]]
        .dropna(subset=["slug"])
        .drop_duplicates()
        .reset_index(drop=True)
    )
    team_leagues["league"] = team_leagues["slug"].map(league_label)

    # Resolve the Sorare league before de-duplicating: a club sitting in two
    # card leagues could otherwise lose the slug that disambiguates its
    # competition name.
    fx["competition_key"] = [
        resolve_competition(c, s) for c, s in zip(fx["competition"], fx["slug"])
    ]
    fx["sorare_league"] = fx["competition_key"].map(SORARE_COMPETITION_MAPPING).fillna(UNMAPPED)
    fx["_resolved"] = fx["sorare_league"].ne(UNMAPPED)

    fx = (
        fx.sort_values(["team", "game_id", "_resolved", "slug"], ascending=[True, True, False, True])
        .drop_duplicates(subset=["team", "game_id"])
        .drop(columns="_resolved")
        .reset_index(drop=True)
    )
    fx["match"] = fx["home_team"] + " v " + fx["away_team"]
    return fx, team_leagues


def load_difficulty(source) -> pd.DataFrame:
    """
    Collapse the opponent-difficulty export to its lookup form.

    Score_mean / Score_median are constant for a given
    (Opponent, Location, Position) triple, where Location is the location of
    the team FACING that opponent. So this is: "when I play <Opponent> at
    <Location>, my <Position> historically scores this much".
    """
    raw = pd.read_csv(source, encoding="utf-8-sig")
    need = ["Opponent", "Location", "Position", "Score_mean", "Score_median"]
    missing = [c for c in need if c not in raw.columns]
    if missing:
        raise ValueError(f"Calculated Opponent Difficulty is missing: {missing}")

    lut = (
        raw[need]
        .dropna(subset=["Opponent", "Location", "Position"])
        .drop_duplicates(subset=["Opponent", "Location", "Position"])
        .rename(
            columns={
                "Opponent": "opponent",
                "Location": "location",
                "Position": "position",
                "Score_mean": "score_mean",
                "Score_median": "score_median",
            }
        )
        .reset_index(drop=True)
    )

    # Raw scores are not comparable across positions (keepers and midfielders
    # sit on different scales), so rank within position.
    for metric in ("mean", "median"):
        lut[f"pct_{metric}"] = lut.groupby("position")[f"score_{metric}"].rank(pct=True)
    return lut


def grade_from_pct(pct: pd.Series) -> pd.Series:
    bins = [-0.01, 0.20, 0.40, 0.60, 0.80, 1.01]
    return pd.cut(pct, bins=bins, labels=GRADES).astype("object")


def attach_difficulty(fx: pd.DataFrame, lut: pd.DataFrame, metric: str = "mean") -> pd.DataFrame:
    """Long frame: one row per (fixture, position) with matchup difficulty."""
    frames = []
    for pos in POSITIONS:
        block = fx.copy()
        block["position"] = pos
        frames.append(block)
    long = pd.concat(frames, ignore_index=True)
    long = long.merge(lut, on=["opponent", "location", "position"], how="left")
    long["score"] = long[f"score_{metric}"]
    long["pct"] = long[f"pct_{metric}"]
    long["grade"] = grade_from_pct(long["pct"])
    long["grade"] = long["grade"].where(long["pct"].notna(), "No data")
    return long


# --------------------------------------------------------------------------
# Lineup-lock maths
# --------------------------------------------------------------------------


def info_window(kickoff, early_lead: float, late_lead: float):
    """When a club's confirmed XI is expected to appear: [KO − early, KO − late]."""
    return (
        kickoff - pd.to_timedelta(early_lead, unit="m"),
        kickoff - pd.to_timedelta(late_lead, unit="m"),
    )


def mesh_frame(
    fixtures: pd.DataFrame,
    focus_kickoff: pd.Timestamp,
    early_lead: float,
    late_lead: float,
) -> pd.DataFrame:
    """
    Score every fixture against the focus club's team-news window.

    Both windows are the same length `L = early − late`, so the overlap between
    them collapses to `L − |Δ|` where Δ is the difference in kickoffs. That one
    number sorts the whole list: positive is minutes of shared coverage,
    negative is minutes of dead air before the next club's news lands.
    """
    out = fixtures.copy()
    length = float(early_lead - late_lead)
    out["delta_min"] = (out["kickoff_utc"] - focus_kickoff).dt.total_seconds() / 60.0
    out["gap_min"] = out["delta_min"].abs()
    out["score_min"] = length - out["gap_min"]
    out["overlap_min"] = out["score_min"].clip(lower=0)
    out["lull_min"] = (-out["score_min"]).clip(lower=0)
    out["window_from"], out["window_to"] = info_window(
        out["kickoff_utc"], early_lead, late_lead
    )
    return out.sort_values(["gap_min", "team"]).reset_index(drop=True)


def quiet_stretches(fixtures: pd.DataFrame, top: int = 5, min_minutes: float = 120):
    """The longest runs of time in a gameweek with no kickoff at all."""
    ks = sorted(set(fixtures["kickoff_utc"].dropna()))
    if len(ks) < 2:
        return []
    gaps = [
        {"from": a, "to": b, "minutes": (b - a).total_seconds() / 60.0}
        for a, b in zip(ks, ks[1:])
        if (b - a).total_seconds() / 60.0 >= min_minutes
    ]
    return sorted(gaps, key=lambda g: -g["minutes"])[:top]


def fmt_duration(delta) -> str:
    minutes = int(round(pd.Timedelta(delta).total_seconds() / 60))
    if minutes < 60:
        return f"{minutes} min"
    return f"{minutes // 60}h {minutes % 60:02d}m"


def default_data_paths() -> tuple[str | None, str | None]:
    here = os.path.dirname(os.path.abspath(__file__))
    candidates = [os.path.join(here, "data"), here, os.getcwd()]
    fixtures = difficulty = None
    for folder in candidates:
        f = os.path.join(folder, "upcoming_fixtures.csv")
        d1 = os.path.join(folder, "Calculated_Opponent_Difficulty.csv")
        d2 = os.path.join(folder, "Calculated Opponent Difficulty.csv")
        if fixtures is None and os.path.exists(f):
            fixtures = f
        if difficulty is None:
            if os.path.exists(d1):
                difficulty = d1
            elif os.path.exists(d2):
                difficulty = d2
    return fixtures, difficulty
