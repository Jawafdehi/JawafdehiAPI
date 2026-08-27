"""Static court → location (district, province) mapping for the unified search.

There is deliberately NO database column behind this: the ``courts`` table is
owned by the SQLAlchemy side of the shared prod NGM database (this app's
migrations are ``--fake``d against it), so location is derived in-repo from the
court identifier instead — district courts resolve through the scraper's
:data:`~courts.scraper.court_ids.DISTRICT_COURTS` table (their ``Court.identifier``
IS the ``code_name``), high courts through the hand-authored seat table below,
and the two national-jurisdiction courts (supreme, special) get an explicit
:data:`NATIONAL` sentinel so "no location" is a visible, filterable group rather
than an absent field.

Spelling is canonical-as-in-repo: district keys match ``district_en`` in
``court_ids.py`` VERBATIM (``Kavrepalanchowk``, ``Therathum``, and the split
districts under ``Nawalpur``/``Nawalparasi`` and ``Rukum``/``Rukumkot``) —
``tests/test_geography.py`` pins full coverage so the two tables cannot drift.
"""

from __future__ import annotations

from courts.scraper.court_ids import DISTRICT_COURTS

#: Sentinel district/province for national-jurisdiction courts. UPPER-CASE on
#: purpose: it can never collide with a real title-case district or province
#: name, and the SPA maps it to a display label like any other raw facet token.
NATIONAL = "NATIONAL"

#: Courts whose jurisdiction is the whole country — no district, no province.
NATIONAL_COURTS = frozenset({"supreme", "special"})

#: All 77 districts → their province (the 7-province structure, 2015
#: constitution). Keys are the repo's ``district_en`` spellings, verbatim.
#: Split-district naming as in ``court_ids.py``: ``Nawalpur`` is Nawalparasi
#: East (Gandaki) and ``Nawalparasi`` is Nawalparasi West (Lumbini);
#: ``Rukumkot`` is Rukum East (Lumbini) and ``Rukum`` is Rukum West (Karnali).
DISTRICT_PROVINCE: dict[str, str] = {
    # Koshi (14)
    "Bhojpur": "Koshi",
    "Dhankuta": "Koshi",
    "Ilam": "Koshi",
    "Jhapa": "Koshi",
    "Khotang": "Koshi",
    "Morang": "Koshi",
    "Okhaldhunga": "Koshi",
    "Panchthar": "Koshi",
    "Sankhuwasabha": "Koshi",
    "Solukhumbu": "Koshi",
    "Sunsari": "Koshi",
    "Taplejung": "Koshi",
    "Therathum": "Koshi",
    "Udayapur": "Koshi",
    # Madhesh (8)
    "Bara": "Madhesh",
    "Dhanusha": "Madhesh",
    "Mahottari": "Madhesh",
    "Parsa": "Madhesh",
    "Rautahat": "Madhesh",
    "Saptari": "Madhesh",
    "Sarlahi": "Madhesh",
    "Siraha": "Madhesh",
    # Bagmati (13)
    "Bhaktapur": "Bagmati",
    "Chitwan": "Bagmati",
    "Dhading": "Bagmati",
    "Dolakha": "Bagmati",
    "Kathmandu": "Bagmati",
    "Kavrepalanchowk": "Bagmati",
    "Lalitpur": "Bagmati",
    "Makwanpur": "Bagmati",
    "Nuwakot": "Bagmati",
    "Ramechhap": "Bagmati",
    "Rasuwa": "Bagmati",
    "Sindhuli": "Bagmati",
    "Sindhupalchowk": "Bagmati",
    # Gandaki (11)
    "Baglung": "Gandaki",
    "Gorkha": "Gandaki",
    "Kaski": "Gandaki",
    "Lamjung": "Gandaki",
    "Manang": "Gandaki",
    "Mustang": "Gandaki",
    "Myagdi": "Gandaki",
    "Nawalpur": "Gandaki",
    "Parbat": "Gandaki",
    "Syangja": "Gandaki",
    "Tanahun": "Gandaki",
    # Lumbini (12)
    "Arghakhanchi": "Lumbini",
    "Banke": "Lumbini",
    "Bardiya": "Lumbini",
    "Dang": "Lumbini",
    "Gulmi": "Lumbini",
    "Kapilbastu": "Lumbini",
    "Nawalparasi": "Lumbini",
    "Palpa": "Lumbini",
    "Pyuthan": "Lumbini",
    "Rolpa": "Lumbini",
    "Rukumkot": "Lumbini",
    "Rupandehi": "Lumbini",
    # Karnali (10)
    "Dailekh": "Karnali",
    "Dolpa": "Karnali",
    "Humla": "Karnali",
    "Jajarkot": "Karnali",
    "Jumla": "Karnali",
    "Kalikot": "Karnali",
    "Mugu": "Karnali",
    "Rukum": "Karnali",
    "Salyan": "Karnali",
    "Surkhet": "Karnali",
    # Sudurpashchim (9)
    "Achham": "Sudurpashchim",
    "Baitadi": "Sudurpashchim",
    "Bajhang": "Sudurpashchim",
    "Bajura": "Sudurpashchim",
    "Dadeldhura": "Sudurpashchim",
    "Darchula": "Sudurpashchim",
    "Doti": "Sudurpashchim",
    "Kailali": "Sudurpashchim",
    "Kanchanpur": "Sudurpashchim",
}

#: High-court identifier → SEAT district. Hand-authored (``HIGH_COURTS`` carries
#: no district); keyed off the identifiers exactly as the repo spells them
#: (``illamhc``, ``birganjhc``). A high court's benches sit in other districts
#: too — this maps the principal seat only.
HIGH_COURT_SEATS: dict[str, str] = {
    "biratnagarhc": "Morang",
    "illamhc": "Ilam",
    "dhankutahc": "Dhankuta",
    "okhaldhungahc": "Okhaldhunga",
    "janakpurhc": "Dhanusha",
    "rajbirajhc": "Saptari",
    "birganjhc": "Parsa",
    "patanhc": "Lalitpur",
    "hetaudahc": "Makwanpur",
    "pokharahc": "Kaski",
    "baglunghc": "Baglung",
    "tulsipurhc": "Dang",
    "butwalhc": "Rupandehi",
    "nepalgunjhc": "Banke",
    "surkhethc": "Surkhet",
    "jumlahc": "Jumla",
    "dipayalhc": "Doti",
    "mahendranagarhc": "Kanchanpur",
}

#: District-court identifier (``code_name``) → district. A district court's
#: ``Court.identifier`` IS its ``code_name`` (see courts/scraper/registry.py).
# str() casts: DISTRICT_COURTS rows also carry int values (district_id), so the
# row type is dict[str, str | int] and the comprehension needs narrowing.
_DISTRICT_COURT_DISTRICT: dict[str, str] = {
    str(c["code_name"]): str(c["district_en"]) for c in DISTRICT_COURTS
}

# Fail at import time, not per-document, if the scraper table ever grows a court
# whose district the province table doesn't know (belt to the test's braces).
assert set(_DISTRICT_COURT_DISTRICT.values()) <= set(DISTRICT_PROVINCE), (
    "court_ids.DISTRICT_COURTS names a district missing from DISTRICT_PROVINCE"
)


def court_location(identifier: str) -> tuple[str, str] | None:
    """``(district, province)`` for a court identifier, or ``None`` if unknown.

    ``None`` (not a guess) for an unrecognized identifier: indexing nothing is
    recoverable; indexing a wrong bucket is a lie the facet then serves.
    """
    if identifier in NATIONAL_COURTS:
        return (NATIONAL, NATIONAL)
    district = _DISTRICT_COURT_DISTRICT.get(identifier) or HIGH_COURT_SEATS.get(
        identifier
    )
    if district is None:
        return None
    return (district, DISTRICT_PROVINCE[district])
