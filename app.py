"""
Sorare Kickoff Planner
======================

Pick a competition and a focus club. Every other club in that competition is
laid out as a bar across the gameweek, with a green block marking when its
confirmed XI is expected. Clubs are sorted by how much their block overlaps the
focus club's — overlapping first, then out through the lulls.

Run with:  streamlit run app.py
"""

from __future__ import annotations

import numpy as np
import pandas as pd
import plotly.graph_objects as go
import streamlit as st

import core
import sorare_api
from core import POSITIONS, fmt_duration, league_label

st.set_page_config(
    page_title="Sorare Kickoff Planner",
    page_icon="images/logo_icon_dark.png",
    layout="wide",
    initial_sidebar_state="expanded",
)

st.markdown(
    """
    <style>
      .block-container {padding-top: 2.4rem; max-width: 1450px;}
      .stat-line {text-align: center; font-size: 1rem; margin: 0;}
      .note {color:#9096b3; font-size:0.84rem; line-height:1.5;}
      .st-key-stat_card, .st-key-cards_panel {background: #161c30;}
      .st-key-stat_card [data-testid="stHorizontalBlock"] {align-items: center;}
      .chart-legend {
        display: flex; flex-wrap: wrap; align-items: center;
        gap: 0.4rem 1.4rem; font-size: 0.82rem; color: #9096b3;
        margin: 0.25rem 0 0.75rem;
      }
      .legend-item {display: inline-flex; align-items: center; gap: 0.45rem;}
      .legend-swatch {width: 12px; height: 12px; border-radius: 3px; flex: none;}
      .legend-tick {width: 3px; height: 14px; border-radius: 1px; flex: none;}
    </style>
    """,
    unsafe_allow_html=True,
)

# st.context.theme.type reports the browser/OS color-scheme preference, not
# which theme is actually rendered — it says "light" even when our own
# .streamlit/config.toml forces base="dark" and the page is visibly dark. The
# app only ships a dark design (no [theme.light] palette), so dark is simply
# the fixed reality here rather than something to detect.
IS_DARK = True

TRACK_FILL = "rgba(129,135,180,0.12)"
TRACK_LINE = "rgba(129,135,180,0.40)"
BLOCK = "#2fae66"
BLOCK_FOCUS = "#6366f1"
KO_TICK = "#e8e8ec" if IS_DARK else "#2b2f36"
BUSY_FILL = "rgba(199,123,60,0.20)"
BUSY_LINE = "rgba(199,123,60,0.65)"
PLOT_TEMPLATE = "plotly_dark" if IS_DARK else "plotly_white"

POS_SHORT = {"Goalkeeper": "GK", "Defender": "DEF", "Midfielder": "MID", "Forward": "FWD"}
SCALE = ["#b3405c", "#cf7f56", "#9aa0a8", "#57a37c", "#1d7d55"]

TZ_CHOICES = [
    "America/New_York", "America/Chicago", "America/Denver", "America/Los_Angeles",
    "America/Sao_Paulo", "America/Mexico_City", "America/Bogota",
    "America/Argentina/Buenos_Aires", "UTC", "Europe/London", "Europe/Lisbon",
    "Europe/Paris", "Europe/Berlin", "Europe/Madrid", "Europe/Rome", "Europe/Amsterdam",
    "Europe/Copenhagen", "Europe/Oslo", "Europe/Zurich", "Europe/Istanbul",
    "Europe/Moscow", "Asia/Tokyo", "Asia/Seoul", "Asia/Shanghai", "Australia/Sydney",
]


def _ver_tuple(v: str):
    out = []
    for part in v.split(".")[:3]:
        digits = "".join(ch for ch in part if ch.isdigit())
        out.append(int(digits) if digits else 0)
    return tuple(out)


_MODERN_WIDTH = _ver_tuple(st.__version__) >= (1, 49, 0)


_SUPPORTS_SELECT = _ver_tuple(st.__version__) >= (1, 35, 0)


def chart(fig, key=None, selectable=False, **kw):
    """Render full width, and return the click selection where supported."""
    if key:
        kw["key"] = key
    if selectable and _SUPPORTS_SELECT and key:
        kw["on_select"] = "rerun"
        kw["selection_mode"] = "points"
    if _MODERN_WIDTH:
        return st.plotly_chart(fig, width="stretch", **kw)
    return st.plotly_chart(fig, use_container_width=True, **kw)


def clicked_club(event):
    """Pull the club name out of a plotly selection, minus the owned-card dot.

    Streamlit hands back an attribute-dict, but plain dicts turn up in tests and
    older builds, so accept either rather than assuming.
    """
    if event is None:
        return None
    selection = getattr(event, "selection", None)
    if selection is None and isinstance(event, dict):
        selection = event.get("selection")
    for point in (selection or {}).get("points") or []:
        label = point.get("y") if isinstance(point, dict) else None
        if isinstance(label, str):
            return label.removeprefix("\u25cf ").strip()
    return None


def clicked_row(event):
    """First selected row index out of a dataframe selection event, or None."""
    if event is None:
        return None
    selection = getattr(event, "selection", None)
    if selection is None and isinstance(event, dict):
        selection = event.get("selection")
    picked_rows = (selection or {}).get("rows") or []
    return picked_rows[0] if picked_rows else None


def show_table(df, key=None, selectable=False, **kw):
    if key:
        kw["key"] = key
    if selectable and _SUPPORTS_SELECT and key:
        kw["on_select"] = "rerun"
        kw["selection_mode"] = "single-row"
    if _MODERN_WIDTH:
        return st.dataframe(df, width="stretch", **kw)
    return st.dataframe(df, use_container_width=True, **kw)


# ---------------------------------------------------------------------------
# Data
# ---------------------------------------------------------------------------


@st.cache_data(show_spinner="Reading fixtures…")
def _fixtures(source):
    return core.load_fixtures(source)


@st.cache_data(show_spinner=False)
def _difficulty(source):
    return core.load_difficulty(source)


@st.cache_data(show_spinner=False)
def _difficulty_long(fx: pd.DataFrame, lut: pd.DataFrame, metric: str):
    return core.attach_difficulty(fx, lut, metric)


fx_path, diff_path = core.default_data_paths()
if not (fx_path and diff_path):
    st.error("Both CSV exports should be committed in `data/`. They are missing.")
    st.stop()

fixtures, team_leagues = _fixtures(fx_path)
lut = _difficulty(diff_path)


# ---------------------------------------------------------------------------
# Controls
# ---------------------------------------------------------------------------

# Streamlit derives a widget's identity from its label *and* its options, so any
# control whose choices depend on another control loses its value the moment
# that other control changes. Explicit keys pin the values into session state
# instead, and the helpers below drop a stored value only once it has genuinely
# stopped being a valid choice.
FILTER_KEYS = [
    "f_group", "f_tz", "f_early", "f_late", "f_leagues", "f_rows",
    "f_uncovered", "f_tbd", "f_gw", "f_search", "f_focus", "f_whole_gw",
    "f_scarcities", "f_owned_only", "f_season_toggle",
]


def reset_filters():
    for key in FILTER_KEYS:
        st.session_state.pop(key, None)


def keep_valid(key, allowed):
    """Forget a stored choice only if it is no longer on offer."""
    if key in st.session_state and st.session_state[key] not in allowed:
        del st.session_state[key]


def keep_valid_many(key, allowed):
    if key in st.session_state:
        kept = [v for v in st.session_state[key] if v in allowed]
        if kept != st.session_state[key]:
            st.session_state[key] = kept


# Streamlit keeps session state across a hot reload, so a gallery stored by an
# older build of this file can outlive the code that wrote it. Bump this
# whenever the stored shape changes and stale entries get dropped on sight.
GALLERY_VERSION = 3


def secret(name: str) -> str:
    """st.secrets raises outright when no secrets file exists, so guard it."""
    try:
        return st.secrets.get(name, "") or ""
    except Exception:  # noqa: BLE001 - any secrets backend problem means "no key"
        return ""


def clear_gallery():
    st.session_state.pop("gallery", None)
    st.session_state.pop("gallery_error", None)
    st.session_state.pop("f_owned_only", None)


def load_gallery(username, scarcities, api_key):
    """Pull a gallery and stash it; errors are surfaced, never raised at the UI."""
    status = st.sidebar.empty()

    def report(message, _fraction):
        status.caption(message)

    try:
        result = sorare_api.fetch_gallery(
            username, scarcities=scarcities, api_key=api_key or None, progress=report
        )
    except sorare_api.SorareError as exc:
        st.session_state["gallery_error"] = str(exc)
        st.session_state.pop("gallery", None)
        return
    finally:
        status.empty()

    st.session_state.pop("gallery_error", None)
    st.session_state["gallery"] = {
        "version": GALLERY_VERSION,
        "nickname": result.nickname,
        "cards": result.cards,
        "pages": result.pages,
        "variant": result.query_variant,
        "truncated": result.truncated,
        "notes": result.notes,
        "scarcities": list(scarcities),
    }


with st.sidebar:
    st.image(
        "images/logo_wordmark_dark.png" if IS_DARK else "images/logo_wordmark_light.png",
        use_container_width=True,
    )
    st.markdown("### My cards")
    username = st.text_input(
        "Sorare username",
        placeholder="username from sorare.com/u/…",
        key="f_user",
        help="Press Enter after typing — Streamlit only registers the value once the "
        "field is committed.",
    )
    api_key = secret("SORARE_API_KEY")

    if st.button("Load gallery", key="f_load"):
        if not username.strip():
            st.session_state["gallery_error"] = (
                "Type your Sorare username above and press Enter, then try again."
            )
        else:
            load_gallery(username, sorare_api.SCARCITIES, api_key)

    gallery = st.session_state.get("gallery")
    if gallery is not None and gallery.get("version") != GALLERY_VERSION:
        clear_gallery()
        gallery = None
        st.info("Your loaded gallery was from an older version of the app. Load it again.")
    if st.session_state.get("gallery_error"):
        st.error(st.session_state["gallery_error"])
    if gallery:
        cards_held = gallery.get("cards")
        detail = ""
        if gallery.get("pages"):
            detail = f", read in {gallery['pages']} page(s) via `{gallery.get('variant', '?')}`"
        st.caption(
            f"**{gallery.get('nickname', 'Gallery')}** — {len(cards_held)} card(s) across "
            f"{cards_held['club'].nunique()} club(s){detail}."
        )
        if gallery.get("truncated"):
            st.warning("Stopped at the page cap — this gallery is larger than what was read.")
        for note in gallery.get("notes", []):
            st.caption(note)
        st.button("Clear gallery", on_click=clear_gallery, key="f_clear_gallery")

    slugs_present = sorted(team_leagues["slug"].unique())
    groups = sorted({core.slug_group(s) for s in slugs_present})
    group = st.selectbox("Competition", ["All"] + groups, index=0, key="f_group")
    group_slugs = (
        slugs_present if group == "All"
        else [s for s in slugs_present if core.slug_group(s) == group]
    )

    # "More options" renders at the bottom of the sidebar, but its values are
    # needed here to build the club pool. Read them from session state — set by
    # that widget on the previous run — with the same defaults it uses, rather
    # than rendering it out of place just to get its values early.
    keep_valid_many("f_leagues", set(group_slugs))
    tz = st.session_state.get("f_tz", TZ_CHOICES[0])
    early_lead = st.session_state.get("f_early", 60)
    late_lead = st.session_state.get("f_late", 0)
    if late_lead > early_lead:
        early_lead, late_lead = late_lead, early_lead
    # No stored default for the multiselect — an unset one starts empty anyway.
    leagues = st.session_state.get("f_leagues", [])
    rows_shown = st.session_state.get("f_rows", 60)
    hide_uncovered = st.session_state.get("f_uncovered", True)
    hide_tbd = st.session_state.get("f_tbd", True)

    pool_slugs = leagues or group_slugs
    pool_teams = set(team_leagues.loc[team_leagues["slug"].isin(pool_slugs), "team"])

    # Scarcity and in-season are applied to the cards already in hand, so
    # changing either re-filters instantly instead of hitting the API again.
    # Everything downstream — the chart, the table, the club pool — reads the
    # same filtered set, so the two views can never disagree.
    owned, unmatched_clubs, held_cards = (pd.DataFrame(), [], pd.DataFrame())
    owned_only = False
    if gallery is not None:
        held_cards = gallery.get("cards").copy()

        has_scarcity = held_cards["scarcity"].ne("").any() if len(held_cards) else False
        if has_scarcity:
            keep_valid_many("f_scarcities", set(sorare_api.SCARCITIES))
            show_scarcities = st.multiselect(
                "Scarcity",
                sorare_api.SCARCITIES,
                format_func=lambda x: sorare_api.SCARCITY_LABELS[x],
                key="f_scarcities",
                help="Empty means all three.",
            ) or list(sorare_api.SCARCITIES)
            held_cards = held_cards[held_cards["scarcity"].isin(show_scarcities)]
        else:
            st.caption("Scarcity per card was unavailable on this schema shape.")

        has_season = (
            "in_season" in held_cards.columns and held_cards["in_season"].notna().any()
        )
        if has_season:
            in_season_only = st.toggle(
                "In-Season Cards",
                value=False,
                key="f_season_toggle",
                help="Off shows all cards. On narrows to clubs where you hold an "
                "in-season card.",
            )
        else:
            in_season_only = False
            st.caption("`inSeasonEligible` was unavailable on this schema shape.")

        held_cards = sorare_api.annotate_teams(
            held_cards, sorted(team_leagues["team"].unique())
        )
        owned, unmatched_clubs = sorare_api.match_clubs(
            held_cards, sorted(team_leagues["team"].unique())
        )
        if has_season and not owned.empty and in_season_only:
            owned = owned[owned["in_season"] > 0]

        owned_only = st.toggle(
            "Only clubs I hold cards for",
            value=True,
            key="f_owned_only",
            help=f"{len(owned)} of your clubs appear in the fixture export.",
        )
        if owned_only:
            pool_teams &= set(owned["team"]) if not owned.empty else set()
            if not pool_teams:
                st.warning("No clubs left. Loosen the scarcity or in-season filter.")
        if unmatched_clubs:
            with st.expander(f"{len(unmatched_clubs)} club(s) not in the export"):
                st.caption(
                    "Cards for these clubs cannot be scheduled — the club has no upcoming "
                    "fixture in the export, or its name differs there."
                )
                st.write(", ".join(unmatched_clubs))
    owned_counts = dict(zip(owned["team"], owned["cards"])) if not owned.empty else {}
    owned_detail = (
        dict(zip(owned["team"], owned["scarcities"])) if not owned.empty else {}
    )

    pool = fixtures[fixtures["team"].isin(pool_teams)]
    if hide_uncovered:
        pool = pool[pool["covered"]]
    if hide_tbd:
        pool = pool[~pool["kickoff_tbd"]]

    # Only offer gameweeks this competition actually plays in. Most leagues are
    # mid-break or pre-season for part of the export's range, and offering an
    # empty week just dead-ends the page.
    gws = [int(g) for g in sorted(pool["gameweek"].dropna().unique()) if g >= 0]
    if not gws:
        st.warning("That competition has no fixtures left once those filters are applied.")
        st.button("Reset all filters", on_click=reset_filters, key="f_reset_empty")
        st.stop()
    upcoming = [g for g in gws if g >= 1]
    default_gw = upcoming[0] if upcoming else gws[0]
    keep_valid("f_gw", set(gws))
    gw_choice = st.selectbox(
        "Gameweek",
        gws,
        index=gws.index(default_gw),
        format_func=core.gameweek_label,
        key="f_gw",
    )
    selected_gws = [gw_choice]

window = pool[pool["gameweek"].isin(selected_gws)].copy()

if hide_tbd:
    withheld = fixtures[
        fixtures["gameweek"].isin(selected_gws)
        & fixtures["team"].isin(pool_teams)
        & fixtures["kickoff_tbd"]
    ]
else:
    withheld = fixtures.iloc[0:0]

with st.sidebar:
    all_clubs = sorted(window["team"].unique())
    query = st.text_input(
        "Focus club",
        placeholder="Type to filter clubs…",
        key="f_search",
    ).strip()
    club_options = (
        [c for c in all_clubs if query.casefold() in c.casefold()] if query else all_clubs
    )
    if not club_options:
        st.caption(f"Nothing matches “{query}”.")
        club_options = all_clubs
    keep_valid("f_focus", set(club_options))
    focus_team = st.selectbox(
        "Focus club", club_options, index=0, label_visibility="collapsed", key="f_focus"
    )

    with st.expander("More options"):
        st.selectbox("Time zone", TZ_CHOICES, index=0, key="f_tz")
        st.slider("Sheets out earliest (min before KO)", 30, 150, 60, 5, key="f_early")
        st.slider("Sheets out latest (min before KO)", 0, 120, 0, 5, key="f_late")
        # No `default` here: keep_valid_many (run earlier) writes to session
        # state, and passing both makes Streamlit warn.
        st.multiselect(
            "Narrow to leagues",
            sorted(group_slugs, key=league_label),
            format_func=league_label,
            help="Optional. Empty means every league in the group.",
            key="f_leagues",
        )
        st.slider("Clubs on the chart", 5, 120, 60, 5, key="f_rows")
        st.toggle("Hide games Sorare does not score", value=True, key="f_uncovered")
        st.toggle("Hide placeholder kickoff times", value=True, key="f_tbd")
        st.button("Reset all filters", on_click=reset_filters, key="f_reset")


def local(ts):
    """UTC -> chosen zone, offset dropped so plotly shapes and bars share an axis."""
    if isinstance(ts, pd.Series):
        return ts.dt.tz_convert(tz).dt.tz_localize(None)
    return ts.tz_convert(tz).tz_localize(None)


def stamp(ts):
    return "—" if pd.isna(ts) else local(ts).strftime("%a %d %b · %H:%M")


def clock(ts):
    return "—" if pd.isna(ts) else local(ts).strftime("%H:%M")


def describe_overlap(score: float) -> str:
    """Positive score is shared coverage; negative is the dead air in between."""
    if pd.isna(score):
        return "—"
    if score > 0:
        return f"{int(round(score))} min shared"
    if score == 0:
        return "just touches"
    return f"lull {fmt_duration(pd.Timedelta(minutes=-score))}"


def signed(minutes: float) -> str:
    if pd.isna(minutes):
        return "—"
    sign = "−" if minutes < 0 else "+"
    m = int(round(abs(minutes)))
    return f"{sign}{m}m" if m < 60 else f"{sign}{m // 60}h {m % 60:02d}m"



def mesh_for(gw: int):
    """Fixtures for one gameweek, scored against the focus club's news window."""
    block = window[window["gameweek"] == gw]
    focus_fx = block[block["team"] == focus_team].sort_values("kickoff_utc")
    if focus_fx.empty:
        return None
    focus_ko = focus_fx.iloc[0]["kickoff_utc"]
    meshed = core.mesh_frame(block, focus_ko, early_lead, late_lead)
    # Pin the focus row to the top so the list reads outward from it.
    is_focus = meshed["team"] == focus_team
    meshed = pd.concat([meshed[is_focus].head(1), meshed[~is_focus]]).reset_index(drop=True)
    meshed["gw"] = gw
    return focus_fx, focus_ko, meshed


def render_my_cards(picked):
    """The 'My {club} cards' panel — shared by the chart click and the table click."""
    if not picked:
        return
    mine = held_cards[held_cards["team"] == picked]
    if mine.empty:
        st.caption(f"No cards for {picked} in the current scarcity/in-season filter.")
        return
    with st.container(border=True, key="cards_panel"):
        st.markdown(f"**My {picked} cards** — {len(mine)}")
        detail = pd.DataFrame({
            "Player": mine["player"],
            "Position": [POS_SHORT.get(x, x or "—") for x in mine["position"]],
            "Scarcity": [
                sorare_api.SCARCITY_LABELS.get(x, x or "—") for x in mine["scarcity"]
            ],
            "In season": [
                "—" if pd.isna(x) else ("yes" if x else "no") for x in mine["in_season"]
            ],
        })
        show_table(detail.sort_values("Player").reset_index(drop=True), hide_index=True)


# ---------------------------------------------------------------------------
# Page
# ---------------------------------------------------------------------------

st.title("Sorare Kickoff Planner")

# The toggle itself renders below the chart (see the render loop), but its
# value is needed here first to build the chart — same session-state
# pre-read pattern as "More options".
whole_gameweek = st.session_state.get("f_whole_gw", False)

def axis_ticks(x_lo, x_hi):
    """Tick spacing in milliseconds, plus a label format, for a given span.

    Half-hourly is the natural grain for kickoff work, but a whole gameweek is
    four days wide and 30-minute ticks would be 190 labels, so it steps back as
    the span grows.
    """
    hours = (x_hi - x_lo).total_seconds() / 3600.0
    for limit, minutes in ((8, 30), (16, 60), (30, 120), (60, 240)):
        if hours <= limit:
            step = minutes
            break
    else:
        step = 360
    fmt = "%H:%M" if hours <= 24 else "%a %d %b<br>%H:%M"
    return step * 60 * 1000, fmt


def overlap_chart(rows: pd.DataFrame, focus_from, focus_to, x_lo, x_hi) -> go.Figure:
    # A dot marks a club you hold cards for — only worth showing when the
    # owned-only filter is off, since otherwise every row would carry one.
    mark = owned_counts and not owned_only
    labels = [f"● {t}" if mark and t in owned_counts else t for t in rows["team"]]
    rows = rows.assign(_label=labels)
    fig = go.Figure()
    span_ms = (x_hi - x_lo).total_seconds() * 1000.0

    # Bars fill most of each row's band (rather than the old 0.72) so there is
    # far less dead space between rows for a click to land on nothing.
    ROW_WIDTH = 0.92

    fig.add_trace(
        go.Bar(
            y=labels,
            x=[span_ms] * len(labels),
            base=[local(x_lo)] * len(labels),
            orientation="h",
            marker=dict(color=TRACK_FILL, line=dict(color=TRACK_LINE, width=1)),
            # "none" (not "skip") — skip also unbinds click/selection events, which
            # would make the row's background unclickable outside the block.
            hoverinfo="none",
            showlegend=False,
            width=ROW_WIDTH,
        )
    )

    for is_focus in (False, True):
        block = rows[rows["team"].eq(focus_team) == is_focus]
        if block.empty:
            continue
        starts = block["window_from"].clip(lower=x_lo, upper=x_hi)
        ends = block["window_to"].clip(lower=x_lo, upper=x_hi)
        widths = (ends - starts).dt.total_seconds() * 1000.0
        fig.add_trace(
            go.Bar(
                y=list(block["_label"]),
                x=list(widths),
                base=list(local(starts)),
                orientation="h",
                marker=dict(color=BLOCK_FOCUS if is_focus else BLOCK, line=dict(width=0)),
                showlegend=False,
                width=ROW_WIDTH,
                customdata=np.stack(
                    [
                        block["match"], block["competition"], block["location"],
                        [stamp(k) for k in block["kickoff_utc"]],
                        [signed(d) for d in block["delta_min"]],
                        [describe_overlap(o) for o in block["score_min"]],
                    ],
                    axis=-1,
                ),
                hovertemplate=(
                    "<b>%{y}</b><br>%{customdata[0]}<br>%{customdata[1]} · %{customdata[2]}"
                    "<br>Kickoff %{customdata[3]}<br>vs focus %{customdata[4]}"
                    "<br>%{customdata[5]}<extra></extra>"
                ),
            )
        )

    # A busy/blocked-off block after kickoff — like a calendar's "busy" shading —
    # marking the ~2h a match is likely still going, whether or not it falls in
    # anyone's news window.
    busy_source = rows[rows["kickoff_utc"] < x_hi]
    if not busy_source.empty:
        busy_starts = busy_source["kickoff_utc"].clip(lower=x_lo)
        busy_ends = (busy_source["kickoff_utc"] + pd.Timedelta(minutes=120)).clip(upper=x_hi)
        busy_widths = (busy_ends - busy_starts).dt.total_seconds() * 1000.0
        fig.add_trace(
            go.Bar(
                y=list(busy_source["_label"]),
                x=list(busy_widths),
                base=list(local(busy_starts)),
                orientation="h",
                marker=dict(
                    color=BUSY_FILL,
                    pattern=dict(shape="/", fgcolor=BUSY_LINE, size=6, solidity=0.28),
                    line=dict(color=BUSY_LINE, width=1),
                ),
                width=ROW_WIDTH,
                hoverinfo="none",
                showlegend=False,
            )
        )

    inside = rows[(rows["kickoff_utc"] >= x_lo) & (rows["kickoff_utc"] <= x_hi)]
    if not inside.empty:
        fig.add_trace(
            go.Scatter(
                x=list(local(inside["kickoff_utc"])),
                y=list(inside["_label"]),
                mode="markers",
                marker=dict(symbol="line-ns", size=15, line=dict(color=KO_TICK, width=2)),
                hoverinfo="none",
                showlegend=False,
            )
        )

    fig.add_vrect(
        x0=local(max(focus_from, x_lo)),
        x1=local(min(focus_to, x_hi)),
        fillcolor=BLOCK_FOCUS,
        opacity=0.13,
        line_width=0,
        layer="below",
    )

    # The bars carry their width in milliseconds with a datetime `base`, which
    # is how plotly draws timelines — but it infers a linear axis from those
    # numbers unless the axis type is pinned to dates, which throws every bar
    # back to zero.
    step_ms, tick_fmt = axis_ticks(x_lo, x_hi)
    time_axis = dict(
        type="date",
        range=[local(x_lo), local(x_hi)],
        dtick=step_ms,
        tickformat=tick_fmt,
        tickangle=-45 if step_ms <= 30 * 60 * 1000 else 0,
        ticks="outside",
        ticklen=4,
        tickfont=dict(size=11),
        title=None,
    )

    # A second axis pinned to the top so the times stay readable on a long list
    # without scrolling back up. It needs a trace of its own to render at all.
    fig.add_trace(
        go.Scatter(x=[], y=[], xaxis="x2", mode="markers", hoverinfo="skip", showlegend=False)
    )

    fig.update_layout(
        template=PLOT_TEMPLATE,
        font=dict(family="Inter, sans-serif"),
        barmode="overlay",
        bargap=0.15,
        height=max(260, 30 * len(labels) + 150),
        margin=dict(l=10, r=20, t=70, b=60),
        # A plain click should always be a click, never the start of a zoom-drag —
        # otherwise the tiny amount of mouse movement in a real click can be read
        # as a drag and swallow the selection.
        dragmode=False,
        xaxis=dict(showgrid=True, gridcolor="rgba(140,144,152,0.22)", **time_axis),
        xaxis2=dict(overlaying="x", side="top", showgrid=False, **time_axis),
        yaxis=dict(
            categoryorder="array",
            categoryarray=labels[::-1],
            showgrid=False,
            automargin=True,
            title=None,
        ),
        plot_bgcolor="rgba(0,0,0,0)",
        paper_bgcolor="rgba(0,0,0,0)",
        hoverlabel=dict(font_size=12),
        showlegend=False,
    )
    return fig


def chart_legend(focus_team: str) -> str:
    """Static HTML legend for the chart's color coding.

    Plotly's in-figure legend positions itself as a fraction of the plot
    area, so a short chart (few clubs) puts it right on top of the axis date
    labels. Rendering it as ordinary HTML above the figure sidesteps that
    entirely and keeps it a fixed height regardless of row count.
    """
    swatches = "".join(
        f"<span class='legend-item'><span class='legend-swatch' "
        f"style='background:{color}'></span>{label}</span>"
        for color, label in [
            (BLOCK, "Other clubs' news window"),
            (BLOCK_FOCUS, f"{focus_team}'s news window"),
            (BUSY_LINE, "Match likely still in progress (~2h from kickoff)"),
        ]
    )
    swatches += (
        f"<span class='legend-item'><span class='legend-tick' "
        f"style='background:{KO_TICK}'></span>Kickoff</span>"
    )
    return f"<div class='chart-legend'>{swatches}</div>"


listings = []

for gw in selected_gws:
    if len(selected_gws) > 1:
        st.markdown(f"#### {core.gameweek_label(gw, tz)}")

    gw_withheld = withheld[withheld["gameweek"] == gw]
    if not gw_withheld.empty:
        clubs = sorted(gw_withheld["team"].unique())
        listed = ", ".join(clubs[:6]) + (f" and {len(clubs) - 6} more" if len(clubs) > 6 else "")
        st.caption(
            f"{len(gw_withheld)} fixture(s) hidden — Sorare has not published a kickoff time yet, "
            f"so the export only carries a placeholder date, which is often the wrong day: {listed}."
        )

    result = mesh_for(gw)
    if result is None:
        st.caption(f"{focus_team} does not play in {core.gameweek_label(gw, tz)}.")
        st.divider()
        continue

    focus_fx, focus_ko, meshed = result
    listings.append(meshed)
    focus_row = focus_fx.iloc[0]
    focus_from, focus_to = core.info_window(focus_ko, early_lead, late_lead)
    others = meshed[meshed["team"] != focus_team]
    overlapping = others[others["overlap_min"] > 0]

    with st.container(border=True, key="stat_card"):
        m1, m2, m3 = st.columns(3)
        m1.markdown(
            f"<p class='stat-line'>{focus_team} kicks off — <b>{stamp(focus_ko)}</b></p>",
            unsafe_allow_html=True,
        )
        m2.markdown(
            f"<p class='stat-line'>Lineup Availability Window — "
            f"<b>{clock(focus_from)} – {clock(focus_to)}</b></p>",
            unsafe_allow_html=True,
        )
        m3.markdown(
            f"<p class='stat-line'>Clubs that overlap — <b>{len(overlapping)}</b></p>",
            unsafe_allow_html=True,
        )

    if not focus_row["covered"]:
        st.warning(f"{focus_team}'s game is flagged NOT_COVERED — Sorare will not score it at all.")
    if len(focus_fx) > 1:
        st.caption(f"{focus_team} plays twice this gameweek. Anchored on the first, {stamp(focus_ko)}.")

    chart_rows = meshed.head(rows_shown + 1)
    if whole_gameweek:
        x_lo, x_hi = core.gameweek_bounds(gw)
    else:
        pad = pd.Timedelta(minutes=45)
        x_lo = chart_rows["window_from"].min() - pad
        x_hi = chart_rows["kickoff_utc"].max() + pad

    st.markdown(chart_legend(focus_team), unsafe_allow_html=True)
    event = chart(
        overlap_chart(chart_rows, focus_from, focus_to, x_lo, x_hi),
        key=f"chart_{gw}",
        selectable=not held_cards.empty,
    )

    st.toggle("Whole gameweek", value=False, key="f_whole_gw")

    picked = clicked_club(event) if not held_cards.empty else None
    if held_cards.empty:
        pass
    elif picked is None and not _SUPPORTS_SELECT:
        options = ["—"] + [c for c in chart_rows["team"] if c in owned_counts]
        choice = st.selectbox("Show my cards for", options, key=f"cards_for_{gw}")
        picked = None if choice == "—" else choice

    if picked:
        render_my_cards(picked)
    elif not held_cards.empty and _SUPPORTS_SELECT:
        st.caption(
            "Click a club's row on the chart — the background, the block, or the kickoff "
            "tick all work — to see the cards you hold for it."
        )

    lulls = core.quiet_stretches(window[window["gameweek"] == gw], top=4, min_minutes=180)
    if lulls:
        st.markdown(
            "**Quiet stretches** — "
            + " · ".join(
                f"{fmt_duration(pd.Timedelta(minutes=g['minutes']))} from {stamp(g['from'])}"
                for g in lulls
            )
        )

    st.divider()

if not listings:
    st.info(f"{focus_team} has no fixture in any of the gameweeks picked.")
    st.stop()

st.markdown(
    f"<p class='note'>The shaded column is {focus_team}'s own window. A block inside it means you "
    "would have both team sheets while the lineup is still editable. The tick on the right of each "
    "block is kickoff — past that, the club cannot go in at all.</p>",
    unsafe_allow_html=True,
)


# --- table -----------------------------------------------------------------

diff = _difficulty_long(fixtures, lut, "mean")
diff = diff[diff["game_id"].isin(set(window["game_id"]))]
score_wide = diff.pivot_table(
    index=["team", "game_id"], columns="position", values="score", aggfunc="first"
)
pct_wide = diff.pivot_table(
    index=["team", "game_id"], columns="position", values="pct", aggfunc="first"
)

listing = pd.concat(listings, ignore_index=True)
keys = list(zip(listing["team"], listing["game_id"]))

table = pd.DataFrame(
    {
        "GW": listing["gw"],
        "Club": listing["team"],
        "My cards": [
            owned_detail.get(t, "—") for t in listing["team"]
        ],
        "Kickoff": [
            stamp(k) + (" (no time set)" if tbd else "")
            for k, tbd in zip(listing["kickoff_utc"], listing["kickoff_tbd"])
        ],
        "vs focus": [signed(d) for d in listing["delta_min"]],
        "Overlap": [describe_overlap(s) for s in listing["score_min"]],
        "Opponent": listing["opponent"],
        "H/A": listing["location"],
        "Competition": listing["competition"],
    }
)
for pos, short in POS_SHORT.items():
    table[short] = [
        round(score_wide[pos].get(k, np.nan), 1) if pos in score_wide else np.nan for k in keys
    ]
pcts = pd.DataFrame(
    {
        short: [pct_wide[pos].get(k, np.nan) if pos in pct_wide else np.nan for k in keys]
        for pos, short in POS_SHORT.items()
    }
)
if len(selected_gws) == 1:
    table = table.drop(columns="GW")


def shade(pct):
    if pd.isna(pct):
        return ""
    band = min(int(pct * len(SCALE)), len(SCALE) - 1)
    return f"background-color: {SCALE[band]}; color: #ffffff;"


def style_table(df: pd.DataFrame):
    def paint(_):
        css = pd.DataFrame("", index=df.index, columns=df.columns)
        for short in POS_SHORT.values():
            css[short] = [shade(p) for p in pcts[short]]
        css["Club"] = np.where(df["Club"] == focus_team, "font-weight:700;", "")
        return css

    return df.style.apply(paint, axis=None).format(precision=1)


weeks = ", ".join(f"GW {g}" for g in selected_gws)
st.markdown(f"**Every club in this competition — {weeks}** · {len(table)} rows")
table_event = show_table(
    style_table(table),
    key="f_table_select",
    selectable=not held_cards.empty,
    hide_index=True,
    height=min(640, 40 + 35 * len(table)),
)
table_row = clicked_row(table_event) if not held_cards.empty else None
table_picked = table["Club"].iloc[table_row] if table_row is not None else None
if table_picked:
    render_my_cards(table_picked)
elif not held_cards.empty and _SUPPORTS_SELECT:
    st.caption("Click a row above to see the cards you hold for that club.")
st.download_button(
    "Download this list",
    table.to_csv(index=False).encode(),
    file_name=f"{focus_team.replace(' ', '_')}_overlap.csv",
    mime="text/csv",
)
st.markdown(
    "<p class='note'>GK / DEF / MID / FWD are the average Sorare score that club's opponent has "
    "allowed to each position, home or away as applicable. Higher and greener is an easier matchup; "
    "shading is the percentile within that position, since the four do not share a scale. "
    "Blank means the opponent has no history in the export — usually a promoted club or a "
    "continental opponent from outside it.</p>",
    unsafe_allow_html=True,
)

with st.expander("How the sorting works"):
    st.markdown(
        f"""
A lineup locks at the first kickoff in it, and a club's confirmed XI lands {early_lead} to
{late_lead} minutes before its own. Each club therefore has a news window of
{early_lead - late_lead} minutes. Two clubs' windows are the same length, so the overlap between
them is just `{early_lead - late_lead} − |difference in kickoffs|`.

That single number sorts the list: positive is minutes of shared coverage, negative is the dead air
before the next club's news lands. Sorting by it puts everything you can pair with at the top and
walks outward through the lulls.

Gameweeks run Friday 10:00 ET → Tuesday 09:59 ET and Tuesday 10:00 ET → Friday 09:59 ET, with GW 1
starting Friday 31 July 2026. Kickoffs in the export are Zulu and get converted first.
        """
    )
