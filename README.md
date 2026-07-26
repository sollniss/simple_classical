# Simple Classical — a MusicBrainz Picard plugin

Lightweight classical-music tagging built around one idea: the plugin
extracts facts from MusicBrainz (people, work hierarchy, key, dates) and a
small mapping interface decides which tags each fact is written to. Every
section can be disabled, every tag name is yours to choose, and an empty tag
list simply writes nothing.

- [Requirements](#requirements)
- [Installation](#installation)
- [Default output](#default-output)
- [The options page](#the-options-page)
- [Work hierarchy templates](#work-hierarchy-templates)
- [Classical-or-not detection](#classical-or-not-detection)
- [Data quirks handled](#data-quirks-handled)
- [Relationship to Classical Extras](#relationship-to-classical-extras)
  - [General approach](#general-approach)
  - [Shared functionality](#shared-functionality)
  - [Only in Simple Classical](#only-in-simple-classical)
  - [Only in Classical Extras](#only-in-classical-extras)
- [Translations](#translations)
- [Development environment](#development-environment)

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
- **Recording date** — session dates from the performance relationship or
  the "recorded at"/"recorded in" relationships, whichever span is more
  precise (the performance relationship wins ties); first day, last day
  (default) or the full range. The place dates are loaded with the same
  extra request the recording location section uses, skipped when the
  performance dates are already day-precise.
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

## Classical-or-not detection

For libraries that mix classical with pop/rock or jazz, the optional
**Classical detection** section decides per release whether the plugin
tags it at all. MusicBrainz has no authoritative "this is classical"
flag, so the decision is built from five signals, all computed from the
release data the plugin already fetches (no extra requests):

- **Work has composer** — a performed work has a composer relationship.
- **Conductor/orchestra** — a recording has a conductor or "performing
  orchestra" relationship.
- **Composer in credit** — one of those composers appears in the release
  artist credit.
- **Classical genre** — a genre or folksonomy tag of the release or its
  release group matches the configurable keyword list (requires "Use
  genres from MusicBrainz" to be enabled in Picard's options; without it
  this signal is always "no").
- **Multi-movement work** — a performed work is part of a larger work.

The signals are combined in a rules table: the release counts as
classical if **any row** matches, and a row matches when every signal
set to _required_ holds and none set to _must not hold_ does. The
default rules are:

| #   | Work has composer | Conductor/orchestra | Composer in credit | Classical genre | Multi-movement work |
| --- | ----------------- | ------------------- | ------------------ | --------------- | ------------------- |
| 1   | required          | required            | —                  | —               | —                   |
| 2   | required          | —                   | required           | —               | —                   |
| 3   | —                 | —                   | —                  | required        | —                   |

With the section disabled (the default) every release is tagged.
Either way the verdict is exported to Picard scripts as
`%_sc_classical%` (`1`/`0`) and every signal as `%_sc_sig_composer%`,
`%_sc_sig_conductor_orchestra%`, `%_sc_sig_composer_in_credit%`,
`%_sc_sig_genre%` and `%_sc_sig_multi_movement%`. The work-hierarchy
`%_sc_...%` variables are also still exported for releases the gate
skips.

The preview on the options page shows, for the loaded release, each
signal's value and which rule (if any) matched and evaluated live with the
current, unsaved rules.

So that the verdict is known before anything is written, the plugin
defers all tag writing until the release's asynchronous data (work
hierarchies, recording places) has arrived. The writes still land before
the album finishes loading, so nothing changes from the user's
perspective.

## Data quirks handled

- Recordings linked to several works (e.g. both a Beethoven symphony
  movement and Liszt's piano transcription of it) are resolved by scoring:
  works that are themselves arrangements are ignored.
- Tag names starting with `_` are written as hidden Picard variables.

## Relationship to Classical Extras

[Classical Extras](https://github.com/MetaTunes/picard-plugins/tree/metabrainz/2.0/plugins/classical_extras)
by Mark Evens has long been _the_ comprehensive classical tagging plugin
for Picard 2.x, and it does far more than this plugin ever intends to.
Simple Classical shares no code with it and is not a port, but it covers
some of the same ground, so here is how the two relate. If a Classical
Extras feature is listed below as not covered, that is most likely a deliberate
scope decision, not an oversight.

### General approach

Classical Extras computes a large set of hidden variables (`_cwp_*`
for the work hierarchy, `_cea_*` for artists) and leaves it to you to
route them into real tags, either through its tag-mapping table or your own
tagger scripts. Everything the plugin knows is exposed, which makes it
extremely flexible, at the price of a five-tab options page and some
scripting for custom output.

Simple Classical turns that model around: each kind of fact is a section
that writes destination tags directly, configured as a tag-name list
plus (for the work hierarchy) a small template. The `%_sc_...%` script
variables are an escape hatch for when the UI is not enough. The trade-off
is less exposed data in exchange for a setup that is one options page,
a live preview and no scripting.

Other differences in approach:

- **Picard version**: Classical Extras supports Picard 2.0–2.7 (PyQt5)
  and has no Picard 3 port at the time of writing; this plugin targets
  Picard 3 only.
- Both require **"Use track and release relationships"**; Classical
  Extras enables the options itself, this plugin only warns in the log.
- Both need **extra MusicBrainz lookups** to climb the work hierarchy
  and cache the results. Classical Extras additionally offers a
  persistent cross-session cache; this plugin caches per album and adds
  one browse request per album (per 100 recordings) for the
  place/date data.
- **Options** here are one global config plus per-player presets;
  Classical Extras can save its entire option set into tags per album
  (or track) and re-apply it later.
- For checking results, this plugin offers a **live preview** on the
  options page; Classical Extras instead offers per-release log files
  and custom columns in Picard's file pane.

### Shared functionality

| Functionality                               | Simple Classical                                                                                                       | Classical Extras                                                                                                            | Compatibility                                                                                                                                                                            |
| ------------------------------------------- | ---------------------------------------------------------------------------------------------------------------------- | --------------------------------------------------------------------------------------------------------------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Work/movement from the MB work hierarchy    | Templates (`%L1%`, `%top%`, ranges), up to 4 levels, per-depth overrides                                               | Unlimited levels; three naming styles: MB canonical, track-title text, or canonical enhanced with title text                | Both default to a `::`-separated multi-level work value; names differ where Classical Extras' default "extended" style appends `{title text}`                                            |
| Movement titles relative to the parent work | Strips the parent title and leading punctuation                                                                        | Also strips repeated text elsewhere in the title, with similarity thresholds and synonym lists                              | Same result on well-formed MB titles; Classical Extras cleans up inconsistent data more aggressively                                                                                     |
| Movement numbering                          | Counts tracks within the parent work per disc; split movements numbered separately                                     | Same semantics, with additional rules for interleaved works                                                                 | Both write `movement`, `movementnumber`, `movementtotal` by default                                                                                                                      |
| Partial performances                        | Suffix on the movement, default `: (part)`                                                                             | Notional sub-part with suffix, default ` (part)`                                                                            | Same concept, slightly different default text                                                                                                                                            |
| Apple Music work display                    | `showmovement` = 1                                                                                                     | "show work movement" = 1                                                                                                    | Same                                                                                                                                                                                     |
| Composer, conductor, orchestra              | Dedicated sections; canonical, as-credited and sort names; multi-value split option                                    | Part of a wider artist engine with per-context credited-as, aliases and ensemble detection by name lists                    | Default `composer`/`composersort`/`conductor` tags line up; for the orchestra this plugin writes `ensemble`/`performer:orchestra` by default, Classical Extras leaves the mapping to you |
| Artist / album artist adjustment            | Per-role keep/remove/add rules applied to the release credit                                                           | Recording-artist replace/merge options; can prefix the _album title_ with composer last names                               | Different mechanisms, compare output before assuming parity                                                                                                                              |
| Key                                         | The work's Key attribute, nearest level that has one                                                                   | Keys from all levels, optionally embedded into work names                                                                   | Equivalent for the common single-key case                                                                                                                                                |
| Composition dates                           | Composer-relationship span, e.g. `1822-1824` + suffix                                                                  | Composed/published/premiered dates, plus period names derived from a period map                                             | This plugin covers the "composed dates" subset                                                                                                                                           |
| Arrangements                                | A recording linked to both an original and an arrangement resolves to the original                                     | The arranged work becomes a pseudo-parent; arrangement names get a prefix                                                   | Different philosophy: pick one work vs. represent the relationship                                                                                                                       |
| Existing-tag handling                       | Per-section policy: replace, append, merge, or only-if-empty                                                           | Tag map appends, per-line "Conditional?" writes only if blank; can preserve pre-existing file tags                          | Similar capability                                                                                                                                                                       |
| Script variables                            | `%_sc_l1%`…, `%_sc_top%`, `%_sc_depth%`, `%_sc_partial%`                                                               | The full `_cwp_*`/`_cea_*` set (dozens of variables)                                                                        | Scripts are **not** portable: names differ and this plugin exports far less                                                                                                              |
| Classical-or-not detection                  | User-defined rules over relationship/genre signals gate all tagging per release; verdict exported as `%_sc_classical%` | Genre lists, artist-equals-composer style rule, Muso composer roster; feeds genre/period tags rather than gating the plugin | Different mechanisms and purposes, compare before relying on parity                                                                                                                      |

### Only in Simple Classical

- Recording date (session dates from the performance or place
  relationships, picking the more precise span; first/last/range).
- Recording location/venue (`location` from the "recorded at" place,
  optional area fallback).
- Live preview of every section's output on the options page.
- One-click destination presets for portable, Picard-native, Roon and
  MPD tag naming.
- Runs on Picard 3.

### Only in Classical Extras

- Extra artist roles: arrangers (including instrument/vocal arrangers),
  orchestrators, lyricists, librettists, translators, chorus masters,
  concertmasters, reconstructors, revisors — each with sort names.
- Performer classification: soloists vs. ensembles, vocalists and
  instrumentalists, album-artist cross-references (album soloists,
  support performers, …).
- Work and artist aliases, per-context as-credited names, non-Latin
  script handling.
- Work names built or enhanced from track-title text, including
  synonym/replacement/similarity text processing.
- Genres, work types, periods, and Muso / SongKong integration.
- Instrument tags from the performer relationships.
- Medleys.
- Splitting a file's lyrics tag into album and track notes.
- Free-form tag mapping with constants and concatenation, custom file
  pane columns, per-release debug logs.

## Translations

The options page uses Picard 3's plugin i18n support. The UI is available
in English, German and Japanese; Picard's interface language (Options →
User Interface) selects the catalog, and any missing key falls back to
English. MusicBrainz relationship and tag names (`recorded at`,
`ensemble`, …) are left untranslated on purpose — they name the actual
data.

To add a language, copy `locale/en.toml` to `locale/<code>.toml` (e.g.
`fr.toml`) and translate the values; the file is picked up on the next
plugin load. Translations of the plugin description live in
`MANIFEST.toml` (`description_i18n`, `long_description_i18n`).

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
