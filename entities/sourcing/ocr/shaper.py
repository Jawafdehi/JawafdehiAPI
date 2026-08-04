"""Shape a scraped OCR (Office of the Company Registrar, Nepal) company record
into an entity *authoring payload* — a registered legal entity (कम्पनी).

Pure + DB-free: takes the crawler's flat company dict (the ``data`` object of
``GET https://company.ocr.gov.np/api/public/v1/company/{id}``) and returns the
authoring payload accepted by ``entities.write_validation.validate_create_payload``
(``{prefix, slug, type, name, …}``) — the crawler in
:mod:`entities.sourcing.ocr.crawl` is the API client that POSTs it to
``/api/entities``. Lives beside its crawler under ``entities/sourcing/ocr/`` — the
home for external-source ENTITY shapers (see ``entities/sourcing/README.md``); the
generic authoring/JSON-LD contract stays in ``entities.write_validation`` /
``entities.validation``.

Status policy (decided with the user): OCR ``companyId`` is a dense integer key
whose space is majority ``DRAFT`` (incomplete applications) plus ``REJECTED``. Only
records that are (or once were) real registrations — ``APPROVED`` and
``DEREGISTERED`` — are shaped into published Organization entities. For any other
status :func:`ocr_company_to_jsonld` returns ``None`` (the crawler still caches the
raw record for audit, it just does not publish it).
"""

from __future__ import annotations

import re
import unicodedata
from typing import Any

from jawafdehi_shared.dates import bs_to_ad_iso
from jawafdehi_shared.entities.ids import MAX_IRI_LENGTH, build_entity_iri
from jawafdehi_shared.search.transliterate import to_roman

#: IRI prefix for an OCR company entity (``/entity/organization/company/<slug>``).
#: A slash-joined prefix under the shared ``organization/`` root, matching the
#: convention the other org waves use (``organization/media/…``, ``organization/
#: education/…`` — see docs/nes/sourcing/*/RESULTS.md).
COMPANY_PREFIX = "organization/company"

#: Provenance authority for the ≥2-source gate's publisher key (host of the portal).
OCR_AUTHORITY = "ocr.gov.np"

#: Statuses that represent a real registration worth publishing as an entity.
#: DRAFT / REJECTED applications are cached but not shaped (return ``None``).
PUBLISHABLE_STATUSES = frozenset({"APPROVED", "DEREGISTERED"})

#: OCR company-type ``baseValue`` → schema.org ``@type``. NONPROFIT is a plain
#: ``Organization`` refined by an ``additionalType`` STRING (``jawafdehi:
#: NonProfitCompany`` is deliberately NOT a known ``@type`` — mirrors how the media
#: wave carried ``jawafdehi:MediaOrganization`` as additionalType, not @type).
_TYPE_BY_BASE = {
    "PRIVATE": "Corporation",
    "PUBLIC": "Corporation",
    "FOREIGN": "Corporation",
    "NONPROFIT": "Organization",
}
_NONPROFIT_ADDITIONAL_TYPE = "jawafdehi:NonProfitCompany"

#: Devanagari digits → ASCII (a name/number may embed them; keep them in the slug).
_DEVA_DIGITS = str.maketrans("०१२३४५६७८९", "0123456789")


def _lang_map(en: Any, ne: Any) -> dict[str, str] | None:
    """A ``{"en": …, "ne": …}`` language map, dropping empty/blank values.

    Returns ``None`` when neither language has a usable value so the caller can
    omit the key entirely rather than emit an empty map.
    """
    out: dict[str, str] = {}
    for key, val in (("en", en), ("ne", ne)):
        text = str(val).strip() if val is not None else ""
        if text:
            out[key] = text
    return out or None


def _slugify(text: str) -> str:
    """Coerce ``text`` to the entity slug grammar ``[a-z0-9][a-z0-9-]*``.

    Devanagari digits → ASCII; NFKD-fold combining diacritics so an IAST
    romanization (``rāma``, ``śarmā``) reduces to plain ASCII (``rama``, ``sarma``)
    instead of losing the accented letter to a ``-``; lowercase; every run of
    non-``[a-z0-9]`` → ``-``; collapse repeats; trim. Returns ``""`` when nothing
    usable survives (so :func:`company_slug` can fall back).
    """
    s = str(text or "").strip().translate(_DEVA_DIGITS)
    # Fold combining marks to ASCII (ā→a, ṇ→n, …); drop the marks, keep base letters.
    s = "".join(
        ch for ch in unicodedata.normalize("NFKD", s) if not unicodedata.combining(ch)
    )
    s = s.lower()
    s = re.sub(r"[^a-z0-9]+", "-", s)
    s = re.sub(r"-{2,}", "-", s).strip("-")
    return s if re.search(r"[a-z0-9]", s) else ""


def company_slug(record: dict[str, Any]) -> str:
    """Stable, unique slug for a company's ``@id``.

    Keyed on the OCR **registration number** (the registry's own primary key,
    globally unique and stable) with a romanized name for human legibility:
    ``<romanized-name>-<registrationNumber>`` (e.g. ``sipradi-yatri-350003``). The
    registration number alone guarantees uniqueness, so the name portion is a
    best-effort readability prefix — a company with an unromanizable (pure
    Devanagari, backend-unavailable) name still gets a valid slug
    (``company-<regno>``). The trailing number keeps two same-named companies
    distinct and makes the slug reconstructable from the record.

    Falls back to the OCR ``companyId`` when no registration number is present
    (DRAFT records may lack one — but those are not published anyway).
    """
    regno = _slugify(record.get("registrationNumber") or record.get("companyId") or "")
    # Prefer the English name; else romanize the Nepali name (IAST via to_roman,
    # which _slugify then diacritic-folds to plain ASCII). In fallback mode (no
    # transliteration backend) to_roman returns the Devanagari unchanged, which
    # _slugify reduces to "" — so we degrade cleanly to "company-<regno>".
    name_en = record.get("companyNameEnglish") or ""
    name_source = name_en or to_roman(record.get("companyNameNepali") or "")
    name_part = _slugify(name_source) or "company"

    slug = f"{name_part}-{regno}" if regno else name_part
    # Guard the IRI length bound (300); if the name prefix blew past it, drop back
    # to the guaranteed-short registration-number form.
    if regno and len(build_entity_iri(COMPANY_PREFIX, slug)) > MAX_IRI_LENGTH:
        slug = f"company-{regno}"
    return slug


def _address(record: dict[str, Any]) -> dict[str, Any] | None:
    """schema.org ``PostalAddress`` from the OCR address + province/district.

    Bilingual sub-fields are language maps; the whole block is omitted when no
    component has a value.
    """
    address: dict[str, Any] = {"@type": "PostalAddress"}
    street = _lang_map(record.get("addressLine"), record.get("addressLineNp"))
    region = _lang_map(
        record.get("provinceNameEnglish"), record.get("provinceNameNepali")
    )
    locality = _lang_map(
        record.get("districtNameEnglish"), record.get("districtNameNepali")
    )
    if street:
        address["streetAddress"] = street
    if region:
        address["addressRegion"] = region
    if locality:
        address["addressLocality"] = locality
    address["addressCountry"] = "NP"
    # Only "@type" + the constant country → nothing real; signal omission.
    return address if len(address) > 2 else None


def _identifiers(record: dict[str, Any]) -> list[dict[str, Any]]:
    """schema.org ``PropertyValue`` identifiers for the registry keys.

    ``propertyID="ocr"`` is the Office of Company Registrar registration number
    (the documented convention, nes-schema-org.md); ``ocr-company-id`` is the
    portal's internal integer id (the crawl key); ``pan`` is the PAN/VAT number —
    already server-side MASKED by OCR (e.g. ``621xxx200``), carried verbatim.
    """
    out: list[dict[str, Any]] = []
    for property_id, value in (
        ("ocr", record.get("registrationNumber")),
        ("ocr-company-id", record.get("companyId")),
        ("pan", record.get("panNumber")),
    ):
        text = str(value).strip() if value is not None else ""
        if text:
            out.append(
                {"@type": "PropertyValue", "propertyID": property_id, "value": text}
            )
    return out


def _nature_of_business(record: dict[str, Any]) -> list[dict[str, Any]]:
    """The NSIC-coded lines of business as a jawafdehi: extension list."""
    out: list[dict[str, Any]] = []
    for item in record.get("natureOfBusiness") or []:
        if not isinstance(item, dict):
            continue
        entry: dict[str, Any] = {}
        code = str(item.get("nsicCode") or "").strip()
        if code:
            entry["nsicCode"] = code
        name = _lang_map(item.get("nameEnglish"), item.get("nameNepali"))
        if name:
            entry["name"] = name
        if entry:
            out.append(entry)
    return out


def ocr_company_to_jsonld(record: dict[str, Any]) -> dict[str, Any] | None:
    """Shape one OCR company record → an entity authoring payload, or ``None``.

    Returns ``None`` (do not publish) when the record has no usable status in
    :data:`PUBLISHABLE_STATUSES` (DRAFT/REJECTED/blank), or lacks the
    registration identity needed to mint a stable ``@id``. Otherwise returns the
    ``{prefix, slug, type, name, …}`` dict for ``validate_create_payload`` plus a
    ``sources`` list (the ≥2-source gate's hook — a single ``ocr.gov.np`` source).
    """
    if not isinstance(record, dict):
        return None
    status = str(record.get("status") or "").strip().upper()
    if status not in PUBLISHABLE_STATUSES:
        return None

    name = _lang_map(record.get("companyNameEnglish"), record.get("companyNameNepali"))
    if not name:
        return None  # nothing to satisfy the required `name` — skip rather than 422.

    base = (
        str((record.get("companyTypeCategory") or {}).get("baseValue") or "")
        .strip()
        .upper()
    )
    schema_type = _TYPE_BY_BASE.get(base, "Organization")

    company_id = record.get("companyId")
    source_url = (
        f"https://company.ocr.gov.np/company/{company_id}"
        if company_id is not None
        else None
    )

    payload: dict[str, Any] = {
        "prefix": COMPANY_PREFIX,
        "slug": company_slug(record),
        "type": schema_type,
        "name": name,
        # Registry status as a jawafdehi: extension (APPROVED / DEREGISTERED).
        "jawafdehi:registryStatus": status,
    }

    # NONPROFIT is an Organization refined by an additionalType STRING (not @type).
    if base == "NONPROFIT":
        payload["additionalType"] = _NONPROFIT_ADDITIONAL_TYPE

    identifiers = _identifiers(record)
    if identifiers:
        payload["identifier"] = identifiers

    address = _address(record)
    if address:
        payload["address"] = address

    # Founding date: AD in the schema.org property, BS verbatim in a jawafdehi:
    # extension (the platform's documented date-pair convention).
    founding_ad = bs_to_ad_iso(record.get("registrationDateBS")) or (
        record.get("registrationDateAD") or None
    )
    if founding_ad:
        payload["foundingDate"] = founding_ad
    if record.get("registrationDateBS"):
        payload["jawafdehi:foundingDateBS"] = record["registrationDateBS"]

    # Dissolution (de-registration) mirrors the founding pair.
    if status == "DEREGISTERED":
        dissolution_ad = record.get("deRegisteredDateAD") or bs_to_ad_iso(
            record.get("deRegisteredDateBs")
        )
        if dissolution_ad:
            payload["dissolutionDate"] = dissolution_ad
        if record.get("deRegisteredDateBs"):
            payload["jawafdehi:deregisteredDateBS"] = record["deRegisteredDateBs"]

    company_type = record.get("companyTypeCategory")
    if company_type:
        payload["jawafdehi:companyType"] = company_type

    if record.get("registeredOffice"):
        payload["jawafdehi:registeredOffice"] = record["registeredOffice"]
    if record.get("annualReportUpdatedUpto"):
        payload["jawafdehi:annualReportUpdatedUpto"] = record["annualReportUpdatedUpto"]

    nob = _nature_of_business(record)
    if nob:
        payload["jawafdehi:natureOfBusiness"] = nob

    if source_url:
        payload["url"] = source_url
        payload["sources"] = [{"url": source_url, "authority": OCR_AUTHORITY}]

    return payload
