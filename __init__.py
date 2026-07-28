# -*- coding: utf-8 -*-
"""Simple Classical

Lightweight classical-music tagging with a simple, customizable mapping
interface.  See MANIFEST.toml and README.md for details.
"""

import json
import re
from functools import partial
from typing import Any
from weakref import WeakKeyDictionary

from picard.plugin3.api import (
    OptionsPage,
    PageOptionConfigs,
    PluginApi,
)

# Not re-exported through picard.plugin3.api, but both are plain module-level
# names in Picard.  Announcing the configured tag names needs the plugin's own
# entries cleared before they are registered again (see
# _register_script_variables), and needs to know which names Picard already
# provides itself.
from picard.extension_points.script_variables import ext_point_script_variables
from picard.tags import script_variable_tag_names
from PyQt6 import (
    QtCore,
    QtWidgets,
)

# ---------------------------------------------------------------------------
# Options
# ---------------------------------------------------------------------------

# People-like sections: each writes canonical/credited/sort tag lists and can
# split multiple people into multi-value tags or join them into one value.
# Default targets follow the names Picard maps to proper frames (artist,
# composer, movement, ...) plus the classical conventions with real player
# support: 'ensemble' and 'location' (MPD, Squeezebox), 'part' (Roon).
# composer/conductor canonical stays empty where Picard writes the standard
# tag itself; sort tags without a standard equivalent get a custom name.
SECTION_DEFAULTS = {
    # section     enabled  canonical       credited  sort              split
    "title": (False, "", "", "", False),
    "artist": (True, "artist", "", "artistsort", False),
    "albumartist": (True, "albumartist", "", "albumartistsort", False),
    "artists": (True, "artists", "", "artists_sort", True),
    "composer": (True, "composer", "", "composersort", True),
    "conductor": (True, "conductor", "", "conductorsort", True),
    "orchestra": (True, "ensemble; performer:orchestra", "", "", True),
    "location": (True, "location", "", "", True),
}

# Classical-or-not detection: signals computed once per release, combined
# by user-defined rules.  A release is classical if any rule matches; a
# rule matches when all its 'require' signals hold and none of its
# 'exclude' signals does.
_CLASSICAL_SIGNALS = (
    "composer",
    "conductor_orchestra",
    "composer_in_credit",
    "genre",
    "multi_movement",
    "tagged",
)

# Signals read from relationships, which Picard only fetches with 'Use track
# and release relationships' enabled.  Without it they are not false, they
# are unknown, and a rule naming one cannot be judged at all.
_RELATIONSHIP_SIGNALS = frozenset(
    {"composer", "conductor_orchestra", "composer_in_credit", "multi_movement"}
)

_DEFAULT_CLASSICAL_RULES = json.dumps(
    [
        {"composer": "require", "conductor_orchestra": "require"},
        {"composer": "require", "composer_in_credit": "require"},
        {"genre": "require"},
    ]
)

_EXTRA_DEFAULTS = [
    # artist / album artist role rules: keep|remove|add per role
    ("artist_role_composer", "remove"),
    ("artist_role_conductor", "keep"),
    ("artist_role_orchestra", "keep"),
    ("albumartist_role_composer", "remove"),
    ("albumartist_role_conductor", "keep"),
    ("albumartist_role_orchestra", "keep"),
    # recording location: 'recorded in' areas are often just a city or
    # country, so the fallback is opt-in
    ("location_area_fallback", False),
    # classical detection; the gate is off by default (tag every release)
    ("classical_enabled", False),
    ("classical_scope", "track"),
    ("classical_rules", _DEFAULT_CLASSICAL_RULES),
    (
        "classical_genres",
        "classical; opera; operetta; ballet; baroque; chamber music; choral; "
        "early music; medieval; renaissance; romanticism; impressionism; "
        "modern classical; contemporary classical; oratorio",
    ),
    # the verdict as a real tag: written to persist it, read back as the
    # 'tagged' signal.  Both sides name their tag separately so a library
    # can be moved to another tag or value in one pass.
    # 'is_classical' = 1 is what Classical Extras writes for a release it
    # considers classical (its cwp_genres_flag_tag/_text defaults), so the
    # detection side reads that out of the box
    ("classical_detect_tag", "is_classical"),
    ("classical_detect_value", "1"),
    # off by default: naming a target makes the plugin own that tag, and
    # owning it includes removing it again where the verdict says so
    ("tag_classical", ""),
    ("classical_value", "1"),
    ("classical_negative_value", ""),
    # work & movement
    ("work_enabled", True),
    ("work_write_policy", "replace"),
    ("tpl_movement", "%L1%"),
    ("tpl_grouping", "%top%"),
    ("tpl_work", "%top..L1{:: }%"),
    ("tag_movement", "movement; part"),  # 'part' is Roon's movement tag
    ("tag_grouping", "grouping"),
    ("tag_work", "work"),
    ("tag_movementnumber", "movementnumber"),
    ("tag_movementtotal", "movementtotal"),
    ("tag_showmovement", "showmovement"),
    ("part_suffix", ": (part)"),
    ("depth_overrides", "[]"),
    # key
    ("key_enabled", True),
    ("key_write_policy", "replace"),
    ("tag_key", "key"),
    # composition year
    ("workyear_enabled", True),
    ("workyear_write_policy", "replace"),
    ("tag_work_year", "work_year"),
    ("composed_suffix", " (composed)"),
    # recording date
    ("recdate_enabled", True),
    ("recdate_write_policy", "replace"),
    ("tag_recordingdate", "recordingdate"),
    ("recording_date_mode", "end"),  # end|begin|range
]


def _default_options():
    """Option name -> default value, for registration and restore defaults."""
    defaults = {}
    for key, (enabled, canonical, credited, sort, split) in SECTION_DEFAULTS.items():
        defaults["%s_enabled" % key] = enabled
        defaults["%s_canonical" % key] = canonical
        defaults["%s_credited" % key] = credited
        defaults["%s_sort" % key] = sort
        defaults["%s_split" % key] = split
        defaults["%s_write_policy" % key] = "replace"
    defaults.update(_EXTRA_DEFAULTS)
    return defaults


def _register_options(api):
    """Register all options in the plugin's private config section.

    The option type (text/bool) is inferred from the default value.  Every
    option the dialog shows is profile-aware and carries the title the
    profiles UI needs (see _option_titles); the internal schema version and
    the three options without a widget stay plain, so that no row a user
    cannot set shows up in the profiles tree."""
    config = api.plugin_config
    config.register_option("defaults_version", 0)
    titles = _option_titles(api)
    for name, default in _default_options().items():
        title = titles.get(name)
        if title is None:
            config.register_option(name, default)
        else:
            config.register_option(name, default, title=title, in_profile=True)


# Saving the options page persists every option, freezing that release's
# defaults into the stored config.  When a default changes in a later
# release, stored values still equal to any older default are moved along;
# values the user actually customized are left alone.
_DEFAULTS_VERSION = 1
_DEFAULT_MIGRATIONS = [
    # (option, old defaults, current default)
    ("composer_enabled", (False,), True),
    ("composer_canonical", ("",), "composer"),
    ("composer_sort", ("",), "composersort"),
    ("conductor_canonical", ("",), "conductor"),
    (
        "orchestra_canonical",
        ("orchestra", "orchestra; ensemble", "ensemble"),
        "ensemble; performer:orchestra",
    ),
    ("orchestra_sort", ("orchestrasort",), ""),
    ("location_canonical", ("recordinglocation",), "location"),
    ("tag_movement", ("movement",), "movement; part"),
]


def _migrate_options(config):
    if config["defaults_version"] >= _DEFAULTS_VERSION:
        return
    for option, old_defaults, new_default in _DEFAULT_MIGRATIONS:
        if option in config and config[option] in old_defaults:
            config[option] = new_default
    config["defaults_version"] = _DEFAULTS_VERSION


# ---------------------------------------------------------------------------
# Tag lists and templates
# ---------------------------------------------------------------------------


def _parse_taglist(text):
    """'work; _hidden' -> ['work', '~hidden'].  Empty text -> []."""
    tags = []
    for name in re.split(r"[;,]", text or ""):
        name = name.strip()
        if not name:
            continue
        if name.startswith("_"):
            name = "~" + name[1:]
        tags.append(name)
    return tags


def _write_tags(metadata, taglist, value, policy="replace"):
    """Write a value to each target according to its section's policy."""
    values = value if isinstance(value, list) else [value]
    for tag in _parse_taglist(taglist):
        if policy == "if_empty":
            if not metadata.getall(tag):
                metadata[tag] = value
        elif policy == "append":
            for item in values:
                metadata.add(tag, item)
        elif policy == "merge":
            for item in values:
                metadata.add_unique(tag, item)
        else:
            metadata[tag] = value


_TEMPLATE_RE = re.compile(r"%(L\d+|top)(?:\.\.(L\d+|top))?(?:\{([^}]*)\})?%")


def _render_template(template, levels):
    """Render a hierarchy template.

    levels is bottom-up: levels[0] is the performed work (%L1%), levels[-1]
    the topmost work (%top%).  Ranges like %top..L1{:: }% join the levels in
    the written order with the separator in braces (default '; ').  Levels
    outside the available depth render as nothing.
    """
    depth = len(levels)

    def index(token):
        return depth - 1 if token == "top" else int(token[1:]) - 1

    def sub(match):
        first, second = match.group(1), match.group(2)
        start = index(first)
        end = index(second) if second else start
        separator = match.group(3) if match.group(3) is not None else "; "
        # Direction follows the syntax: %Ln..top% climbs up, %top..Ln% goes
        # down.  With a fixed 'top' endpoint this matters when the hierarchy
        # is shallower than the fixed level (the range is then empty, not
        # reversed).
        if second and first == "top":
            step = -1
        elif second == "top":
            step = 1
        else:
            step = 1 if end >= start else -1
        indexes = [i for i in range(start, end + step, step) if 0 <= i < depth]
        return separator.join(levels[i] for i in indexes)

    return _TEMPLATE_RE.sub(sub, template or "")


def _depth_overrides(setting, logger=None):
    """{depth: {'movement'|'grouping'|'work': template}} from the options."""
    try:
        rows = json.loads(setting["depth_overrides"] or "[]")
    except ValueError:
        if logger:
            logger.warning("cannot parse depth overrides")
        return {}
    result = {}
    for row in rows:
        try:
            depth = int(row.get("depth"))
        except (TypeError, ValueError):
            continue
        result[depth] = {
            field: (row.get(field) or "") for field in ("movement", "grouping", "work")
        }
    return result


# ---------------------------------------------------------------------------
# MusicBrainz JSON helpers
# ---------------------------------------------------------------------------

_ARRANGER_REL_TYPES = {
    "arranger",
    "instrument arranger",
    "vocal arranger",
    "orchestrator",
}


def _work_rels(entity, reltype=None, direction=None, target="work"):
    for rel in entity.get("relations") or []:
        if rel.get("target-type") != target:
            continue
        if reltype and rel.get("type") != reltype:
            continue
        if direction and rel.get("direction") != direction:
            continue
        yield rel


def _is_arrangement(work):
    """True if the work is itself an arrangement of another work
    (e.g. Liszt's piano transcriptions of Beethoven symphonies)."""
    for _ in _work_rels(work, "arrangement", "backward"):
        return True
    for rel in _work_rels(work, target="artist"):
        if rel.get("type") in _ARRANGER_REL_TYPES:
            return True
    return False


def _pick_rel(rels, score):
    """The highest-scoring relationship, or None if there is no candidate.

    score(rel) returns a number, or None for a relationship to skip.  Ties
    keep the one MusicBrainz listed first, so an unscored field never
    reshuffles what the plugin used to pick.
    """
    best, best_score = None, None
    for rel in rels:
        value = score(rel)
        if value is None:
            continue
        if best_score is None or value > best_score:
            best, best_score = rel, value
    return best


def _performance_score(rel):
    """Recordings are sometimes linked to several works (typically the
    original plus an arrangement of it).  Prefer non-arrangements and
    relationships that carry performance dates."""
    if not rel.get("work"):
        return None
    score = 0
    if _is_arrangement(rel["work"]):
        score -= 10
    if rel.get("begin") or rel.get("end"):
        score += 2
    if "cover" in (rel.get("attributes") or []):
        score -= 1
    return score


def _pick_performance(recording):
    """Pick the most plausible performance relationship of a recording."""
    return _pick_rel(_work_rels(recording, "performance"), _performance_score)


# Work types that inherently have parts.  A parent typed this way is more
# likely to be the real hierarchy than an untyped work that merely collects
# pieces together.
_CONTAINER_WORK_TYPES = {
    "Ballet",
    "Cantata",
    "Concerto",
    "Mass",
    "Musical",
    "Opera",
    "Operetta",
    "Oratorio",
    "Partita",
    "Quartet",
    "Sonata",
    "Song-cycle",
    "Suite",
    "Symphony",
    "Zarzuela",
}


def _parent_rel(work):
    """Pick the 'part of' parent to climb to when a work has several.

    A work can sit in more than one hierarchy (a movement of a symphony
    that is also part of a compilation work), and MusicBrainz lists those
    parents in no meaningful order.  Prefer the parent whose title really
    prefixes this work's title, then one that numbers this work as one of
    its parts, then one typed as a multi-movement container.

    Only the fields MusicBrainz inlines on the parent stub can be used;
    scoring an arrangement parent down would need its own relationships,
    which would cost one request per candidate before the climb even
    starts.
    """
    title = work.get("title") or ""

    def score(rel):
        parent = rel.get("work") or {}
        if not parent.get("id"):
            return None
        parent_title = parent.get("title") or ""
        value = 0
        if parent_title and _strip_parent_title(title, parent_title) != title:
            value += 3
        if rel.get("ordering-key") is not None:
            value += 2
        if parent.get("type") in _CONTAINER_WORK_TYPES:
            value += 1
        return value

    return _pick_rel(_work_rels(work, "parts", "backward"), score)


def _composer_rels(work):
    for rel in _work_rels(work, target="artist"):
        if rel.get("type") == "composer":
            yield rel


# One separator between the parent title and the part: punctuation with
# optional whitespace around it (': ', ', ', '. ', ' - ', ' / ', ':') or
# plain whitespace.  The punctuation branch must come first so ' - ' is
# consumed entirely, not just its leading space.
_PARENT_SEP_RE = re.compile(r"^\s*[:,;./\-–—]\s*|^\s+")


def _strip_parent_title(title, parent_title):
    """'Symphony no. 9 ...: IV. Finale' -> 'IV. Finale' given the parent.

    Exactly one separator is consumed, so an ellipsis after ': ' survives.
    Without any separator the prefix merely continues a word ('Der
    Wanderer' / 'Der Wanderers Lied') and the title is kept whole.
    """
    if parent_title and title.startswith(parent_title):
        rest = title[len(parent_title) :]
        match = _PARENT_SEP_RE.match(rest)
        if match:
            rest = rest[match.end() :].strip()
            if rest:
                return rest
    return title


def _key_attribute(work):
    for attr in work.get("attributes") or []:
        if attr.get("type") == "Key" and attr.get("value"):
            return attr["value"]
    return None


def _year_span(rels):
    """'1822-1824' (or '1808') from the begin/end dates of relationships."""
    begins = sorted(r["begin"][:4] for r in rels if r.get("begin"))
    ends = sorted(r["end"][:4] for r in rels if r.get("end"))
    first = begins[0] if begins else (ends[0] if ends else None)
    last = ends[-1] if ends else (begins[-1] if begins else None)
    if not first:
        return None
    return first if first == last else "%s-%s" % (first, last)


def _iter_release_works(release):
    for medium in release.get("media") or []:
        for track in medium.get("tracks") or []:
            recording = track.get("recording") or {}
            for rel in _work_rels(recording, "performance"):
                if rel.get("work"):
                    yield rel["work"]


# ---------------------------------------------------------------------------
# People entries (canonical/credited/sort triples)
# ---------------------------------------------------------------------------


def _credit_entries(credits, indexes):
    """Entries for artist-credit members; 'join' is the string used before
    the *next* entry when writing a single joined value (the credit's own
    join phrase where the credits are still adjacent, otherwise '; ')."""
    entries = []
    for position, index in enumerate(indexes):
        credit = credits[index]
        artist = credit["artist"]
        join = None
        if position < len(indexes) - 1:
            adjacent = indexes[position + 1] == index + 1
            join = (credit.get("joinphrase") or "; ") if adjacent else "; "
        entries.append(
            {
                "id": artist["id"],
                "name": artist["name"],
                "credit": credit.get("name") or artist["name"],
                "sort": artist.get("sort-name") or artist["name"],
                "join": join,
            }
        )
    return entries


def _rel_entries(entity, target, reltype):
    """Entries for the targets of relationships of one type, deduplicated.
    Works for artists, places and areas (places have no sort name; the
    plain name is used then)."""
    entries, seen = [], set()
    for rel in _work_rels(entity, reltype, target=target):
        node = rel.get(target)
        if not node or node["id"] in seen:
            continue
        seen.add(node["id"])
        entries.append(
            {
                "id": node["id"],
                "name": node["name"],
                "credit": rel.get("target-credit") or node["name"],
                "sort": node.get("sort-name") or node["name"],
                "join": "; ",
            }
        )
    return entries


def _location_entries(recording, area_fallback):
    """'recorded at' places, optionally falling back to 'recorded in'
    areas."""
    entries = _rel_entries(recording, "place", "recorded at")
    if not entries and area_fallback:
        entries = _rel_entries(recording, "area", "recorded in")
    return entries


_ROLES = ("composer", "conductor", "orchestra")


def _adjust_credit(setting, section, credits, roles):
    """Apply the section's per-role keep/remove/add rules to the release
    artist credit.

    roles maps each role to {"ids": artist ids used for removal, "entries":
    entries used for addition}.
    """
    remove_ids, additions = set(), []
    present = {credit["artist"]["id"] for credit in credits}
    for role in _ROLES:
        action = setting["%s_role_%s" % (section, role)]
        role_data = roles.get(role) or {}
        if action == "remove":
            remove_ids |= role_data.get("ids") or set()
        elif action == "add":
            for entry in role_data.get("entries") or []:
                if entry["id"] not in present:
                    present.add(entry["id"])
                    additions.append(entry)
    indexes = [
        i
        for i, credit in enumerate(credits)
        if credit["artist"]["id"] not in remove_ids
    ]
    return _credit_entries(credits, indexes) + additions


def _join_entries(entries, field):
    parts = []
    for position, entry in enumerate(entries):
        parts.append(entry[field])
        if position < len(entries) - 1:
            parts.append(entry.get("join") or "; ")
    return "".join(parts)


def _people_values(setting, key, entries):
    """The (canonical, credited, sort) values a people section produces."""
    if setting["%s_split" % key]:
        return (
            [e["name"] for e in entries],
            [e["credit"] for e in entries],
            [e["sort"] for e in entries],
        )
    return (
        _join_entries(entries, "name"),
        _join_entries(entries, "credit"),
        _join_entries(entries, "sort"),
    )


def _apply_people(setting, metadata, key, entries):
    if not setting["%s_enabled" % key] or not entries:
        return
    canonical, credited, sort_names = _people_values(setting, key, entries)
    policy = setting["%s_write_policy" % key]
    _write_tags(metadata, setting["%s_canonical" % key], canonical, policy)
    _write_tags(metadata, setting["%s_credited" % key], credited, policy)
    _write_tags(metadata, setting["%s_sort" % key], sort_names, policy)


# ---------------------------------------------------------------------------
# Per-album caches
# ---------------------------------------------------------------------------

# Keyed weakly by Album so entries disappear with the album and nothing is
# stored on Picard's own objects.
_ALBUM_CACHES = WeakKeyDictionary()


def _album_cache(album):
    cache = _ALBUM_CACHES.get(album)
    if cache is None:
        cache = {"works": {}, "roles": None, "groups": None, "locations": None}
        _ALBUM_CACHES[album] = cache
    return cache


def _collect_release_roles(release):
    """Release-wide role data ({role: {"ids", "entries"}}): conductors and
    orchestras from all recordings, composers from all performed works."""
    entries = {role: [] for role in _ROLES}
    seen = {role: set() for role in _ROLES}

    def collect(role, found):
        for entry in found:
            if entry["id"] not in seen[role]:
                seen[role].add(entry["id"])
                entries[role].append(entry)

    for medium in release.get("media") or []:
        for track in medium.get("tracks") or []:
            recording = track.get("recording") or {}
            collect("conductor", _rel_entries(recording, "artist", "conductor"))
            collect(
                "orchestra", _rel_entries(recording, "artist", "performing orchestra")
            )
    for work in _iter_release_works(release):
        collect("composer", _rel_entries(work, "artist", "composer"))
    return {role: {"ids": seen[role], "entries": entries[role]} for role in _ROLES}


def _release_roles(album, release):
    """_collect_release_roles, computed once per album."""
    cache = _album_cache(album)
    if cache["roles"] is None:
        cache["roles"] = _collect_release_roles(release)
    return cache["roles"]


def _track_roles(release_roles, recording, work):
    """Role data for one track's artist rules: removal matches release-wide
    role members, additions use this track's own conductor/orchestra and
    the performed work's composers."""
    return {
        "composer": {
            "ids": release_roles["composer"]["ids"],
            "entries": (_rel_entries(work, "artist", "composer") if work else []),
        },
        "conductor": {
            "ids": release_roles["conductor"]["ids"],
            "entries": _rel_entries(recording, "artist", "conductor"),
        },
        "orchestra": {
            "ids": release_roles["orchestra"]["ids"],
            "entries": _rel_entries(recording, "artist", "performing orchestra"),
        },
    }


def _collect_movement_groups(release):
    """Map track id -> (movementnumber, movementtotal).

    Tracks on the same medium that belong to the same parent work form one
    group; each track is one 'movement'.  This intentionally counts tracks
    rather than using the work's ordering key, so a movement split over two
    tracks (a partial performance) yields two movements.
    """
    groups = {}
    for medium in release.get("media") or []:
        members = {}  # group id -> [track ids in order]
        for track in sorted(
            medium.get("tracks") or [], key=lambda t: t.get("position") or 0
        ):
            rel = _pick_performance(track.get("recording") or {})
            if not rel:
                continue
            parent = _parent_rel(rel["work"])
            group = parent["work"]["id"] if parent else rel["work"]["id"]
            members.setdefault(group, []).append(track["id"])
        for track_ids in members.values():
            for number, track_id in enumerate(track_ids, 1):
                groups[track_id] = (number, len(track_ids))
    return groups


def _movement_groups(album, release):
    """_collect_movement_groups, computed once per album."""
    cache = _album_cache(album)
    if cache["groups"] is None:
        cache["groups"] = _collect_movement_groups(release)
    return cache["groups"]


# ---------------------------------------------------------------------------
# Asynchronous work lookups (key attribute and hierarchy climbing)
# ---------------------------------------------------------------------------

_WORK_INC = "artist-rels+work-rels"
# The hierarchy is climbed to its top, so the usable levels (%L1%, %L2%, …)
# follow the data.  This is only a backstop against runaway request chains
# from absurdly deep hierarchies; cycles are caught separately.
_MAX_DEPTH = 32

# Levels announced to the script completer.  Every level up to _MAX_DEPTH is
# exported, but hierarchies deeper than a handful are rare and listing all of
# them would bury both the completer and the scripting documentation under
# near-identical entries.  The ones listed say so in their documentation.
_SCRIPT_VAR_LEVELS = 6


def _complete_album_task(api, album, task_id):
    """complete_album_task plus the finalization check Picard skips.

    As of Picard 3.0.0b7, completing a plugin task only removes it from
    the album's pending list; nothing re-checks whether the album can now
    finish loading.  Since this plugin's blocking tasks are created while
    the album is being finalized, the album would otherwise stay in
    'loading' state forever once the last task completes.
    """
    api.complete_album_task(album, task_id)
    if not getattr(album, "loaded", True) and not album.has_critical_tasks():
        album._finalize_loading(error=False)


def _fetch_work(api, album, work_id, callback):
    """Fetch a work (once per album), then call callback(work_json_or_None).

    A blocking album task holds the album open until the response arrives, so
    the tags are in place before the album finishes loading; creating the
    request through the task's request_factory lets Picard abort it on album
    removal or timeout.
    """
    works = _album_cache(album)["works"]
    entry = works.get(work_id)
    if entry is None:
        works[work_id] = {"callbacks": [callback]}
        task_id = "work_" + work_id
        handler = partial(_work_response, api, album, work_id, task_id)
        api.add_album_task(
            album,
            task_id,
            "Loading work data",
            blocking=True,
            timeout=25,
            request_factory=lambda: api.mb_api.get(
                "/work/" + work_id,
                handler,
                unencoded_queryargs={"inc": _WORK_INC},
                priority=True,
                important=True,
            ),
        )
    elif "data" in entry:
        callback(entry["data"])
    else:
        entry["callbacks"].append(callback)


def _work_response(api, album, work_id, task_id, document, reply, error):
    try:
        if error:
            api.logger.warning("error loading work %s", work_id)
            document = None
        entry = _album_cache(album)["works"][work_id]
        entry["data"] = document
        for callback in entry.pop("callbacks", []):
            callback(document)
    finally:
        _complete_album_task(api, album, task_id)


_PLACE_INC = "place-rels+area-rels"
_BROWSE_LIMIT = 100


def _fetch_locations(api, album, release_id, callback):
    """Fetch the recording locations of a release (once per album), then call
    callback({recording id: entries}).

    Picard's own release request does not include place/area relationships,
    so they are browsed separately: one request per 100 recordings.
    """
    cache = _album_cache(album)
    entry = cache["locations"]
    if entry is None:
        cache["locations"] = {"callbacks": [callback]}
        _request_locations(api, album, release_id, 0, {})
    elif "data" in entry:
        callback(entry["data"])
    else:
        entry["callbacks"].append(callback)


def _request_locations(api, album, release_id, offset, collected):
    task_id = "locations_%d" % offset
    handler = partial(
        _locations_response, api, album, release_id, offset, collected, task_id
    )
    api.add_album_task(
        album,
        task_id,
        "Loading recording locations",
        blocking=True,
        timeout=25,
        request_factory=lambda: api.mb_api.get(
            "/recording",
            handler,
            unencoded_queryargs={
                "release": release_id,
                "inc": _PLACE_INC,
                "limit": str(_BROWSE_LIMIT),
                "offset": str(offset),
            },
            priority=True,
            important=True,
        ),
    )


def _locations_response(
    api, album, release_id, offset, collected, task_id, document, reply, error
):
    try:
        recordings, total = [], 0
        if error:
            api.logger.warning(
                "error loading recording locations for release %s", release_id
            )
        else:
            recordings = (document or {}).get("recordings") or []
            total = (document or {}).get("recording-count") or 0
        for recording in recordings:
            places = _rel_entries(recording, "place", "recorded at")
            areas = _rel_entries(recording, "area", "recorded in")
            dates = _recording_date_span(recording)
            if places or areas or dates != (None, None):
                collected[recording["id"]] = {
                    "place": places,
                    "area": areas,
                    "dates": dates,
                }
        next_offset = offset + len(recordings)
        if not error and recordings and next_offset < total:
            _request_locations(api, album, release_id, next_offset, collected)
        else:
            entry = _album_cache(album)["locations"]
            entry["data"] = collected
            for callback in entry.pop("callbacks", []):
                callback(collected)
    finally:
        _complete_album_task(api, album, task_id)


def _apply_location(setting, metadata, recording_id, locations):
    found = locations.get(recording_id) or {}
    entries = found.get("place") or []
    if not entries and setting["location_area_fallback"]:
        entries = found.get("area") or []
    _apply_people(setting, metadata, "location", entries)


def _next_parent_id(document, chain):
    """The work to climb to after document, or None at the end of the climb.

    The climb ends at a work without a parent, at a work already in the
    chain ('part of' cycles do occur in the data) or at the depth backstop.
    document is expected to be the last entry of chain."""
    if len(chain) >= _MAX_DEPTH:
        return None
    parent = _parent_rel(document)
    if not parent:
        return None
    parent_id = parent["work"]["id"]
    if any(doc.get("id") == parent_id for doc in chain):
        return None
    return parent_id


def _resolve_chain(api, album, work_id, callback, _chain=None):
    """Fetch work_id and climb 'part of' parents to the top, then call
    callback(chain); chain[0] is work_id's data, chain[-1] the topmost
    fetched work."""
    chain = _chain if _chain is not None else []

    def _done(document):
        if document is None:
            callback(chain)
            return
        chain.append(document)
        parent_id = _next_parent_id(document, chain)
        if parent_id:
            _resolve_chain(api, album, parent_id, callback, chain)
        else:
            callback(chain)

    _fetch_work(api, album, work_id, _done)


def _levels_from_docs(docs):
    """Bottom-up relative titles: each level stripped of its parent's title,
    the topmost level keeping its full title."""
    levels = []
    for position, doc in enumerate(docs):
        title = doc.get("title") or ""
        if position + 1 < len(docs):
            title = _strip_parent_title(title, docs[position + 1].get("title") or "")
        levels.append(title)
    return levels


# ---------------------------------------------------------------------------
# Preview (options page)
# ---------------------------------------------------------------------------

# Same relationship data Picard requests for an album, plus the place/area
# rels the plugin otherwise browses separately and the genres/tags for the
# classical detection preview.
_PREVIEW_INC = (
    "artist-credits+recordings+artist-rels+work-rels"
    "+recording-level-rels+work-level-rels+place-rels+area-rels"
    "+release-groups+genres+tags"
)

_MBID_RE = re.compile(
    r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", re.I
)


def _preview_sections(
    setting, release, track, get_work, use_genres=True, unavailable=()
):
    """Values every section would produce for one track, computed with the
    given (possibly unsaved) option values.

    get_work(work_id) -> (known, doc): known is False while the work is not
    fetched yet.  Returns (sections, pending) where pending lists work ids
    still needed to finish the hierarchy.

    sections: people keys map to {"canonical", "credited", "sort"} (None
    for no data), "work" maps to the _work_values dict (or "pending"/None)
    and "recordingdate" to the date string.
    """
    recording = track.get("recording") or {}
    performance = _pick_performance(recording)
    work = performance["work"] if performance else None
    credits = release.get("artist-credit") or []
    release_roles = _collect_release_roles(release)
    roles = _track_roles(release_roles, recording, work)

    signals = _classical_signals(
        release, release_roles, _genre_keywords(setting), with_genres=use_genres
    )
    if setting["classical_scope"] == "track":
        # the preview shows one track, so under per-track scope it has to
        # show that track's own signals rather than the release's
        signals = _track_signals(
            track, _credit_artist_ids(release), signals["genre"], False
        )
    # a rule naming a signal Picard is not fetching cannot be judged; with
    # none left to judge, a real run makes no verdict at all
    rules = _classical_rules(setting)
    usable = [rule for rule in rules if not (set(unavailable) & set(rule))]
    judged = not (rules and not usable)
    sections: dict[str, Any] = {
        "classical": {
            "signals": signals,
            "rule": _match_classical_rule(usable, signals) if judged else None,
            "judged": judged,
        },
        "title": {
            "canonical": recording.get("title") or "",
            "credited": track.get("title") or recording.get("title") or "",
        },
    }

    def people(key, entries):
        if not entries:
            sections[key] = None
            return
        canonical, credited, sort_names = _people_values(setting, key, entries)
        sections[key] = {
            "canonical": canonical,
            "credited": credited,
            "sort": sort_names,
        }

    people("artist", _adjust_credit(setting, "artist", credits, roles))
    people(
        "albumartist", _adjust_credit(setting, "albumartist", credits, release_roles)
    )
    people("artists", _credit_entries(credits, list(range(len(credits)))))
    people("composer", _rel_entries(work, "artist", "composer") if work else [])
    people("conductor", _rel_entries(recording, "artist", "conductor"))
    people("orchestra", _rel_entries(recording, "artist", "performing orchestra"))
    people("location", _location_entries(recording, setting["location_area_fallback"]))

    pending = []
    sections["work"] = None
    begin, end = _best_span(
        _performance_span(performance), _recording_date_span(recording)
    )
    sections["recordingdate"] = _date_value(setting, begin, end)
    if performance and work:
        parent = _parent_rel(work)
        chain, missing = [], None
        work_id = parent["work"]["id"] if parent else work["id"]
        while work_id:
            known, doc = get_work(work_id)
            if not known:
                missing = work_id
                break
            if doc is None:
                break
            chain.append(doc)
            work_id = _next_parent_id(doc, chain)
        if missing:
            pending.append(missing)
            sections["work"] = "pending"
        else:
            numbering = _collect_movement_groups(release).get(track.get("id"))
            sections["work"] = _work_values(
                setting, work, performance, parent, numbering, chain
            )
    return sections, pending


# ---------------------------------------------------------------------------
# Classical-or-not detection
# ---------------------------------------------------------------------------


def _genre_keywords(setting):
    return {
        part.strip().lower()
        for part in (setting["classical_genres"] or "").split(";")
        if part.strip()
    }


def _genre_names(release):
    """Lowercased genre and folksonomy tag names of the release and its
    release group.  Picard only requests them when 'Use genres from
    MusicBrainz' is enabled; without it the sets are simply empty."""
    names = set()
    for node in (release, release.get("release-group") or {}):
        for key in ("genres", "tags", "user-genres", "user-tags"):
            for item in node.get(key) or []:
                name = (item.get("name") or "").strip().lower()
                if name:
                    names.add(name)
    return names


def _carries_tag(metadata, tags, wanted):
    """True if metadata holds one of the tags, at the wanted value if given
    (an empty value matches any non-empty one)."""
    for tag in tags:
        for item in metadata.getall(tag):
            item = (item or "").strip()
            if item and (not wanted or item.lower() == wanted):
                return True
    return False


# The ids Picard writes for the track and the recording; a file that has
# been tagged before names the track it belongs to through them.
_FILE_ID_TAGS = ("musicbrainz_trackid", "musicbrainz_recordingid")


def _tagged_files(album, taglist, value):
    """Which files heading into this album already carry the marker tag.

    Returns (ids, unplaceable): the MusicBrainz track and recording ids
    those files name in their own tags, and whether any of them named none
    at all.  Picard matches files to tracks only after every metadata
    processor has run, so its own matching cannot be used here; going by
    the ids the files already carry places them without it.  A marker file
    without ids cannot be placed and counts for the whole release instead.

    orig_metadata is what the file holds on disk, not what this session has
    already written to it.
    """
    tags = _parse_taglist(taglist)
    iterfiles = getattr(album, "iterfiles", None)
    if not tags or iterfiles is None:
        return set(), False
    wanted = (value or "").strip().lower()
    ids, unplaceable = set(), False
    for file in iterfiles():
        metadata = getattr(file, "orig_metadata", None)
        if metadata is None or not _carries_tag(metadata, tags, wanted):
            continue
        found = {
            item.strip()
            for tag in _FILE_ID_TAGS
            for item in metadata.getall(tag)
            if (item or "").strip()
        }
        if found:
            ids |= found
        else:
            unplaceable = True
    return ids, unplaceable


def _unavailable_signals(global_setting):
    """Signals whose source data Picard was not asked to fetch.

    Both switches live in Picard's own Options > Metadata, and without them
    the data never reaches the plugin, so the signals that read it say
    nothing about the release either way.
    """
    unavailable = set()
    if not global_setting["track_ars"]:
        unavailable |= _RELATIONSHIP_SIGNALS
    if not global_setting["use_genres"]:
        unavailable.add("genre")
    return frozenset(unavailable)


def _credit_artist_ids(release):
    return {credit["artist"]["id"] for credit in release.get("artist-credit") or []}


def _track_signals(track, credit_ids, genre, tagged):
    """The signals judged from one track's own recording and work.

    'genre' is release-level (MusicBrainz has no per-track genres) and so is
    passed down unchanged; 'tagged' is decided per track by the caller.
    """
    recording = track.get("recording") or {}
    performance = _pick_performance(recording)
    work = performance["work"] if performance else None
    composer_ids = (
        {entry["id"] for entry in _rel_entries(work, "artist", "composer")}
        if work
        else set()
    )
    return {
        "composer": bool(composer_ids),
        "conductor_orchestra": bool(
            _rel_entries(recording, "artist", "conductor")
            or _rel_entries(recording, "artist", "performing orchestra")
        ),
        "composer_in_credit": bool(credit_ids & composer_ids),
        "genre": genre,
        "multi_movement": bool(work and _parent_rel(work)),
        "tagged": tagged,
    }


def _release_track_signals(release, genre, tagged_ids, unplaceable):
    """{track id: signals}, each track judged on its own."""
    credit_ids = _credit_artist_ids(release)
    result = {}
    for medium in release.get("media") or []:
        for track in medium.get("tracks") or []:
            recording = track.get("recording") or {}
            own = {track.get("id"), recording.get("id")} - {None}
            result[track.get("id")] = _track_signals(
                track, credit_ids, genre, bool(own & tagged_ids) or unplaceable
            )
    return result


def _classical_signals(release, roles, keywords, with_genres=True, tagged=False):
    """The per-release detection signals, all computed from the release
    document (no extra requests).

    'tagged' is the exception: it comes from the files being matched rather
    than from MusicBrainz, so it is passed in - the options preview has no
    files to look at and leaves it False.
    """
    credit_ids = _credit_artist_ids(release)
    return {
        "composer": bool(roles["composer"]["ids"]),
        "conductor_orchestra": bool(
            roles["conductor"]["ids"] or roles["orchestra"]["ids"]
        ),
        "composer_in_credit": bool(credit_ids & roles["composer"]["ids"]),
        "genre": bool(keywords & _genre_names(release)) if with_genres else False,
        "multi_movement": any(
            _parent_rel(work) for work in _iter_release_works(release)
        ),
        "tagged": tagged,
    }


def _classical_rules(setting, logger=None):
    """The rule rows as a list of {signal: 'require'|'exclude'} dicts;
    rows without any constraint are dropped (they would match anything)."""
    try:
        rows = json.loads(setting["classical_rules"] or "[]")
    except ValueError:
        rows = None
    if not isinstance(rows, list):
        if logger is not None:
            logger.warning("invalid classical detection rules; using the defaults")
        rows = json.loads(_DEFAULT_CLASSICAL_RULES)
    rules = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        rule = {
            signal: mode
            for signal, mode in row.items()
            if signal in _CLASSICAL_SIGNALS and mode in ("require", "exclude")
        }
        if rule:
            rules.append(rule)
    return rules


def _match_classical_rule(rules, signals):
    """Index of the first matching rule, or None."""
    for index, rule in enumerate(rules):
        if all(
            signals.get(signal, False) == (mode == "require")
            for signal, mode in rule.items()
        ):
            return index
    return None


# ---------------------------------------------------------------------------
# Album coordinator: defer every write until all data is fetched
# ---------------------------------------------------------------------------

# All tag writing is deferred: each track (and the album itself) registers a
# finisher once its asynchronous data (work chains, locations) has arrived.
# When the last track is ready the classical verdict is decided once per
# release and every finisher runs with it.  The async fetches hold blocking
# album tasks, so all writes still land before the album finishes loading.


def _start_album(setting, album, release_node, unavailable=()):
    """Reset the per-load coordinator (track count, signals, finishers).

    A disabled section detects nothing at all: the signals are left unread,
    which leaves no verdict to gate with, to export or to persist.  Reading
    them here rather than at decide time also keeps one album load
    consistent if the option is toggled while it runs.

    Per-track scope judges every track of the release on its own, computed
    here because the release document holds them all; the release-wide
    signals stay alongside for the album's own decisions.
    """
    expected = 0
    for medium in release_node.get("media") or []:
        expected += len(medium.get("tracks") or [])
        expected += len(medium.get("data-tracks") or [])
        if medium.get("pregap"):
            expected += 1
    signals = track_signals = None
    if setting["classical_enabled"]:
        tagged_ids, unplaceable = _tagged_files(
            album,
            setting["classical_detect_tag"],
            setting["classical_detect_value"],
        )
        signals = _classical_signals(
            release_node,
            _release_roles(album, release_node),
            _genre_keywords(setting),
            tagged=bool(tagged_ids) or unplaceable,
        )
        if setting["classical_scope"] == "track":
            track_signals = _release_track_signals(
                release_node, signals["genre"], tagged_ids, unplaceable
            )
    coord = {
        "expected": expected,
        "done": 0,
        "signals": signals,
        "track_signals": track_signals,
        "unavailable": frozenset(unavailable),
        "finishers": [],
        "decided": None,
        "track_decided": None,
    }
    _album_cache(album)["coord"] = coord
    return coord


def _decide_album(setting, coord, logger=None):
    """Decide the verdicts once, then release every finisher.

    A verdict of None means nothing was judged, so nothing is gated and
    neither the script variables nor the marker tag are written.  That
    covers a disabled section and, just as importantly, a ruleset that
    cannot be evaluated because Picard never fetched the data it names:
    calling such a release 'not classical' would gate all tagging away on
    evidence the plugin never had.
    """
    rules = _classical_rules(setting, logger) if coord["signals"] is not None else []
    usable = [rule for rule in rules if not (coord["unavailable"] & set(rule))]
    if coord["signals"] is None or (rules and not usable):
        if rules and not usable and logger is not None:
            logger.warning(
                "classical detection: every rule needs a signal Picard did "
                "not fetch (%s); no verdict is made and every release is "
                "tagged",
                ", ".join(sorted(coord["unavailable"])),
            )
        coord["decided"] = (True, None)
    else:
        coord["decided"] = _decision(usable, coord["signals"])
        if coord["track_signals"] is not None:
            coord["track_decided"] = {
                track_id: _decision(usable, signals)
                for track_id, signals in coord["track_signals"].items()
            }
    for finisher, track_id in coord["finishers"]:
        finisher(*_decision_for(coord, track_id))
    coord["finishers"] = []


def _decision(rules, signals):
    """(write, verdict) for one set of signals; the gate follows the verdict."""
    verdict = _match_classical_rule(rules, signals) is not None
    return (verdict, verdict)


def _decision_for(coord, track_id):
    """The decision that applies to one track, or the release's own."""
    per_track = coord["track_decided"]
    if per_track is not None and track_id in per_track:
        return per_track[track_id]
    return coord["decided"]


def _signals_for(coord, track_id):
    """The signals behind that decision, for the script variables."""
    per_track = coord["track_signals"]
    if per_track is not None and track_id in per_track:
        return per_track[track_id]
    return coord["signals"]


def _when_decided(
    setting, coord, finisher, track_done=False, logger=None, track_id=None
):
    """Run finisher(write, verdict) once the verdict that applies is known.

    track_id picks the track's own verdict under per-track scope; the
    release's is used for the album itself and whenever there is none.
    Without a coordinator (no release node) tags are written
    unconditionally and no verdict is available."""
    if coord is None:
        finisher(True, None)
        return
    if track_done:
        coord["done"] += 1
    if coord["decided"] is not None:
        finisher(*_decision_for(coord, track_id))
        return
    coord["finishers"].append((finisher, track_id))
    if coord["done"] >= coord["expected"]:
        _decide_album(setting, coord, logger)


def _write_verdict_vars(metadata, signals, verdict):
    metadata["~sc_classical"] = "1" if verdict else "0"
    for name, value in signals.items():
        metadata["~sc_sig_" + name] = "1" if value else "0"


def _write_classical_tag(setting, metadata, verdict):
    """Keep the marker tag in step with the verdict, so a later run can read
    it back as the 'tagged' signal.

    Written whatever the verdict decided about the other tags: a release
    ruled out is exactly when the negative marker is worth having.  No value
    configured for the verdict at hand means the tag should not be on the
    file, and that has to be an explicit delete - unless 'Clear existing
    tags' is on, Picard merges the tags it writes into the ones the file
    already has, so a marker nobody writes any more would survive on disk
    and keep confirming itself.
    """
    targets = _parse_taglist(setting["tag_classical"])
    if not targets:
        return
    value = (
        setting["classical_value"] if verdict else setting["classical_negative_value"]
    )
    for tag in targets:
        if value:
            metadata[tag] = value
        else:
            del metadata[tag]


# ---------------------------------------------------------------------------
# Metadata processors
# ---------------------------------------------------------------------------


def process_album(api, album, metadata, release_node):
    setting = api.plugin_config
    if not api.global_config.setting["track_ars"]:
        api.logger.warning(
            "'Use track and release relationships' is disabled in "
            "Options > Metadata; work and movement tags cannot be created, "
            "and classical detection has no relationship data to judge from."
        )
    if release_node is None:
        return
    coord = _start_album(
        setting,
        album,
        release_node,
        _unavailable_signals(api.global_config.setting),
    )
    credits = release_node.get("artist-credit") or []
    entries = _adjust_credit(
        setting, "albumartist", credits, _release_roles(album, release_node)
    )

    def finisher(write, verdict):
        if verdict is not None:
            _write_verdict_vars(metadata, coord["signals"], verdict)
        if write:
            _apply_people(setting, metadata, "albumartist", entries)

    _when_decided(setting, coord, finisher, logger=api.logger)


def process_track(api, track, metadata, track_node, release_node=None):
    setting = api.plugin_config
    album = track.album
    recording = track_node.get("recording") or track_node
    coord = _album_cache(album).get("coord") if release_node is not None else None

    performance = _pick_performance(recording)
    work = performance["work"] if performance else None
    parent = _parent_rel(work) if work else None

    # -- everything synchronous, gathered up front ----------------------
    title_canonical = recording.get("title") or ""
    title_credited = track_node.get("title") or title_canonical

    artist_entries = albumartist_entries = artists_entries = None
    numbering = None
    if release_node is not None:
        credits = release_node.get("artist-credit") or []
        release_roles = _release_roles(album, release_node)
        roles = _track_roles(release_roles, recording, work)
        artist_entries = _adjust_credit(setting, "artist", credits, roles)
        # tracks copy the album metadata before the deferred album write
        # happens, so the album artist is applied per track as well
        albumartist_entries = _adjust_credit(
            setting, "albumartist", credits, release_roles
        )
        artists_entries = _credit_entries(credits, list(range(len(credits))))
        numbering = _movement_groups(album, release_node).get(track_node.get("id"))

    conductor_entries = _rel_entries(recording, "artist", "conductor")
    orchestra_entries = _rel_entries(recording, "artist", "performing orchestra")
    composer_entries = _rel_entries(work, "artist", "composer") if work else []
    span = _performance_span(performance)

    # -- asynchronous needs ----------------------------------------------
    # release id for the locations browse; None when browsing is not possible
    browse_id = None
    if release_node is not None and recording.get("id"):
        browse_id = release_node.get("id")
    # a less-than-day-precise performance span may be beaten by dates on
    # the 'recorded at'/'recorded in' relationships, which only the
    # locations browse carries
    need_locations = bool(browse_id) and (
        setting["location_enabled"]
        or (setting["recdate_enabled"] and _span_score(*span) < _FULL_SPAN_SCORE)
    )
    need_chain = work is not None and (
        setting["work_enabled"] or setting["key_enabled"] or setting["workyear_enabled"]
    )

    results = {"locations": None, "chain": None}

    def finisher(write, verdict):
        if verdict is not None:
            _write_verdict_vars(
                metadata, _signals_for(coord, track_node.get("id")), verdict
            )
            _write_classical_tag(setting, metadata, verdict)
        values = None
        if results["chain"] is not None:
            values = _work_values(
                setting,
                work,
                performance,
                parent,
                numbering,
                results["chain"],
                api.logger,
            )
            # the hierarchy script variables are exported regardless of the
            # verdict, so gating scripts can still see the work data
            _write_hierarchy_vars(metadata, values)
        if not write:
            return
        if setting["title_enabled"]:
            policy = setting["title_write_policy"]
            if title_canonical:
                _write_tags(
                    metadata, setting["title_canonical"], title_canonical, policy
                )
            if title_credited:
                _write_tags(metadata, setting["title_credited"], title_credited, policy)
        _apply_people(setting, metadata, "artist", artist_entries or [])
        _apply_people(setting, metadata, "albumartist", albumartist_entries or [])
        _apply_people(setting, metadata, "artists", artists_entries or [])
        _apply_people(setting, metadata, "conductor", conductor_entries)
        _apply_people(setting, metadata, "orchestra", orchestra_entries)
        _apply_people(setting, metadata, "composer", composer_entries)
        if results["locations"] is not None:
            _apply_location(setting, metadata, recording["id"], results["locations"])
        if setting["recdate_enabled"]:
            if results["locations"] is not None:
                _apply_recording_date_browsed(
                    setting, metadata, recording["id"], span, results["locations"]
                )
            else:
                _write_recording_date(setting, metadata, _date_value(setting, *span))
        if values is not None:
            _write_work_tags(setting, metadata, values, numbering)

    # one guard step so the finisher cannot fire before all fetches are
    # even requested; each async need adds one more step
    pending = {"count": 1}

    def step_done(*_args):
        pending["count"] -= 1
        if pending["count"] == 0:
            _when_decided(
                setting,
                coord,
                finisher,
                track_done=True,
                logger=api.logger,
                track_id=track_node.get("id"),
            )

    if need_locations:
        pending["count"] += 1

        def on_locations(locations):
            results["locations"] = locations
            step_done()

        _fetch_locations(api, album, browse_id, on_locations)

    if work is not None and need_chain:
        pending["count"] += 1
        start_id = parent["work"]["id"] if parent else work["id"]

        def on_chain(chain):
            results["chain"] = chain
            step_done()

        _resolve_chain(api, album, start_id, on_chain)

    step_done()


def _work_values(setting, work, performance, parent, numbering, chain, logger=None):
    """Everything derived from the fetched work hierarchy: levels, partial
    flag, key, work_year (with suffix), and the rendered movement (with
    suffix)/grouping/work template values."""
    if parent:
        if chain:
            levels = [
                _strip_parent_title(
                    work.get("title") or "", chain[0].get("title") or ""
                )
            ] + _levels_from_docs(chain)
        else:  # lookup failed: fall back to the parent title we already have
            parent_title = parent["work"].get("title") or ""
            levels = [
                _strip_parent_title(work.get("title") or "", parent_title),
                parent_title,
            ]
    else:
        levels = _levels_from_docs(chain) if chain else [work.get("title") or ""]
    depth = len(levels)
    partial_performance = "partial" in (performance.get("attributes") or [])

    key = None
    for doc in [work] + chain:
        key = _key_attribute(doc)
        if key:
            break

    span = _year_span(list(_composer_rels(work)))
    if not span:
        for doc in chain:
            span = _year_span(list(_composer_rels(doc)))
            if span:
                break

    overrides = _depth_overrides(setting, logger).get(depth) or {}

    def template(field):
        return overrides.get(field) or setting["tpl_" + field]

    # a standalone work (depth 1) has no movement unless an override says so
    movement_template = (
        template("movement") if depth >= 2 else overrides.get("movement") or ""
    )
    movement_value = _render_template(movement_template, levels)
    if movement_value and partial_performance:
        movement_value += setting["part_suffix"]

    return {
        "levels": levels,
        "partial": partial_performance,
        "key": key,
        "work_year": span + setting["composed_suffix"] if span else None,
        "movement": movement_value,
        "grouping": _render_template(template("grouping"), levels),
        "work": _render_template(template("work"), levels),
        "numbering": numbering,
    }


def _write_hierarchy_vars(metadata, values):
    """The hierarchy variables for Picard scripting."""
    levels = values["levels"]
    metadata["~sc_depth"] = str(len(levels))
    metadata["~sc_top"] = levels[-1]
    metadata["~sc_partial"] = "1" if values["partial"] else "0"
    for position, value in enumerate(levels, 1):
        metadata["~sc_l%d" % position] = value


def _write_work_tags(setting, metadata, values, numbering):
    """The tags derived from the fetched work hierarchy."""
    if setting["key_enabled"] and values["key"]:
        _write_tags(
            metadata, setting["tag_key"], values["key"], setting["key_write_policy"]
        )
    if setting["workyear_enabled"] and values["work_year"]:
        _write_tags(
            metadata,
            setting["tag_work_year"],
            values["work_year"],
            setting["workyear_write_policy"],
        )

    if not setting["work_enabled"]:
        return
    policy = setting["work_write_policy"]
    if values["work"]:
        _write_tags(metadata, setting["tag_work"], values["work"], policy)
    if values["grouping"]:
        _write_tags(metadata, setting["tag_grouping"], values["grouping"], policy)
    if values["movement"]:
        _write_tags(metadata, setting["tag_movement"], values["movement"], policy)
        _write_tags(metadata, setting["tag_showmovement"], "1", policy)
        if numbering:
            _write_tags(
                metadata, setting["tag_movementnumber"], str(numbering[0]), policy
            )
            _write_tags(
                metadata, setting["tag_movementtotal"], str(numbering[1]), policy
            )


def _date_value(setting, begin, end):
    mode = setting["recording_date_mode"]
    if mode == "begin":
        return begin or end
    if mode == "range" and begin and end and begin != end:
        return "%s - %s" % (begin, end)
    return end or begin  # "end"


def _span_score(begin, end):
    """Precision/completeness of a date span: day-precise endpoints score
    higher than month- or year-only ones, and two endpoints beat one."""
    return len(begin or "") + len(end or "")


# both endpoints day-precise ('YYYY-MM-DD'): no other span can score higher
_FULL_SPAN_SCORE = 20


def _performance_span(performance):
    if not performance:
        return (None, None)
    return (performance.get("begin"), performance.get("end"))


def _best_span(performance_span, place_span):
    """The more precise of the two spans; the performance relationship (the
    canonical recording date) wins ties."""
    if _span_score(*place_span) > _span_score(*performance_span):
        return place_span
    return performance_span


def _recording_date_span(recording):
    """(begin, end) from the recording's 'recorded at'/'recorded in'
    relationship dates.

    Session dates are often entered on the place relationship rather than
    the performance relationship.  Several dated relationships span from
    the earliest begin to the latest end."""
    for target, reltype in (("place", "recorded at"), ("area", "recorded in")):
        rels = [
            rel
            for rel in _work_rels(recording, reltype, target=target)
            if rel.get("begin") or rel.get("end")
        ]
        if rels:
            begins = sorted(rel["begin"] for rel in rels if rel.get("begin"))
            ends = sorted(rel["end"] for rel in rels if rel.get("end"))
            return (begins[0] if begins else None, ends[-1] if ends else None)
    return (None, None)


def _write_recording_date(setting, metadata, value):
    if value:
        _write_tags(
            metadata,
            setting["tag_recordingdate"],
            value,
            setting["recdate_write_policy"],
        )


def _apply_recording_date_browsed(setting, metadata, recording_id, span, locations):
    place_span = (locations.get(recording_id) or {}).get("dates") or (None, None)
    begin, end = _best_span(span, place_span)
    _write_recording_date(setting, metadata, _date_value(setting, begin, end))


# ---------------------------------------------------------------------------
# Options page
# ---------------------------------------------------------------------------

_PEOPLE_UI = [
    (
        "title",
        "Title",
        "Canonical = the recording's title, credited = "
        "the track title as printed on this release.",
        False,
        False,
        False,
    ),
    (
        "artist",
        "Artist",
        "The release credit, adjusted by the role rules "
        "below (default: composers removed).",
        True,
        True,
        True,
    ),
    ("albumartist", "Album artist", None, True, True, True),
    (
        "artists",
        "Artists",
        "The full release credit, including the composer.",
        True,
        True,
        False,
    ),
    (
        "composer",
        "Composer",
        "From the composer relationship of the "
        "performed work, written to the standard composer/composersort tags. "
        "Existing values are replaced by default; choose another policy to "
        "preserve them.",
        True,
        True,
        False,
    ),
    (
        "conductor",
        "Conductor",
        "Written to the standard 'conductor' tag. "
        "Existing values are replaced by default; choose another policy to "
        "preserve them.",
        True,
        True,
        False,
    ),
    (
        "orchestra",
        "Orchestra",
        "From the 'performing orchestra' "
        "relationship. 'ensemble' is the classical tag understood by players "
        "like MPD, Roon or Squeezebox; 'performer:orchestra' is Picard's own "
        "tag for this relationship.",
        True,
        True,
        False,
    ),
    (
        "location",
        "Recording location",
        "From the recording's 'recorded at' "
        "place relationship (e.g. the church whose organ was played). "
        "'location' is understood e.g. by MPD. Needs one extra MusicBrainz "
        "request per album.",
        False,
        True,
        False,
    ),
]

# mode tables: (stored value, translation key, English default text)
_ROLE_MODES = [
    ("keep", "role_mode.keep", "Keep as credited"),
    ("remove", "role_mode.remove", "Remove if present"),
    ("add", "role_mode.add", "Add if missing"),
]

_WRITE_POLICIES = [
    ("replace", "policy.replace", "Replace existing values"),
    ("append", "policy.append", "Append generated values"),
    ("merge", "policy.merge", "Merge without duplicates"),
    ("if_empty", "policy.if_empty", "Write only if the tag is empty"),
]

# classical detection rule cells and signal column labels
_RULE_CELL_MODES = [
    ("", "classical.cell.ignore", "—"),
    ("require", "classical.cell.require", "required"),
    ("exclude", "classical.cell.exclude", "must not hold"),
]

_CLASSICAL_SCOPES = [
    ("track", "scope.track", "Each track on its own"),
    ("release", "scope.release", "The release as a whole"),
]

_CLASSICAL_SIGNAL_LABELS = {
    "composer": "Work has composer",
    "conductor_orchestra": "Conductor/orchestra",
    "composer_in_credit": "Composer in credit",
    "genre": "Classical genre",
    "multi_movement": "Multi-movement work",
    "tagged": "Already tagged",
}

# sections with a "Write sort to" field; the preview shows no sort row for
# the others (e.g. location - places have no sort names)
_SORT_SECTIONS = {
    key for key, _label, _note, has_sort, _split, _roles in _PEOPLE_UI if has_sort
}

_DATE_MODES = [
    ("end", "date_mode.end", "Last day of the sessions"),
    ("begin", "date_mode.begin", "First day of the sessions"),
    ("range", "date_mode.range", "Full range (e.g. 1983-09-20 - 1983-09-27)"),
]

_PORTABLE_TAGS = {
    "title_canonical": "",
    "title_credited": "",
    "artist_canonical": "artist",
    "artist_credited": "",
    "artist_sort": "artistsort",
    "albumartist_canonical": "albumartist",
    "albumartist_credited": "",
    "albumartist_sort": "albumartistsort",
    "artists_canonical": "artists",
    "artists_credited": "",
    "artists_sort": "artists_sort",
    "composer_canonical": "composer",
    "composer_credited": "",
    "composer_sort": "composersort",
    "conductor_canonical": "conductor",
    "conductor_credited": "",
    "conductor_sort": "conductorsort",
    "orchestra_canonical": "ensemble; performer:orchestra",
    "orchestra_credited": "",
    "location_canonical": "location",
    "location_credited": "",
    "tag_movement": "movement; part",
    "tag_grouping": "grouping",
    "tag_work": "work",
    "tag_movementnumber": "movementnumber",
    "tag_movementtotal": "movementtotal",
    "tag_showmovement": "showmovement",
    "tag_key": "key",
    "tag_work_year": "work_year",
    "tag_recordingdate": "recordingdate",
}

_TAG_PRESETS = [
    ("portable", "Portable (default)", _PORTABLE_TAGS),
    (
        "picard",
        "Picard-native",
        dict(
            _PORTABLE_TAGS,
            orchestra_canonical="performer:orchestra",
            tag_movement="movement",
        ),
    ),
    (
        "roon",
        "Roon",
        dict(
            _PORTABLE_TAGS,
            orchestra_canonical="ensemble",
            tag_movement="part",
            tag_grouping="",
            tag_showmovement="",
        ),
    ),
    (
        "mpd",
        "MPD",
        dict(_PORTABLE_TAGS, orchestra_canonical="ensemble", tag_movement="movement"),
    ),
]

_TEMPLATE_HELP = (
    "Templates: %L1% = performed work (deepest level), %L2%… = its "
    "parents, %top% = topmost work. Ranges join several levels: "
    "%top..L1{:: }% renders them in the written order, glued with the "
    "separator in braces (default '; '). Missing levels render as "
    "nothing. Tag lists may name several tags separated by ';'. The "
    "hierarchy is also available to Picard scripts as %_sc_l1%…, "
    "%_sc_top%, %_sc_depth% and %_sc_partial%."
)


# ---------------------------------------------------------------------------
# Option metadata
# ---------------------------------------------------------------------------
#
# Every option the dialog shows is profile-aware, which means Picard needs a
# human-readable title for it and, to highlight it, the name of the widget
# that edits it.  Both are derived from the labels the options page already
# puts on screen, so a title costs no string of its own.

# Section heading per option group.  The people sections reuse the labels the
# page builds from _PEOPLE_UI; the rest name theirs here.
_SECTION_LABELS = {
    key: ("section.%s.label" % key, label)
    for key, label, _note, _sort, _split, _roles in _PEOPLE_UI
}
_SECTION_LABELS.update(
    {
        "classical": ("section.classical.label", "Classical detection"),
        "recdate": ("section.recdate.label", "Recording date"),
        "work": ("section.work.label", "Work && movement"),
        "key": ("section.key.label", "Key"),
        "workyear": ("section.workyear.label", "Composition year"),
    }
)

# The role combos share one label with the section they belong to ("Composer:"
# labels both the Composer section and the composer role rule of the Artist
# section), so the rules get a title of their own.
_ROLE_TITLES = {
    "composer": ("option.role.composer", "Composer role"),
    "conductor": ("option.role.conductor", "Conductor role"),
    "orchestra": ("option.role.orchestra", "Orchestra role"),
}

# Options outside the people sections: (option, section, key, English text).
# Almost all of them reuse the key the page already puts in front of the
# widget; only the rows the dialog labels with something other than a caption
# (the section check box, the two tables, a check box legend) name a new one.
_OPTION_ROWS = [
    ("location_area_fallback", "location", "option.location_fallback", "Area fallback"),
    ("classical_enabled", "classical", "option.enabled", "Enabled"),
    ("classical_scope", "classical", "classical.scope", "Judge:"),
    ("classical_rules", "classical", "option.classical_rules", "Detection rules"),
    ("classical_genres", "classical", "classical.genres", "Genre keywords:"),
    ("classical_detect_tag", "classical", "classical.detect_tag", "Detect from tag:"),
    (
        "classical_detect_value",
        "classical",
        "classical.detect_value",
        "Detect tag value:",
    ),
    ("tag_classical", "classical", "classical.write_tag", "Write verdict to:"),
    ("classical_value", "classical", "classical.value", "Value when classical:"),
    (
        "classical_negative_value",
        "classical",
        "classical.negative_value",
        "Value when not classical:",
    ),
    ("recdate_enabled", "recdate", "option.enabled", "Enabled"),
    ("recdate_write_policy", "recdate", "ui.existing_tags", "Existing tags:"),
    ("tag_recordingdate", "recdate", "ui.write_to", "Write to:"),
    ("recording_date_mode", "recdate", "ui.date_style", "Date style:"),
    ("work_enabled", "work", "option.enabled", "Enabled"),
    ("work_write_policy", "work", "ui.existing_tags", "Existing tags:"),
    ("tpl_movement", "work", "work.movement_value", "Movement value:"),
    ("tag_movement", "work", "work.write_movement", "Write movement to:"),
    ("tpl_grouping", "work", "work.grouping_value", "Grouping value:"),
    ("tag_grouping", "work", "work.write_grouping", "Write grouping to:"),
    ("tpl_work", "work", "work.work_value", "Work value:"),
    ("tag_work", "work", "work.write_work", "Write work to:"),
    ("part_suffix", "work", "work.part_suffix", "Partial performance suffix:"),
    (
        "tag_movementnumber",
        "work",
        "work.write_movementnumber",
        "Write movement number to:",
    ),
    (
        "tag_movementtotal",
        "work",
        "work.write_movementtotal",
        "Write movement total to:",
    ),
    (
        "tag_showmovement",
        "work",
        "work.write_showmovement",
        "Write show movement to:",
    ),
    ("depth_overrides", "work", "option.depth_overrides", "Depth overrides"),
    ("key_enabled", "key", "option.enabled", "Enabled"),
    ("key_write_policy", "key", "ui.existing_tags", "Existing tags:"),
    ("tag_key", "key", "ui.write_to", "Write to:"),
    ("workyear_enabled", "workyear", "option.enabled", "Enabled"),
    ("workyear_write_policy", "workyear", "ui.existing_tags", "Existing tags:"),
    ("tag_work_year", "workyear", "ui.write_to", "Write to:"),
    ("composed_suffix", "workyear", "ui.suffix", "Suffix:"),
]


def _build_option_labels():
    """Option name -> (section, translation key, English text), for every
    option the dialog actually shows.

    The has_sort/has_split flags that decide whether _add_people_box builds a
    widget decide the same thing here, so the three options without one
    (title_sort, title_split, location_sort) stay out by construction: no
    widget, nothing to title, nothing a profile could override."""
    labels = {}
    for key, _label, _note, has_sort, has_split, has_roles in _PEOPLE_UI:
        rows = [
            ("enabled", "option.enabled", "Enabled"),
            ("write_policy", "ui.existing_tags", "Existing tags:"),
            ("canonical", "ui.write_canonical", "Write canonical to:"),
            ("credited", "ui.write_credited", "Write credited to:"),
        ]
        if has_sort:
            rows.append(("sort", "ui.write_sort", "Write sort to:"))
        if has_split:
            rows.append(("split", "ui.split", "Split into multiple values"))
        for suffix, tr_key, text in rows:
            labels["%s_%s" % (key, suffix)] = (key, tr_key, text)
        if has_roles:
            for role in _ROLES:
                labels["%s_role_%s" % (key, role)] = (key,) + _ROLE_TITLES[role]
    for option, section, tr_key, text in _OPTION_ROWS:
        labels[option] = (section, tr_key, text)
    return labels


_OPTION_LABELS = _build_option_labels()


def _plain(text):
    """QGroupBox reads a single '&' as a mnemonic, so section labels escape it
    as '&&'.  A plain title or a tree row must not."""
    return text.replace("&&", "&")


def _option_titles(api):
    """Option name -> the title the profiles UI shows for it.

    Composed from the section heading and the row label the options page
    already shows: 'Composer' + 'Write canonical to:' becomes
    'Composer: Write canonical to'."""
    titles = {}
    for option, (section, tr_key, text) in _OPTION_LABELS.items():
        heading_key, heading_text = _SECTION_LABELS[section]
        heading = _plain(api.tr(heading_key, heading_text))
        titles[option] = "%s: %s" % (heading, api.tr(tr_key, text).rstrip(" :："))
    return titles


# The two tables are QAbstractItemView subclasses, and Picard skips those when
# highlighting (a stylesheet breaks checkable item rendering).  They stay
# profile-aware, they just get no widget to mark.
_UNSTYLED_OPTIONS = frozenset({"classical_rules", "depth_overrides"})


def _page_options():
    """OPTIONS for the options page: the widget Picard marks when a profile
    tracks or overrides an option.  Every option-bearing widget is named after
    its option (see SimpleClassicalOptionsPage.__init__), so the mapping is
    the identity."""
    options: PageOptionConfigs = {}
    for option in _OPTION_LABELS:
        options[option] = {} if option in _UNSTYLED_OPTIONS else {"widgets": [option]}
    return options


class SimpleClassicalOptionsPage(OptionsPage):
    NAME = "simple_classical"
    TITLE = "Simple Classical"
    OPTIONS: PageOptionConfigs = _page_options()

    def __init__(self, parent=None):
        super().__init__(parent)
        self._checks = {}  # option name -> widget with setChecked
        self._texts = {}  # option name -> QLineEdit
        self._modes = {}  # option name -> QComboBox with data values
        self._previews = {}  # preview key -> (container, layout, label pairs)

        # preview state; the alive flag lets pending network callbacks
        # detect that the page has been destroyed
        self._preview_release = None
        self._preview_works = {}
        self._preview_inflight = set()
        self._preview_rows = {}  # preview key -> rows currently shown
        self._pending_loaded = None  # release response not yet shown
        self._alive = {"value": True}
        self.destroyed.connect(
            lambda *args, alive=self._alive: alive.update(value=False)
        )

        # All preview updates go through this timer so that asynchronous
        # network responses never relayout the page while a popup, menu or
        # tooltip is up.  Moving the widget under a live popup makes Qt
        # reposition the popup's Wayland surface, which some compositors
        # (cosmic-comp) mishandle; the broken popup state then makes every
        # popup in the application close right after opening.
        self._preview_refresh_timer = QtCore.QTimer(self)
        self._preview_refresh_timer.setSingleShot(True)
        self._preview_refresh_timer.timeout.connect(self._refresh_preview)

        outer = QtWidgets.QVBoxLayout(self)
        scroll = QtWidgets.QScrollArea(self)
        scroll.setWidgetResizable(True)
        scroll.setFocusPolicy(QtCore.Qt.FocusPolicy.NoFocus)
        content = QtWidgets.QWidget()
        self._layout = QtWidgets.QVBoxLayout(content)
        self._layout.setSizeConstraint(
            QtWidgets.QLayout.SizeConstraint.SetMinAndMaxSize
        )
        scroll.setWidget(content)
        outer.addWidget(scroll)

        self._add_preset_box()
        self._add_preview_box()
        self._add_classical_box()
        for key, label, note, has_sort, has_split, has_roles in _PEOPLE_UI:
            self._add_people_box(key, label, note, has_sort, has_split, has_roles)
        # everything read straight off the release/recording first (people,
        # location above, date here), then the work-derived sections
        # (hierarchy, key, composition year share the work lookups)
        self._add_recdate_box()
        self._add_work_box()
        self._add_simple_box(
            "key_enabled",
            self._tr("section.key.label", "Key"),
            [(self._tr("ui.write_to", "Write to:"), "tag_key")],
            "key",
        )
        self._add_simple_box(
            "workyear_enabled",
            self._tr("section.workyear.label", "Composition year"),
            [
                (self._tr("ui.write_to", "Write to:"), "tag_work_year"),
                (self._tr("ui.suffix", "Suffix:"), "composed_suffix"),
            ],
            "work_year",
        )
        self._layout.addStretch()

        # Profile highlighting: Picard looks a tracked option's widget up as
        # an attribute of the page and then styles it through an object-name
        # selector, so an option-bearing widget needs both, named after its
        # option (see OPTIONS).  The two tables are left out - Picard skips
        # item views, and they are reached through self.rules_table /
        # self.overrides_table anyway.
        for widgets in (self._checks, self._texts, self._modes):
            for option, widget in widgets.items():
                widget.setObjectName(option)
                setattr(self, option, widget)

        # Recompute the preview whenever an option that affects its
        # generated values changes. Write policies only affect existing file
        # metadata, which the release preview does not have.
        for widget in self._checks.values():
            widget.toggled.connect(self._schedule_preview_refresh)
        for widget in self._texts.values():
            widget.textChanged.connect(self._schedule_preview_refresh)
        for option, combo in self._modes.items():
            if not option.endswith("_write_policy"):
                combo.currentIndexChanged.connect(self._schedule_preview_refresh)
        self.overrides_table.cellChanged.connect(self._schedule_preview_refresh)

    # -- section builders ------------------------------------------------

    def _tr(self, key, text):
        """Translate a UI string through the plugin's locale/ catalogs."""
        return self.api.tr(key, text)

    def _add_preset_box(self):
        box = QtWidgets.QGroupBox(self._tr("preset.box", "Tagging preset"))
        self._layout.addWidget(box)
        form = QtWidgets.QFormLayout(box)
        self._preset_combo = QtWidgets.QComboBox()
        for key, label, _values in _TAG_PRESETS:
            self._preset_combo.addItem(self._tr("preset.%s" % key, label), key)
        apply_button = QtWidgets.QPushButton(self._tr("preset.apply", "Apply preset"))
        apply_button.clicked.connect(self._apply_preset)
        row = QtWidgets.QHBoxLayout()
        row.addWidget(self._preset_combo)
        row.addWidget(apply_button)
        form.addRow(row)
        self._note_row(
            form,
            self._tr(
                "preset.note",
                "Presets change output tag fields only. Enabled sections, "
                "templates and existing-tag policies remain unchanged, and every "
                "field stays editable.",
            ),
        )

    def _apply_preset(self):
        selected = self._preset_combo.currentData()
        for key, _label, values in _TAG_PRESETS:
            if key != selected:
                continue
            for option, value in values.items():
                edit = self._texts.get(option)
                if edit is not None:
                    edit.setText(value)
            break

    def _add_preview_box(self):
        box = QtWidgets.QGroupBox(self._tr("preview.box", "Preview"))
        self._layout.addWidget(box)
        form = QtWidgets.QFormLayout(box)
        row = QtWidgets.QHBoxLayout()
        self._preview_input = QtWidgets.QLineEdit()
        self._preview_input.setPlaceholderText(
            self._tr("preview.placeholder", "MusicBrainz release URL or MBID")
        )
        self._preview_input.returnPressed.connect(self._load_preview)
        load_button = QtWidgets.QPushButton(self._tr("preview.load", "Load"))
        load_button.clicked.connect(self._load_preview)
        row.addWidget(self._preview_input)
        row.addWidget(load_button)
        form.addRow(row)
        self._preview_tracks = QtWidgets.QComboBox()
        self._preview_tracks.currentIndexChanged.connect(self._schedule_preview_refresh)
        form.addRow(self._tr("preview.track", "Track:"), self._preview_tracks)
        self._preview_status = QtWidgets.QLabel(
            self._tr(
                "preview.hint",
                "Load a release to see, next to each section, the values it "
                "produces with the current (unsaved) settings.",
            )
        )
        self._preview_status.setWordWrap(True)
        form.addRow(self._preview_status)

    def _add_classical_box(self):
        tr = self._tr
        form = self._add_box(
            "classical_enabled", tr("section.classical.label", "Classical detection")
        )
        self._note_row(
            form,
            tr(
                "section.classical.note",
                "Decides per release whether the plugin tags it at all: the "
                "release counts as classical if any rule row matches, and a "
                "row matches when every signal set to 'required' holds and "
                "none set to 'must not hold' does. With this section "
                "disabled nothing is detected: every release is tagged, and "
                "no verdict is exported or written. While it is enabled the "
                "verdict is exported to Picard scripts as %_sc_classical% "
                "(single signals as %_sc_sig_...%). The genre signal needs "
                "'Use genres from MusicBrainz' enabled in Picard's options.",
            ),
        )
        self._mode_row(
            form,
            tr("classical.scope", "Judge:"),
            "classical_scope",
            _CLASSICAL_SCOPES,
        )
        self._note_row(
            form,
            tr(
                "classical.scope_note",
                "Per track, each track is judged on its own recording and "
                "work, so a mixed box set tags only the classical part of "
                "it. Two signals cannot be split that way: the genre is a "
                "property of the release, and a file only counts towards "
                "its own track if it carries MusicBrainz ids (which files "
                "tagged by Picard before do) — one that does not counts for "
                "the whole release, as it did before.",
            ),
        )
        self._text_row(
            form, tr("classical.genres", "Genre keywords:"), "classical_genres"
        )
        self._text_row(
            form, tr("classical.detect_tag", "Detect from tag:"), "classical_detect_tag"
        )
        self._text_row(
            form,
            tr("classical.detect_value", "Detect tag value:"),
            "classical_detect_value",
        )
        self.rules_table = QtWidgets.QTableWidget(0, len(_CLASSICAL_SIGNALS))
        self.rules_table.setHorizontalHeaderLabels(
            [
                tr("classical.col.%s" % signal, _CLASSICAL_SIGNAL_LABELS[signal])
                for signal in _CLASSICAL_SIGNALS
            ]
        )
        horizontal_header = self.rules_table.horizontalHeader()
        assert horizontal_header is not None
        horizontal_header.setSectionResizeMode(
            QtWidgets.QHeaderView.ResizeMode.ResizeToContents
        )
        horizontal_header.setStretchLastSection(True)
        form.addRow(self.rules_table)
        buttons = QtWidgets.QHBoxLayout()
        add_button = QtWidgets.QPushButton(tr("classical.add_rule", "Add rule"))
        remove_button = QtWidgets.QPushButton(
            tr("classical.remove_selected", "Remove selected")
        )
        add_button.clicked.connect(self._add_rule_row)
        remove_button.clicked.connect(self._remove_rule_row)
        buttons.addWidget(add_button)
        buttons.addWidget(remove_button)
        buttons.addStretch()
        form.addRow(buttons)
        self._note_row(
            form,
            tr(
                "classical.marker_note",
                "Writing the verdict to a tag lets a later run pick it up "
                "again through the 'already tagged' signal, together with "
                "releases tagged by hand or by another plugin. Detection "
                "and writing name their tag separately, so a library can be "
                "moved to a new tag or value in one pass. An empty detect "
                "value matches any value. Naming a target tag hands it to "
                "the plugin: an empty value for a verdict means the tag "
                "does not belong on the file and is removed from it. The "
                "verdict is written even when this section gates all other "
                "tags away.",
            ),
        )
        self._text_row(
            form, tr("classical.write_tag", "Write verdict to:"), "tag_classical"
        )
        self._text_row(
            form, tr("classical.value", "Value when classical:"), "classical_value"
        )
        self._text_row(
            form,
            tr("classical.negative_value", "Value when not classical:"),
            "classical_negative_value",
        )
        self._preview_row(form, "classical")

    def _add_rule_row(self, values=None):
        row = self.rules_table.rowCount()
        self.rules_table.insertRow(row)
        values = values if isinstance(values, dict) else {}
        for column, signal in enumerate(_CLASSICAL_SIGNALS):
            combo = QtWidgets.QComboBox()
            for value, tr_key, text in _RULE_CELL_MODES:
                combo.addItem(self._tr(tr_key, text), value)
            combo.setCurrentIndex(max(combo.findData(values.get(signal) or ""), 0))
            combo.currentIndexChanged.connect(self._schedule_preview_refresh)
            self.rules_table.setCellWidget(row, column, combo)

    def _remove_rule_row(self):
        row = self.rules_table.currentRow()
        if row >= 0:
            self.rules_table.removeRow(row)
            self._schedule_preview_refresh()

    def _load_rules(self, text):
        self.rules_table.setRowCount(0)
        try:
            rows = json.loads(text or "[]")
        except ValueError:
            rows = []
        if not isinstance(rows, list):
            rows = []
        for row in rows:
            self._add_rule_row(row)

    def _save_rules(self):
        rules = []
        for row in range(self.rules_table.rowCount()):
            rule = {}
            for column, signal in enumerate(_CLASSICAL_SIGNALS):
                combo = self.rules_table.cellWidget(row, column)
                value = (
                    combo.currentData()
                    if isinstance(combo, QtWidgets.QComboBox)
                    else ""
                )
                if value:
                    rule[signal] = value
            rules.append(rule)
        return json.dumps(rules)

    def _preview_row(self, form, key):
        """Add a lightweight, non-interactive field/value preview."""
        container = QtWidgets.QWidget()
        layout = QtWidgets.QGridLayout(container)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setColumnStretch(1, 1)
        container.hide()
        self._previews[key] = (container, layout, [])
        form.addRow(container)

    def _add_box(self, enabled_option, label):
        box = QtWidgets.QGroupBox(label)
        box.setCheckable(True)
        self._checks[enabled_option] = box
        self._layout.addWidget(box)
        form = QtWidgets.QFormLayout(box)
        return form

    def _text_row(self, form, label, option):
        edit = QtWidgets.QLineEdit()
        self._texts[option] = edit
        form.addRow(label, edit)

    def _note_row(self, form, note):
        widget = QtWidgets.QLabel(note)
        widget.setWordWrap(True)
        form.addRow(widget)

    def _mode_row(self, form, label, option, modes):
        combo = QtWidgets.QComboBox()
        for value, tr_key, text in modes:
            combo.addItem(self._tr(tr_key, text), value)
        self._modes[option] = combo
        form.addRow(label, combo)

    def _add_people_box(self, key, label, note, has_sort, has_split, has_roles):
        form = self._add_box(
            "%s_enabled" % key, self._tr("section.%s.label" % key, label)
        )
        if note:
            self._note_row(form, self._tr("section.%s.note" % key, note))
        self._mode_row(
            form,
            self._tr("ui.existing_tags", "Existing tags:"),
            "%s_write_policy" % key,
            _WRITE_POLICIES,
        )
        if has_roles:
            for role in _ROLES:
                self._mode_row(
                    form,
                    self._tr("role.%s" % role, role.capitalize() + ":"),
                    "%s_role_%s" % (key, role),
                    _ROLE_MODES,
                )
        self._text_row(
            form,
            self._tr("ui.write_canonical", "Write canonical to:"),
            "%s_canonical" % key,
        )
        self._text_row(
            form,
            self._tr("ui.write_credited", "Write credited to:"),
            "%s_credited" % key,
        )
        if has_sort:
            self._text_row(
                form, self._tr("ui.write_sort", "Write sort to:"), "%s_sort" % key
            )
        if has_split:
            check = QtWidgets.QCheckBox(
                self._tr("ui.split", "Split into multiple values")
            )
            check.setToolTip(
                self._tr(
                    "ui.split_tooltip",
                    "Unchecked: one value joined with the credited join "
                    "phrases (e.g. “A & B”), or '; ' where none exist.",
                )
            )
            self._checks["%s_split" % key] = check
            form.addRow(check)
        if key == "location":
            check = QtWidgets.QCheckBox(
                self._tr(
                    "ui.location_fallback",
                    "Fall back to the “recorded in” area if no “recorded at” "
                    "place is linked",
                )
            )
            check.setToolTip(
                self._tr(
                    "ui.location_fallback_tooltip",
                    "Areas are often just a city or country; leave unchecked "
                    "to write the tag only for an actual venue.",
                )
            )
            self._checks["location_area_fallback"] = check
            form.addRow(check)
        self._preview_row(form, key)

    def _add_work_box(self):
        form = self._add_box(
            "work_enabled", self._tr("section.work.label", "Work && movement")
        )
        self._note_row(form, self._tr("work.template_help", _TEMPLATE_HELP))
        self._mode_row(
            form,
            self._tr("ui.existing_tags", "Existing tags:"),
            "work_write_policy",
            _WRITE_POLICIES,
        )
        tr = self._tr
        self._text_row(
            form, tr("work.movement_value", "Movement value:"), "tpl_movement"
        )
        self._text_row(
            form, tr("work.write_movement", "Write movement to:"), "tag_movement"
        )
        self._text_row(
            form, tr("work.grouping_value", "Grouping value:"), "tpl_grouping"
        )
        self._text_row(
            form, tr("work.write_grouping", "Write grouping to:"), "tag_grouping"
        )
        self._text_row(form, tr("work.work_value", "Work value:"), "tpl_work")
        self._text_row(form, tr("work.write_work", "Write work to:"), "tag_work")
        self._text_row(
            form, tr("work.part_suffix", "Partial performance suffix:"), "part_suffix"
        )
        self._text_row(
            form,
            tr("work.write_movementnumber", "Write movement number to:"),
            "tag_movementnumber",
        )
        self._text_row(
            form,
            tr("work.write_movementtotal", "Write movement total to:"),
            "tag_movementtotal",
        )
        self._text_row(
            form,
            tr("work.write_showmovement", "Write show movement to:"),
            "tag_showmovement",
        )

        self._note_row(
            form,
            tr(
                "work.overrides_note",
                "Depth-specific overrides (empty cell = use the general value above):",
            ),
        )
        self.overrides_table = QtWidgets.QTableWidget(0, 4)
        self.overrides_table.setHorizontalHeaderLabels(
            [
                tr("work.col_depth", "Depth"),
                tr("work.col_movement", "Movement"),
                tr("work.col_grouping", "Grouping"),
                tr("work.col_work", "Work"),
            ]
        )
        horizontal_header = self.overrides_table.horizontalHeader()
        assert horizontal_header is not None
        horizontal_header.setStretchLastSection(True)
        form.addRow(self.overrides_table)
        buttons = QtWidgets.QHBoxLayout()
        add_button = QtWidgets.QPushButton(tr("work.add_override", "Add override"))
        remove_button = QtWidgets.QPushButton(
            tr("work.remove_selected", "Remove selected")
        )
        add_button.clicked.connect(self._add_override_row)
        remove_button.clicked.connect(self._remove_override_row)
        buttons.addWidget(add_button)
        buttons.addWidget(remove_button)
        buttons.addStretch()
        form.addRow(buttons)
        self._preview_row(form, "work")

    def _add_simple_box(self, enabled_option, label, rows, preview_key=None):
        form = self._add_box(enabled_option, label)
        policy_option = enabled_option.removesuffix("_enabled") + "_write_policy"
        self._mode_row(
            form,
            self._tr("ui.existing_tags", "Existing tags:"),
            policy_option,
            _WRITE_POLICIES,
        )
        for row_label, option in rows:
            self._text_row(form, row_label, option)
        if preview_key:
            self._preview_row(form, preview_key)

    def _add_recdate_box(self):
        form = self._add_box(
            "recdate_enabled", self._tr("section.recdate.label", "Recording date")
        )
        self._note_row(
            form,
            self._tr(
                "section.recdate.note",
                "From the performance relationship's dates or the “recorded "
                "at”/“recorded in” relationship dates, whichever is more "
                "precise (the performance relationship wins ties). Checking "
                "the place dates needs one extra MusicBrainz request per album "
                "(shared with the recording location section), skipped when "
                "the performance dates are already day-precise.",
            ),
        )
        self._mode_row(
            form,
            self._tr("ui.existing_tags", "Existing tags:"),
            "recdate_write_policy",
            _WRITE_POLICIES,
        )
        self._text_row(form, self._tr("ui.write_to", "Write to:"), "tag_recordingdate")
        self._mode_row(
            form,
            self._tr("ui.date_style", "Date style:"),
            "recording_date_mode",
            _DATE_MODES,
        )
        self._preview_row(form, "recordingdate")

    # -- preview ---------------------------------------------------------

    def _load_preview(self):
        text = self._preview_input.text().strip()
        match = _MBID_RE.search(text)
        if not match or ("musicbrainz.org" in text and "/release/" not in text):
            self._preview_status.setText(
                self._tr(
                    "preview.invalid",
                    "Please paste a release link or MBID (other entities are "
                    "not supported).",
                )
            )
            return
        self._preview_release = None
        self._preview_works = {}
        self._preview_inflight = set()
        self._pending_loaded = None
        self._preview_tracks.blockSignals(True)
        self._preview_tracks.clear()
        self._preview_tracks.blockSignals(False)
        self._preview_status.setText(self._tr("preview.loading", "Loading release…"))
        alive = self._alive

        def handler(document, reply, error):
            if alive["value"]:
                self._preview_loaded(document, error)

        self.api.mb_api.get(
            "/release/" + match.group(0).lower(),
            handler,
            unencoded_queryargs={"inc": _PREVIEW_INC},
        )

    def _preview_loaded(self, document, error):
        # network callback: no widget updates here, only once it is safe
        self._pending_loaded = (document, error)
        self._schedule_preview_refresh()

    def _apply_pending_loaded(self):
        if self._pending_loaded is None:
            return
        document, error = self._pending_loaded
        self._pending_loaded = None
        if error or not document:
            self._preview_status.setText(
                self._tr("preview.failed", "Could not load the release.")
            )
            return
        self._preview_release = document
        self._preview_tracks.blockSignals(True)
        for medium_index, medium in enumerate(document.get("media") or []):
            for track_index, track in enumerate(medium.get("tracks") or []):
                self._preview_tracks.addItem(
                    "%s-%s %s"
                    % (
                        medium.get("position") or medium_index + 1,
                        track.get("number") or track_index + 1,
                        track.get("title") or "",
                    ),
                    (medium_index, track_index),
                )
        self._preview_tracks.blockSignals(False)
        artist = "".join(
            (credit.get("name") or "") + (credit.get("joinphrase") or "")
            for credit in document.get("artist-credit") or []
        )
        self._preview_status.setText("%s — %s" % (artist, document.get("title") or ""))

    def _preview_get_work(self, work_id):
        if work_id in self._preview_works:
            return True, self._preview_works[work_id]
        return False, None

    def _fetch_preview_work(self, work_id):
        if work_id in self._preview_inflight:
            return
        self._preview_inflight.add(work_id)
        alive = self._alive

        def handler(document, reply, error):
            if not alive["value"]:
                return
            self._preview_works[work_id] = None if error else document
            self._preview_inflight.discard(work_id)
            self._schedule_preview_refresh()

        self.api.mb_api.get(
            "/work/" + work_id, handler, unencoded_queryargs={"inc": _WORK_INC}
        )

    def _ui_settings(self):
        """The option values as currently shown in the dialog (unsaved)."""
        setting = {}
        for option, widget in self._checks.items():
            setting[option] = widget.isChecked()
        for option, widget in self._texts.items():
            setting[option] = widget.text()
        for option, combo in self._modes.items():
            setting[option] = combo.currentData()
        setting["depth_overrides"] = self._save_overrides()
        setting["classical_rules"] = self._save_rules()
        return setting

    def _set_preview(self, key, rows):
        """Fill a lightweight preview with (field, value) label pairs."""
        if rows == self._preview_rows.get(key):
            return  # unchanged: do not touch widgets, do not relayout
        self._preview_rows[key] = rows
        container, layout, labels = self._previews[key]
        while len(labels) < len(rows):
            row = len(labels)
            field_label = QtWidgets.QLabel()
            field_label.setAlignment(
                QtCore.Qt.AlignmentFlag.AlignLeft | QtCore.Qt.AlignmentFlag.AlignTop
            )
            value_label = QtWidgets.QLabel()
            value_label.setTextInteractionFlags(
                QtCore.Qt.TextInteractionFlag.TextSelectableByMouse
            )
            value_label.setWordWrap(True)
            layout.addWidget(field_label, row, 0)
            layout.addWidget(value_label, row, 1)
            labels.append((field_label, value_label))
        for row, (field_label, value_label) in enumerate(labels):
            visible = row < len(rows)
            if visible:
                field, value = rows[row]
                field_label.setText(field)
                value_label.setText(value)
            field_label.setVisible(visible)
            value_label.setVisible(visible)
        container.setVisible(bool(rows))

    def _schedule_preview_refresh(self, *args):
        self._preview_refresh_timer.start(150)

    def _refresh_preview(self):
        # Wait for a quiet moment: no popup/menu, no tooltip (not covered
        # by activePopupWidget) and no held mouse button (a combo box may
        # be between press and popup-show).
        app = QtWidgets.QApplication
        if (
            app.activePopupWidget() is not None
            or app.mouseButtons() != QtCore.Qt.MouseButton.NoButton
            or QtWidgets.QToolTip.isVisible()
        ):
            self._preview_refresh_timer.start(150)
            return

        self._apply_pending_loaded()
        release = self._preview_release
        position = self._preview_tracks.currentData()
        if release is None or position is None:
            return
        medium_index, track_index = position
        track = release["media"][medium_index]["tracks"][track_index]
        use_genres = bool(self.api.global_config.setting["use_genres"])
        # the preview always fetches relationships, but a real run only has
        # them when Picard is set to, so it is shown the way a run would see it
        unavailable = _unavailable_signals(self.api.global_config.setting)
        sections, pending = _preview_sections(
            self._ui_settings(),
            release,
            track,
            self._preview_get_work,
            use_genres,
            unavailable,
        )
        for work_id in pending:
            self._fetch_preview_work(work_id)

        def fmt(value):
            return "; ".join(value) if isinstance(value, list) else value

        tr = self._tr
        nothing_found = tr("preview.nothing_found", "(nothing found)")

        info = sections.get("classical")
        if info:
            if not info["judged"]:
                verdict = tr(
                    "preview.classical_unjudged",
                    "No verdict — every rule needs a signal Picard is not "
                    "fetching, so nothing is gated",
                )
            elif info["rule"] is not None:
                verdict = tr("preview.classical_yes", "Classical — rule %d matched") % (
                    info["rule"] + 1
                )
            else:
                verdict = tr("preview.classical_no", "Not classical — no rule matched")
            yes = tr("preview.signal_yes", "yes")
            no = tr("preview.signal_no", "no")
            classical_rows = [("", verdict)]
            for signal in _CLASSICAL_SIGNALS:
                value = yes if info["signals"][signal] else no
                if signal in unavailable:
                    value = tr(
                        "preview.signal_unavailable",
                        "unknown (Picard is not fetching this data)",
                    )
                elif signal == "tagged":
                    value = tr(
                        "preview.tagged_unavailable",
                        "not checked (the preview has no files)",
                    )
                classical_rows.append(
                    (
                        tr(
                            "classical.col.%s" % signal,
                            _CLASSICAL_SIGNAL_LABELS[signal],
                        ),
                        value,
                    )
                )
            self._set_preview("classical", classical_rows)
        for key in (
            "title",
            "artist",
            "albumartist",
            "artists",
            "composer",
            "conductor",
            "orchestra",
            "location",
        ):
            data = sections.get(key)
            fields = (
                ("canonical", "credited", "sort")
                if key in _SORT_SECTIONS
                else ("canonical", "credited")
            )
            if not data:
                rows = [("", nothing_found)]
            else:
                rows = [
                    (tr("field.%s" % field, field), fmt(data[field]))
                    for field in fields
                    if fmt(data.get(field))
                ]
            self._set_preview(key, rows or [("", nothing_found)])

        values = sections.get("work")
        if values == "pending":
            work_rows = [("", tr("preview.loading_works", "(loading work hierarchy…)"))]
            key_value = year_value = tr("preview.loading_short", "(loading…)")
        elif not values:
            no_work = tr("preview.no_work", "(no linked work)")
            work_rows = [("", no_work)]
            key_value = year_value = no_work
        else:
            work_rows = [
                (tr("field.%s" % name, name), values[name])
                for name in ("movement", "grouping", "work")
                if values[name]
            ]
            if values["movement"] and values["numbering"]:
                work_rows.append(
                    (
                        tr("preview.movement_no", "movement no."),
                        tr("preview.movement_of", "%d of %d") % values["numbering"],
                    )
                )
            work_rows = work_rows or [("", tr("preview.empty", "(empty)"))]
            none_value = tr("preview.none", "(none)")
            key_value = values["key"] or none_value
            year_value = values["work_year"] or none_value
        self._set_preview("work", work_rows)
        self._set_preview("key", [("", key_value)])
        self._set_preview("work_year", [("", year_value)])
        self._set_preview(
            "recordingdate",
            [("", sections.get("recordingdate") or tr("preview.no_date", "(no date)"))],
        )

    # -- overrides table -------------------------------------------------

    def _add_override_row(self, values=None):
        row = self.overrides_table.rowCount()
        self.overrides_table.insertRow(row)
        for column, value in enumerate(values or ("", "", "", "")):
            self.overrides_table.setItem(row, column, QtWidgets.QTableWidgetItem(value))

    def _remove_override_row(self):
        row = self.overrides_table.currentRow()
        if row >= 0:
            self.overrides_table.removeRow(row)

    def _load_overrides(self, text):
        self.overrides_table.setRowCount(0)
        try:
            rows = json.loads(text or "[]")
        except ValueError:
            rows = []
        for row in rows:
            self._add_override_row(
                (
                    str(row.get("depth", "")),
                    row.get("movement", "") or "",
                    row.get("grouping", "") or "",
                    row.get("work", "") or "",
                )
            )

    def _save_overrides(self):
        rows = []
        for row in range(self.overrides_table.rowCount()):

            def cell(column):
                item = self.overrides_table.item(row, column)
                return item.text().strip() if item else ""

            try:
                depth = int(cell(0))
            except ValueError:
                continue
            rows.append(
                {
                    "depth": depth,
                    "movement": cell(1),
                    "grouping": cell(2),
                    "work": cell(3),
                }
            )
        return json.dumps(rows)

    # -- load/save -------------------------------------------------------

    def load(self):
        setting = self.api.plugin_config
        for option, widget in self._checks.items():
            widget.setChecked(setting[option])
        for option, widget in self._texts.items():
            widget.setText(setting[option])
        for option, combo in self._modes.items():
            combo.setCurrentIndex(max(combo.findData(setting[option]), 0))
        self._load_overrides(setting["depth_overrides"])
        self._load_rules(setting["classical_rules"])

    def save(self):
        setting = self.api.plugin_config
        for option, widget in self._checks.items():
            setting[option] = widget.isChecked()
        for option, widget in self._texts.items():
            setting[option] = widget.text()
        for option, combo in self._modes.items():
            setting[option] = combo.currentData()
        setting["depth_overrides"] = self._save_overrides()
        setting["classical_rules"] = self._save_rules()
        # A tag name may have changed; announce the variables under the names
        # now configured rather than the ones from when the plugin was enabled.
        _register_script_variables(self.api)


# ---------------------------------------------------------------------------
# Plugin entry points
# ---------------------------------------------------------------------------


_PLUGIN_MODULE_PREFIX = "picard.plugins."

# The key ExtensionPoint files this plugin's entries under, derived from the
# module name exactly the way ExtensionPoint.register derives it.  Outside
# Picard (a test harness importing the file directly) the name does not match
# and nothing is cleared, which is the safe way round: entries registered
# under None are Picard's own, and dropping those would take the application
# with them.
_EXT_POINT_KEY = None
if __name__.startswith(_PLUGIN_MODULE_PREFIX):
    _EXT_POINT_KEY = __name__[len(_PLUGIN_MODULE_PREFIX) :].split(".")[0]


_PEOPLE_FIELDS = ("_canonical", "_credited", "_sort")


def _is_tag_target(option):
    """Options holding a tag list the plugin writes to."""
    return option.startswith("tag_") or option.endswith(_PEOPLE_FIELDS)


# What the value in a tag actually is, in the style of Picard's own variable
# documentation: the content, not the option that produced it.
_TAG_VAR_DESCRIPTIONS = {
    "title_canonical": ("var.tag.title_canonical", "The recording's title."),
    "title_credited": (
        "var.tag.title_credited",
        "The track title as printed on this release.",
    ),
    "artist_canonical": (
        "var.tag.artist_canonical",
        "The track artists after the role rules are applied (composers are "
        "removed by default), under their canonical MusicBrainz names.",
    ),
    "artist_credited": (
        "var.tag.artist_credited",
        "The track artists after the role rules are applied, as credited on "
        "this release.",
    ),
    "artist_sort": (
        "var.tag.artist_sort",
        "The sort names of the track artists after the role rules are applied.",
    ),
    "albumartist_canonical": (
        "var.tag.albumartist_canonical",
        "The release artists after the role rules are applied (composers are "
        "removed by default), under their canonical MusicBrainz names.",
    ),
    "albumartist_credited": (
        "var.tag.albumartist_credited",
        "The release artists after the role rules are applied, as credited on "
        "this release.",
    ),
    "albumartist_sort": (
        "var.tag.albumartist_sort",
        "The sort names of the release artists after the role rules are "
        "applied.",
    ),
    "artists_canonical": (
        "var.tag.artists_canonical",
        "The full release credit including the composer, under their "
        "canonical MusicBrainz names.",
    ),
    "artists_credited": (
        "var.tag.artists_credited",
        "The full release credit including the composer, as credited on this "
        "release.",
    ),
    "artists_sort": (
        "var.tag.artists_sort",
        "The sort names of the full release credit, including the composer.",
    ),
    "composer_canonical": (
        "var.tag.composer_canonical",
        "The composers of the performed work, under their canonical "
        "MusicBrainz names.",
    ),
    "composer_credited": (
        "var.tag.composer_credited",
        "The composers of the performed work, as credited on this release.",
    ),
    "composer_sort": (
        "var.tag.composer_sort",
        "The sort names of the composers of the performed work.",
    ),
    "conductor_canonical": (
        "var.tag.conductor_canonical",
        "The conductors of the recording, under their canonical MusicBrainz "
        "names.",
    ),
    "conductor_credited": (
        "var.tag.conductor_credited",
        "The conductors of the recording, as credited on this release.",
    ),
    "conductor_sort": (
        "var.tag.conductor_sort",
        "The sort names of the conductors of the recording.",
    ),
    "orchestra_canonical": (
        "var.tag.orchestra_canonical",
        "The orchestras performing the recording, under their canonical "
        "MusicBrainz names.",
    ),
    "orchestra_credited": (
        "var.tag.orchestra_credited",
        "The orchestras performing the recording, as credited on this release.",
    ),
    "orchestra_sort": (
        "var.tag.orchestra_sort",
        "The sort names of the orchestras performing the recording.",
    ),
    "location_canonical": (
        "var.tag.location_canonical",
        "The place the recording was made, under its canonical MusicBrainz "
        "name.",
    ),
    "location_credited": (
        "var.tag.location_credited",
        "The place the recording was made, as credited on this release.",
    ),
    "tag_movement": (
        "var.tag.movement",
        "The movement, rendered from the work hierarchy through the movement "
        "template (by default the performed work), with the partial "
        "performance suffix where one applies.",
    ),
    "tag_grouping": (
        "var.tag.grouping",
        "The grouping, rendered from the work hierarchy through the grouping "
        "template (by default the topmost work).",
    ),
    "tag_work": (
        "var.tag.work",
        "The work, rendered from the work hierarchy through the work template "
        "(by default every level from the topmost work down to the performed "
        "one).",
    ),
    "tag_movementnumber": (
        "var.tag.movementnumber",
        "The position of this track among the tracks of its parent work on "
        "this disc.",
    ),
    "tag_movementtotal": (
        "var.tag.movementtotal",
        "The number of tracks belonging to that parent work on this disc.",
    ),
    "tag_showmovement": (
        "var.tag.showmovement",
        "Set to 1 whenever a movement was produced, so that players "
        "supporting it show the work and movement instead of the track title.",
    ),
    "tag_key": (
        "var.tag.key",
        "The key of the performed work, taken from the nearest level of the "
        "hierarchy that names one.",
    ),
    "tag_work_year": (
        "var.tag.work_year",
        "The year or span in which the work was composed, from the composer "
        "relationship dates, followed by the configured suffix.",
    ),
    "tag_recordingdate": (
        "var.tag.recordingdate",
        "The dates of the recording sessions, in the configured date style.",
    ),
    "tag_classical": (
        "var.tag.classical",
        "The classical detection verdict for the release or track, written as "
        "the configured value for classical or for not classical.",
    ),
}


def _is_multi_value(setting, option):
    """A people section writes one value per person while 'Split into
    multiple values' is on and one joined value otherwise; every other
    section writes a single value."""
    for suffix in _PEOPLE_FIELDS:
        if option.endswith(suffix):
            return bool(setting["%s_split" % option[: -len(suffix)]])
    return False


def _tag_variables(setting):
    """Configured tag name -> the option that writes it.

    Names Picard already provides (composer, work, movement, ...) are left
    out: registering one would add a second documentation entry for it and
    log a duplicate warning.  A target naming a hidden variable is announced
    the way a script spells it, with '_' rather than '~'."""
    system = set(script_variable_tag_names())
    found = {}
    for option in _OPTION_LABELS:
        if not _is_tag_target(option):
            continue
        for tag in _parse_taglist(setting[option]):
            name = "_" + tag[1:] if tag.startswith("~") else tag
            if name not in system and name not in found:
                found[name] = option
    return found


def _register_script_variables(api):
    """Announce the variables the plugin writes to the script completer and
    the scripting documentation.

    Called again whenever the options are saved, so that a tag the user has
    renamed is announced under its new name without a restart.  The entries
    have to be cleared first: the extension point appends without checking
    for a name it already holds, and the scripting documentation lists every
    entry it holds, oldest description first."""
    if _EXT_POINT_KEY:
        ext_point_script_variables.unregister_module(_EXT_POINT_KEY)
    tr = api.tr
    api.register_script_variable(
        "_sc_classical",
        tr(
            "var.classical",
            "Whether the release (or track) was judged classical: 1 or 0. "
            "Only set while classical detection is enabled.",
        ),
    )
    for signal in _CLASSICAL_SIGNALS:
        api.register_script_variable(
            "_sc_sig_" + signal,
            tr(
                "var.signal",
                "Classical-detection signal “{label}”: 1 when it holds, "
                "0 when it does not.",
                label=tr("classical.col.%s" % signal, _CLASSICAL_SIGNAL_LABELS[signal]),
            ),
        )
    api.register_script_variable(
        "_sc_depth",
        tr(
            "var.depth",
            "Number of levels in the performed work's hierarchy; 1 means a "
            "standalone work.",
        ),
    )
    api.register_script_variable(
        "_sc_top",
        tr(
            "var.top",
            "The topmost work of the hierarchy, the same value the %top% "
            "template token renders.",
        ),
    )
    api.register_script_variable(
        "_sc_partial",
        tr(
            "var.partial",
            "1 when the recording is a partial performance of the work, 0 otherwise.",
        ),
    )
    for level in range(1, _SCRIPT_VAR_LEVELS + 1):
        api.register_script_variable(
            "_sc_l%d" % level,
            tr(
                "var.level",
                "Level {n} of the work hierarchy: 1 is the performed work "
                "itself, higher numbers are its parents up to %_sc_top%. "
                "%_sc_depth% gives the number of levels a track has, at "
                "most {max}.",
                n=level,
                max=_MAX_DEPTH,
            ),
        )
    setting = api.plugin_config
    for tag, option in _tag_variables(setting).items():
        described = _TAG_VAR_DESCRIPTIONS.get(option)
        if described is None:
            # A tag target without a description of its own: say which
            # section it belongs to rather than nothing at all.
            section, _tr_key, _text = _OPTION_LABELS[option]
            heading_key, heading_text = _SECTION_LABELS[section]
            documentation = tr(
                "var.tag",
                "Tag Simple Classical is configured to write the “{section}” "
                "section to.",
                section=_plain(tr(heading_key, heading_text)),
            )
        else:
            documentation = tr(*described)
        if _is_multi_value(setting, option):
            # Picard notes this for its own multi-value variables; the plugin
            # name it appends itself is a plain line too, so this one is not
            # marked up either - it renders with or without Markdown.
            documentation += "\n\n" + tr(
                "var.multi", "Notes: multi-value variable."
            )
        try:
            api.register_script_variable(tag, documentation)
        except ValueError:
            # A tag name is not necessarily a legal variable name; Picard
            # allows letters, digits, underscores and colons.  The tag is
            # still written, it just cannot be announced.
            continue


def enable(api: PluginApi) -> None:
    """Called when the plugin is enabled."""
    _register_options(api)
    _migrate_options(api.plugin_config)
    _register_script_variables(api)
    api.register_album_metadata_processor(process_album)
    api.register_track_metadata_processor(process_track)
    api.register_options_page(SimpleClassicalOptionsPage)


def disable() -> None:
    """Called when the plugin is disabled; Picard unregisters the extension
    points registered through the api automatically."""
