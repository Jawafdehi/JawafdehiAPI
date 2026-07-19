"""Principled, reproducible sample selection for the three-way A/B.

Why sampling is constrained at all: 4 of the 5 enrichers read evidence text,
and Arm A can only see that text through the input adapter documented in
`arm_a_patches.md`, which maps `material_type` -> the donor's `source_type`
for `press_release` / `ciaa_press_release` / `court_order` ONLY. A case with
no adapter-mapped material would leave Arm A structurally unable to extract
anything, and the comparison would measure the adapter's coverage rather
than the enrichers -- producing exactly the empty-vs-empty false parity this
task exists to detect. So the sample FRAME is "cases carrying at least one
adapter-mapped material", and that restriction is a stated limit on
generalisability, not a hidden convenience.

`charge_sheet` is deliberately unmapped by the adapter (the donor's
`MILESTONE_SOURCE_TYPES` treats a charge sheet as a separate, HIGHER-priority
type, `AG_ABHIYOG_PATRA`). A case whose only press-type material is a charge
sheet therefore behaves differently between the arms for reasons that have
nothing to do with the port, so such cases are excluded from the frame and
reported separately.

Selection is stratified and seeded so the same sample is reproducible.
"""

import hashlib

MAPPED_TYPES = ("press_release", "ciaa_press_release", "court_order")
PRESS_TYPES = ("press_release", "ciaa_press_release")
COURT_TYPES = ("court_order",)


def _types(entry):
    return list(entry.get("material_types") or [])


def has_mapped_material(entry):
    """True when the adapter can show Arm A at least one document."""
    return bool(set(_types(entry)) & set(MAPPED_TYPES))


def is_charge_sheet_only(entry):
    """Charge sheet present but NO adapter-mapped material (Fact 6 exclusion)."""
    types = set(_types(entry))
    return "charge_sheet" in types and not (types & set(MAPPED_TYPES))


def has_repeated_mapped_type(entry):
    """True when a mapped type appears more than once.

    Flags the known, pre-existing algorithmic difference: the donor's
    per-enricher helpers read only the FIRST matching evidence entry and
    break, while the port's `materials.source_text` aggregates ALL matching
    entries. A divergence on such a case is a real donor-vs-port algorithm
    difference, not a port defect -- so these are tracked as a subgroup.
    """
    types = _types(entry)
    return any(types.count(t) > 1 for t in MAPPED_TYPES)


def stratum(entry):
    """Assign a case to an evidence-shape stratum."""
    types = set(_types(entry))
    press = bool(types & set(PRESS_TYPES))
    court = bool(types & set(COURT_TYPES))
    if press and court:
        return "press+court"
    if press:
        return "press_only"
    if court:
        return "court_only"
    return "none"


def build_frame(survey):
    """Split the surveyed cases into the eligible frame and the exclusions."""
    frame, excluded = {}, {}
    for slug, entry in survey.items():
        if entry.get("error"):
            excluded[slug] = "survey_error"
        elif is_charge_sheet_only(entry):
            excluded[slug] = "charge_sheet_only"
        elif not has_mapped_material(entry):
            excluded[slug] = "no_adapter_mapped_material"
        else:
            frame[slug] = entry
    return frame, excluded


def _stable_key(slug, seed):
    """Deterministic per-slug ordering key, independent of dict iteration order."""
    return hashlib.sha256(f"{seed}:{slug}".encode()).hexdigest()


def select_sample(survey, n=20, seed="task-16"):
    """Pick a stratified, reproducible sample of `n` cases.

    Allocation is proportional to stratum size, with at least one case from
    every non-empty stratum so that no evidence shape is silently absent.
    Within a stratum, cases are ordered by a seeded hash of the slug -- stable
    across runs and independent of survey ordering.
    """
    frame, excluded = build_frame(survey)
    strata = {}
    for slug, entry in frame.items():
        strata.setdefault(stratum(entry), []).append(slug)
    for slugs in strata.values():
        slugs.sort(key=lambda s: _stable_key(s, seed))

    total = sum(len(v) for v in strata.values())
    if not total:
        return {"slugs": [], "strata": {}, "frame_size": 0, "excluded": excluded}

    n = min(n, total)
    # Largest-remainder allocation, floored at 1 per non-empty stratum.
    quotas, remainders = {}, {}
    for name, slugs in strata.items():
        exact = len(slugs) * n / total
        quotas[name] = max(1, int(exact))
        remainders[name] = exact - int(exact)
    # Trim or top up to hit exactly n.
    while sum(quotas.values()) > n:
        name = max(
            (k for k in quotas if quotas[k] > 1),
            key=lambda k: (-quotas[k], _stable_key(k, seed)),
            default=None,
        )
        if name is None:
            break
        quotas[name] -= 1
    while sum(quotas.values()) < n:
        name = max(
            (k for k in strata if quotas[k] < len(strata[k])),
            key=lambda k: (remainders[k], _stable_key(k, seed)),
            default=None,
        )
        if name is None:
            break
        quotas[name] += 1

    chosen, picked = [], {}
    for name in sorted(strata):
        take = min(quotas.get(name, 0), len(strata[name]))
        picked[name] = strata[name][:take]
        chosen.extend(picked[name])

    chosen.sort(key=lambda s: _stable_key(s, seed))
    return {
        "slugs": chosen,
        "strata": picked,
        "frame_size": total,
        "excluded": excluded,
        "repeated_mapped_type": [
            s for s in chosen if has_repeated_mapped_type(frame[s])
        ],
    }
