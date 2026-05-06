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
    "birgunjhc",
    "butwalhc",
    "dhankutahc",
    "dipayalhc",
    "hetaudahc",
    "ilamhc",
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
    "arghakhanchidc",
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
    "ramechhapdc",
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
    "tehrathumdc",
    "udayapurdc",
]

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
