"""Case-title prompt rules and validation, shared by the description and
title enrichers so titles stay consistent across both write paths.

Ported verbatim from the deleted donor (0321a85:casework/common.py, around
``TITLE_RULES`` / ``validate_title`` / ``_HEADCOUNT_RE``).
"""

import re

# The public-headline contract. Both the description enricher (which regenerates
# the title as a side-effect of the description pass) and the standalone title
# enricher embed this verbatim so titles stay consistent across both paths.
# It is the rules ONLY — each script appends its own OUTPUT FORMAT block, since
# the description pass emits {"title", "description"} and the title pass {"title"}.
TITLE_RULES = """\
TITLE RULES (when asked to regenerate the title):
- Lead with the real, recognisable subject of the case — a named scheme/project,
  the institution, or the principal accused — and name the nature of the offence
  (भ्रष्टाचार / घोटाला / गैरकानूनी सम्पत्ति आर्जन / ठेक्का अनियमितता, etc.).
- Write a concise, SEARCHABLE headline, not a clause-stuffed sentence. Vary the
  construction across cases; do not use a rigid template. A colon split is fine
  ("<subject>: <offence + hook>"). Use clean, idiomatic Nepali grammar/spelling.
- Be catchy but strictly factual — ground every word in the provided data; never
  invent names, amounts, sections, or outcomes.
- HOOK (reward when warranted, never forced): when the case data carries a
  salient, quantifiable detail, surface it instead of burying it — the बिगो
  amount in natural scale (e.g. ३.२ अर्ब, ३३ करोड), a marquee named scheme, a
  notable principal accused, or a vivid concrete scale from the data (e.g.
  "१५ शैय्या अस्पताल"). A large बिगो or a marquee scheme left out of the title is
  a missed hook — pull it forward. But do NOT manufacture a hook where the data
  has none: a plain, clear, accurate title for a routine case is correct and
  complete, not a weakness.
- AMOUNT ACCURACY: any monetary figure in the title MUST be the verified बिगो (or
  a loss/disputed figure stated explicitly in the case data) — never a different
  or contradictory number. If the figures are uncertain or conflict, OMIT the
  number and use a non-numeric hook. A catchy-but-wrong number is worse than no
  number.
- NEVER put a defendant HEADCOUNT in the title. Forbidden: any "<संख्या> जना",
  "समेत X जना", "X प्रतिवादी(माथि/मा)", "तीन/चार… अध्यक्षसहित", or similar count
  of people. This applies even when there are many defendants.
    * Many defendants → name the ONE principal accused (or the institution) and
      mark the rest with "समेत" and NO number, e.g.
      "…सचिव संजय शर्मासमेतविरुद्ध भ्रष्टाचार मुद्दा", NOT "…सचिवसमेत १२ जना…".
- GRAMMAR — do NOT stack postpositions. Never jam "सहित" or "लगायत" directly onto
  "विरुद्ध" / "माथि" (WRONG: "रसाईलीसहितविरुद्ध", "भुषाललगायतविरुद्ध",
  "यादवलगायतमाथि"). Use exactly ONE connective: "<नाम>विरुद्ध …" for a single
  accused, or "<नाम>समेतविरुद्ध …" when there are co-accused. Keep "विरुद्ध"
  attached to the name it governs.
- The title MUST end with the special-court case number in parentheses, exactly
  as given to you, e.g. "… (080-CR-0047)". Keep the hook from crowding it out.
- Keep it under ~160 characters."""

# A court case number like 080-CR-0047 / 081-WO-1234. Case-insensitive to match
# the review gate's detector; the lookbehind/ahead anchor the token so a
# malformed adjacent token can't match the real number as a substring.
COURT_RE = re.compile(r"(?<![\dA-Za-z])\d{2,3}-[A-Za-z]{1,3}-\d{3,4}(?![\dA-Za-z])")

# Defendant-headcount patterns a title must avoid: a Devanagari/ASCII number
# immediately followed by जना / व्यक्ति / प्रतिवादी. The court number itself
# (080-CR-0098) won't match — no such trailing noun.
_HEADCOUNT_RE = re.compile(r"[०-९0-9]+\s*(जना|व्यक्ति|प्रतिवादी)")


def validate_title(title, court_number):
    """Return a problem string if the title violates the court-number contract
    (a number present, matching court_number, in trailing parens), else None."""
    nums = {m.group(0).upper() for m in COURT_RE.finditer(title or "")}
    if not nums:
        return "regenerated title has no court case number"
    if court_number and court_number.upper() not in nums:
        return (
            f"title number(s) {sorted(nums)} do not include the special-court "
            f"number {court_number}"
        )
    if court_number:
        expected = f"({court_number.upper()})"
        if not (title or "").upper().rstrip().endswith(expected):
            return (
                f"title must end with the special-court case number "
                f"in parentheses, e.g. '… {expected}'"
            )
    return None


def title_has_headcount(title) -> bool:
    """True if the title carries a forbidden defendant headcount."""
    return bool(_HEADCOUNT_RE.search(title or ""))
