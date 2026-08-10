#!/usr/bin/env python
"""Tell two same-named court defendants apart, from their press releases.

A court record carries a defendant's NAME and nothing else usable: 0 of the
1,414 defendant rows in the FY078/079 census carry an address, and none carry
an `nes_id`. So `enrich_court_record.held_names` holds any name appearing on
two or more cases in a run instead of guessing whether that is one person or
two -- binding one entity to both would state "this man did both", and binding
two would duplicate one man.

The press release is where the identifying detail lives. CIAA writes the
district, the local unit and the office into its own title:

    जिल्ला झापा, दमक नगरपालिकाका वातावरण अधिकृत ज्ञानेन्द्र चौधरी, ...
    जिल्ला रौतहट, फतुवा विजयपुर नगरपालिका ... बडा अध्यक्षहरु ... समेत २१ जनाउपर

Those two lines settle that particular collision on their own -- an elected
ward chair in Rautahat is not a contracted environment officer in Jhapa.

ONE MODEL CALL PER HELD NAME. Not per case, and not per pair: a name on three
cases is one call comparing all three. On the measured corpus that is 80 calls
for ~1,414 defendant rows, because only 80 names land on more than one case.

NO EXTRA HTTP. Every field of a `CaseIdentity` comes out of the case payload
`enrich_court_record`'s pass 1 has already read. The binder runs at a measured
8.8 requests per case against a 5,000/hour ceiling; a comparison that fetched
its own sources would spend that headroom.

WHAT THE VERDICT MAY DO is decided by `HeldVerdict.is_actionable`, not here.
`unclear` is a first-class answer, and the honest one whenever the cards do not
carry a distinguishing fact -- a case with no bound press release cannot be
compared at all, and is refused before the model is ever called.
"""

import re
from dataclasses import dataclass, field

from casework.entity_resolver import normalise_name

#: Characters of `description` kept either side of a name mention.
MENTION_WINDOW = 220

#: Mentions kept per name per case. The first few carry the role; a long
#: judgment repeats the name in every procedural paragraph after that.
MAX_MENTIONS = 3

#: Minimum `evidence` length for an actionable verdict. A SHAPE floor, not a
#: measured one: `llm.invoke.salvage_json` repairs a reply truncated at
#: `max_tokens` by closing the open string, so an overflowing call yields a
#: verdict whose evidence stops mid-sentence. Non-blank, and worthless as the
#: audit record of why two people were merged or split.
EVIDENCE_FLOOR = 40

#: Trailing NES disambiguation code on a location slug (`jhapa-np0104`).
_LOCATION_CODE = re.compile(r"-(?:np|pa)[0-9a-z]*[0-9][0-9a-z]*$")


@dataclass(frozen=True)
class CaseIdentity:
    """What one case says about the people it accuses. Built without HTTP.

    `mentions` is keyed by `normalise_name` of the defendant, matching the
    `held` mapping's own keys, and is populated in pass 1 while that case's
    court record is in hand -- which is the only moment the binder knows a
    case's defendant names without a second read.
    """
    slug: str
    court_cases: tuple = ()
    title: str = ""
    press_titles: tuple = ()
    districts: tuple = ()
    mentions: dict = field(default_factory=dict)

    def carries_identity(self, key):
        """Whether this card says anything that could tell `key` from a namesake.

        The case title is deliberately NOT counted. Every title in this corpus
        is generated to the same template (`<lead defendant> समेत <N>`), so it
        separates nothing -- a card carrying only a title would send the model
        two strings that differ merely in the number of co-defendants and
        invite a verdict from the shared name alone.
        """
        return bool(self.press_titles or self.districts or self.mentions.get(key))


def _windows(text, name, *, window=MENTION_WINDOW, limit=MAX_MENTIONS):
    """Up to `limit` excerpts of `text` around `name`, non-overlapping.

    Matched on the raw name rather than a normalised form: `description` is
    prose, so there is no normalised copy of it to search, and the portal's
    defendant spelling is what the prose uses. A name that does not appear
    verbatim simply yields no excerpt, which `carries_identity` then reports as
    "this card cannot tell them apart" -- the cautious answer.
    """
    if not text or not name:
        return ()
    found, at = [], 0
    while len(found) < limit:
        hit = text.find(name, at)
        if hit < 0:
            break
        found.append(text[max(0, hit - window):hit + len(name) + window])
        # Past the END of this window, not past the match: consecutive
        # mentions one sentence apart would otherwise yield near-identical
        # excerpts and spend the whole budget on one paragraph.
        at = hit + len(name) + window
    return tuple(found)


def _press_titles(case_detail):
    """The `display_name` of every press release bound to the case.

    This is the highest-signal line available and it costs nothing: CIAA's own
    title names the district, the local unit and the office.
    """
    titles = []
    for entry in case_detail.get("evidence") or ():
        material = (entry or {}).get("material") or {}
        if material.get("material_type") != "press_release":
            continue
        name = (material.get("display_name") or "").strip()
        if name:
            titles.append(name)
    return tuple(dict.fromkeys(titles))


def _districts(case_detail):
    """District names from the case's bound location entities.

    Read off the IRI tail with the NES disambiguation code stripped, so
    `location/district/jhapa-np0104` and a bare `location/district/jhapa` --
    both of which sit on one real case -- collapse to the same `jhapa` instead
    of counting as two districts and blocking `discriminator`.
    """
    names = []
    for bind in case_detail.get("entities") or ():
        nes_id = (bind or {}).get("nes_id") or ""
        if "/district/" not in nes_id:
            continue
        tail = nes_id.rstrip("/").rsplit("/", 1)[-1]
        tail = _LOCATION_CODE.sub("", tail)
        if tail:
            names.append(tail)
    return tuple(dict.fromkeys(names))


def case_identity(case_detail, names, *, court_cases=()):
    """Build one case's `CaseIdentity` for `names`. No HTTP, no model call."""
    description = case_detail.get("description") or ""
    mentions = {}
    for name in names:
        key = normalise_name(name)
        if not key or key in mentions:
            continue
        found = _windows(description, name)
        if found:
            mentions[key] = found
    return CaseIdentity(
        slug=case_detail.get("slug") or "",
        court_cases=tuple(court_cases),
        title=(case_detail.get("title") or "").strip(),
        press_titles=_press_titles(case_detail),
        districts=_districts(case_detail),
        mentions=mentions)


def discriminator(card):
    """A slug fragment separating this case's person from their namesake.

    DERIVED, never taken from the model's reply. This fragment lands in a
    permanent public entity IRI, so it is read off the case itself: the one
    district the case is bound to, else the court case number. Both are facts
    the case already records.

    Falls back whenever the district is not unique -- `079-CR-0071` is bound to
    both Jhapa and Morang, because the river it concerns is the border between
    them, and neither is "the" district of the accused.
    """
    if len(card.districts) == 1:
        return card.districts[0]
    for number in card.court_cases:
        if number:
            return normalise_name(number).replace(" ", "-")
    return ""


@dataclass(frozen=True)
class HeldVerdict:
    """The model's answer for one held name.

    `failed` marks "the model did not answer" as distinct from "the model could
    not tell". Both leave the name held; only one means the run is degraded.
    Without the distinction a provider outage produces a full set of `unclear`
    verdicts, which for this stage is an entirely ordinary-looking result.
    """
    verdict: str
    confidence: str = ""
    evidence: str = ""
    per_case: dict = field(default_factory=dict)
    failed: bool = False

    @property
    def is_actionable(self):
        """Whether the binder may act on this instead of holding the name.

        Every condition is load-bearing:

        `high` is the bar because both actions are irreversible in public. A
        `same` verdict merges two people's cases onto one entity; a `different`
        verdict publishes two entities for what may be one man. Neither is a
        coin-flip decision, and `medium` is the model saying it is guessing.

        `evidence` must clear `EVIDENCE_FLOOR` because it IS the audit record.
        A verdict whose reasoning was truncated to `"रौतहट र झापा"` cannot be
        reviewed, and this is the only place that reasoning is kept.

        `per_case` must name EVERY case, not just some: the operator reading
        the held file needs the role this verdict assigns to each side, and a
        `different` verdict that describes only one of three cases has not
        actually separated the other two.
        """
        if self.failed or self.verdict not in ("same", "different"):
            return False
        if self.confidence != "high" or len(self.evidence.strip()) < EVIDENCE_FLOOR:
            return False
        return bool(self.per_case)

    def covers(self, slugs):
        """Whether `per_case` describes every case sharing the name."""
        return set(self.per_case) >= set(slugs)


SYSTEM = """You compare defendants named in Nepali anti-corruption filings.

You are given ONE personal name and the cases that name it as a defendant
(प्रतिवादी). Decide whether those cases accuse the SAME human being or
DIFFERENT people who share a name.

Weigh only identifying facts:
  - district (जिल्ला) and local unit (नगरपालिका/गाउँपालिका)
  - the office or post held (पद) -- an elected वडा अध्यक्ष is not a contracted
    अधिकृत, and neither is a ठेकेदार
  - dates of service, where a post is held continuously

The shared name is NOT evidence of anything. Common Nepali surnames such as
चौधरी, यादव, साह, श्रेष्ठ and पौडेल recur constantly across unrelated people, and
these filings carry no address, citizenship number or father's name to
separate them.

Answer "unclear" whenever the material does not settle it. "unclear" is a
correct and expected answer, and it is strongly preferred over a guess: a wrong
"same" publicly attaches a person to a case they were never in, and a wrong
"different" splits one person's record in two.

Reply with JSON only:
{"verdict": "same" | "different" | "unclear",
 "confidence": "high" | "medium" | "low",
 "evidence": "one or two sentences citing the specific facts you compared",
 "per_case": {"<case slug>": "that case's post and place for this person"}}

Use "high" only when a stated fact rules the alternative out, not when one
reading merely seems likelier."""


def build_content(name, cards):
    """The user message for one held name: its cards, one block per case."""
    key = normalise_name(name)
    lines = [f"Name under comparison: {name}", ""]
    for card in cards:
        lines.append(f"## case slug: {card.slug}")
        if card.court_cases:
            lines.append(f"court case(s): {', '.join(card.court_cases)}")
        if card.title:
            lines.append(f"case title: {card.title}")
        for title in card.press_titles:
            lines.append(f"CIAA press release title: {title}")
        if card.districts:
            lines.append(f"districts bound to the case: {', '.join(card.districts)}")
        for excerpt in card.mentions.get(key, ()):
            lines.append(f"mention in the case summary: ...{excerpt}...")
        lines.append("")
    return "\n".join(lines)


def _verdict_from(reply):
    """Parse the model's JSON into a `HeldVerdict`, or a failed one."""
    if not isinstance(reply, dict):
        return HeldVerdict("unclear", failed=True,
                           evidence=f"the model returned {type(reply).__name__}, "
                                    "not a JSON object")
    verdict = str(reply.get("verdict") or "").strip().lower()
    if verdict not in ("same", "different", "unclear"):
        return HeldVerdict("unclear", failed=True,
                           evidence=f"the model returned verdict={verdict!r}, "
                                    "which is not one of same/different/unclear")
    per_case = reply.get("per_case")
    if not isinstance(per_case, dict):
        per_case = {}
    return HeldVerdict(
        verdict=verdict,
        confidence=str(reply.get("confidence") or "").strip().lower(),
        evidence=str(reply.get("evidence") or "").strip(),
        per_case={str(k): str(v) for k, v in per_case.items()})


def compare_identities(name, cards, invoke_json, *, tier="premium", usage=None,
                       max_tokens=700):
    """One model call: is `name` one person across `cards`, or several?

    Refused WITHOUT a call when fewer than two cards carry a distinguishing
    fact. A card with no press release, no district and no mention of the name
    contributes only its slug and its templated title, so the model would be
    left comparing the shared name against itself -- the one input the system
    prompt forbids it to reason from. Cheaper and more honest to hold.
    """
    key = normalise_name(name)
    usable = [c for c in cards if c.carries_identity(key)]
    if len(usable) < 2:
        thin = ", ".join(c.slug for c in cards if not c.carries_identity(key))
        return HeldVerdict(
            "unclear",
            evidence=("not compared: no press release, district or summary "
                      f"mention to tell this name apart on {thin or 'these cases'}"))
    try:
        reply = invoke_json(SYSTEM, build_content(name, cards),
                            max_tokens=max_tokens, tier=tier, usage=usage)
    except Exception as exc:  # noqa: BLE001 - one name's call failing is not the run's
        return HeldVerdict("unclear", failed=True,
                           evidence=f"the comparison call raised "
                                    f"{type(exc).__name__}")
    got = _verdict_from(reply)
    if got.is_actionable and not got.covers(c.slug for c in cards):
        # Actionable on its own fields, but silent about at least one case it
        # was asked about. Downgraded rather than dropped: the verdict text is
        # still worth showing a human, it just may not drive a bind.
        missing = sorted({c.slug for c in cards} - set(got.per_case))
        return HeldVerdict("unclear", confidence=got.confidence,
                           evidence=(f"{got.evidence} [downgraded: the reply "
                                     f"said nothing about {', '.join(missing)}]"),
                           per_case=got.per_case)
    return got


def compare_held(held, cards_by_slug, invoke_json, *, tier="premium", usage=None,
                 on_verdict=None):
    """`{name: HeldVerdict}` for every held name. One call each, in name order.

    `on_verdict(name, slugs, verdict)` is called as each answer lands so the
    caller can log it while the sweep is still running -- these are the only
    LLM calls in the stage, and a silent minutes-long gap between pass 1 and
    pass 2 is what the binder's own pass-1 progress logging exists to avoid.
    """
    verdicts = {}
    for name, slugs in sorted(held.items()):
        cards = [cards_by_slug[s] for s in sorted(slugs) if s in cards_by_slug]
        got = compare_identities(name, cards, invoke_json, tier=tier, usage=usage)
        verdicts[name] = got
        if on_verdict:
            on_verdict(name, sorted(slugs), got)
    return verdicts
