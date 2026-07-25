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
    PluginApi,
)
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


def _register_options(config):
    """Register all options in the plugin's private config section.

    The option type (text/bool) is inferred from the default value."""
    config.register_option("defaults_version", 0)
    for name, default in _default_options().items():
        config.register_option(name, default)


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


def _pick_performance(recording):
    """Pick the most plausible performance relationship of a recording.

    Recordings are sometimes linked to several works (typically the original
    plus an arrangement of it).  Prefer non-arrangements and relationships
    that carry performance dates.
    """
    best, best_score = None, None
    for rel in _work_rels(recording, "performance"):
        if not rel.get("work"):
            continue
        score = 0
        if _is_arrangement(rel["work"]):
            score -= 10
        if rel.get("begin") or rel.get("end"):
            score += 2
        if "cover" in (rel.get("attributes") or []):
            score -= 1
        if best_score is None or score > best_score:
            best, best_score = rel, score
    return best


def _parent_rel(work):
    for rel in _work_rels(work, "parts", "backward"):
        return rel
    return None


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
_MAX_DEPTH = 4


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
            if places or areas:
                collected[recording["id"]] = {"place": places, "area": areas}
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


def _resolve_chain(api, album, work_id, callback, _chain=None):
    """Fetch work_id and climb 'part of' parents, then call callback(chain);
    chain[0] is work_id's data, chain[-1] the topmost fetched work."""
    chain = _chain if _chain is not None else []

    def _done(document):
        if document is None:
            callback(chain)
            return
        chain.append(document)
        parent = _parent_rel(document)
        if parent and len(chain) < _MAX_DEPTH:
            _resolve_chain(api, album, parent["work"]["id"], callback, chain)
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
# rels the plugin otherwise browses separately.
_PREVIEW_INC = (
    "artist-credits+recordings+artist-rels+work-rels"
    "+recording-level-rels+work-level-rels+place-rels+area-rels"
)

_MBID_RE = re.compile(
    r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", re.I
)


def _preview_sections(setting, release, track, get_work):
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

    sections: dict[str, Any] = {
        "title": {
            "canonical": recording.get("title") or "",
            "credited": track.get("title") or recording.get("title") or "",
        }
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
    sections["recordingdate"] = None
    if performance and work:
        sections["recordingdate"] = _recording_date_value(setting, performance)
        parent = _parent_rel(work)
        chain, missing = [], None
        work_id = parent["work"]["id"] if parent else work["id"]
        while work_id and len(chain) < _MAX_DEPTH:
            known, doc = get_work(work_id)
            if not known:
                missing = work_id
                break
            if doc is None:
                break
            chain.append(doc)
            parent_rel = _parent_rel(doc)
            work_id = parent_rel["work"]["id"] if parent_rel else None
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
# Metadata processors
# ---------------------------------------------------------------------------


def process_album(api, album, metadata, release_node):
    if not api.global_config.setting["track_ars"]:
        api.logger.warning(
            "'Use track and release relationships' is disabled in "
            "Options > Metadata; work and movement tags cannot be created."
        )
    if release_node is None:
        return
    credits = release_node.get("artist-credit") or []
    _apply_people(
        api.plugin_config,
        metadata,
        "albumartist",
        _adjust_credit(
            api.plugin_config,
            "albumartist",
            credits,
            _release_roles(album, release_node),
        ),
    )


def process_track(api, track, metadata, track_node, release_node=None):
    setting = api.plugin_config
    album = track.album
    recording = track_node.get("recording") or track_node

    if setting["title_enabled"]:
        canonical = recording.get("title") or ""
        credited = track_node.get("title") or canonical
        policy = setting["title_write_policy"]
        if canonical:
            _write_tags(metadata, setting["title_canonical"], canonical, policy)
        if credited:
            _write_tags(metadata, setting["title_credited"], credited, policy)

    performance = _pick_performance(recording)
    work = performance["work"] if performance else None

    if release_node is not None:
        credits = release_node.get("artist-credit") or []
        roles = _track_roles(_release_roles(album, release_node), recording, work)
        _apply_people(
            setting,
            metadata,
            "artist",
            _adjust_credit(setting, "artist", credits, roles),
        )
        _apply_people(
            setting,
            metadata,
            "artists",
            _credit_entries(credits, list(range(len(credits)))),
        )

    _apply_people(
        setting, metadata, "conductor", _rel_entries(recording, "artist", "conductor")
    )
    _apply_people(
        setting,
        metadata,
        "orchestra",
        _rel_entries(recording, "artist", "performing orchestra"),
    )

    if (
        setting["location_enabled"]
        and release_node is not None
        and recording.get("id")
        and release_node.get("id")
    ):
        _fetch_locations(
            api,
            album,
            release_node["id"],
            partial(_apply_location, setting, metadata, recording["id"]),
        )

    if not performance or not work:
        return

    _apply_people(
        setting, metadata, "composer", _rel_entries(work, "artist", "composer")
    )

    if setting["recdate_enabled"]:
        _apply_recording_date(setting, metadata, performance)

    if not (
        setting["work_enabled"] or setting["key_enabled"] or setting["workyear_enabled"]
    ):
        return

    numbering = None
    if release_node is not None:
        numbering = _movement_groups(album, release_node).get(track_node.get("id"))
    parent = _parent_rel(work)
    start_id = parent["work"]["id"] if parent else work["id"]
    _resolve_chain(
        api,
        album,
        start_id,
        partial(_finish_work, api, metadata, work, performance, parent, numbering),
    )


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


def _finish_work(api, metadata, work, performance, parent, numbering, chain):
    """Called when the work hierarchy has been fetched; writes everything
    that depends on it."""
    setting = api.plugin_config
    values = _work_values(
        setting, work, performance, parent, numbering, chain, api.logger
    )
    levels = values["levels"]

    # hierarchy variables for Picard scripting
    metadata["~sc_depth"] = str(len(levels))
    metadata["~sc_top"] = levels[-1]
    metadata["~sc_partial"] = "1" if values["partial"] else "0"
    for position, value in enumerate(levels, 1):
        metadata["~sc_l%d" % position] = value

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


def _recording_date_value(setting, performance):
    begin, end = performance.get("begin"), performance.get("end")
    mode = setting["recording_date_mode"]
    if mode == "begin":
        return begin or end
    if mode == "range" and begin and end and begin != end:
        return "%s - %s" % (begin, end)
    return end or begin  # "end"


def _apply_recording_date(setting, metadata, performance):
    value = _recording_date_value(setting, performance)
    if value:
        _write_tags(
            metadata,
            setting["tag_recordingdate"],
            value,
            setting["recdate_write_policy"],
        )


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

_ROLE_MODES = [
    ("keep", "Keep as credited"),
    ("remove", "Remove if present"),
    ("add", "Add if missing"),
]

_WRITE_POLICIES = [
    ("replace", "Replace existing values"),
    ("append", "Append generated values"),
    ("merge", "Merge without duplicates"),
    ("if_empty", "Write only if the tag is empty"),
]

# sections with a "Write sort to" field; the preview shows no sort row for
# the others (e.g. location - places have no sort names)
_SORT_SECTIONS = {
    key for key, _label, _note, has_sort, _split, _roles in _PEOPLE_UI if has_sort
}

_DATE_MODES = [
    ("end", "Last day of the sessions"),
    ("begin", "First day of the sessions"),
    ("range", "Full range (e.g. 1983-09-20 - 1983-09-27)"),
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


class SimpleClassicalOptionsPage(OptionsPage):
    NAME = "simple_classical"
    TITLE = "Simple Classical"

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
        for key, label, note, has_sort, has_split, has_roles in _PEOPLE_UI:
            self._add_people_box(key, label, note, has_sort, has_split, has_roles)
        # everything read straight off the release/recording first (people,
        # location above, date here), then the work-derived sections
        # (hierarchy, key, composition year share the work lookups)
        self._add_recdate_box()
        self._add_work_box()
        self._add_simple_box("key_enabled", "Key", [("Write to:", "tag_key")], "key")
        self._add_simple_box(
            "workyear_enabled",
            "Composition year",
            [("Write to:", "tag_work_year"), ("Suffix:", "composed_suffix")],
            "work_year",
        )
        self._layout.addStretch()

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

    def _add_preset_box(self):
        box = QtWidgets.QGroupBox("Tagging preset")
        self._layout.addWidget(box)
        form = QtWidgets.QFormLayout(box)
        self._preset_combo = QtWidgets.QComboBox()
        for key, label, _values in _TAG_PRESETS:
            self._preset_combo.addItem(label, key)
        apply_button = QtWidgets.QPushButton("Apply preset")
        apply_button.clicked.connect(self._apply_preset)
        row = QtWidgets.QHBoxLayout()
        row.addWidget(self._preset_combo)
        row.addWidget(apply_button)
        form.addRow(row)
        self._note_row(
            form,
            "Presets change output tag fields only. Enabled sections, "
            "templates and existing-tag policies remain unchanged, and every "
            "field stays editable.",
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
        box = QtWidgets.QGroupBox("Preview")
        self._layout.addWidget(box)
        form = QtWidgets.QFormLayout(box)
        row = QtWidgets.QHBoxLayout()
        self._preview_input = QtWidgets.QLineEdit()
        self._preview_input.setPlaceholderText("MusicBrainz release URL or MBID")
        self._preview_input.returnPressed.connect(self._load_preview)
        load_button = QtWidgets.QPushButton("Load")
        load_button.clicked.connect(self._load_preview)
        row.addWidget(self._preview_input)
        row.addWidget(load_button)
        form.addRow(row)
        self._preview_tracks = QtWidgets.QComboBox()
        self._preview_tracks.currentIndexChanged.connect(self._schedule_preview_refresh)
        form.addRow("Track:", self._preview_tracks)
        self._preview_status = QtWidgets.QLabel(
            "Load a release to see, next to each section, the values it "
            "produces with the current (unsaved) settings."
        )
        self._preview_status.setWordWrap(True)
        form.addRow(self._preview_status)

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
        for value, text in modes:
            combo.addItem(text, value)
        self._modes[option] = combo
        form.addRow(label, combo)

    def _add_people_box(self, key, label, note, has_sort, has_split, has_roles):
        form = self._add_box("%s_enabled" % key, label)
        if note:
            self._note_row(form, note)
        self._mode_row(form, "Existing tags:", "%s_write_policy" % key, _WRITE_POLICIES)
        if has_roles:
            for role in _ROLES:
                self._mode_row(
                    form,
                    role.capitalize() + ":",
                    "%s_role_%s" % (key, role),
                    _ROLE_MODES,
                )
        self._text_row(form, "Write canonical to:", "%s_canonical" % key)
        self._text_row(form, "Write credited to:", "%s_credited" % key)
        if has_sort:
            self._text_row(form, "Write sort to:", "%s_sort" % key)
        if has_split:
            check = QtWidgets.QCheckBox("Split into multiple values")
            check.setToolTip(
                "Unchecked: one value joined with the credited join "
                "phrases (e.g. “A & B”), or '; ' where none exist."
            )
            self._checks["%s_split" % key] = check
            form.addRow(check)
        if key == "location":
            check = QtWidgets.QCheckBox(
                "Fall back to the “recorded in” area if no “recorded at” "
                "place is linked"
            )
            check.setToolTip(
                "Areas are often just a city or country; leave unchecked "
                "to write the tag only for an actual venue."
            )
            self._checks["location_area_fallback"] = check
            form.addRow(check)
        self._preview_row(form, key)

    def _add_work_box(self):
        form = self._add_box("work_enabled", "Work && movement")
        self._note_row(form, _TEMPLATE_HELP)
        self._mode_row(form, "Existing tags:", "work_write_policy", _WRITE_POLICIES)
        self._text_row(form, "Movement value:", "tpl_movement")
        self._text_row(form, "Write movement to:", "tag_movement")
        self._text_row(form, "Grouping value:", "tpl_grouping")
        self._text_row(form, "Write grouping to:", "tag_grouping")
        self._text_row(form, "Work value:", "tpl_work")
        self._text_row(form, "Write work to:", "tag_work")
        self._text_row(form, "Partial performance suffix:", "part_suffix")
        self._text_row(form, "Write movement number to:", "tag_movementnumber")
        self._text_row(form, "Write movement total to:", "tag_movementtotal")
        self._text_row(form, "Write show movement to:", "tag_showmovement")

        self._note_row(
            form, "Depth-specific overrides (empty cell = use the general value above):"
        )
        self.overrides_table = QtWidgets.QTableWidget(0, 4)
        self.overrides_table.setHorizontalHeaderLabels(
            ["Depth", "Movement", "Grouping", "Work"]
        )
        horizontal_header = self.overrides_table.horizontalHeader()
        assert horizontal_header is not None
        horizontal_header.setStretchLastSection(True)
        form.addRow(self.overrides_table)
        buttons = QtWidgets.QHBoxLayout()
        add_button = QtWidgets.QPushButton("Add override")
        remove_button = QtWidgets.QPushButton("Remove selected")
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
        self._mode_row(form, "Existing tags:", policy_option, _WRITE_POLICIES)
        for row_label, option in rows:
            self._text_row(form, row_label, option)
        if preview_key:
            self._preview_row(form, preview_key)

    def _add_recdate_box(self):
        form = self._add_box("recdate_enabled", "Recording date")
        self._mode_row(form, "Existing tags:", "recdate_write_policy", _WRITE_POLICIES)
        self._text_row(form, "Write to:", "tag_recordingdate")
        self._mode_row(form, "Date style:", "recording_date_mode", _DATE_MODES)
        self._preview_row(form, "recordingdate")

    # -- preview ---------------------------------------------------------

    def _load_preview(self):
        text = self._preview_input.text().strip()
        match = _MBID_RE.search(text)
        if not match or ("musicbrainz.org" in text and "/release/" not in text):
            self._preview_status.setText(
                "Please paste a release link or MBID (other entities are "
                "not supported)."
            )
            return
        self._preview_release = None
        self._preview_works = {}
        self._preview_inflight = set()
        self._pending_loaded = None
        self._preview_tracks.blockSignals(True)
        self._preview_tracks.clear()
        self._preview_tracks.blockSignals(False)
        self._preview_status.setText("Loading release…")
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
            self._preview_status.setText("Could not load the release.")
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
        sections, pending = _preview_sections(
            self._ui_settings(), release, track, self._preview_get_work
        )
        for work_id in pending:
            self._fetch_preview_work(work_id)

        def fmt(value):
            return "; ".join(value) if isinstance(value, list) else value

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
                rows = [("", "(nothing found)")]
            else:
                rows = [
                    (field, fmt(data[field]))
                    for field in fields
                    if fmt(data.get(field))
                ]
            self._set_preview(key, rows or [("", "(nothing found)")])

        values = sections.get("work")
        if values == "pending":
            work_rows = [("", "(loading work hierarchy…)")]
            key_value = year_value = "(loading…)"
        elif not values:
            work_rows = [("", "(no linked work)")]
            key_value = year_value = "(no linked work)"
        else:
            work_rows = [
                (name, values[name])
                for name in ("movement", "grouping", "work")
                if values[name]
            ]
            if values["movement"] and values["numbering"]:
                work_rows.append(("movement no.", "%d of %d" % values["numbering"]))
            work_rows = work_rows or [("", "(empty)")]
            key_value = values["key"] or "(none)"
            year_value = values["work_year"] or "(none)"
        self._set_preview("work", work_rows)
        self._set_preview("key", [("", key_value)])
        self._set_preview("work_year", [("", year_value)])
        self._set_preview(
            "recordingdate", [("", sections.get("recordingdate") or "(no date)")]
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

    def save(self):
        setting = self.api.plugin_config
        for option, widget in self._checks.items():
            setting[option] = widget.isChecked()
        for option, widget in self._texts.items():
            setting[option] = widget.text()
        for option, combo in self._modes.items():
            setting[option] = combo.currentData()
        setting["depth_overrides"] = self._save_overrides()

    def restore_defaults(self):
        """Show the plugin's defaults in the dialog (nothing is saved until
        the user saves).  Picard's own implementation only covers options
        declared through OptionsPage.register_setting, which this page does
        not use, so without this override the button would merely re-load
        the stored values."""
        defaults = _default_options()
        for option, widget in self._checks.items():
            widget.setChecked(defaults[option])
        for option, widget in self._texts.items():
            widget.setText(defaults[option])
        for option, combo in self._modes.items():
            combo.setCurrentIndex(max(combo.findData(defaults[option]), 0))
        self._load_overrides(defaults["depth_overrides"])


# ---------------------------------------------------------------------------
# Plugin entry points
# ---------------------------------------------------------------------------


def enable(api: PluginApi) -> None:
    """Called when the plugin is enabled."""
    _register_options(api.plugin_config)
    _migrate_options(api.plugin_config)
    api.register_album_metadata_processor(process_album)
    api.register_track_metadata_processor(process_track)
    api.register_options_page(SimpleClassicalOptionsPage)


def disable() -> None:
    """Called when the plugin is disabled; Picard unregisters the extension
    points registered through the api automatically."""
