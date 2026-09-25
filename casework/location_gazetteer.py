"""NES district/municipality lookup: a place as written in a document to its coded IRI."""

import re
from dataclasses import dataclass

from casework.common.grounding import location_quote_problem, normalise_for_match

#: Devanagari block, used to require a form match starts at a word boundary.
_DEVANAGARI = "ऀ-ॿ"

#: Abbreviation expansions, longest key first so a shorter one never fires inside a longer one.
_ABBREVIATIONS = (
    ("उ.म.न.पा.", "उपमहानगरपालिका"),
    ("म.न.पा.", "महानगरपालिका"),
    ("गा.पा.", "गाउंपालिका"),
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
    "dang": ("दाङ्ग",),
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
    t = t.replace("गांउ", "गाउं")
    t = re.sub(r"^जिल्ला ", "", t)
    t = re.sub(r"( जिल्ला| district| जि\.)$", "", t)
    return t


def _matches(key: str, text: str) -> bool:
    """Whether `key` occurs in `text` at a position not preceded by a Devanagari letter."""
    return bool(key) and re.search(rf"(?<![{_DEVANAGARI}])" + re.escape(key), text) is not None


def _alt_names(alt) -> set[str]:
    """`alternateName` as a flat set of strings, whether it is a list or a `{lang: [..]}` dict."""
    if not alt:
        return set()
    if isinstance(alt, dict):
        return {v for vals in alt.values() for v in vals}
    return set(alt)


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
            for form in forms:
                key = place_key(form)
                if key:
                    self._unit_forms.setdefault(key, []).append((u["@id"], parent))

    def district_for(self, name: str) -> str | None:
        key = place_key(name)
        if key in self._ambiguous:
            return None
        return self._district_forms.get(key)

    def districts_in(self, text: str) -> set:
        normalized = place_key(text)
        found = {iri for key, iri in self._district_forms.items() if _matches(key, normalized)}
        for key, iris in self._ambiguous.items():
            # A disambiguated compound (e.g. "रुकुम पश्चिम") also contains the bare
            # ambiguous word as a word-start substring. Only add the ambiguous pair
            # when neither half was already found through that more specific form --
            # otherwise a self-disambiguating text would still yield both halves.
            if _matches(key, normalized) and not (set(iris) & found):
                found.update(iris)
        return found

    def localunits_in(self, text: str) -> list:
        normalized = place_key(text)
        found = []
        for key, entries in self._unit_forms.items():
            if _matches(key, normalized):
                found.extend(entries)
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

    def resolve(self, place: str, district_claim: str) -> PlaceDecision:
        claim_iri = self.district_for(district_claim)
        named = self.districts_in(place)
        units = self.localunits_in(place)
        parents = {p for _, p in units}
        if claim_iri is None:
            pool = (named & parents) or parents or named
            if len(pool) != 1:
                return PlaceDecision(None, None, "no single district in the place as written")
            claim_iri = next(iter(pool))
        if claim_iri not in named | parents:
            return PlaceDecision(None, None, "the place as written does not name the claimed district")
        matching = [u for u, p in units if p == claim_iri]
        if len(matching) == 1:
            return PlaceDecision(claim_iri, matching[0], "")
        if len(matching) > 1:
            return PlaceDecision(claim_iri, None, "several named municipalities in the district; not bound")
        if units:
            return PlaceDecision(claim_iri, None, "a named municipality sits in another district; not bound")
        return PlaceDecision(claim_iri, None, "")

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


def _redirect_match(gaz: Gazetteer, candidates, query_key: str) -> str | None:
    """The first district/localunit IRI a candidate's title redirects to, or None."""
    for candidate in candidates:
        title = candidate.get("title") or {}
        forms = [f for f in (title.get("ne"), title.get("en")) if f]
        if not any(place_key(form) == query_key for form in forms):
            continue
        nes_id = (candidate.get("id") or "").strip()
        for form in forms:
            iri = gaz.redirect(nes_id, form)
            if iri:
                return iri
    return None


def resolve_locations(
    api, gaz: Gazetteer, answers: list[dict], source_text: str, caption_end: int
) -> tuple[list[LocationBind], list[dict]]:
    """Grounded location answers to district/municipality `LocationBind`s, deduped on `nes_id`."""
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

        problem = location_quote_problem(evidence, query, source_text, caption_end)
        if problem:
            rejected.append({"place": place, "district": district_claim, "evidence": evidence, "reason": problem})
            continue

        decision = gaz.resolve(place, district_claim)
        if decision.district:
            emit(decision.district, notes, place, evidence, "gazetteer")
            if decision.localunit:
                emit(decision.localunit, notes, place, evidence, "gazetteer")
            continue

        resolved = _redirect_match(gaz, api.search_entities(query), place_key(query))
        if resolved is None:
            rejected.append({"place": place, "district": district_claim, "evidence": evidence,
                              "reason": "NES match is not a district or municipality"})
            continue
        if "/location/district/" in resolved:
            emit(resolved, notes, place, evidence, "nes-redirect")
        else:
            parent = gaz.parent_district(resolved)
            if parent is None:
                rejected.append({"place": place, "district": district_claim, "evidence": evidence,
                                  "reason": "municipality has no known district in the gazetteer"})
                continue
            emit(parent, notes, place, evidence, "nes-redirect")
            emit(resolved, notes, place, evidence, "nes-redirect")

    return binds, rejected
