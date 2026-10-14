"""CoreOS — Activity Engine core (Phase 5E).

Deterministic, provider-neutral logic that turns an *activity intent* into a
*ranked, truthfully-classified* set of real local candidates.

    activity intent
      -> Activity Requirements Profile      (this module: ACTIVITY_PROFILES)
      -> real candidate search              (places_provider, Phase 5C/5D)
      -> evidence evaluation                (this module: evaluate_evidence)
      -> relevance / confidence             (this module: score_candidate)
      -> truthful classification            (this module: classify / reason_for)
      -> distance / deterministic order     (this module: rank)

Design rules
------------
* Nothing here invents a place fact. It only *interprets* evidence already
  present on a normalised CorePlace (name, provider types/tags, 5C
  ``activity_match``, rating). Missing evidence stays missing.
* The engine is the authority for requirements / evidence / relevance. No LLM.
* Distance is ONE factor and is applied only AFTER a minimum-relevance gate and
  capped so it can never move a candidate across an evidence tier: a real
  badminton centre 3 km away must still outrank a generic sports hall 400 m away.
* Pure: no I/O, no time, no randomness — every function is unit-testable.
"""
from __future__ import annotations

import math
import re
from typing import Any, Optional

import places_core as pc

# --------------------------------------------------------------------------- #
# Activity Requirements Profiles
# --------------------------------------------------------------------------- #
# Signal tokens are matched against a candidate's evidence:
#   * OSM provider_types look like  "leisure=pitch", "sport=badminton"
#   * Google provider_types look like "sports_complex", "swimming_pool", "park"
#   * name keywords are matched case-insensitively as whole words
#
# strong_*   -> the evidence names the activity / a purpose-built facility
# moderate_* -> a venue type that credibly hosts the activity
# public_geo_* -> open public geography where the activity needs no facility
# generic_venue_google -> "it's sport-related" and nothing more  => weak tier


class ActivityProfile:
    __slots__ = (
        "activity_id", "label", "requires_facility", "allows_public_geography",
        "strong_osm", "strong_google_types", "strong_name_kw",
        "moderate_osm", "moderate_google_types", "moderate_name_kw",
        "public_geo_osm", "public_geo_google_types",
        "generic_venue_google", "facility_words",
        "min_relevance", "distance_sensitivity",
        "google_text_queries", "google_included_type",
    )

    def __init__(self, **kw: Any) -> None:
        for s in self.__slots__:
            setattr(self, s, kw.get(s))
        # sensible defaults
        for s in ("strong_osm", "strong_google_types", "strong_name_kw",
                  "moderate_osm", "moderate_google_types", "moderate_name_kw",
                  "public_geo_osm", "public_geo_google_types", "generic_venue_google"):
            if getattr(self, s) is None:
                setattr(self, s, frozenset())
            else:
                setattr(self, s, frozenset(getattr(self, s)))
        if self.facility_words is None:
            self.facility_words = frozenset({
                "court", "centre", "center", "complex", "club", "arena", "hall",
                "academy", "stadium", "field", "pitch", "gym", "ground", "park",
            })
        if self.min_relevance is None:
            self.min_relevance = 0.30
        # How quickly practical usefulness decays with distance for THIS activity
        # (1.0 = default). People will travel further for golf than for a run.
        if self.distance_sensitivity is None:
            self.distance_sensitivity = 1.0
        if self.google_text_queries is None:
            self.google_text_queries = []


_GENERIC_SPORT_GOOGLE = frozenset({
    "sports_complex", "sports_club", "sports_activity_location", "gym",
    "fitness_center", "fitness_centre", "stadium", "arena",
})
# court / pitch sports: a bare gym or fitness centre is NOT a plausible venue,
# so it is excluded from both the "moderate" and the "weak/generic" sets.
_COURT_VENUE_GOOGLE = frozenset({
    "sports_complex", "sports_club", "sports_activity_location", "stadium", "arena",
})


def _P(**kw: Any) -> ActivityProfile:
    return ActivityProfile(**kw)


ACTIVITY_PROFILES: dict[str, ActivityProfile] = {
    "futsal": _P(
        activity_id="futsal", label="Futsal",
        requires_facility=True, allows_public_geography=False,
        strong_osm={"sport=futsal"},
        strong_google_types={"futsal"},
        strong_name_kw={"futsal"},
        moderate_osm={"leisure=pitch", "leisure=sports_centre", "sport=soccer", "sport=football", "sport=multi"},
        moderate_google_types={"soccer"},
        moderate_name_kw={"soccer", "football", "5-a-side", "five a side"},
        generic_venue_google=_COURT_VENUE_GOOGLE,
        google_text_queries=["futsal court", "indoor soccer futsal"],
        google_included_type=None,
    ),
    "football": _P(
        activity_id="football", label="Football",
        requires_facility=True, allows_public_geography=False,
        strong_osm={"sport=soccer", "sport=football"},
        strong_google_types={"soccer", "stadium"},
        strong_name_kw={"football", "soccer"},
        moderate_osm={"leisure=pitch", "leisure=stadium", "leisure=sports_centre", "sport=multi"},
        moderate_google_types=set(),
        moderate_name_kw={"padang"},
        generic_venue_google=_COURT_VENUE_GOOGLE,
        google_text_queries=["football field", "soccer field"],
        google_included_type=None,
    ),
    "basketball": _P(
        activity_id="basketball", label="Basketball",
        min_relevance=0.42,
        requires_facility=True, allows_public_geography=False,
        strong_osm={"sport=basketball"},
        strong_google_types={"basketball"},
        strong_name_kw={"basketball"},
        moderate_osm={"leisure=pitch", "leisure=sports_centre", "sport=multi"},
        moderate_google_types=set(),
        moderate_name_kw={"hoops"},
        generic_venue_google=_COURT_VENUE_GOOGLE,
        google_text_queries=["basketball court"],
        google_included_type=None,
    ),
    "volleyball": _P(
        activity_id="volleyball", label="Volleyball",
        min_relevance=0.42,
        requires_facility=True, allows_public_geography=False,
        # genuine, activity-specific OSM tags only. The official OpenStreetMap
        # value for beach volleyball is `sport=beachvolleyball` (ONE word, no
        # underscore). Beach volleyball is NEVER inferred from a generic "beach"
        # / "sand" name, a plain sports centre, or any unrelated venue.
        strong_osm={"sport=volleyball", "sport=beachvolleyball"},
        # There is no official Google Places TYPE that names volleyball, so
        # Google `types` alone never CONFIRM volleyball. A generic Google sports
        # venue stays weak/moderate via generic_venue_google; Confirmed
        # volleyball needs a volleyball-specific OSM tag or explicit reliable
        # volleyball name/text evidence.
        strong_google_types=set(),
        strong_name_kw={"volleyball", "bola tampar"},
        moderate_osm={"leisure=sports_centre", "leisure=sports_hall", "sport=multi"},
        moderate_google_types=set(),
        moderate_name_kw={"beach volleyball"},
        generic_venue_google=_COURT_VENUE_GOOGLE,
        google_text_queries=["volleyball court", "beach volleyball court"],
        google_included_type=None,
    ),
    "badminton": _P(
        activity_id="badminton", label="Badminton",
        min_relevance=0.42,
        requires_facility=True, allows_public_geography=False,
        strong_osm={"sport=badminton"},
        strong_google_types={"badminton"},
        strong_name_kw={"badminton", "shuttlecock"},
        # a multi-sport centre / hall is only a *moderate* signal for badminton —
        # a generic basketball or netball court is NOT (that is handled as a
        # conflicting specific sport below).
        moderate_osm={"leisure=sports_centre", "leisure=sports_hall", "sport=multi", "sport=table_tennis"},
        moderate_google_types=set(),
        moderate_name_kw={"racquet", "racket", "sports hall"},
        generic_venue_google=_COURT_VENUE_GOOGLE,
        google_text_queries=["badminton court", "badminton hall"],
        google_included_type=None,
    ),
    "swimming": _P(
        activity_id="swimming", label="Swimming",
        requires_facility=True, allows_public_geography=False,
        strong_osm={"leisure=swimming_pool", "sport=swimming", "amenity=swimming_pool"},
        strong_google_types={"swimming_pool"},
        strong_name_kw={"swimming", "aquatic", "pool", "aquatics"},
        moderate_osm={"leisure=water_park", "sport=multi"},
        moderate_google_types={"water_park"},
        moderate_name_kw={"lido", "water park"},
        # a plain sports complex / gym with no pool evidence is NOT a swimming result
        generic_venue_google=frozenset(),
        google_text_queries=["public swimming pool", "aquatic centre"],
        google_included_type="swimming_pool",
    ),
    "running": _P(
        activity_id="running", label="Running",
        requires_facility=False, allows_public_geography=True,
        distance_sensitivity=1.35,
        strong_osm={"sport=athletics", "sport=running", "leisure=track"},
        strong_google_types={"athletic_field"},
        strong_name_kw={"running track", "athletic", "athletics", "jogging track"},
        # running is outdoors — an indoor sports hall is not a running venue.
        moderate_osm={"sport=multi"},
        moderate_google_types=set(),
        moderate_name_kw={"jogging", "run "},
        public_geo_osm={
            "leisure=park", "leisure=recreation_ground", "leisure=nature_reserve",
            "leisure=garden", "boundary=national_park", "leisure=common",
        },
        public_geo_google_types={"park", "national_park", "hiking_area", "tourist_attraction"},
        generic_venue_google=set(),
        google_text_queries=["running track", "jogging park", "lake park"],
        google_included_type=None,
    ),
    "cycling": _P(
        activity_id="cycling", label="Cycling",
        requires_facility=False, allows_public_geography=True,
        distance_sensitivity=1.2,
        strong_osm={"sport=cycling", "leisure=track"},
        strong_google_types=set(),
        strong_name_kw={"velodrome", "cycling track", "bike park", "cycle park"},
        moderate_osm={"route=bicycle", "sport=multi"},
        moderate_google_types=set(),
        moderate_name_kw={"cycling", "bicycle", "bike trail", "cycle path"},
        public_geo_osm={
            "leisure=park", "leisure=nature_reserve", "leisure=recreation_ground",
            "boundary=national_park",
        },
        public_geo_google_types={"park", "national_park", "hiking_area"},
        # deliberately no bicycle *shop* signal
        generic_venue_google=set(),
        google_text_queries=["cycling track", "park cycling path", "bicycle trail"],
        google_included_type=None,
    ),
    "tennis": _P(
        activity_id="tennis", label="Tennis",
        min_relevance=0.42,
        requires_facility=True, allows_public_geography=False,
        strong_osm={"sport=tennis"},
        strong_google_types={"tennis"},
        strong_name_kw={"tennis"},
        moderate_osm={"leisure=sports_centre", "sport=multi"},
        moderate_google_types=set(),
        moderate_name_kw={"racquet", "racket"},
        generic_venue_google=_COURT_VENUE_GOOGLE,
        google_text_queries=["tennis court"],
        google_included_type=None,
    ),
    "golf": _P(
        activity_id="golf", label="Golf",
        min_relevance=0.42, distance_sensitivity=0.7,
        requires_facility=True, allows_public_geography=False,
        strong_osm={"leisure=golf_course", "sport=golf", "golf=driving_range", "golf=course"},
        strong_google_types={"golf_course"},
        strong_name_kw={"golf", "driving range"},
        moderate_osm={"leisure=miniature_golf"},
        moderate_google_types={"country_club"},
        moderate_name_kw={"country club", "links"},
        generic_venue_google=frozenset(),
        google_text_queries=["golf course", "golf driving range"],
        google_included_type="golf_course",
    ),
}

SUPPORTED_ACTIVITY_IDS = tuple(ACTIVITY_PROFILES.keys())

# The activity IDs the current FYP scope REQUIRES to exist. The registry is
# intentionally OPEN, not count-locked: ACTIVITY_PROFILES is plain data and every
# consumer (validate_activity_id, evaluate_evidence, rank, _run_activity_search,
# the frontend picker) is driven entirely off it. A NEW activity is added by
# appending ONE ActivityProfile above — no engine, endpoint, ranking or adaptive
# 8/12/20 km change is needed.
#
# Minimum fields for a new profile (see ActivityProfile.__slots__):
#   * activity_id, label
#   * requires_facility: bool, allows_public_geography: bool
#   * at least one strong_* signal (strong_osm / strong_google_types /
#     strong_name_kw) — genuine, activity-specific evidence only
#   * a non-empty google_text_queries list
#   * when allows_public_geography is True: at least one public_geo_osm /
#     public_geo_google_types entry
# Then, if the sport has its own OSM `sport=` value(s), map them in _SPORT_OWNER
# and make sure they are not left in _FOREIGN_SPECIFIC_SPORTS.
#
# DEFERRED — recorded, NOT built in this phase: "Universal Activity Catalog —
# required after core/report closure". Intended direction: a searchable activity
# catalog with Recent / Suggested / Favorites; broader intent groups (Move, Play,
# Get Out, Create, Learn, Relax, Social, gaming, reading, studying, cooking...);
# custom user-entered activities; universal planning through Mo; map discovery
# only for activities that genuinely need a venue; and per-activity capability
# metadata instead of treating every activity as a sport or a map query. This
# deferred work must NOT block B9.1 or P10/P11.
MANDATORY_ACTIVITY_IDS = frozenset({
    "futsal", "football", "basketball", "volleyball", "badminton",
    "swimming", "running", "cycling", "tennis", "golf",
})

# Which activity "owns" a specific OSM sport / leisure value. Used to detect a
# CONFLICT: a place tagged for one specific sport, asked about a different one,
# with no positive evidence for the asked activity.
_SPORT_OWNER: dict[str, str] = {
    "futsal": "futsal", "soccer": "football", "football": "football",
    "basketball": "basketball", "badminton": "badminton", "swimming": "swimming",
    "athletics": "running", "running": "running", "cycling": "cycling",
    "tennis": "tennis", "golf": "golf",
    "volleyball": "volleyball", "beachvolleyball": "volleyball",
}
_SPECIFIC_LEISURE_OWNER: dict[str, str] = {
    "swimming_pool": "swimming", "golf_course": "golf",
}
# specific sports we recognise but that are nobody's CoreOS activity — a place
# that is ONLY one of these is a conflict for every asked activity. (volleyball /
# beachvolleyball moved OUT of this set in B9.1 — they are a real activity now.)
_FOREIGN_SPECIFIC_SPORTS = frozenset({
    "netball", "hockey", "cricket", "rugby", "baseball",
    "skateboard", "bmx", "climbing", "bowls", "petanque", "squash",
    "table_tennis", "handball",
})


# --------------------------------------------------------------------------- #
# validation
# --------------------------------------------------------------------------- #
def validate_activity_id(raw: Any) -> str:
    if not isinstance(raw, str):
        raise ValueError("activity_id is required.")
    key = raw.strip().lower()
    if key not in ACTIVITY_PROFILES:
        raise ValueError("activity_id is not a supported CoreOS activity.")
    return key


# --------------------------------------------------------------------------- #
# evidence
# --------------------------------------------------------------------------- #
EVIDENCE_TIERS = ("strong", "moderate", "weak", "none")


def _tokens(place: dict) -> tuple[set[str], set[str], set[str]]:
    """Return (raw provider_type tokens lowercased,
                osm key=value tokens,
                osm 'sport' values)."""
    raw = {str(t).strip().lower() for t in (place.get("provider_types") or []) if str(t).strip()}
    kv = {t for t in raw if "=" in t}
    sports: set[str] = set()
    for t in kv:
        k, _, v = t.partition("=")
        if k == "sport":
            sports |= {p.strip() for p in re.split(r"[;,]", v) if p.strip()}
    return raw, kv, sports


def _name_has(name_l: str, keywords) -> Optional[str]:
    for kw in keywords:
        k = kw.strip().lower()
        if not k:
            continue
        if " " in k:
            if k in name_l:
                return kw
        elif re.search(rf"(?<![a-z]){re.escape(k)}(?![a-z])", name_l):
            return kw
    return None


def _google_type_hit(raw_tokens: set[str], needles) -> Optional[str]:
    for n in needles:
        nl = n.strip().lower()
        if not nl:
            continue
        for t in raw_tokens:
            if "=" in t:
                continue
            if nl == t or nl in t:
                return n
    return None


def evaluate_evidence(profile: ActivityProfile, place: dict) -> dict:
    """Classify how strongly the provider evidence supports THIS activity.

    ``place`` MUST be a candidate returned by a place search for the SAME
    activity_id (its 5C ``activity_match`` is interpreted relative to that).

    Returns {tier, signals, conflicts, is_public_geo, has_specific_sport_tag,
             name_strong}.
    """
    raw, kv, sports = _tokens(place)
    name_l = str(place.get("name") or "").lower()
    match_5c = place.get("activity_match")  # "exact" | "broad" | None (5C tag exactness)

    signals: list[str] = []

    # sports this profile itself declares as a positive OSM signal
    profile_sport_values = {
        t.split("=", 1)[1] for t in (profile.strong_osm | profile.moderate_osm)
        if t.startswith("sport=")
    }

    # ---- STRONG evidence from real, activity-specific provider facts ----
    strong_real = False
    has_specific_sport_tag = False
    if kv & profile.strong_osm:
        strong_real = True
        signals += sorted(kv & profile.strong_osm)
        has_specific_sport_tag = any(
            t.startswith(("sport=", "golf=")) or t.split("=", 1)[-1] in _SPECIFIC_LEISURE_OWNER
            for t in (kv & profile.strong_osm)
        )
    gt = _google_type_hit(raw, profile.strong_google_types)
    if gt:
        strong_real = True
        signals.append(f"type:{gt}")
    name_strong_kw = _name_has(name_l, profile.strong_name_kw)
    name_strong = False
    if name_strong_kw and (not profile.requires_facility or _name_has(name_l, profile.facility_words)):
        strong_real = True
        name_strong = True
        signals.append(f"name:{name_strong_kw}")

    our_sport_present = (
        "multi" in sports
        or any(_SPORT_OWNER.get(sp) == profile.activity_id for sp in sports)
        or bool(sports & profile_sport_values)
    )

    # ---- CONFLICT: another specific sport, no support for the ask -------
    other_specific: set[str] = set()
    for sp in sports:
        if sp in profile_sport_values or _SPORT_OWNER.get(sp) == profile.activity_id:
            continue
        if _SPORT_OWNER.get(sp) or sp in _FOREIGN_SPECIFIC_SPORTS:
            other_specific.add(sp)
    for t in kv:
        k, _, v = t.partition("=")
        if k == "leisure" and v in _SPECIFIC_LEISURE_OWNER and _SPECIFIC_LEISURE_OWNER[v] != profile.activity_id:
            other_specific.add(v)
    conflicts: list[str] = []
    if other_specific and not strong_real and not our_sport_present:
        conflicts += sorted(other_specific)

    # 5C tag-exact match corroborates STRONG, but only when it is not
    # contradicted by a conflicting specific sport.
    strong = strong_real
    if match_5c == "exact" and not conflicts:
        strong = True
        if not strong_real:
            signals.append("osm:exact-tag")

    # ---- moderate evidence ----------------------------------------
    moderate = False
    if not strong:
        if kv & profile.moderate_osm:
            moderate = True
            signals += sorted(kv & profile.moderate_osm)
        gm = _google_type_hit(raw, profile.moderate_google_types)
        if gm and gm not in profile.generic_venue_google:
            moderate = True
            signals.append(f"type:{gm}")
        nm = _name_has(name_l, profile.moderate_name_kw)
        if nm:
            moderate = True
            signals.append(f"name:{nm}")
        if name_strong_kw and not name_strong:
            # sport keyword in the name but it doesn't read like a venue
            moderate = True
            signals.append(f"name:{name_strong_kw}")
        if "multi" in sports or "sport=multi" in kv:
            moderate = True
            signals.append("sport=multi")

    # ---- public geography ------------------------------------------
    is_public_geo = False
    if profile.allows_public_geography:
        pg_osm = kv & profile.public_geo_osm
        pg_g = _google_type_hit(raw, profile.public_geo_google_types)
        if pg_osm or pg_g:
            is_public_geo = True
            signals += sorted(pg_osm)
            if pg_g:
                signals.append(f"type:{pg_g}")

    # ---- weak: sport-related venue and nothing else --------------
    weak = False
    if not strong and not moderate and not is_public_geo:
        if _google_type_hit(raw, profile.generic_venue_google):
            weak = True
            signals.append("generic-sports-venue")
        elif (kv & {"leisure=sports_centre", "leisure=pitch", "leisure=sports_hall"}) and not conflicts:
            weak = True
            signals += sorted(kv & {"leisure=sports_centre", "leisure=pitch", "leisure=sports_hall"})

    if strong:
        tier = "strong"
    elif moderate:
        tier = "moderate"
    elif is_public_geo:
        tier = "moderate" if profile.allows_public_geography else "weak"
    elif weak:
        tier = "weak"
    else:
        tier = "none"

    # a hard conflict with no positive signal is never presented
    if conflicts and tier in ("weak", "none"):
        tier = "none"

    # de-dup signals, keep order
    seen: set[str] = set()
    sig = [s for s in signals if not (s in seen or seen.add(s))]

    return {
        "tier": tier,
        "signals": sig,
        "conflicts": sorted(set(conflicts)),
        "is_public_geo": is_public_geo,
        "has_specific_sport_tag": has_specific_sport_tag,
        "name_strong": name_strong,
    }


# --------------------------------------------------------------------------- #
# relevance score
# --------------------------------------------------------------------------- #
# Two numbers per candidate:
#   * evidence_score  — how well the PROVIDER EVIDENCE supports the activity.
#                       Decides the truthful class (strong/likely/possible).
#                       Distance / rating / popularity / name-wording play NO
#                       part here — evidence is evidence.
#   * score           — evidence_score minus a PRACTICAL-DISTANCE penalty.
#                       Decides the recommendation ORDER only.
#
# The distance penalty is deliberately non-linear: near a candidate barely
# matters, but the penalty grows with distance and gets large enough that a
# genuinely relevant nearby "likely" venue can overtake a far "strong" one —
# while it can never let WEAK evidence beat STRONG (strong floor 0.40 > weak
# 0.36). "How far is too far" is activity-aware (`distance_sensitivity`): people
# travel less for a run than for a round of golf.
_TIER_BASE = {"strong": 1.00, "moderate": 0.66, "weak": 0.36, "none": 0.0}

_DIST_FREE_M = 1_000.0           # the first km is "practically here" — no penalty
_DIST_FAR_M = 18_000.0          # by here the penalty is ~maxed
_DIST_MAX_PENALTY = 0.60        # < (strong 1.00 - weak 0.36): weak can never win
_DIST_GAMMA = 0.85


def _distance_penalty(distance_m: Any, confident: bool, sensitivity: float = 1.0) -> float:
    if not confident:
        return 0.0
    if not isinstance(distance_m, (int, float)) or isinstance(distance_m, bool):
        return 0.0
    d = float(distance_m)
    if not math.isfinite(d) or d <= 0:
        return 0.0
    x = (d - _DIST_FREE_M) / (_DIST_FAR_M - _DIST_FREE_M)
    x = 0.0 if x < 0.0 else (1.0 if x > 1.0 else x)
    p = _DIST_MAX_PENALTY * (x ** _DIST_GAMMA) * max(0.1, sensitivity)
    return round(min(_DIST_MAX_PENALTY, p), 4)


def evidence_score(profile: ActivityProfile, ev: dict) -> float:
    """Evidence-only relevance. NO distance, NO rating, NO popularity, NO name
    wording. Used to decide the truthful class."""
    s = _TIER_BASE.get(ev["tier"], 0.0)
    if s <= 0.0:
        return 0.0
    # the only bonuses: things that ARE materially stronger activity evidence.
    if ev["has_specific_sport_tag"]:           # OSM sport=<activity> / leisure=<specific>
        s += 0.05
    if ev["is_public_geo"] and profile.allows_public_geography:
        s += 0.04                              # public geography IS this activity's evidence
    if ev["conflicts"]:
        s -= 0.50
    return round(max(0.0, s), 4)


def score_candidate(
    profile: ActivityProfile, place: dict, ev: dict, *,
    distance_confident: bool = True,
) -> float:
    """Ordering score = evidence_score - practical-distance penalty."""
    es = evidence_score(profile, ev)
    if es <= 0.0:
        return 0.0
    pen = _distance_penalty(
        place.get("distance_m"), distance_confident,
        getattr(profile, "distance_sensitivity", None) or 1.0,
    )
    return round(max(0.0, es - pen), 4)


# --------------------------------------------------------------------------- #
# truthful classification + reason
# --------------------------------------------------------------------------- #
RELEVANCE_CLASSES = ("strong", "likely", "possible")


def classify(profile: ActivityProfile, ev: dict) -> Optional[str]:
    """The truthful evidence class, or None to drop entirely. Distance is NOT a
    factor here — a real-but-far venue is still "Confirmed"; it just ranks
    lower. A weak signal that is also contradicted by a conflicting specific
    sport (evidence_score <= 0) is dropped."""
    if ev["tier"] == "none":
        return None
    if evidence_score(profile, ev) <= 0.0:
        return None
    return {"strong": "strong", "moderate": "likely", "weak": "possible"}[ev["tier"]]


def reason_for(profile: ActivityProfile, place: dict, ev: dict, cls: str) -> str:
    """A short, factual, evidence-grounded explanation. NOT premium copy, and it
    never states a fact the provider did not supply."""
    label = profile.label.lower()
    prov = "Google" if place.get("provider") == "google" else "OpenStreetMap"
    if cls == "strong":
        if any(s.startswith(("sport=", "osm:exact", "leisure=swimming_pool", "leisure=golf_course")) for s in ev["signals"]):
            return f"Tagged specifically for {label} in {prov}."
        if any(s.startswith("type:") for s in ev["signals"]):
            return f"Listed as a {label} venue on {prov}."
        if any(s.startswith("name:") for s in ev["signals"]):
            return f"Named as a {label} venue; identity from {prov}."
        return f"{prov} evidence confirms {label}."
    if cls == "likely":
        if ev["is_public_geo"]:
            return f"Open public space that supports {label}; no dedicated facility is listed."
        return f"A sports venue whose listing points to {label}, though it isn't tagged specifically."
    # possible
    if ev["is_public_geo"]:
        return f"Public space that may be usable for {label}; no {label} facility is listed."
    return f"A general sports venue — {label} is not confirmed by the listing."


# --------------------------------------------------------------------------- #
# rank  — a recommendation set, not a raw directory
# --------------------------------------------------------------------------- #
RECOMMENDATION_LIMIT = 12       # a useful shortlist, NOT "always 20"
POSSIBLE_KEEP_MAX = 4          # at most this many weak/"possible" candidates ...
MIN_GOOD_TO_DROP_POSSIBLE = 3  # ... and NONE of them once we have this many strong/likely
MIN_USEFUL_SCORE = 0.15       # a far-away, thin-evidence result is not a useful
                             # recommendation — drop it (a far but CONFIRMED
                             # venue still clears this: 1.00 - 0.60 = 0.40).


def rank(activity_id: str, places: list[dict], *, distance_confident: bool = True) -> list[dict]:
    """Evaluate every candidate, attach an ``activity_relevance`` block, and
    return a RECOMMENDATION SET ordered by (score desc, distance asc, place_id):

      * class comes from evidence alone (strong/likely/possible);
      * order comes from evidence minus a practical-distance penalty, so a
        genuinely relevant nearby venue can outrank a far confirmed one;
      * "possible" (weak-evidence) candidates are dropped entirely when there
        are >= MIN_GOOD_TO_DROP_POSSIBLE strong/likely results, and otherwise
        capped at POSSIBLE_KEEP_MAX — CoreOS does not pad the list;
      * the result is capped at RECOMMENDATION_LIMIT.

    Deterministic and reproducible. Input dicts are not mutated (shallow copies
    with the extra block are returned).
    """
    profile = ACTIVITY_PROFILES[activity_id]
    scored: list[tuple[float, float, str, str, dict]] = []
    for p in places:
        if not isinstance(p, dict):
            continue
        ev = evaluate_evidence(profile, p)
        cls = classify(profile, ev)
        if cls is None:
            continue
        score = score_candidate(profile, p, ev, distance_confident=distance_confident)
        if score < MIN_USEFUL_SCORE:
            continue
        out = dict(p)
        out["activity_relevance"] = {
            "class": cls,
            "score": score,
            "evidence_score": evidence_score(profile, ev),
            "tier": ev["tier"],
            "reason": reason_for(profile, p, ev, cls),
            "signals": ev["signals"][:6],
            "conflicts": ev["conflicts"],
            "is_public_geography": ev["is_public_geo"],
        }
        d = out.get("distance_m")
        dist_key = float(d) if isinstance(d, (int, float)) and not isinstance(d, bool) and math.isfinite(float(d)) else math.inf
        scored.append((-score, dist_key, str(out.get("place_id") or ""), cls, out))

    scored.sort(key=lambda t: (t[0], t[1], t[2]))

    good = [t for t in scored if t[3] in ("strong", "likely")]
    poss = [t for t in scored if t[3] == "possible"]
    if len(good) >= MIN_GOOD_TO_DROP_POSSIBLE:
        poss = []
    else:
        poss = poss[:POSSIBLE_KEEP_MAX]
    keep_ids = {id(t[4]) for t in good} | {id(t[4]) for t in poss}
    ordered = [t[4] for t in scored if id(t[4]) in keep_ids]
    return ordered[:RECOMMENDATION_LIMIT]
