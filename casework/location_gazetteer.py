"""NES district/municipality lookup: a place as written in a document to its coded IRI."""

import re
from dataclasses import dataclass

from casework.common.grounding import (has_word, location_quote_problem, normalise_for_match,
                                       verbatim_quote)

_SPLIT_SUB_METRO = re.compile(r"उप[\s\-‐–—]*महानगर")
_SPLIT_PALIKA = re.compile(r"(गाउं|नगर)[\s\-‐–—]+पालिका")
#: काठमाडौं as NES and the orders spell it: काठमाण्डौ, काठमान्डौ, काठमाडौ, काठमाण्डाै.
_KATHMANDU = re.compile(r"काठमा(?:ण्ड|न्ड|ड)(?:ौ|ाै)ं?")

#: Abbreviation expansions, longest key first so a shorter one never fires inside a longer one.
_ABBREVIATIONS = (
    ("ल.पु.उ.म.न.पा.", "ललितपुर उपमहानगरपालिका"),
    ("का.म.न.पा.", "काठमाडौं महानगरपालिका"),
    ("उ.म.न.पा.", "उपमहानगरपालिका"),
    ("म.न.पा.", "महानगरपालिका"),
    ("गा.पा.", "गाउंपालिका"),
    ("का.जि.", "काठमाडौं जिल्ला"),
    ("न.पा.", "नगरपालिका"),
)

DISTRICT_VARIANTS = {
    "kathmandu": ("काठमाडौं", "काठमाडौ", "काठमाण्डौ", "काठमाण्डौं", "काठमाण्डाै"),
    "kanchanpur": ("कन्चनपुर", "कंचनपुर"),
    "ilam": ("ईलाम",),
    "lamjung": ("लम्जुङ", "लम्जुङ्ग"),
    "syangja": ("स्याङ्जा", "स्याङ्‌जा"),
    "tanahu": ("तनहुं", "तनहु"),
    "banke": ("बांके",),
    "panchthar": ("पांचथर",),
    "terhathum": ("तेर्हथुम",),
    "sankhuwasabha": ("सङ्खुवासभा",),
    "okhaldhunga": ("ओखलढुंगा", "ओखलढुङ"),
    "pyuthan": ("प्युठान",),
    "rupandehi": ("रूपन्देही",),
    "kapilbastu": ("कपिलबस्तु",),
    "kavrepalanchok": ("काभ्रे", "काभ्रेपलान्चोक"),
    "arghakhanchi": ("अर्घाखाची",),
    "solukhumbu": ("सोलुखुम्बू",),
    "dadeldhura": ("डडेलधुरा",),
    "siraha": ("सिराहा",),
    "dang": ("दाङ्ग", "दाङदेउखुरी"),
    "nawalparasi-east": ("नवलपुर", "नवलपरासी पूर्व"),
    "nawalparasi-west": ("परासी", "नवलपरासी पश्चिम"),
    "rukum-east": ("रुकुम (पूर्व)", "रुकुम पूर्व", "पूर्वी रुकुम"),
    "rukum-west": ("रुकुम पश्चिम", "पश्चिमी रुकुम"),
}
AMBIGUOUS_FORMS = {"नवलपरासी": ("nawalparasi-east", "nawalparasi-west"),
                   "रुकुम": ("rukum-east", "rukum-west")}


@dataclass(frozen=True)
class PlaceDecision:
    district: str | None
    localunit: str | None
    reason: str


#: An answer's `notes` field is truncated to this many characters on a bind.
NOTES_MAX_CHARS = 200


@dataclass(frozen=True)
class LocationBind:
    nes_id: str
    notes: str
    place: str
    evidence: str
    via: str


def place_key(text: str) -> str:
    """Fold a place name to the form the gazetteer indexes and matches on."""
    t = normalise_for_match(text).lower()
    for abbr, full in _ABBREVIATIONS:
        t = t.replace(abbr, full)
    t = _KATHMANDU.sub("काठमाडौं", t)
    # Orders split the local-level word (`उप-महानगरपालिका`, `नगर पालिका`); NES never does.
    t = t.replace("महाननगर", "महानगर")
    t = t.replace("गांउ", "गाउं")  # before the split join, which only knows गाउं
    t = _SPLIT_SUB_METRO.sub("उपमहानगर", t)
    t = _SPLIT_PALIKA.sub(r"\1पालिका", t)
    # Lalitpur became a metropolitan city in 2017; older orders still say उपमहानगरपालिका.
    t = t.replace("ललितपुर उपमहानगरपालिका", "ललितपुर महानगरपालिका")
    t = re.sub(r"^जिल्ला ", "", t)
    t = re.sub(r"( जिल्ला| district| जि\.)$", "", t)
    return t


def _alt_names(alt) -> set[str]:
    """`alternateName` as a flat set of strings, whether it is a list or a `{lang: [..]}` dict."""
    if not alt:
        return set()
    if isinstance(alt, str):
        return {alt}
    if isinstance(alt, dict):
        result: set[str] = set()
        for vals in alt.values():
            if isinstance(vals, str):
                result.add(vals)
            elif isinstance(vals, list):
                result.update(v for v in vals if isinstance(v, str))
        return result
    return set(alt) if alt else set()


def _district_stem(iri: str) -> str:
    """The IRI segment after `/district/`, minus its `-npNNNN` suffix."""
    return re.sub(r"-np\d+$", "", iri.rsplit("/", 1)[-1])


class Gazetteer:
    """A place as written in a Nepali document, resolved to its NES district/municipality IRI."""

    def __init__(self, districts: list, localunits: list, variants: dict = DISTRICT_VARIANTS):
        self.variants = variants
        self._by_stem = {_district_stem(d["@id"]): d["@id"] for d in districts}
        self._ambiguous = {
            place_key(word): tuple(self._by_stem[s] for s in stems if s in self._by_stem)
            for word, stems in AMBIGUOUS_FORMS.items()
        }
        self._district_forms: dict[str, str] = {}
        for d in districts:
            stem = _district_stem(d["@id"])
            forms = _alt_names(d.get("alternateName")) | set(self.variants.get(stem, ()))
            name = d.get("name") or {}
            forms |= {name[lang] for lang in ("ne", "en") if name.get(lang)}
            for form in forms:
                key = place_key(form)
                if key and key not in self._ambiguous:
                    self._district_forms[key] = d["@id"]
        self._unit_forms: dict[str, list] = {}
        self._unit_parent: dict[str, str | None] = {}
        for u in localunits:
            parent = (u.get("containedInPlace") or {}).get("@id")
            self._unit_parent[u["@id"]] = parent
            name = u.get("name") or {}
            forms = _alt_names(u.get("alternateName")) | {name[lang] for lang in ("ne", "en") if name.get(lang)}
            # Keys, not forms: `खजुरा गाउँपालिका` and `खजुरा गाउंपालिका` are one entry, not two namesakes.
            for key in {place_key(form) for form in forms} - {""}:
                self._unit_forms.setdefault(key, []).append((u["@id"], parent))
        self._names: dict[str, set] = {}
        for key, iri in self._district_forms.items():
            self._names.setdefault(iri, set()).add(key)
        for key, entries in self._unit_forms.items():
            for iri, _parent in entries:
                self._names.setdefault(iri, set()).add(key)

    def names_for(self, *iris) -> set:
        """Every key the gazetteer matches for these district or localunit IRIs."""
        return set().union(*(self._names.get(iri, set()) for iri in iris if iri))

    def district_for(self, name: str) -> str | None:
        key = place_key(name)
        if key in self._ambiguous:
            return None
        return self._district_forms.get(key)

    def districts_in(self, text: str) -> set:
        normalized = place_key(text)
        found = {iri for key, iri in self._district_forms.items() if has_word(key, normalized)}
        for key, iris in self._ambiguous.items():
            # A disambiguated compound (e.g. "रुकुम पश्चिम") also contains the bare
            # ambiguous word as a word-start substring. Only add the ambiguous pair
            # when neither half was already found through that more specific form --
            # otherwise a self-disambiguating text would still yield both halves.
            if has_word(key, normalized) and not (set(iris) & found):
                found.update(iris)
        return found

    def localunits_in(self, text: str) -> list:
        normalized = place_key(text)
        found = []
        for key, entries in self._unit_forms.items():
            if has_word(key, normalized):
                found.extend(e for e in entries if e not in found)
        return found

    def parent_district(self, localunit_iri: str) -> str | None:
        """The district IRI containing a localunit IRI, or None if the unit is unknown."""
        return self._unit_parent.get(localunit_iri)

    def redirect(self, nes_id: str, name: str) -> str | None:
        if "/location/district/" in nes_id or "/location/localunit/" in nes_id:
            return nes_id
        district = self.district_for(name)
        if district is not None:
            return district
        units = self._unit_forms.get(place_key(name), [])
        return units[0][0] if len(units) == 1 else None

    def resolve(self, place: str, district_claim: str, evidence: str = "") -> PlaceDecision:
        """Only the place and the quote ground a district: the claim may pick among them, never add one."""
        claim_iri = self.district_for(district_claim)
        named = self.districts_in(place)
        units = self.localunits_in(place)
        parents = {p for _, p in units if p is not None}
        if claim_iri is None:
            if named and parents and not named & parents:
                return PlaceDecision(None, None, "the named district does not contain the named municipality")
            pool = (named & parents) or parents or named
            if len(pool) != 1:
                return PlaceDecision(None, None, "no single district in the place as written")
            claim_iri = next(iter(pool))
        elif claim_iri not in named | parents | (quoted := self.districts_in(evidence)):
            return PlaceDecision(None, None, "neither the place nor its quote names the claimed district")
        elif named and claim_iri not in named:
            return PlaceDecision(None, None, "the named district does not contain the named municipality")
        elif claim_iri not in named and len(parents) > 1 and claim_iri not in quoted:
            return PlaceDecision(None, None, "the district that picks the municipality is not in the quote")
        matching = [u for u, p in units if p == claim_iri]
        if len(matching) == 1:
            return PlaceDecision(claim_iri, matching[0], "")
        if len(matching) > 1:
            return PlaceDecision(claim_iri, None, "several named municipalities in the district; not bound")
        if units and claim_iri not in named:
            # Only the quote backs the claim, and the place's own municipality says otherwise.
            return PlaceDecision(None, None, "the named municipality is in another district than the claim")
        if units:
            return PlaceDecision(claim_iri, None, "a named municipality sits in another district; not bound")
        return PlaceDecision(claim_iri, None, "")

    def has_no_match(self, place: str, district_claim: str) -> bool:
        """True when neither the place nor the claim names any district or municipality."""
        return not (self.districts_in(place) or self.localunits_in(place)
                    or self.districts_in(district_claim))

    def missing_variant_keys(self) -> list:
        return [stem for stem in self.variants if stem not in self._by_stem]


def _page_all(api, entity_prefix: str, limit: int = 100) -> list:
    """Every entity under `entity_prefix`, paged until a page is empty or offset reaches total."""
    rows, offset = [], 0
    while True:
        page = api.get("/entities", {"entity_prefix": entity_prefix, "limit": limit, "offset": offset})
        entities = page.get("entities") or []
        if not entities:
            break
        rows.extend(entities)
        offset += limit
        if offset >= (page.get("total") or 0):
            break
    return rows


def load_gazetteer(api) -> Gazetteer:
    """Build a `Gazetteer` from NES, refusing to proceed if NES no longer matches our assumptions."""
    districts = _page_all(api, "location/district")
    if len(districts) != 77:
        raise RuntimeError(f"expected 77 NES districts, got {len(districts)}")
    localunits = _page_all(api, "location/localunit")
    gaz = Gazetteer(districts, localunits)
    missing = gaz.missing_variant_keys()
    if missing:
        raise RuntimeError(f"DISTRICT_VARIANTS stems missing from NES: {missing}")
    return gaz


def _is_coded(nes_id: str) -> bool:
    return "/location/district/" in nes_id or "/location/localunit/" in nes_id


def _redirect_match(gaz: Gazetteer, candidates, query_key: str) -> tuple[str | None, str]:
    """The one IRI the un-coded exact-title twins redirect to, or `(None, why)`.

    A coded candidate is skipped: the gazetteer already indexes every coded
    place, so one it did not match is not the place as written.
    """
    resolved: set[str] = set()
    for candidate in candidates:
        nes_id = (candidate.get("id") or "").strip()
        if _is_coded(nes_id):
            continue
        title = candidate.get("title") or {}
        forms = [f for f in (title.get("ne"), title.get("en")) if f]
        if not any(place_key(form) == query_key for form in forms):
            continue
        iri = next((i for i in (gaz.redirect(nes_id, form) for form in forms) if i), None)
        if iri:
            resolved.add(iri)
    if not resolved:
        return None, "NES match is not a district or municipality"
    districts = {iri if "/location/district/" in iri else gaz.parent_district(iri)
                 for iri in resolved}
    if len(districts) > 1:
        return None, "NES twins disagree"
    if len(resolved) > 1:
        return next(iter(districts)), ""
    return next(iter(resolved)), ""


#: `stage` on a rejected row: the quote failed grounding, or the grounded place did not resolve.
GROUNDING, RESOLUTION = "grounding", "resolution"


def resolve_locations(
    api, gaz: Gazetteer, answers: list[dict], source_text: str, caption_end: int
) -> tuple[list[LocationBind], list[dict]]:
    """Grounded location answers to district/municipality `LocationBind`s, deduped on `nes_id`.

    Each rejected row carries `stage`: `GROUNDING` rows are dropped, `RESOLUTION` rows go to
    review -- including a named municipality refused beside the district that did bind.
    """
    binds: list[LocationBind] = []
    rejected: list[dict] = []
    seen: set[str] = set()

    def emit(nes_id: str, notes: str, place: str, evidence: str, via: str) -> None:
        if nes_id not in seen:
            seen.add(nes_id)
            binds.append(LocationBind(nes_id, notes, place, evidence, via))

    for answer in answers:
        place = answer.get("place_as_written") or ""
        district_claim = answer.get("district") or ""
        evidence = answer.get("evidence") or ""
        notes = (answer.get("notes") or "")[:NOTES_MAX_CHARS]
        query = place or district_claim

        def reject(reason: str, stage: str = RESOLUTION, **extra: str) -> None:
            rejected.append({"place": place, "district": district_claim, "evidence": evidence,
                             "reason": reason, "stage": stage, **extra})

        # Only the verbatim part of the quote grounds anything, so it is also what gets
        # recorded; the place counts as in it when the gazetteer's own name for what it
        # resolved to is, not only the phrase the model assembled (077-CR-0004).
        verified = verbatim_quote(evidence, source_text)
        decision = gaz.resolve(place, district_claim, verified or evidence)
        names = gaz.names_for(decision.district, decision.localunit) if decision.district else ()
        problem = location_quote_problem(evidence, query, source_text, caption_end,
                                         names=names, fold=place_key)
        if problem:
            reject(problem, GROUNDING)
            continue
        evidence = verified

        if decision.district:
            emit(decision.district, notes, place, evidence, "gazetteer")
            quoted = place_key(evidence)
            if decision.localunit and any(has_word(key, quoted)
                                          for key in gaz.names_for(decision.localunit)):
                emit(decision.localunit, notes, place, evidence, "gazetteer")
            elif decision.localunit:
                reject("the municipality is not in the quote; not bound")
            elif decision.reason:
                reject(decision.reason)
            continue

        if not gaz.has_no_match(place, district_claim):
            reject(decision.reason)
            continue
        resolved, why = _redirect_match(gaz, api.search_entities(query), place_key(query))
        if resolved is None:
            reject(why, gazetteer_reason=decision.reason)
            continue
        if "/location/district/" in resolved:
            emit(resolved, notes, place, evidence, "nes-redirect")
        else:
            parent = gaz.parent_district(resolved)
            if parent is None:
                reject("municipality has no known district in the gazetteer",
                       gazetteer_reason=decision.reason)
                continue
            emit(parent, notes, place, evidence, "nes-redirect")
            emit(resolved, notes, place, evidence, "nes-redirect")

    return binds, rejected
