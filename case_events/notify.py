# SPDX-License-Identifier: LicenseRef-Hippocratic-3.0
"""Announcing a proposal to an outbound webhook.

Separate from :mod:`case_events.consumers.handlers` because the interesting parts
here are about *what may leave the building*, not about consuming a message.

**The payload carries the drafted change, and that is a deliberate reversal.**
This module used to send only a pointer — proposal id, case slug, a link — on the
grounds that a PENDING proposal is unreviewed model output about named
individuals and that posting it would publish it to a third party before a human
agreed with it. That reasoning was sound and the result was still wrong in
practice: the message said a proposal existed without saying what it proposed, so
nobody could tell an acquittal being recorded from a hearing being adjourned
without opening the queue. 345 proposals accumulated and one was ever acted on.
A notification nobody can triage is not a cautious notification, it is an ignored
one.

So the rule is now narrower rather than absent, and it is worth stating what was
given up. The drafted entry — which names accused people, and often the judges on
the bench — is rendered into a chat message with its own retention, forwardable in
one tap, before anyone has checked that the model got it right. The mitigations
are that the channel is the team's, the draft is always marked as a draft, and
nothing here can write to a case.

**What still does not leave:**

* An unresolvable case is said to be unresolvable, never linked. A public case URL
  is only ever emitted for a PUBLISHED case — a DRAFT or IN_REVIEW case 404s for
  anyone without a casework role, so linking one would be both broken and a claim
  that unpublished editorial work is public.
* ``detected_by`` is never rendered. For hand-filed proposals it holds a
  caseworker's personal email address, and a provenance field is not worth
  publishing someone's address to a chat server.
* The webhook URL is never logged. It is the credential.

**Failures never propagate.** The proposal row already exists and the queue UI is
the primary surface; a webhook that is down must not cause a JetStream redelivery,
because redelivery would re-notify rather than re-do anything useful. Every failure
mode logs and returns False.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request

import structlog
from django.conf import settings

logger = structlog.get_logger(__name__)

#: What the reviewer is being pointed at. Kept here rather than built from a
#: request, because a consumer has no request to build one from.
QUEUE_PATH = "/admin/proposals"

#: U+200B. Named and escaped so it is visible in source — see
#: :func:`defang_mentions`.
ZERO_WIDTH_SPACE = "\u200b"

#: Discord's SUPPRESS_EMBEDS message flag (1 << 2).
#:
#: Without it every message renders the site's generic og: card underneath —
#: identical boilerplate, three times taller than the message, repeated once per
#: proposal. Any receiver that does not know the field ignores it, the same way
#: ``allowed_mentions`` is harmless elsewhere. The ``<...>`` around each URL below
#: is the second half of the same belt-and-braces: it suppresses the unfurl at the
#: markdown level, which is what keeps a *visible* URL (the material IRI) quiet
#: without hiding it behind a label.
SUPPRESS_EMBEDS = 1 << 2

#: Wording for the change being proposed, matching the labels the admin queue
#: shows. Two surfaces describing the same proposal differently is how a reviewer
#: ends up unsure whether they are looking at the same thing.
INTENT_LABELS = {
    "append_timeline_entry": "Add timeline entry",
    "link_material": "Link material",
    "raw_patch": "Direct edit",
    "set_entity_outcome": "Set accused outcome",
}

#: Where the underlying fact was observed. Rendered instead of ``detected_by``,
#: which is an account handle rather than a provenance anybody wants to read.
SOURCE_PHRASES = {
    "ngm_docket": "from the court docket",
    "court_order": "from a published court order",
    "ciaa_press": "from a CIAA press release",
    "news": "from a news match",
    "caseworker": "filed by hand",
}


def _base_url() -> str:
    configured = (getattr(settings, "FRONTEND_BASE_URL", "") or "").rstrip("/")
    return configured or "https://jawafdehi.org"


def defang_mentions(text: str) -> str:
    """Make ``@`` in interpolated text unable to ping anyone.

    That the payload now carries the drafted change widens this considerably
    rather than retiring it: ``case_title`` and the drafted entry's title and
    description all reach the rendered line, and the drafted ones are model output
    shaped by scraped court text. ``@everyone`` pings an entire server from a case
    record. That is not a data leak, it is worse in one respect: it is a
    notification channel becoming a nuisance channel, which gets it muted, which
    silently ends the notifications.

    A zero-width space after the ``@`` is the standard trick: it renders
    indistinguishably and stops the mention resolving.

    Written as the ``\\u200b`` ESCAPE rather than the character itself, on purpose.
    A literal zero-width space in source is invisible: it can be deleted by an
    editor, a reformat or a careless selection with nothing looking wrong
    afterwards, and the test for this would then be the only sign left.
    """
    return text.replace("@", f"@{ZERO_WIDTH_SPACE}")


def _link(label: str, url: str) -> str:
    """A masked link: one clickable thing, no unfurl.

    The label is defanged, the URL is not — a mention cannot resolve inside a URL,
    and inserting a zero-width space into one would break it.
    """
    return f"[{defang_mentions(label)}](<{url}>)"


def resolve_case(slug: str):
    """A case by its current slug, falling back to a retired one. None if neither.

    Shares :func:`case_events.consumers.handlers.resolve_case` rather than
    repeating it: ``case_slug`` on a proposal is a plain field with no foreign
    key, and 7 of the 293 slugs now in the queue resolve to nothing at all. Two
    copies of "how do I turn a slug into a case" would drift, and the one here
    would be the copy that forgot about retired slugs.
    """
    from case_events.consumers.handlers import resolve_case as _resolve

    return _resolve(slug)


def _case_line(payload: dict, case) -> str:
    """The ``Case:`` line — linked when that link would work, plain when not.

    Falls back to the slug when ``case_title`` is empty, which is not an edge
    case: every hand-filed proposal in the queue has a blank title, so a template
    that trusts it renders ``Case: ""`` eleven times out of 345.
    """
    slug = payload.get("case_slug") or ""
    title = (payload.get("case_title") or "").strip() or slug or "unknown case"

    if case is None:
        # Still name it. The title is the only human-readable handle on a proposal
        # whose slug resolves to nothing, and 7 of the 293 slugs in the queue do.
        if title == slug:
            return f"**Case:** `{defang_mentions(slug) or '—'}` *(no case found)*"
        return f"**Case:** {defang_mentions(title)} *(no case found for `{defang_mentions(slug)}`)*"

    if case.state != "PUBLISHED":
        # Deliberately unlinked. The public URL 404s for anyone without a
        # casework role, so a link here would be dead for most readers and would
        # assert that unpublished editorial work is public.
        return f"**Case:** {defang_mentions(title)} *(not published)*"

    return f"**Case:** {_link(title, f'{_base_url()}/case/{case.slug}')}"


def _status_line(case) -> str | None:
    """Court-side status and publication state, or None when there is no case.

    Two different questions — "where has this reached in the courts" and "is it
    live on the site" — and a reviewer wants both. ``Case.status`` is derived on
    read from the court stages and every accused party's outcome, so it is the
    court-side answer and not the public badge, which is known to contradict its
    own docket on a large minority of CIAA cases.
    """
    if case is None:
        return None

    # `status` is a PROPERTY, not a column, so there is no get_status_display()
    # for it the way there is for `state`. Look the label up, and fall back to the
    # raw value rather than raising: `status_override` can in principle hold
    # something the enum does not know, and a notification is not the place to
    # discover that.
    from cases.models import CaseStatus

    raw = case.status or ""
    try:
        status = CaseStatus(raw).label
    except ValueError:
        status = raw or "unknown"

    return f"**Case status:** {status} · {case.get_state_display().lower()}"


def _timeline_detail(intent: dict) -> list[str]:
    entry = intent.get("entry") or {}
    bs, ad = entry.get("date_bs") or "", entry.get("date") or ""
    # BS first: it is the date the court record itself carries, and the AD date is
    # a conversion of it.
    dates = " · ".join(part for part in (f"**{bs}**" if bs else "", ad) if part)
    lines = [dates] if dates else []
    for key in ("title", "description"):
        value = (entry.get(key) or "").strip()
        if value:
            lines.append(value)
    return lines or ["(an empty timeline entry)"]


def _material_detail(intent: dict) -> list[str]:
    """What is being attached, named by the material store rather than the model.

    The drafted ``relation`` is free text and in practice disagrees with itself —
    the fourteen of these in the queue carry five different spellings of "court
    order", one of which calls the document a फैसला while the store types it as an
    order. So the model's wording is quoted as a suggestion and the store's own
    name and type are shown beside it as the fact.
    """
    iri = intent.get("material") or ""
    relation = (intent.get("relation") or "").strip()

    name, kind = _material_facts(iri)
    headline = f"Attach **{name}**" if name else "Attach a material"
    if kind:
        headline += f" ({kind})"
    if relation:
        headline += f', proposed as "{relation}"'

    lines = [headline]
    if iri:
        # Left bare inside <> rather than masked: the IRI is the identifier a
        # reviewer may want to read and copy, so hiding it behind a label would
        # lose the thing worth showing.
        lines.append(f"<{iri}>")
    return lines


def _material_facts(iri: str) -> tuple[str, str]:
    """``(display name, material type)`` for a material IRI, best effort.

    Best effort on purpose: a notification must not fail because a lookup did.
    """
    if not iri:
        return "", ""
    try:
        from materials.models import Material

        material = Material.objects.filter(iri=iri).first()
        if material is None:
            return "", ""
        name = (material.data or {}).get("name") or {}
        if isinstance(name, dict):
            label = name.get("ne") or name.get("en") or ""
        else:
            label = str(name)
        return label, (material.material_type or "")
    except Exception as exc:  # noqa: BLE001 - a notification is not worth an exception
        logger.warning("case_events.material_lookup_failed", error=str(exc)[:200])
        return "", ""


def _patch_detail(intent: dict) -> list[str]:
    """A JSON Patch summarised, and flagged when it cannot be applied.

    ``raw_patch`` may only touch a fixed set of Case scalars; ``entities`` and
    friends are excluded because they have their own transition paths. Ten of the
    eleven hand-filed patches in the queue target exactly those excluded paths, so
    approving them returns a 400. Saying so in the message is cheaper than finding
    out by clicking approve.
    """
    ops = intent.get("patch") or []
    if not ops:
        return ["(an empty patch)"]

    summary = ", ".join(f"{op.get('op', '?')} {op.get('path', '?')}" for op in ops[:4])
    if len(ops) > 4:
        summary += f", and {len(ops) - 4} more"
    lines = [f"{len(ops)} change{'s' if len(ops) != 1 else ''}: {summary}"]

    blocked = sorted(_unapplyable_fields(ops))
    if blocked:
        lines.append(
            f"⚠️ Targets `{'`, `'.join(blocked)}`, which a direct edit cannot apply."
        )
    return lines


def _unapplyable_fields(ops: list) -> set[str]:
    try:
        from case_proposals.apply import RAW_PATCH_FIELDS
    except Exception:  # noqa: BLE001 - never block a notification on an import
        return set()

    blocked = set()
    for op in ops:
        top = str(op.get("path", "")).lstrip("/").split("/")[0]
        if top and top not in RAW_PATCH_FIELDS:
            blocked.add(top)
    return blocked


def _outcome_detail(intent: dict) -> list[str]:
    outcomes = intent.get("outcomes") or []
    if not outcomes:
        return ["(no outcomes listed)"]
    return [
        f"Set outcome for {len(outcomes)} accused: "
        + ", ".join(str(o.get("outcome", "?")) for o in outcomes[:6])
    ]


_DETAIL_RENDERERS = {
    "append_timeline_entry": _timeline_detail,
    "link_material": _material_detail,
    "raw_patch": _patch_detail,
    "set_entity_outcome": _outcome_detail,
}


def _detail_block(intent) -> str:
    """The drafted change, as a markdown blockquote.

    Quoted rather than run together with the rest on purpose: it is the one part
    of the message a model wrote, and a reader has to be able to tell which parts
    are facts about the case and which part is the draft being judged.

    An intent type with no renderer falls back to naming the type. A new intent
    type should produce a message saying so, not a message that silently omits
    the only thing the reviewer needs.
    """
    if not isinstance(intent, dict):
        return "> (no change recorded)"

    intent_type = intent.get("type") or ""
    render = _DETAIL_RENDERERS.get(intent_type)
    lines = render(intent) if render else [f"({intent_type or 'unrecognised'} — open the queue to read it)"]
    return "\n".join(f"> {defang_mentions(line)}" for line in lines)


def _headline(status: str, payload: dict) -> str:
    """Different shapes for different transitions.

    A decision and an arrival used to differ by one bolded word in the middle of
    an identical sentence, which is invisible in a scrolling channel.
    """
    if status == "pending":
        return "**A new case proposal has arrived!**"
    reviewer = (payload.get("reviewer") or "").strip()
    by = f" by {defang_mentions(reviewer)}" if reviewer else ""
    return f"**Case proposal {status}{by}**"


def build_message(payload: dict, case=None) -> dict:
    """The body posted to the webhook.

    Carries both a rendered ``content`` and the structured fields beside it, so a
    different receiver does not have to parse the prose back out. ``case`` is
    passed in rather than looked up here so this stays a pure function of its
    inputs and can be tested without a database.
    """
    status = payload.get("status") or "updated"
    proposal_id = payload.get("proposal_id")
    intent = payload.get("intent")
    intent_type = intent.get("type") if isinstance(intent, dict) else ""

    label = INTENT_LABELS.get(intent_type, "Change proposed")
    source = SOURCE_PHRASES.get(payload.get("source_kind") or "")
    type_line = f"**Proposal type:** {label}" + (f" — {source}" if source else "")

    path = f"{QUEUE_PATH}/{proposal_id}" if proposal_id else QUEUE_PATH
    link = f"{_base_url()}{path}"
    where = f"#{proposal_id}" if proposal_id else "proposal"

    lines = [
        _headline(status, payload),
        "",
        _case_line(payload, case),
    ]
    status_line = _status_line(case)
    if status_line:
        lines.append(status_line)
    lines += [
        type_line,
        "",
        "**Proposal details**",
        _detail_block(intent),
        "",
        _link(f"**View {where} →**", link),
    ]

    return {
        "content": "\n".join(lines),
        # Belt to the escaping's braces, and the half that actually binds a Discord
        # receiver: with `parse` empty, even an unescaped mention is not resolved.
        # Harmless to any other receiver, which ignores a field it does not know.
        "allowed_mentions": {"parse": []},
        "flags": SUPPRESS_EMBEDS,
        # NOT defanged. The structured half is for machines, and mangling an
        # identifier a receiver might match on would be worse than the risk this
        # guards. Only the prose that gets RENDERED is escaped.
        "proposal_id": proposal_id,
        "case_slug": payload.get("case_slug"),
        "status": status,
        "intent_type": intent_type,
        "source_kind": payload.get("source_kind"),
        # Kept in the structured half but deliberately NOT rendered. It is the
        # same number on 201 of the 345 proposals filed so far, so in the prose it
        # reads as information while carrying none.
        "confidence": payload.get("confidence"),
        "reviewer": payload.get("reviewer"),
        "url": link,
    }


def post(payload: dict) -> bool:
    """Announce one proposal transition. Returns whether it was delivered.

    Never raises. A notification is the least important thing on this thread.
    """
    url = getattr(settings, "CASE_EVENTS_WEBHOOK_URL", "") or ""
    if not url:
        # Not a warning. Log-only is the documented default, and a warning per
        # proposal would train people to ignore the log this consumer exists for.
        logger.debug("case_events.webhook_not_configured")
        return False

    try:
        case = resolve_case(payload.get("case_slug") or "")
    except Exception as exc:  # noqa: BLE001 - a dead database must not raise here
        logger.warning("case_events.case_lookup_failed", error=str(exc)[:200])
        case = None

    body = json.dumps(build_message(payload, case)).encode("utf-8")
    request = urllib.request.Request(
        url,
        data=body,
        headers={"Content-Type": "application/json", "User-Agent": "jawafdehi-case-events/1.0"},
        method="POST",
    )
    timeout = float(getattr(settings, "CASE_EVENTS_WEBHOOK_TIMEOUT", 5))

    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            # 2xx. Discord answers 204 with no body; treat any 2xx as delivered
            # rather than pinning one code a different receiver would not use.
            delivered = 200 <= response.status < 300
    except urllib.error.HTTPError as exc:
        # The endpoint answered and refused. Worth a warning: a rotated or revoked
        # webhook fails this way forever and silently, and the URL must NOT be
        # logged — it is the credential.
        logger.warning("case_events.webhook_rejected", status=exc.code, reason=str(exc.reason)[:200])
        return False
    except Exception as exc:  # noqa: BLE001 - timeouts, DNS, TLS, malformed URL
        logger.warning("case_events.webhook_failed", error=str(exc)[:200])
        return False

    if not delivered:
        logger.warning("case_events.webhook_unexpected_status", status=response.status)
    return delivered
