"""
Minimal Sorare GraphQL client: fetch a user's gallery and work out which clubs
their cards currently play for.

Endpoint and limits are per https://github.com/sorare/api:

* `https://api.sorare.com/graphql`, POST, JSON.
* Unauthenticated calls are capped at 20/minute, with a query depth limit of 7
  and a complexity limit of 500. An API key in an `APIKEY` header lifts that to
  600/minute and much higher complexity.
* 429 responses carry a `Retry-After` header in seconds.
* The API rejects browser calls from other origins, so this has to run
  server-side — which is where Streamlit runs anyway.

The card connection's exact shape has moved around across schema versions
(`cards` vs `anyCards`, `player` vs `anyPlayer`), so rather than betting on one
spelling this tries a list of candidate queries and keeps the first that comes
back clean. Scarcity is always filtered client-side off `rarityTyped`, so a
wrong guess about the server-side filter argument costs pages, not correctness.
"""

from __future__ import annotations

import re
import time
import unicodedata
from dataclasses import dataclass, field

import pandas as pd
import requests

ENDPOINT = "https://api.sorare.com/graphql"

# What the app offers. `rarityTyped` comes back in this vocabulary.
SCARCITIES = ["limited", "rare", "super_rare"]
SCARCITY_LABELS = {"limited": "Limited", "rare": "Rare", "super_rare": "Super Rare"}

# Unauthenticated is 20 calls/minute; leave a little headroom.
DELAY_ANON = 3.2
DELAY_KEYED = 0.2


class SorareError(RuntimeError):
    """Anything that stopped us getting the gallery."""


@dataclass
class GalleryResult:
    nickname: str
    cards: pd.DataFrame
    pages: int
    query_variant: str
    truncated: bool = False
    notes: list[str] = field(default_factory=list)


# --------------------------------------------------------------------------
# Candidate queries
# --------------------------------------------------------------------------
# Each must expose: user.nickname, a card connection with pageInfo, and per card
# a rarity plus the player's current club. `{after}` is bound as a variable.

# `cards` returns an interface, so the nodes only become reachable inside a
# `... on AnyCardInterfaceConnection` type condition — a flat `cards { nodes }`
# is rejected. Same for `anyPlayer`, which needs `... on AnyPlayerInterface`.
# Rarities and sport are inlined as enum literals rather than bound as variables
# so we never have to guess the enum's type name.
_QUERY_TEMPLATE = """
query Gallery($slug: String!, $first: Int!, $after: String) {
  user(slug: $slug) {
    nickname
    slug
    cards(rarities: [%(rarities)s], sport: FOOTBALL%(owned)s, first: $first, after: $after) {
      ... on AnyCardInterfaceConnection {
        nodes {
          slug%(rarity_field)s%(season_field)s
          anyPlayer {
            ... on AnyPlayerInterface {
              slug%(name_field)s
              anyPositions
              activeClub { name }
            }
          }
        }
        pageInfo { hasNextPage endCursor }
      }
    }
  }
}
"""

_RARITY = "\n          rarityTyped"
_SEASON = "\n          inSeasonEligible"
_NAME = "\n              displayName"

# Ordered by what we expect to work. `ownedByMe: true` is deliberately NOT in
# the first shape: with only an APIKEY header there is no authenticated user, so
# it matches nothing — it stays available further down in case a future auth
# mode needs it. Everything after the first drops one optional field.
_SHAPES = [
    ("full", {"owned": "", "rarity_field": _RARITY, "season_field": _SEASON,
              "name_field": _NAME}),
    ("no-inSeason", {"owned": "", "rarity_field": _RARITY, "season_field": "",
                     "name_field": _NAME}),
    ("no-displayName", {"owned": "", "rarity_field": _RARITY, "season_field": _SEASON,
                        "name_field": ""}),
    ("owned-by-me", {"owned": ", ownedByMe: true", "rarity_field": _RARITY,
                     "season_field": _SEASON, "name_field": _NAME}),
    ("minimal", {"owned": "", "rarity_field": "", "season_field": "", "name_field": ""}),
]


def build_variants(scarcities):
    """Concrete queries for the requested scarcities, most featured first."""
    safe = [r for r in scarcities if r in SCARCITIES] or list(SCARCITIES)
    rarities = ", ".join(safe)
    return [
        (name, _QUERY_TEMPLATE % {**parts, "rarities": rarities})
        for name, parts in _SHAPES
    ]


# --------------------------------------------------------------------------
# Transport
# --------------------------------------------------------------------------


def _default_post(query: str, variables: dict, api_key: str | None, timeout: int = 30):
    headers = {"content-type": "application/json"}
    if api_key:
        headers["APIKEY"] = api_key
    response = requests.post(
        ENDPOINT, json={"query": query, "variables": variables}, headers=headers, timeout=timeout
    )
    if response.status_code == 429:
        wait = int(response.headers.get("Retry-After", "30"))
        raise RateLimited(wait)
    if response.status_code >= 400:
        raise SorareError(f"Sorare returned HTTP {response.status_code}: {response.text[:300]}")
    return response.json()


class UserNotFound(SorareError):
    """The username does not exist — no point trying another query spelling."""


class RateLimited(SorareError):
    def __init__(self, retry_after: int):
        super().__init__(f"Rate limited; retry in {retry_after}s")
        self.retry_after = retry_after


def _first_error(payload: dict) -> str | None:
    errors = payload.get("errors")
    if not errors:
        return None
    return "; ".join(str(e.get("message", e)) for e in errors[:3])


def _connection(user: dict) -> tuple[str, dict] | tuple[None, None]:
    """Find whichever card connection the response actually carries."""
    for key in ("cards", "anyCards", "paginatedCards"):
        block = user.get(key)
        if isinstance(block, dict) and "nodes" in block:
            return key, block
    return None, None


def _player_of(node: dict) -> dict:
    for key in ("player", "anyPlayer"):
        value = node.get(key)
        if isinstance(value, dict):
            return value
    return {}


def _name_from_slug(slug) -> str | None:
    """Readable fallback when a shape without `displayName` was used."""
    if not isinstance(slug, str) or not slug:
        return None
    return " ".join(part.capitalize() for part in slug.split("-") if not part.isdigit())


def normalise_scarcity(value) -> str:
    """`super_rare`, `SUPER_RARE`, `superRare` -> `super_rare`."""
    if not isinstance(value, str):
        return ""
    spaced = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", "_", value)
    return re.sub(r"[^a-z0-9]+", "_", spaced.lower()).strip("_")


def normalise_position(value) -> str:
    """`GOALKEEPER`, `Goalkeeper` -> `Goalkeeper`, matching `core.POSITIONS`."""
    if not isinstance(value, str) or not value:
        return ""
    return " ".join(part.capitalize() for part in re.split(r"[_\s]+", value) if part)


def _primary_position(player: dict) -> str:
    """`anyPositions` is a list — a card's listed position is its first entry."""
    positions = player.get("anyPositions")
    if not isinstance(positions, list) or not positions:
        return ""
    return normalise_position(positions[0])


# --------------------------------------------------------------------------
# Fetching
# --------------------------------------------------------------------------


def probe(username: str, api_key: str | None = None, poster=None,
          scarcities: list[str] | None = None) -> dict:
    """
    One cheap call to check a key and username before committing to a full read.

    Asks for a single card so it cannot breach any complexity limit, and reports
    which query spelling the live schema accepted.
    """
    post = poster or _default_post
    attempts = []
    for variant_name, query in build_variants(scarcities or SCARCITIES):
        try:
            payload = post(query, {"slug": username.strip(), "first": 1, "after": None}, api_key)
        except RateLimited:
            raise
        except SorareError as exc:
            attempts.append(f"{variant_name}: {exc}")
            continue

        error = _first_error(payload)
        if error:
            attempts.append(f"{variant_name}: {error}")
            continue

        user = (payload.get("data") or {}).get("user")
        if not user:
            raise UserNotFound(
                f"Reached Sorare, but there is no user called “{username}”."
            )
        key, block = _connection(user)
        if block is None:
            attempts.append(f"{variant_name}: response had no card list")
            continue
        node = (block.get("nodes") or [{}])[0]
        player = _player_of(node)
        return {
            "ok": True,
            "variant": variant_name,
            "connection": key,
            "nickname": user.get("nickname"),
            "sample_card": node.get("slug"),
            "sample_scarcity": normalise_scarcity(node.get("rarityTyped")),
            "sample_club": (player.get("activeClub") or {}).get("name"),
            "sample_player": player.get("displayName") or _name_from_slug(player.get("slug")),
            "attempts": attempts,
        }
    return {"ok": False, "attempts": attempts}


def fetch_gallery(
    nickname: str,
    scarcities: list[str] | None = None,
    api_key: str | None = None,
    page_size: int = 50,
    max_pages: int = 40,
    poster=None,
    progress=None,
    sleep=time.sleep,
) -> GalleryResult:
    """
    Page through a user's gallery and return one row per card.

    `poster` and `sleep` are injectable so the paging, retry and parsing logic
    can be tested without touching the network.
    """
    nickname = (nickname or "").strip()
    if not nickname:
        raise SorareError("Give me a Sorare username first.")
    scarcities = scarcities or list(SCARCITIES)
    post = poster or _default_post
    delay = DELAY_KEYED if api_key else DELAY_ANON

    # Anonymous calls are capped at complexity 500, which a 50-card page can
    # breach. Sorare documents the API key as raising the *rate* limit; whether
    # it also raises complexity is not stated, so keep a step-down ladder either
    # way rather than betting on it.
    sizes = [page_size, 25, 10] if api_key else [min(page_size, 25), 10]
    sizes = sorted({s for s in sizes if s > 0}, reverse=True)

    last_error = None
    empty_result = None
    for variant_name, query in build_variants(scarcities):
        for size in sizes:
            try:
                result = _drain(
                    nickname, query, variant_name, scarcities, api_key,
                    size, max_pages, post, progress, sleep, delay,
                )
            except (RateLimited, UserNotFound):
                raise
            except SorareError as exc:
                last_error = exc
                message = str(exc).lower()
                # A schema mismatch means try the next spelling; a complexity
                # complaint means try a smaller page of the same one.
                if "complexity" in message or "depth" in message:
                    continue
                break

            if not result.cards.empty:
                return result
            # A valid query that matches nothing is not an error, so the loop
            # above never sees it — but it is exactly what `ownedByMe: true`
            # does when no user is authenticated. Keep the first empty answer
            # and try the next shape before believing the gallery is empty.
            empty_result = empty_result or result
            break

    if empty_result is not None:
        empty_result.notes.append(
            "No cards matched. If this gallery is not empty, the account may hold "
            "no Limited/Rare/Super Rare football cards for the chosen scarcities."
        )
        return empty_result
    raise SorareError(
        f"Could not read that gallery. Last error from Sorare: {last_error}"
    )


def _drain(
    nickname, query, variant_name, scarcities, api_key,
    page_size, max_pages, post, progress, sleep, delay,
):
    rows, cursor, pages, truncated = [], None, 0, False
    display_name = nickname

    while pages < max_pages:
        variables = {"slug": nickname, "first": page_size, "after": cursor}
        try:
            payload = post(query, variables, api_key)
        except RateLimited as limited:
            if progress:
                progress(f"Rate limited by Sorare, waiting {limited.retry_after}s…", None)
            sleep(limited.retry_after + 1)
            payload = post(query, variables, api_key)

        error = _first_error(payload)
        if error:
            raise SorareError(error)

        user = (payload.get("data") or {}).get("user")
        if not user:
            raise UserNotFound(
                f"No Sorare user called “{nickname}”. Use the username from the "
                "profile URL, e.g. sorare.com/u/<username>."
            )
        display_name = user.get("nickname") or nickname

        key, block = _connection(user)
        if block is None:
            raise SorareError("That response carried no card list (schema mismatch).")

        for node in block.get("nodes") or []:
            player = _player_of(node)
            club = player.get("activeClub") or {}
            rows.append(
                {
                    "card_slug": node.get("slug"),
                    "scarcity": normalise_scarcity(node.get("rarityTyped")),
                    "in_season": node.get("inSeasonEligible"),
                    "player": player.get("displayName") or _name_from_slug(player.get("slug")),
                    "player_slug": player.get("slug"),
                    "position": _primary_position(player),
                    "club": (club or {}).get("name"),
                    "club_slug": (club or {}).get("slug"),
                }
            )

        pages += 1
        info = block.get("pageInfo") or {}
        cursor = info.get("endCursor")
        if progress:
            progress(f"Read {len(rows)} cards…", pages / max_pages)
        if not info.get("hasNextPage"):
            break
        sleep(delay)
    else:
        truncated = True

    cards = pd.DataFrame(rows, columns=[
        "card_slug", "scarcity", "in_season", "player", "player_slug", "position",
        "club", "club_slug",
    ])
    notes = []
    if not cards.empty:
        no_club = int(cards["club"].isna().sum())
        if no_club:
            notes.append(f"{no_club} card(s) have no current club — free agents or retired.")
        if cards["scarcity"].ne("").any():
            cards = cards[cards["scarcity"].isin(scarcities)]
        else:
            # A shape without `rarityTyped` was used, so the server-side
            # `rarities:` filter is the only guarantee — do not drop everything.
            notes.append("Scarcity per card unavailable on this schema shape.")
    return GalleryResult(
        nickname=display_name,
        cards=cards.reset_index(drop=True),
        pages=pages,
        query_variant=variant_name,
        truncated=truncated,
        notes=notes,
    )


# --------------------------------------------------------------------------
# Matching gallery clubs onto the fixture export
# --------------------------------------------------------------------------


def _key(name: str) -> str:
    """Fold accents, case and punctuation so club names compare sensibly."""
    if not isinstance(name, str):
        return ""
    folded = unicodedata.normalize("NFKD", name)
    folded = "".join(c for c in folded if not unicodedata.combining(c))
    return re.sub(r"[^a-z0-9]+", "", folded.lower())


def annotate_teams(cards: pd.DataFrame, fixture_teams) -> pd.DataFrame:
    """Add a `team` column holding the matching club name from the fixture export."""
    out = cards.copy()
    if out.empty:
        out["team"] = []
        return out
    lookup = {}
    for team in fixture_teams:
        lookup.setdefault(_key(team), team)
    out["team"] = out["club"].map(lambda c: lookup.get(_key(c)) if isinstance(c, str) else None)
    return out


def match_clubs(cards: pd.DataFrame, fixture_teams) -> tuple[pd.DataFrame, list[str]]:
    """
    Map gallery club names onto the club names in the fixture export.

    Both sides come from Sorare so they usually match exactly; the folded key is
    there for the accent and punctuation drift that shows up in CSV round-trips.
    Returns per-club card counts plus the club names that found no fixture.
    """
    if cards.empty:
        return pd.DataFrame(
            columns=["club", "team", "cards", "scarcities", "players", "in_season"]
        ), []

    held = annotate_teams(cards.dropna(subset=["club"]), fixture_teams)
    if "in_season" not in held.columns:
        held["in_season"] = None
    unmatched = sorted(held.loc[held["team"].isna(), "club"].unique())

    matched = held.dropna(subset=["team"])
    if matched.empty:
        return pd.DataFrame(
            columns=["club", "team", "cards", "scarcities", "players", "in_season"]
        ), unmatched

    summary = (
        matched.groupby("team")
        .agg(
            cards=("card_slug", "count"),
            scarcities=(
                "scarcity",
                lambda s: ", ".join(
                    f"{SCARCITY_LABELS.get(k, k)}×{v}"
                    for k, v in sorted(s.value_counts().items(), key=lambda kv: -kv[1])
                ),
            ),
            players=("player", lambda s: ", ".join(sorted(set(x for x in s if x)))),
            in_season=("in_season", lambda s: int(sum(bool(x) for x in s))),
        )
        .reset_index()
        .sort_values("cards", ascending=False)
    )
    summary["club"] = summary["team"]
    return summary.reset_index(drop=True), unmatched
