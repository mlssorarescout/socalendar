# Sorare Kickoff Planner

Pick a competition and a focus club. Every other club in that competition is laid out as a bar across
the gameweek, with a green block marking when its confirmed XI is expected. Clubs are sorted by how
much their block overlaps the focus club's — the ones you can safely pair with sit at the top, and
the lulls open up as you scroll.

## Why it sorts that way

A lineup locks at the kickoff of the earliest game in it, and a club's confirmed XI lands shortly
before its own kickoff. So each club has a news window — by default `[kickoff − 60, kickoff]`.
Two windows are the same length, so the overlap between them collapses to:

```
overlap = window length − |difference in kickoffs|
```

That one signed number drives everything. Positive is minutes of shared coverage — both team sheets
are out while the lineup is still editable. Negative is dead air before the next club's news lands.
Sorting by it walks outward from the focus club through the lulls.

## Running it

```bash
pip install -r requirements.txt
streamlit run app.py
```

Both CSVs are committed to `data/` and load automatically — nothing to upload, nothing to configure.

### Deploying to Streamlit Community Cloud

1. Push this folder to a GitHub repo. `data/` goes with it (~5 MB).
2. On [share.streamlit.io](https://share.streamlit.io), create an app from the repo with `app.py` as
   the entrypoint.
3. To refresh, commit newer CSVs over the ones in `data/`.

## The screen

**Sidebar** — competition group, gameweek, focus club — with a search box above the club picker. That is the whole required setup. *More options* holds the time zone, the news-window sliders (60 minutes before kickoff out to
kickoff itself, by default), an optional narrowing to specific leagues within the group, how many
clubs to chart (60 by default), and two filters that are on by default: hide games Sorare does not score, and hide
fixtures whose kickoff time is still a placeholder.

**Chart** — one row per club. The track is the timeframe, the green block is the news window, the
tick on its right edge is kickoff. The x-axis is clock time, labelled along both the top and the
bottom so it stays readable on a long list, gridded every 30 minutes when the span is short and
stepping back to hourly, two-hourly and six-hourly as it widens. The shaded column is the focus club's own window; a block inside
it means you would have both sheets while the lineup is still editable. *Around the focus club* fits
the axis to the clubs shown; *Whole gameweek* zooms out to the full Friday-to-Tuesday span so the
quiet stretches are visible.

**Table** — every club in the competition that gameweek, same order, with the four positional
difficulty scores.

Every control keeps its value when another one changes. Streamlit derives a widget's identity from
its label *and* its options, so anything whose choices depend on the competition — the gameweek, the
league narrowing, the club picker — would otherwise silently snap back to its default each time you
switched group. They are pinned to session state instead, and a stored choice is only forgotten once
it genuinely stops being on offer (picking MLS drops a Norwegian focus club, but leaves your time
zone, sliders and toggles alone). **Reset all filters** at the bottom of *More options* clears the
lot back to defaults.

The gameweek list only offers weeks the chosen competition actually plays, so picking Eredivisie in
late July jumps to GW 3 rather than dead-ending on an empty week. If the focus club is idle in one of
the weeks you picked, that section says so and the rest still render. The focus club is held across
competition and gameweek changes whenever it is still a valid choice.

## Pulling in your gallery

Type a Sorare username into **My cards** and hit *Load gallery*. The app queries
`https://api.sorare.com/graphql` for that user's cards, keeps the Limited, Rare and Super Rare ones,
resolves each card's player to the club they currently play for, and offers **Only clubs I hold cards
for** — which narrows the competition pool, the club picker and the table to exactly those clubs.

Turn that toggle off and everything comes back, with your clubs still marked: a dot beside the name
on the chart, and a *My cards* column reading e.g. `Limited×2, Rare×1`. The filter feeds the club
pool, so the chart and the table can never disagree about which clubs are on show.

**Scarcity** and **in-season** filters apply to the cards already in hand, so changing either
re-filters instantly rather than hitting the API again — the fetch always pulls all three scarcities.
The in-season filter works at club level: keep only clubs where you hold at least one in-season card,
or only clubs where you hold none. Both feed the same club pool as the owned filter, so they narrow
both views together.

**Click a club's row on the chart** and the cards you hold for it drop out underneath — player,
scarcity, in-season — narrowed by whatever scarcity and in-season filters are active. This uses
Streamlit's plotly selection events (1.35+); on older versions it falls back to a dropdown under the
chart.

A few things the client has to handle, all per [Sorare's API docs](https://github.com/sorare/api):

* **Rate limits.** Unauthenticated calls are capped at 20 a minute, so paging is spaced ~3.2s apart.
  A `429` is honoured by reading the `Retry-After` header and waiting. An `APIKEY` header raises the
  cap to 600 a minute and drops the delay to 0.2s. Paging stops at 40 pages and says so rather than
  grinding on.
* **Complexity limits.** Anonymous queries are capped at complexity 500, which a 50-card page can
  breach, so unauthenticated reads request 25 at a time and fall back to 10 if the server complains.
  Sorare documents the API key as raising the *rate* limit and is silent on whether it raises
  complexity, so keyed reads start at 50 and keep the same 25 → 10 step-down rather than assuming.
* **Interface types.** `cards` returns an interface, so the nodes are only reachable inside a
  `... on AnyCardInterfaceConnection` type condition — a flat `cards { nodes { … } }` is rejected.
  `anyPlayer` likewise needs `... on AnyPlayerInterface`. Rarities and `sport: FOOTBALL` are inlined
  as enum literals rather than bound as GraphQL variables, so the enum's type name never has to be
  guessed.
* **Optional fields.** `displayName`, `rarityTyped` and `inSeasonEligible` may or may not resolve.
  The client generates five query shapes, dropping one field each time, and keeps the first that
  answers. Where a field is missing the UI says so and hides the filter that depends on it rather
  than filtering on nulls.
* **`ownedByMe` needs a user, not a key.** With only an `APIKEY` header there is no authenticated
  user, so `ownedByMe: true` matches nothing — a *valid* query returning zero rows, not an error.
  It is therefore not in the first shape, and an empty first page counts as a reason to try the next
  shape rather than concluding the gallery is empty. A genuinely empty gallery still returns cleanly
  after every shape has been tried.
* **CORS.** Sorare blocks browser calls from other origins. Streamlit runs server-side, so this is
  fine as written — but it does mean the feature cannot be ported to a client-side app unchanged.

### Where to put your API key

The app only reads the key from Streamlit secrets — there is no box to type it into, so it can never
end up in browser history or a session. Never commit the real key. Two places to put it:

1. **Streamlit Community Cloud** — App settings → Secrets, paste `SORARE_API_KEY = "..."`.
2. **Local** — copy `.streamlit/secrets.toml.example` to `.streamlit/secrets.toml` and fill it in.
   That path is in `.gitignore`.

The sidebar's "API key" panel just reports whether one was found — ✓ if `SORARE_API_KEY` resolved,
otherwise a reminder of the two places above. Without a key the app still works, capped at Sorare's
unauthenticated 20 calls/minute. **Test connection** spends a single call to confirm the key and
username work and to report which schema spelling the live API accepted — worth running once before a
full gallery read.

Club names are matched against the fixture export on an accent- and punctuation-folded key. Both
sides come from Sorare so they usually match exactly; anything that does not match is listed under
*club(s) not in the export*, since a card whose club has no upcoming fixture cannot be scheduled.

`test_sorare_api.py` covers pagination, the schema fallback, rate-limit retry, the page cap, scarcity
normalisation and club matching against a fake transport:

```bash
python test_sorare_api.py
```

## Gameweek calendar

Gameweeks are numbered from the Sorare schedule rather than the export's own column, which is blank
for most future fixtures:

* Friday 10:00 ET → Tuesday 09:59 ET (weekend)
* Tuesday 10:00 ET → Friday 09:59 ET (midweek)
* GW 1 starts Friday 31 July 2026.

Kickoffs in the export are Zulu and are converted before bucketing. The arithmetic runs on **naive
Eastern wall-clock time**: the boundary is 10:00 on the clock all year, and adding fixed seven-day
offsets to a tz-aware timestamp would slide it an hour at the November change. GW 27 correctly spans
EDT into EST.

The numbering reproduces the export's own gameweek column exactly, offset by 699 — all 1,641 rows
that carry a value agree. Fixtures before GW 1 get numbers ≤ 0 and stay selectable.

## Competition grouping

The **Group** dropdown runs on `SORARE_COMPETITION_MAPPING`, so choosing *Contender* pulls in the
Austrian Bundesliga, HNL, Eliteserien, Liga MX and the rest in one selection. **All** is the default.

Mapping a fixture needs both the competition name and the club's card-league slug, because names
repeat across countries: in this data `Bundesliga` is Austria's, `Serie A` is Brazil's, `Premier
League` is Russia's, `Super League` is Switzerland's and `Primera División` is Chile's. Continental
cups resolve on name alone; ambiguous domestic names resolve against the slug via
`AMBIGUOUS_BY_SLUG`. Resolution happens *before* club-games are de-duplicated, so a club sitting in
two card leagues cannot lose the slug that disambiguates it. One alias: `Premier League` +
`premier-league-gb-eng` → `EPL`.

Everything resolves except **Leagues Cup**, which has no key in the mapping — add one to bucket it.

## Data notes

**`upcoming_fixtures.csv`** — one row per club per upcoming game.

* Fixtures without a published kickoff time carry an exact midnight-UTC placeholder, and the date is
  often wrong too — Hajduk Split's round-3 game reads 08 Aug when it is actually played on the 9th.
  These have to be told apart from genuine 00:00Z kickoffs, which do exist: 00:00Z is 20:00 ET, a
  normal MLS or Leagues Cup slot. Three signals catch them, any one of which is enough:
  the competition never otherwise kicks off within 90 minutes of midnight UTC (Portugal and Croatia
  play evenings local, i.e. late afternoon Zulu); every fixture in that competition's round sits at
  midnight; or the export never assigned the fixture a gameweek at all. That flags 2,355 rows and
  leaves 42 genuine midnight kickoffs across MLS, Leagues Cup, Liga Pro, Chile and Brazil.
  Placeholders are hidden by default, with a note naming the clubs affected so a missing fixture is
  never silent. Shown, they are labelled *(no time set)*.
* Midnight Zulu is also the *previous evening* in Eastern time, which would drop a placeholder Friday
  fixture into the midweek gameweek that ends that morning, so those rows are nudged to a plausible
  afternoon slot before being numbered.
* `Coverage Status = NOT_COVERED` means Sorare will not score that game at all. Hidden by default,
  and warned about if it is the focus club's own game.
* Relegated and promoted clubs appear under two card-league slugs, so the app keeps one row per
  club-game and holds the club → league mapping separately.

**`Calculated Opponent Difficulty.csv`** — `Score_mean` / `Score_median` are constant for a given
`(Opponent, Location, Position)` triple, so the app collapses it to a lookup: *facing this opponent,
at home or away, this position historically scores X*. Roughly 84% of upcoming fixtures find a match;
the gaps are newly promoted clubs and continental opponents from leagues outside the export, and show
as blanks. Raw scores are not comparable across positions, so the shading uses each score's
percentile **within its position**.

## Layout

```
app.py            Streamlit UI — one page
core.py           Gameweek calendar, competition mapping, overlap maths. No Streamlit imports.
sorare_api.py     Sorare GraphQL client: gallery fetch, paging, club matching
test_sorare_api.py  Tests for the client against a fake transport
data/             The two CSV exports, committed
requirements.txt
```

`core.py` is deliberately Streamlit-free so the maths can be tested or reused in a notebook:

```python
import core
fixtures, team_leagues = core.load_fixtures("data/upcoming_fixtures.csv")
gw1 = fixtures[(fixtures["gameweek"] == 1) & fixtures["covered"] & ~fixtures["kickoff_tbd"]]

core.gameweek_label(1)          # 'GW 1 · Fri 31 Jul – Mon 03 Aug'
focus = gw1[gw1["team"] == "Molde FK"]["kickoff_utc"].min()
core.mesh_frame(gw1, focus, 90, 30)[["team", "delta_min", "overlap_min", "lull_min"]]
core.quiet_stretches(gw1)
```
