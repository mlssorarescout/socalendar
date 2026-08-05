"""Exercise the Sorare client without touching the network."""

import pandas as pd
import sorare_api as api


def make_poster(pages, connection="cards", player_key="anyPlayer", fail_variants=()):
    """A fake transport that serves `pages` lists of cards, cursor by cursor."""
    calls = []

    def poster(query, variables, api_key):
        calls.append(dict(variables))
        for bad in fail_variants:
            if bad in query:
                return {"errors": [{"message": f"Field '{bad}' doesn't exist on type 'User'"}]}
        after = variables["after"]
        index = 0 if after is None else int(after.split(":")[1])
        # Mirror the server: only return fields the query actually asked for.
        wants_rarity = "rarityTyped" in query
        wants_name = "displayName" in query
        nodes = []
        for c in pages[index]:
            player = {
                "slug": c["player"].lower().replace(" ", "-"),
                "activeClub": {"name": c["club"]} if c["club"] else None,
            }
            if wants_name:
                player["displayName"] = c["player"]
            node = {"slug": c["slug"], player_key: player}
            if wants_rarity:
                node["rarityTyped"] = c["rarity"]
            nodes.append(node)
        return {
            "data": {
                "user": {
                    "nickname": "TheRealThomas",
                    "slug": "thomas",
                    connection: {
                        "nodes": nodes,
                        "pageInfo": {
                            "hasNextPage": index + 1 < len(pages),
                            "endCursor": f"cur:{index + 1}",
                        },
                    },
                }
            }
        }

    poster.calls = calls
    return poster


def card(slug, rarity, player, club):
    return {"slug": slug, "rarity": rarity, "player": player, "club": club}


PAGE_A = [
    card("c1", "limited", "Erling Haaland", "Molde FK"),
    card("c2", "rare", "Marco Reus", "AFC Ajax"),
    card("c3", "common", "Nobody Common", "AFC Ajax"),
]
PAGE_B = [
    card("c4", "super_rare", "Star Player", "Molde FK"),
    card("c5", "unique", "Unique Guy", "AFC Ajax"),
    card("c6", "limited", "Free Agent", None),
]


def test_paginates_and_filters_scarcity():
    poster = make_poster([PAGE_A, PAGE_B])
    out = api.fetch_gallery("thomas", poster=poster, sleep=lambda s: None)
    assert out.pages == 2, out.pages
    assert out.nickname == "TheRealThomas"
    assert poster.calls[0]["after"] is None
    assert poster.calls[1]["after"] == "cur:1"
    kept = sorted(out.cards["card_slug"])
    assert kept == ["c1", "c2", "c4", "c6"], kept  # common + unique dropped
    assert any("no current club" in n for n in out.notes), out.notes
    print("paginates and filters scarcity: OK", kept)


def test_falls_back_when_a_field_is_missing():
    """displayName may not exist on AnyPlayerInterface; drop it and retry."""
    poster = make_poster([PAGE_A], fail_variants=("displayName",))
    out = api.fetch_gallery("thomas", poster=poster, sleep=lambda s: None)
    assert out.query_variant == "no-displayName", out.query_variant
    assert sorted(out.cards["card_slug"]) == ["c1", "c2"]
    print("field fallback: OK ->", out.query_variant)


def test_falls_back_when_ownedByMe_is_rejected():
    poster = make_poster([PAGE_A], fail_variants=("ownedByMe",))
    out = api.fetch_gallery("thomas", poster=poster, sleep=lambda s: None)
    assert out.query_variant == "no-ownedByMe", out.query_variant
    print("ownedByMe fallback: OK ->", out.query_variant)


def test_query_matches_the_verified_shape():
    name, query = api.build_variants(["limited", "rare"])[0]
    for needed in ("... on AnyCardInterfaceConnection",
                   "... on AnyPlayerInterface",
                   "rarities: [limited, rare]",
                   "sport: FOOTBALL",
                   "activeClub { name }",
                   "pageInfo { hasNextPage endCursor }"):
        assert needed in query, needed
    print("query shape: OK ->", name)


def test_scarcity_survives_a_shape_without_rarityTyped():
    """The server-side `rarities:` filter is the only guarantee then."""
    poster = make_poster([PAGE_A], fail_variants=("rarityTyped",))
    out = api.fetch_gallery("thomas", poster=poster, sleep=lambda s: None)
    assert out.query_variant == "minimal", out.query_variant
    assert len(out.cards) == 3, out.cards  # nothing wrongly dropped
    assert any("Scarcity per card unavailable" in n for n in out.notes), out.notes
    print("no-rarityTyped path: OK, kept", len(out.cards), "cards")


def test_retries_after_rate_limit():
    inner = make_poster([PAGE_A])
    state = {"first": True}
    slept = []

    def flaky(query, variables, api_key):
        if state["first"]:
            state["first"] = False
            raise api.RateLimited(7)
        return inner(query, variables, api_key)

    out = api.fetch_gallery("thomas", poster=flaky, sleep=slept.append)
    assert slept and slept[0] == 8, slept
    assert len(out.cards) == 2
    print("rate-limit retry: OK, waited", slept[0], "s")


def test_unknown_user_is_a_clear_error():
    def poster(query, variables, api_key):
        return {"data": {"user": None}}

    try:
        api.fetch_gallery("nobody", poster=poster, sleep=lambda s: None)
    except api.SorareError as exc:
        assert "nobody" in str(exc), exc
        print("unknown user: OK ->", exc)
    else:
        raise AssertionError("should have raised")


def test_truncates_at_max_pages():
    endless = [PAGE_A] * 10

    def poster(query, variables, api_key):
        base = make_poster(endless)(query, variables, api_key)
        base["data"]["user"]["cards"]["pageInfo"]["hasNextPage"] = True
        return base

    out = api.fetch_gallery("thomas", poster=poster, sleep=lambda s: None, max_pages=3)
    assert out.truncated and out.pages == 3, (out.truncated, out.pages)
    print("max-page guard: OK")


def test_scarcity_normalisation():
    cases = {"SUPER_RARE": "super_rare", "superRare": "super_rare", "Limited": "limited",
             "super rare": "super_rare", None: ""}
    for raw, want in cases.items():
        got = api.normalise_scarcity(raw)
        assert got == want, (raw, got, want)
    print("scarcity normalisation: OK")


def test_club_matching_handles_accents_and_misses():
    cards = pd.DataFrame([
        {"card_slug": "a", "scarcity": "limited", "player": "P1",
         "player_slug": "p1", "club": "Atletico Gimnasia y Esgrima de Mendoza", "club_slug": "x"},
        {"card_slug": "b", "scarcity": "rare", "player": "P2",
         "player_slug": "p2", "club": "Molde FK", "club_slug": "y"},
        {"card_slug": "c", "scarcity": "rare", "player": "P3",
         "player_slug": "p3", "club": "Some Unlisted FC", "club_slug": "z"},
        {"card_slug": "d", "scarcity": "super_rare", "player": "P4",
         "player_slug": "p4", "club": "Molde FK", "club_slug": "y"},
    ])
    teams = ["Atlético Gimnasia y Esgrima de Mendoza", "Molde FK", "AFC Ajax"]
    summary, unmatched = api.match_clubs(cards, teams)
    assert unmatched == ["Some Unlisted FC"], unmatched
    got = dict(zip(summary["team"], summary["cards"]))
    assert got == {"Molde FK": 2, "Atlético Gimnasia y Esgrima de Mendoza": 1}, got
    assert "Rare×1" in summary.set_index("team").loc["Molde FK", "scarcities"]
    print("club matching: OK ->", got, "| unmatched", unmatched)


if __name__ == "__main__":
    test_paginates_and_filters_scarcity()
    test_falls_back_when_a_field_is_missing()
    test_falls_back_when_ownedByMe_is_rejected()
    test_query_matches_the_verified_shape()
    test_scarcity_survives_a_shape_without_rarityTyped()
    test_retries_after_rate_limit()
    test_unknown_user_is_a_clear_error()
    test_truncates_at_max_pages()
    test_scarcity_normalisation()
    test_club_matching_handles_accents_and_misses()
    print("\nall passed")


def test_probe_reports_working_variant():
    poster = make_poster([PAGE_A], fail_variants=("displayName",))
    out = api.probe("thomas", poster=poster)
    assert out["ok"] and out["variant"] == "no-displayName", out
    assert out["nickname"] == "TheRealThomas"
    assert out["sample_club"] == "Molde FK", out
    assert poster.calls[-1]["first"] == 1, poster.calls
    print("probe: OK ->", out["variant"], "|", out["sample_club"])


def test_probe_flags_bad_username():
    def poster(query, variables, api_key):
        return {"data": {"user": None}}
    try:
        api.probe("nobody", poster=poster)
    except api.UserNotFound as exc:
        print("probe bad username: OK ->", exc)
    else:
        raise AssertionError("should have raised")


def test_keyed_read_still_steps_down_on_complexity():
    """An API key raises the rate limit; the docs don't promise complexity."""
    seen = []

    def poster(query, variables, api_key):
        seen.append(variables["first"])
        if variables["first"] > 25:
            return {"errors": [{"message": "Query has complexity of 900, which exceeds max"}]}
        return make_poster([PAGE_A])(query, variables, api_key)

    out = api.fetch_gallery("thomas", api_key="k", poster=poster, sleep=lambda s: None)
    assert seen[0] == 50 and 25 in seen, seen
    assert len(out.cards) == 2
    print("keyed complexity step-down: OK ->", seen)


test_probe_reports_working_variant()
test_probe_flags_bad_username()
test_keyed_read_still_steps_down_on_complexity()


def test_empty_page_advances_to_the_next_shape():
    """`ownedByMe: true` returns zero rows, not an error, when nobody is authed."""
    calls = []

    def poster(query, variables, api_key):
        calls.append("ownedByMe" in query)
        nodes = [] if "ownedByMe" in query else [
            {"slug": "c1", "rarityTyped": "rare",
             "anyPlayer": {"slug": "a-b", "displayName": "A B",
                           "activeClub": {"name": "Molde FK"}}}
        ]
        return {"data": {"user": {"nickname": "MLS Sorare Scout", "slug": "s",
                "cards": {"nodes": nodes,
                          "pageInfo": {"hasNextPage": False, "endCursor": None}}}}}

    out = api.fetch_gallery("s", poster=poster, sleep=lambda s: None)
    assert out.query_variant == "no-ownedByMe", out.query_variant
    assert out.cards["club"].tolist() == ["Molde FK"]
    print("empty-page fallback: OK ->", out.query_variant)


def test_genuinely_empty_gallery_returns_cleanly():
    def poster(query, variables, api_key):
        return {"data": {"user": {"nickname": "Nobody", "slug": "n",
                "cards": {"nodes": [],
                          "pageInfo": {"hasNextPage": False, "endCursor": None}}}}}

    out = api.fetch_gallery("n", poster=poster, sleep=lambda s: None)
    assert out.cards.empty and any("No cards matched" in n for n in out.notes), out.notes
    print("empty gallery: OK, no exception")


test_empty_page_advances_to_the_next_shape()
test_genuinely_empty_gallery_returns_cleanly()
