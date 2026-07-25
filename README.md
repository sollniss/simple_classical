# Simple Classical — a MusicBrainz Picard plugin

Lightweight classical-music tagging built around one idea: the plugin
extracts facts from MusicBrainz (people, work hierarchy, key, dates) and a
small mapping interface decides which tags each fact is written to. Every
section can be disabled, every tag name is yours to choose, and an empty tag
list simply writes nothing.

## Requirements

- Picard 3.x (for Picard 2.x use the `simple_classical` package instead)
- **Options → Metadata → "Use track and release relationships"** must be
  enabled (the plugin warns in the log if it is not).

## Installation

Picard 3 installs plugins from a directory or git URL. Either use
Options → Plugins → Install, or from the command line:

    picard-cli plugins install /path/to/simple_classical3

The folder must contain `MANIFEST.toml` and `__init__.py` at its top level.

## Default output

Example: DG "Beethoven" Karajan box
(release [`4ed74b57-c4aa-476b-acc6-5e2e2ff1e73c`](https://musicbrainz.org/release/4ed74b57-c4aa-476b-acc6-5e2e2ff1e73c)),
Symphony no. 9, last track:

| Tag                | Value                                                                            |
| ------------------ | -------------------------------------------------------------------------------- |
| `artist`           | Herbert von Karajan                                                              |
| `artistsort`       | Karajan, Herbert von                                                             |
| `albumartist`      | Herbert von Karajan (+ `albumartistsort`)                                        |
| `artists`          | Ludwig van Beethoven; Herbert von Karajan _(multi-value)_                        |
| `artists_sort`     | Beethoven, Ludwig van; Karajan, Herbert von _(multi-value)_                      |
| `composer`         | Ludwig van Beethoven (+ `composersort`) _(multi-value)_                          |
| `conductor`        | Herbert von Karajan (+ `conductorsort`) _(multi-value)_                          |
| `ensemble`         | Berliner Philharmoniker (+ `performer:orchestra`) _(multi-value)_                |
| `location`         | Berliner Philharmonie                                                            |
| `grouping`         | Symphony no. 9 in D minor, op. 125 “Choral”                                      |
| `key`              | D minor                                                                          |
| `movement`, `part` | IV. Finale. Presto – Allegro assai: (part)                                       |
| `movementnumber`   | 5                                                                                |
| `movementtotal`    | 5                                                                                |
| `showmovement`     | 1                                                                                |
| `work`             | Symphony no. 9 in D minor, op. 125 “Choral”:: IV. Finale. Presto – Allegro assai |
| `work_year`        | 1822-1824 (composed)                                                             |
| `recordingdate`    | 1983-09-27                                                                       |

## The options page

Options → Plugins → Simple Classical. A Preview box at the top
takes a MusicBrainz release URL or MBID: pick a track and every section
shows, in place, the values it produces — recomputed live as you change
any option, before saving. (Values are shown even for disabled sections,
so you can see what enabling one would do.)

The **Tagging preset** box can populate all output tag fields for:

- **Portable (default)** — writes both broadly supported fields and useful
  player-specific aliases such as `movement; part` and
  `ensemble; performer:orchestra`.
- **Picard-native** — uses Picard's `performer:orchestra` and `movement`.
- **Roon** — uses `ensemble`, `work` and `part`.
- **MPD** — uses MPD's `ensemble`, `work`, `movement` and `grouping` fields.

Applying a preset changes destination fields only; it does not change enabled
sections, templates or write policies, and every populated field remains
editable.

Each section is a checkable group and has an Existing tags policy:

- **Replace existing values** — the original behavior and default.
- **Append generated values** — preserve existing values and add all generated
  values, including duplicates.
- **Merge without duplicates** — preserve existing values and add only values
  not already present.
- **Write only if the tag is empty** — leave each populated destination alone.

The policy is evaluated separately for every tag in a destination list. The
preview shows the generated values before the policy is applied because it does
not have a loaded file's existing metadata to compare against.

The individual sections are:

- **Title, Artist, Album artist, Artists, Composer, Conductor, Orchestra** —
  every section has "Write canonical to" / "Write credited to" / "Write sort
  to" tag lists (separate multiple tag names with `;`; empty = don't write)
  and a "Split into multiple values" checkbox.
  - _Canonical vs credited_: canonical is the artist's full MusicBrainz name
    ("Herbert von Karajan"), credited is the name as printed on the release
    ("Karajan"). Sort tags always use the canonical sort name — MusicBrainz
    has no as-credited sort names. For Title, canonical is the recording
    title and credited the track title.
  - _Split off_ keeps collaborations as one value using the credit's own
    join phrases ("A & B", "A feat. B"); where removing the composer breaks
    adjacency, artists are joined with `; `.
  - Artist/Album artist start from the release credit and apply a
    configurable rule per role — Composer, Conductor, Orchestra — each set
    to _Keep as credited_, _Remove if present_ or _Add if missing_
    (default: composers removed, everything else kept). Roles are matched
    via the relationships: composers of the performed works, conductors
    and performing orchestras of the recordings. The section's write
    policy decides whether generated values replace or coexist with Picard's
    values; soloists and other performers in the credit are always kept as-is.
    Artists is the full credit.
  - Conductor, Orchestra and Composer come from the recording/work
    relationships and default to the tags players actually read: the
    standard `composer`/`composersort` and `conductor`, and — for the
    orchestra — `ensemble` (the classical tag understood by MPD, Roon and
    Squeezebox) plus `performer:orchestra`, Picard's own tag for the
    relationship. The conductor sort name additionally goes to the custom
    `conductorsort`. These sections replace Picard's values by default; choose
    another write policy to append, merge or preserve existing values.
  - Recording location (default tag `location`, the classical Vorbis
    field also supported by MPD) is the recording's "recorded at" place —
    relevant e.g. for organ recordings, where the venue identifies the
    instrument. Where no place is linked, an optional fallback (off by
    default) uses the "recorded in" area instead; areas are often just a
    city or country, so leaving it off writes the tag only for actual
    venues. Picard's release data does not include place relationships,
    so the plugin loads them with one extra MusicBrainz request per album
    (per 100 recordings).
- **Recording date** — from the performance relationship's session dates;
  first day, last day (default) or the full range.
- **Work & movement** — see below.
- **Key** — the work's "Key" attribute (nearest level that has one).
- **Composition year** — from the composer relationship dates
  (`1822-1824` + configurable suffix).

## Work hierarchy templates

MusicBrainz has no "work" and "movement" fields
("IV. Finale…" is a _part of_ "Symphony no. 9…"). The plugin climbs that
hierarchy (each work fetched once per album, cached) and renders three
values through templates:

- `%L1%`: the performed work (deepest level), title relative to its parent
- `%L2%`, `%L3%`, etc. refer parents and `%top%` the topmost work (full title)
- Ranges join several levels: `%top..L1{:: }%` renders top-down glued with
  `:: `; `%L2..top{; }%` renders bottom-up. Separator defaults to `; `.
  Direction follows the written order and levels that don't exist render as
  nothing, so one template works for every depth.

Defaults: movement `%L1%`, grouping `%top%`, work `%top..L1{:: }%`. The
movement is written to both `movement` (Picard/Apple Music/MPD) and
`part` (Roon's movement tag, which groups compositions via `work`+`part`). A
standalone work (depth 1) gets `work`/`grouping` but no movement tags.
Partial performances (one movement split over several tracks) get a
configurable suffix (default `: (part)`) appended to the movement, and
movement numbers count _tracks_ within the parent work on each disc, so a
split finale correctly yields movements 4 and 5 of 5.

**Depth overrides**: a table with one row per depth (columns: depth,
movement, grouping, work). Empty cells fall back to the general templates.
E.g. for depth 3 (aria → act → opera) you might set grouping to `%L3%` to
show only the opera.

**Scripting**: the hierarchy is exported to Picard scripts and file naming
as `%_sc_l1%`…, `%_sc_top%`, `%_sc_depth%` and `%_sc_partial%`, so anything
the templates can't express is one `$set()` away in Options → Scripting.

## Data quirks handled

- Recordings linked to several works (e.g. both a Beethoven symphony
  movement and Liszt's piano transcription of it) are resolved by scoring:
  works that are themselves arrangements are ignored.
- Tag names starting with `_` are written as hidden Picard variables.

## Known bugs

### Combo boxes and menus "flicker" (open and immediately close) on COSMIC

After loading a preview release, dropdowns and menus across all of Picard
may start closing the moment they open, until the affected windows are
closed and reopened (or Picard is restarted).

This is a [cosmic-comp](https://github.com/pop-os/cosmic-comp) popup-handling bug,
not a plugin bug. The compositor loses track of popup surfaces (it logs
`surface missing from known popups`), and the broken popup state then
affects the whole application. It occurs on COSMIC without this plugin
too; the preview merely triggers it reliably. The plugin mitigates it by
deferring preview updates while any popup, menu or tooltip is open and by
never relayouting the page when nothing changed, but it cannot fix the
compositor. Related upstream issues:
[cosmic-comp#1815](https://github.com/pop-os/cosmic-comp/issues/1815),
[cosmic-comp#2064](https://github.com/pop-os/cosmic-comp/issues/2064),
[cosmic-epoch#1577](https://github.com/pop-os/cosmic-epoch/issues/1577).
Workaround: run Picard on XWayland, where the problem does not occur:

    QT_QPA_PLATFORM=xcb picard

## Development environment

The Nix development shell provides Python 3.13, PyQt6, Picard's Python runtime
dependencies, Pyright, Ruff, and pytest. Picard 3.0.0b7's source is pinned as a
flake input so the Python LSP can resolve the new `picard.plugin3.api`.

Enter the environment manually:

    nix develop

For automatic editor integration, install `direnv`, then approve this
repository's checked-in `.envrc` once:

    direnv allow

Zed is configured in `.zed/settings.json` to load direnv and use the Pyright
and Ruff binaries from the shell. Restart its language servers after the first
environment build if the project was already open. Run both checks with:

    check

Update all pinned Nix inputs with `nix flake update`. When updating the Picard
beta specifically, also change the `picard` input tag in `flake.nix`.
