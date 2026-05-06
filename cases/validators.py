"""
Validation functions for Case model fields.

This module provides centralized validation logic for case fields,
following Django's convention of separating validation concerns.
"""

import re
from django.core.exceptions import ValidationError

# Valid court identifiers for Nepal's court system
VALID_COURT_IDENTIFIERS = [
    # Supreme/Special
    "supreme",
    "special",
    # High Courts
    "baglunghc",
    "biratnagarhc",
    "birganjhc",
    "butwalhc",
    "dhankutahc",
    "dipayalhc",
    "hetaudahc",
    "illamhc",
    "janakpurhc",
    "jumlahc",
    "mahendranagarhc",
    "nepalgunjhc",
    "okhaldhungahc",
    "patanhc",
    "pokharahc",
    "rajbirajhc",
    "surkhethc",
    "tulsipurhc",
    # District Courts
    "achhamdc",
    "argakhanchidc",
    "baglungdc",
    "baitadidc",
    "bajhangdc",
    "bajuradc",
    "bankedc",
    "baradc",
    "bardiyadc",
    "bhaktapurdc",
    "bhojpurdc",
    "chitwandc",
    "dadeldhuradc",
    "dailekhdc",
    "dangdc",
    "darchuladc",
    "dhadingdc",
    "dhankutadc",
    "dhanusadc",
    "dolakhadc",
    "dolpadc",
    "dotidc",
    "gorkhadc",
    "gulmidc",
    "humladc",
    "ilamdc",
    "jajarkotdc",
    "jhapadc",
    "jumladc",
    "kailalidc",
    "kalikotdc",
    "kanchanpurdc",
    "kapilbastudc",
    "kaskidc",
    "kathmandudc",
    "kavrepalanchowkdc",
    "khotangdc",
    "lalitpurdc",
    "lamjungdc",
    "mahottaridc",
    "makwanpurdc",
    "manangdc",
    "morangdc",
    "mugudc",
    "mustangdc",
    "myagdidc",
    "nawalparasidc",
    "nawalpurdc",
    "nuwakotdc",
    "okhaldhungadc",
    "palpadc",
    "panchthardc",
    "parbatdc",
    "parsadc",
    "pyuthandc",
    "ramechapdc",
    "rasuwadc",
    "rautahatdc",
    "rolpadc",
    "rukumdc",
    "rukumkotdc",
    "rupandehidc",
    "salyandc",
    "sankhuwasabhadc",
    "saptaridc",
    "sarlahidc",
    "sindhulidc",
    "sindhupalchowkdc",
    "sirahadc",
    "solukhumbudc",
    "sunsaridc",
    "surkhetdc",
    "syangjadc",
    "tanahundc",
    "taplejungdc",
    "therathumdc",
    "udayapurdc",
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


def validate_court_cases(value):
    """
    Validate court_cases list structure and content.

    Rules:
    - Must be a list
    - Each element must be a string
    - Each string must match format: <court_identifier>:<case_number>
    - Court identifier must be in VALID_COURT_IDENTIFIERS list

    Args:
        value: The court_cases list to validate

    Raises:
        ValidationError: If the court_cases list is invalid

    Examples:
        Valid: ["supreme:2078-CR-0123"],
               ["special:2076-CR-0456"],
               []
        Invalid: "supreme:2078-CR-0123" (string instead of list),
                 ["invalid-court:123"] (unknown court identifier),
                 ["supreme-2078-CR-0123"] (missing colon),
                 ["supreme:2078:CR:0123"] (multiple colons),
                 ["supreme:"] (empty case number)
    """
    # Check if value is a list
    if not isinstance(value, list):
        raise ValidationError("court_cases must be a list")

    # Validate each element in the list
    for item in value:
        # Check if element is a string
        if not isinstance(item, str):
            raise ValidationError("Each court case reference must be a string")

        # Check format: must contain exactly one colon
        if item.count(":") != 1:
            raise ValidationError(
                "Court case reference must be in format <court_identifier>:<case_number>"
            )

        # Split and validate court identifier
        court_identifier, case_number = item.split(":", 1)

        # Validate case_number is not empty
        if not case_number or not case_number.strip():
            raise ValidationError("Case number cannot be empty in court case reference")

        if court_identifier not in VALID_COURT_IDENTIFIERS:
            valid_list = ", ".join(VALID_COURT_IDENTIFIERS)
            raise ValidationError(
                f"Invalid court identifier '{court_identifier}'. "
                f"Valid identifiers are: {valid_list}"
            )
