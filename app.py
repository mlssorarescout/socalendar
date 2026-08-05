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
    page_icon="⏱",
    layout="wide",
    initial_sidebar_state="expanded",
)

st.markdown(
    """
    <style>
      .block-container {padding-top: 2.4rem; max-width: 1450px;}
      div[data-testid="stMetricValue"] {font-size: 1.35rem;}
      .note {color:#7a7f87; font-size:0.84rem; line-height:1.5;}
    </style>
    """,
    unsafe_allow_html=True,
)

TRACK_FILL = "rgba(140,144,152,0.10)"
TRACK_LINE = "rgba(140,144,152,0.45)"
BLOCK = "#2fae66"
BLOCK_FOCUS = "#3f6fe0"
KO_TICK = "#2b2f36"

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


def show_table(df, **kw):
    if _MODERN_WIDTH:
        st.dataframe(df, width="stretch", **kw)
    else:
        st.dataframe(df, use_container_width=True, **kw)


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
    "f_uncovered", "f_tbd", "f_gw", "f_search", "f_focus", "f_view",
    "f_scarcities", "f_owned_only", "f_season",
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
    st.markdown("### My cards")
    username = st.text_input(
        "Sorare username",
        placeholder="username from sorare.com/u/…",
        key="f_user",
        help="Press Enter after typing — Streamlit only registers the value once the "
        "field is committed.",
    )
    api_key = secret("SORARE_API_KEY")
    with st.expander("API key" + (" ✓" if api_key else " (none)")):
        if api_key:
            st.caption(
                "Using `SORARE_API_KEY` from Streamlit secrets — 600 calls a minute."
            )
        else:
            st.caption(
                "No `SORARE_API_KEY` in Streamlit secrets, so Sorare allows 20 calls a "
                "minute and a large gallery reads slowly. Locally, copy "
                "`.streamlit/secrets.toml.example` to `.streamlit/secrets.toml` and fill "
                "it in; on Streamlit Community Cloud, add it under App settings → Secrets."
            )
        if st.button("Test connection", key="f_probe"):
            if not username.strip():
                st.session_state["probe"] = {
                    "ok": False,
                    "attempts": ["Type your Sorare username in the box above first."],
                }
            else:
                try:
                    st.session_state["probe"] = sorare_api.probe(
                        username, api_key or None
                    )
                except sorare_api.SorareError as exc:
                    st.session_state["probe"] = {"ok": False, "attempts": [str(exc)]}
        result = st.session_state.get("probe")
        if result:
            if result.get("ok"):
                st.success(
                    f"Reached Sorare as **{result['nickname']}**. "
                    f"Schema variant `{result['variant']}`."
                )
                if result.get("sample_club"):
                    st.caption(f"Sample card resolves to {result['sample_club']}.")
            else:
                st.error("No query variant worked. What Sorare said:")
                for line in result.get("attempts", []):
                    st.caption(f"• {line}")

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

    st.markdown("### Competition")
    slugs_present = sorted(team_leagues["slug"].unique())
    groups = sorted({core.slug_group(s) for s in slugs_present})
    group = st.selectbox("Group", ["All"] + groups, index=0, key="f_group")
    group_slugs = (
        slugs_present if group == "All"
        else [s for s in slugs_present if core.slug_group(s) == group]
    )

    with st.expander("More options"):
        tz = st.selectbox("Time zone", TZ_CHOICES, index=0, key="f_tz")
        early_lead = st.slider("Sheets out earliest (min before KO)", 30, 150, 60, 5, key="f_early")
        late_lead = st.slider("Sheets out latest (min before KO)", 0, 120, 0, 5, key="f_late")
        if late_lead > early_lead:
            early_lead, late_lead = late_lead, early_lead
        keep_valid_many("f_leagues", set(group_slugs))
        # No `default` here: keep_valid_many writes to session state, and passing
        # both makes Streamlit warn. An unset multiselect starts empty anyway.
        leagues = st.multiselect(
            "Narrow to leagues",
            sorted(group_slugs, key=league_label),
            format_func=league_label,
            help="Optional. Empty means every league in the group.",
            key="f_leagues",
        )
        rows_shown = st.slider("Clubs on the chart", 5, 120, 60, 5, key="f_rows")
        hide_uncovered = st.toggle(
            "Hide games Sorare does not score", value=True, key="f_uncovered"
        )
        hide_tbd = st.toggle("Hide placeholder kickoff times", value=True, key="f_tbd")
        st.button("Reset all filters", on_click=reset_filters, key="f_reset")

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
            season_mode = st.radio(
                "In-season cards",
                ["Any", "Only clubs I have in-season", "Only clubs I have none"],
                key="f_season",
            )
        else:
            season_mode = "Any"
            st.caption("`inSeasonEligible` was unavailable on this schema shape.")

        held_cards = sorare_api.annotate_teams(
            held_cards, sorted(team_leagues["team"].unique())
        )
        owned, unmatched_clubs = sorare_api.match_clubs(
            held_cards, sorted(team_leagues["team"].unique())
        )
        if has_season and not owned.empty and season_mode != "Any":
            wanted = owned["in_season"] > 0
            owned = owned[wanted if season_mode.endswith("in-season") else ~wanted]

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

    st.markdown("### Gameweek")
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
        "Which gameweek",
        gws,
        index=gws.index(default_gw),
        format_func=core.gameweek_label,
        label_visibility="collapsed",
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
    st.markdown("### Focus club")
    all_clubs = sorted(window["team"].unique())
    query = st.text_input(
        "Search clubs",
        placeholder="Type to filter clubs…",
        label_visibility="collapsed",
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


# ---------------------------------------------------------------------------
# Page
# ---------------------------------------------------------------------------

st.title("Sorare Kickoff Planner")
st.markdown(
    f"<p class='note'>Green marks when a club's confirmed XI is expected — "
    f"{early_lead} to {late_lead} minutes before it kicks off. Clubs are sorted by how much "
    f"their block overlaps <b>{focus_team}</b>'s, so the ones you can safely pair with sit at the "
    "top and the lulls open up as you scroll.</p>",
    unsafe_allow_html=True,
)

view = st.radio(
    "Timeframe",
    ["Around the focus club", "Whole gameweek"],
    horizontal=True,
    label_visibility="collapsed",
    key="f_view",
)

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
            width=0.72,
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
                width=0.72,
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
        barmode="overlay",
        bargap=0.28,
        height=max(260, 30 * len(labels) + 150),
        margin=dict(l=10, r=20, t=60, b=60),
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
    )
    return fig



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
    misses = others[others["score_min"] < 0]

    m1, m2, m3, m4 = st.columns(4)
    m1.metric(f"{focus_team} kicks off", stamp(focus_ko))
    m2.metric("Its news window", f"{clock(focus_from)} – {clock(focus_to)}")
    m3.metric("Clubs that overlap", len(overlapping))
    m4.metric(
        "Then a lull of",
        fmt_duration(pd.Timedelta(minutes=misses["lull_min"].min())) if len(misses) else "—",
        help="Dead air between the end of the overlapping group's news and the next club's.",
    )

    if not focus_row["covered"]:
        st.warning(f"{focus_team}'s game is flagged NOT_COVERED — Sorare will not score it at all.")
    if len(focus_fx) > 1:
        st.caption(f"{focus_team} plays twice this gameweek. Anchored on the first, {stamp(focus_ko)}.")

    chart_rows = meshed.head(rows_shown + 1)
    if view == "Whole gameweek":
        x_lo, x_hi = core.gameweek_bounds(gw)
    else:
        pad = pd.Timedelta(minutes=45)
        x_lo = chart_rows["window_from"].min() - pad
        x_hi = chart_rows["kickoff_utc"].max() + pad

    event = chart(
        overlap_chart(chart_rows, focus_from, focus_to, x_lo, x_hi),
        key=f"chart_{gw}",
        selectable=not held_cards.empty,
    )

    picked = clicked_club(event) if not held_cards.empty else None
    if held_cards.empty:
        pass
    elif picked is None and not _SUPPORTS_SELECT:
        options = ["—"] + [c for c in chart_rows["team"] if c in owned_counts]
        choice = st.selectbox("Show my cards for", options, key=f"cards_for_{gw}")
        picked = None if choice == "—" else choice

    if picked:
        mine = held_cards[held_cards["team"] == picked]
        if mine.empty:
            st.caption(f"No cards for {picked} in the current scarcity/in-season filter.")
        else:
            with st.container(border=True):
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
    elif not held_cards.empty and _SUPPORTS_SELECT:
        st.caption("Click a club's row on the chart to see the cards you hold for it.")

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
show_table(style_table(table), hide_index=True, height=min(640, 40 + 35 * len(table)))
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
