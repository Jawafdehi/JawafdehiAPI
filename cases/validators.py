"""
Validation functions for Case model fields.

This module provides centralized validation logic for case fields,
following Django's convention of separating validation concerns.
"""

import re

from django.core.exceptions import ValidationError

from jawafdehi_shared.entities.ids import (
    build_courtcase_iri,
    is_valid_courtcase_iri,
    parse_courtcase_iri,
)

COURT_CHOICES = [
    ("supreme", "Supreme Court"),
    ("special", "Special Court"),
    ("baglunghc", "Baglung High Court"),
    ("biratnagarhc", "Biratnagar High Court"),
    ("birgunjhc", "Birgunj High Court"),
    ("butwalhc", "Butwal High Court"),
    ("dhankutahc", "Dhankuta High Court"),
    ("dipayalhc", "Dipayal High Court"),
    ("hetaudahc", "Hetauda High Court"),
    ("ilamhc", "Ilam High Court"),
    ("janakpurhc", "Janakpur High Court"),
    ("jumlahc", "Jumla High Court"),
    ("mahendranagarhc", "Mahendranagar High Court"),
    ("nepalgunjhc", "Nepalgunj High Court"),
    ("okhaldhungahc", "Okhaldhungha High Court"),
    ("patanhc", "Patan High Court"),
    ("pokharahc", "Pokhara High Court"),
    ("rajbirajhc", "Rajbiraj High Court"),
    ("surkhethc", "Surkhet High Court"),
    ("tulsipurhc", "Tulsipur High Court"),
    ("achhamdc", "Achham District Court"),
    ("arghakhanchidc", "Arghakhanchi District Court"),
    ("baglungdc", "Baglung District Court"),
    ("baitadidc", "Baitadi District Court"),
    ("bajhangdc", "Bajhang District Court"),
    ("bajuradc", "Bajura District Court"),
    ("bankedc", "Banke District Court"),
    ("baradc", "Bara District Court"),
    ("bardiyadc", "Bardiya District Court"),
    ("bhaktapurdc", "Bhaktapur District Court"),
    ("bhojpurdc", "Bhojpur District Court"),
    ("chitwandc", "Chitwan District Court"),
    ("dadeldhuradc", "Dadeldhura District Court"),
    ("dailekhdc", "Dailekh District Court"),
    ("dangdc", "Dang District Court"),
    ("darchuladc", "Darchula District Court"),
    ("dhadingdc", "Dhading District Court"),
    ("dhankutadc", "Dhankuta District Court"),
    ("dhanusadc", "Dhanusa District Court"),
    ("dolakhadc", "Dolakha District Court"),
    ("dolpadc", "Dolpa District Court"),
    ("dotidc", "Doti District Court"),
    ("gorkhadc", "Gorkha District Court"),
    ("gulmidc", "Gulmi District Court"),
    ("humladc", "Humla District Court"),
    ("ilamdc", "Ilam District Court"),
    ("jajarkotdc", "Jajarkot District Court"),
    ("jhapadc", "Jhapa District Court"),
    ("jumladc", "Jumla District Court"),
    ("kailalidc", "Kailali District Court"),
    ("kalikotdc", "Kalikot District Court"),
    ("kanchanpurdc", "Kanchanpur District Court"),
    ("kapilbastudc", "Kapilbastu District Court"),
    ("kaskidc", "Kaski District Court"),
    ("kathmandudc", "Kathmandu District Court"),
    ("kavrepalanchowkdc", "Kavrepalanchowk District Court"),
    ("khotangdc", "Khotang District Court"),
    ("lalitpurdc", "Lalitpur District Court"),
    ("lamjungdc", "Lamjung District Court"),
    ("mahottaridc", "Mahottari District Court"),
    ("makwanpurdc", "Makwanpur District Court"),
    ("manangdc", "Manang District Court"),
    ("morangdc", "Morang District Court"),
    ("mugudc", "Mugu District Court"),
    ("mustangdc", "Mustang District Court"),
    ("myagdidc", "Myagdi District Court"),
    ("nawalparasidc", "Nawalparasi District Court"),
    ("nawalpurdc", "Nawalpur District Court"),
    ("nuwakotdc", "Nuwakot District Court"),
    ("okhaldhungadc", "Okhaldhungha District Court"),
    ("palpadc", "Palpa District Court"),
    ("panchthardc", "Panchthar District Court"),
    ("parbatdc", "Parbat District Court"),
    ("parsadc", "Parsa District Court"),
    ("pyuthandc", "Pyuthan District Court"),
    ("ramechhapdc", "Ramechhap District Court"),
    ("rasuwadc", "Rasuwa District Court"),
    ("rautahatdc", "Rautahat District Court"),
    ("rolpadc", "Rolpa District Court"),
    ("rukumdc", "Rukum District Court"),
    ("rukumkotdc", "Rukumkot District Court"),
    ("rupandehidc", "Rupandehi District Court"),
    ("salyandc", "Salyan District Court"),
    ("sankhuwasabhadc", "Sankhuwasabha District Court"),
    ("saptaridc", "Saptari District Court"),
    ("sarlahidc", "Sarlahi District Court"),
    ("sindhulidc", "Sindhuli District Court"),
    ("sindhupalchowkdc", "Sindhupalchowk District Court"),
    ("sirahadc", "Siraha District Court"),
    ("solukhumbudc", "Solukhumbu District Court"),
    ("sunsaridc", "Sunsari District Court"),
    ("surkhetdc", "Surkhet District Court"),
    ("syangjadc", "Syangja District Court"),
    ("tanahundc", "Tanahun District Court"),
    ("taplejungdc", "Taplejung District Court"),
    ("tehrathumdc", "Tehrathum District Court"),
    ("udayapurdc", "Udayapur District Court"),
]


def validate_slug(value):
    """
    Validate slug format and content.

    Rules:
    - Must start with a letter (a-z, A-Z)
    - Can contain letters, numbers, and hyphens
    - Cannot be empty or whitespace-only
    - Maximum 50 characters
    - Regex: ^[a-zA-Z][a-zA-Z0-9-]{0,49}$

    Args:
        value: The slug string to validate

    Raises:
        ValidationError: If the slug is invalid

    Examples:
        Valid: "corruption-case-2078", "land-encroachment-baluwatar", "a"
        Invalid: "123-case", "-case", "case_name", "case name", ""
    """
    # Check for empty or whitespace-only
    if not value or not value.strip():
        raise ValidationError("Slug cannot be empty or whitespace-only")

    # Validate format with regex
    pattern = r"^[a-zA-Z][a-zA-Z0-9-]{0,49}$"
    if not re.match(pattern, value):
        raise ValidationError(
            "Slug must start with a letter and contain only letters, numbers, "
            "and hyphens (max 50 characters)"
        )


# Known court identifiers (the COURT_CHOICES keys) — court-case references may
# only name these courts.
VALID_COURT_IDENTIFIERS = frozenset(c[0] for c in COURT_CHOICES)


def parse_courtcase_ref(ref):
    """Parse a canonical court-case ``@id`` IRI into ``(court, case_number)``.

    Stored references are ALWAYS the canonical IRI
    (``https://<base>/courtcase/<court>/<case_number>``); this is the
    read-side convenience parser (shape check only — host not anchored).
    Returns ``None`` if the value is not a court-case IRI.
    """
    if not isinstance(ref, str) or not ref.strip():
        return None
    try:
        parsed = parse_courtcase_iri(ref.strip())
    except ValueError:
        return None
    return parsed.court, parsed.case_number


def short_courtcase_ref(ref):
    """The compact ``<court>:<CASE-NUMBER>`` spelling of a court-case @id IRI.

    IRIs are lowercase; the case number reads naturally uppercased. This is
    the ONE formatter for the compact spelling (admin display, LLM prompts,
    logs) — the reference stays the IRI everywhere it is stored or sent.
    Returns ``None`` when ``ref`` is not a court-case IRI.
    """
    parts = parse_courtcase_ref(ref)
    if parts is None:
        return None
    return f"{parts[0]}:{parts[1].upper()}"


def courtcase_input_to_iri(value):
    """Convert a ``<court_identifier>:<case_number>`` INPUT row to the @id IRI.

    This is the admin widget's input format (a court dropdown + case-number
    field serialized as ``court:number``) — an input convenience only. The
    API and the model accept canonical IRIs exclusively; the conversion
    happens at the form edge.

    Raises:
        ValidationError: If the row is malformed, names an unknown court, or
            the case number falls outside the IRI grammar.
    """
    if not isinstance(value, str) or "://" in value or value.count(":") != 1:
        raise ValidationError(
            f"Invalid court case input {value!r}. Expected "
            "<court_identifier>:<case_number> (e.g. special:080-CR-0111)."
        )
    court, _, case_number = value.partition(":")
    court, case_number = court.strip().lower(), case_number.strip()
    if not court or not case_number:
        raise ValidationError(
            f"Invalid court case input {value!r}. Expected "
            "<court_identifier>:<case_number> (e.g. special:080-CR-0111)."
        )
    if court not in VALID_COURT_IDENTIFIERS:
        valid_list = ", ".join(sorted(VALID_COURT_IDENTIFIERS))
        raise ValidationError(
            f"Invalid court identifier '{court}'. Valid identifiers are: {valid_list}"
        )
    try:
        return build_courtcase_iri(court, case_number)
    except ValueError:
        raise ValidationError(
            f"Invalid case number {case_number!r} in court case input "
            f"{value!r}. Case numbers must be letters/digits with '.', '_' "
            "or '-' separators (e.g. 080-CR-0111)."
        )


def validate_courtcase_iri(value):
    """Validator: a court-case reference must be the canonical ``@id`` IRI.

    ``https://<base>/courtcase/<court>/<case_number>`` — lowercase grammar,
    scheme + host anchored to the platform ``iri_base()``, and the court must
    be a known identifier (``COURT_CHOICES``). No other reference form is
    accepted (clean-slate contract, mirroring ``nes_id``/``material_iri``).
    """
    if not is_valid_courtcase_iri(value):
        raise ValidationError(
            f"{value!r} is not a valid court-case @id IRI "
            "(expected https://<base>/courtcase/<court>/<case_number>)."
        )
    court = parse_courtcase_iri(value).court
    if court not in VALID_COURT_IDENTIFIERS:
        valid_list = ", ".join(sorted(VALID_COURT_IDENTIFIERS))
        raise ValidationError(
            f"Invalid court identifier '{court}'. Valid identifiers are: {valid_list}"
        )


def validate_court_cases(value):
    """
    Validate a court_cases list of court-case references.

    Rules:
    - Must be a list
    - Each element must be a string
    - Each string must be the canonical court-case @id IRI
      (https://<base>/courtcase/<court>/<case_number>) naming a known court

    Args:
        value: The court_cases list to validate

    Raises:
        ValidationError: If the court_cases list is invalid

    Examples:
        Valid: ["https://jawafdehi.org/courtcase/special/080-cr-0111"],
               []
        Invalid: "https://jawafdehi.org/courtcase/special/080-cr-0111"
                 (string instead of list),
                 ["supreme:2078-CR-0123"] (short form — IRIs only),
                 ["https://jawafdehi.org/courtcase/invalid-court/123"]
                 (unknown court identifier)
    """
    # Check if value is a list
    if not isinstance(value, list):
        raise ValidationError("court_cases must be a list")

    # Validate each element in the list
    for item in value:
        # Check if element is a string
        if not isinstance(item, str):
            raise ValidationError("Each court case reference must be a string")

        validate_courtcase_iri(item)
