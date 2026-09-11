"""Unit tests for ``courts.search_visibility`` — the public-search show/hide gate.

Pure-function tests over lightweight fake cases + the real committed
``case_type_codes.json.gz`` map; no DB needed (the published-reference set is
patched in).
"""

import unicodedata
from types import SimpleNamespace

import pytest

from courts import search_visibility as sv


def _case(
    *,
    case_type=None,
    court_id="patanhc",
    case_number="081-CI-0001",
    is_deleted=False,
    iri="https://jawafdehi.org/courtcase/patanhc/081-ci-0001",
    plaintiff="नेपाल सरकार",
    defendant="प्रतिवादी",
):
    return SimpleNamespace(
        case_type=case_type,
        court_id=court_id,
        case_number=case_number,
        is_deleted=is_deleted,
        iri=iri,
        plaintiff=plaintiff,
        defendant=defendant,
    )


@pytest.fixture(autouse=True)
def _empty_published(monkeypatch):
    # Default: no PUBLISHED references (patched, so no DB hit). Overridden per-test.
    monkeypatch.setattr(sv, "_published_iris", frozenset())


def test_case_type_code_known_and_unknown():
    assert sv.case_type_code("भ्रष्टाचार") == "CORRUPTION"
    assert sv.case_type_code("सम्बन्ध विच्छेद") == "DIVORCE"
    assert sv.case_type_code("____ not a real case type ____") is None
    assert sv.case_type_code(None) is None


def test_is_cr_series():
    assert sv.is_cr_series("081-CR-0081")
    assert not sv.is_cr_series("081-CI-0081")
    assert not sv.is_cr_series(None)


def test_show_code_is_visible():
    assert sv.court_case_public_visible(_case(case_type="भ्रष्टाचार")) is True


def test_private_code_is_hidden():
    # लेनदेन → MONEYLENDING_DEBT (HIDE) in an ordinary court.
    assert sv.court_case_public_visible(_case(case_type="लेनदेन")) is False


def test_sensitive_floor_overrides_forum():
    # DIVORCE is sensitive → hidden even in the Special Court forum.
    c = _case(case_type="सम्बन्ध विच्छेद", court_id="special")
    assert sv.case_type_code(c.case_type) == "DIVORCE"
    assert sv.court_case_public_visible(c) is False


def test_forum_shows_nonprocedural_unknown():
    # An unmapped case_type in the Special Court (not procedural) → shown by forum.
    c = _case(case_type="____ novel charge ____", court_id="special")
    assert sv.case_type_code(c.case_type) is None
    assert sv.court_case_public_visible(c) is True


def test_forum_hides_procedural():
    # निवेदन → MISC_PETITION (procedural): a special-court petition stays hidden.
    c = _case(case_type="निवेदन", court_id="special")
    assert sv.case_type_code(c.case_type) == "MISC_PETITION"
    assert sv.court_case_public_visible(c) is False


def test_cr_series_forum_shows():
    """The CR-series forum rule, now scoped to the court it was written for.

    It originally matched ``NNN-CR-NNNN`` on ANY court, on the premise that a CR
    series means a CIAA prosecution. The corpus says otherwise: no high or district
    court has a CR series at all, and the Supreme Court's is a general criminal
    docket (SEXUAL_OFFENSE 869, HOMICIDE 762, CORRUPTION 665 in a 4k sample). The
    patanhc case this test used was synthetic.
    """
    c = _case(
        case_type="____ novel ____", court_id="special", case_number="081-CR-0009"
    )
    assert sv.court_case_public_visible(c) is True

    # Same shape, wrong court: an unmapped Supreme criminal case is NOT admitted
    # to a "curated corruption slice" on the strength of its number alone.
    other = _case(
        case_type="____ novel ____", court_id="supreme", case_number="081-CR-0009"
    )
    assert sv.court_case_public_visible(other) is False


def test_is_deleted_is_hidden():
    c = _case(case_type="भ्रष्टाचार", is_deleted=True)
    assert sv.court_case_public_visible(c) is False


def test_published_link_shows_otherwise_hidden(monkeypatch):
    iri = "https://jawafdehi.org/courtcase/patanhc/081-ci-0777"
    c = _case(case_type="लेनदेन", case_number="081-CI-0777", iri=iri)
    assert sv.court_case_public_visible(c) is False
    monkeypatch.setattr(sv, "_published_iris", frozenset({iri}))
    assert sv.court_case_public_visible(c) is True


def test_published_link_does_not_override_sensitive(monkeypatch):
    iri = "https://jawafdehi.org/courtcase/patanhc/081-ci-0888"
    c = _case(case_type="सम्बन्ध विच्छेद", case_number="081-CI-0888", iri=iri)
    monkeypatch.setattr(sv, "_published_iris", frozenset({iri}))
    assert sv.court_case_public_visible(c) is False


class TestCorruptionForumIsCourtScoped:
    """``NNN-CR-NNNN`` is not a CIAA marker on its own.

    The Supreme Court's general criminal register uses the same shape (7,133 rows
    of homicide, forgery, cheque dishonour). Matching the number alone swept that
    whole docket into an index whose stated scope is a curated corruption slice.
    It stayed hidden only by accident — every Supreme row carried the coarse
    'फौजदारी', which maps to OTHER_CRIMINAL and is excluded as procedural — so the
    moment those rows were backfilled with their real charges, an estimated 5,093
    became public, ~1,200 of them homicide.
    """

    def test_supreme_homicide_is_not_in_the_corruption_forum(self):
        case = _case(
            court_id="supreme", case_number="081-CR-1641", case_type="कर्तव्य ज्यान"
        )
        assert sv.in_corruption_forum(case) is False
        assert sv.court_case_public_visible(case) is False

    def test_special_court_cr_still_is(self):
        case = _case(
            court_id="special", case_number="076-CR-0294", case_type="नक्कली प्रमाण पत्र"
        )
        assert sv.in_corruption_forum(case) is True

    def test_a_supreme_corruption_appeal_is_still_shown(self):
        """The fix costs nothing: SHOW_CODES carries it on the code axis."""
        case = _case(
            court_id="supreme", case_number="071-CR-0306", case_type="भ्रष्टाचार"
        )
        assert sv.in_corruption_forum(case) is False
        assert sv.court_case_public_visible(case) is True

    def test_a_district_criminal_docket_is_not_a_corruption_forum(self):
        case = _case(
            court_id="kathmandudc", case_number="080-CR-0012", case_type="कर्तव्य ज्यान"
        )
        assert sv.court_case_public_visible(case) is False

    def test_the_sensitive_floor_is_unaffected(self):
        case = _case(
            court_id="special", case_number="076-CR-0294", case_type="जवरजस्ती करणी"
        )
        assert sv.court_case_public_visible(case) is False


class TestCompositeChargesReachTheSensitiveFloor:
    """A composite charge is coded by its LEAD offence, so the code axis missed.

    ``case_type`` is free text that often names several offences at once, and the
    map gives one code per string. A charge of cheating + kidnapping + rape codes
    as FRAUD_CHEATING — in SHOW_CODES — so the show rule fired and the
    SENSITIVE_CODES floor was never reached. The protected charge had simply never
    become the code, which is why no amount of tuning the code lists could have
    caught it. Every composite of this shape in the corpus was publicly searchable.
    """

    @pytest.mark.parametrize(
        "case_type",
        [
            "ठगी तथा जबरजस्ती करणी",
            "ठगी, अपहरण तथा जबरजस्ती करणी",
            "ठगी तथा मानव बेचबिखन र ओसारपसार",
            "ठगी तथा बालविवाह",
        ],
    )
    def test_a_sensitive_offence_anywhere_hides_the_case(self, case_type):
        """The charge is coded non-sensitively, yet the case must still be hidden."""
        assert sv.case_type_code(case_type) not in sv.SENSITIVE_CODES
        assert sv.names_a_sensitive_offence(case_type) is True
        assert sv.court_case_public_visible(_case(case_type=case_type)) is False

    def test_the_floor_beats_every_show_rule(self):
        """Forum and publish-link must not resurrect it either."""
        iri = "https://jawafdehi.org/courtcase/patanhc/081-ci-0404"
        case = _case(
            case_type="ठगी तथा जबरजस्ती करणी",
            court_id="special",
            case_number="081-CR-0404",
            iri=iri,
        )
        assert sv.court_case_public_visible(case) is False

    def test_an_unmapped_sensitive_charge_survives_the_publish_link(self, monkeypatch):
        """The publish-link path is the one rule an unknown code can still reach.

        A case_type absent from the map fails rule 1 and, outside the corruption
        forum, rule 2 — so a PUBLISHED Jawafdehi case referencing it is the only
        thing that could surface it. The text floor has to hold there too.
        """
        iri = "https://jawafdehi.org/courtcase/patanhc/081-ci-0406"
        case = _case(
            case_type="____ novel ____ जबरजस्ती करणी",
            court_id="patanhc",
            case_number="081-CI-0406",
            iri=iri,
        )
        assert sv.case_type_code(case.case_type) is None
        monkeypatch.setattr(sv, "_published_iris", frozenset({iri}))
        assert sv.is_published_referenced(case) is True
        assert sv.court_case_public_visible(case) is False


class TestSpellingVariantsAreFolded:
    """The registers spell one offence many ways, sometimes inside one string.

    जबरजस्ती/जवरजस्ती, मानव/मानब, बेचबिखन/बेचविखन, बालविवाह/वालविवाह — all appear in
    live rows. A literal substring list catches roughly half of them, which is a
    silent, partial fix: the worst kind for a privacy gate.
    """

    @pytest.mark.parametrize(
        "case_type",
        [
            "ठगी र जवरजस्ती करणी",
            "ठगी मानब बेचबिखन तथा ओसारपसार",
            "ठगी तथा मानव बेचविखन",
            "ठगी तथा मानव बेच बिखन",
            "ठगी तथा वालविवाह",
            "सम्बन्ध बिच्छेद",
        ],
    )
    def test_variant_spellings_are_caught(self, case_type):
        """Each of these spellings occurs in live rows, not just in theory."""
        assert sv.names_a_sensitive_offence(case_type) is True
        assert sv.court_case_public_visible(_case(case_type=case_type)) is False

    def test_zero_width_joiners_are_stripped(self):
        """The registers carry stray ZWJ/ZWNJ — 'राजश्‍व चुहावट' has one mid-word."""
        assert sv.names_a_sensitive_offence("ठगी तथा जबरज‍स्ती कर‌णी") is True

    def test_nukta_forms_are_nfc_normalised(self):
        """Nukta letters arrive both composed and decomposed; NFC settles it."""
        decomposed = unicodedata.normalize("NFD", "चेलीबेटी खख़रिद")
        assert "ख़" not in decomposed  # genuinely decomposed by NFD
        assert sv.names_a_sensitive_offence(decomposed) is True


class TestTheGateDoesNotTrustTheMap:
    """The map misclassifies the commonest form of a protected offence.

    ``जिउ मास्ने बेच्ने`` is the pre-2074 statutory name for human trafficking. The
    map codes the bare form as HOMICIDE (मास्ने read as killing) and its व-spelling
    as OTHER_CRIMINAL, while coding eight LONGER phrasings of the same offence
    correctly as HUMAN_TRAFFICKING. Those rows are absent from the index today only
    because HOMICIDE happens to miss SHOW_CODES — the same accident that already
    failed once, for Supreme 'फौजदारी', the moment the data improved.
    """

    def test_the_archaic_trafficking_name_is_hidden_despite_its_code(self):
        """Coded HOMICIDE and OTHER_CRIMINAL respectively — hidden regardless."""
        for case_type in ("जिउ मास्ने बेच्ने", "जिउ मास्ने वेच्ने", "जीउ मास्ने बेच्ने र ठगी"):
            assert sv.court_case_public_visible(_case(case_type=case_type)) is False

    def test_it_is_hidden_in_the_corruption_forum_too(self):
        """Its code is procedural, so only the text floor can hold here."""
        case = _case(
            case_type="जिउ मास्ने वेच्ने", court_id="special", case_number="081-CR-0505"
        )
        assert sv.in_corruption_forum(case) is True
        assert sv.court_case_public_visible(case) is False


class TestTheFloorDoesNotOverreach:
    """Terms held OUT of the list on purpose, because they are ordinary language.

    Hiding these would cost real accountability cases for nothing.
    """

    @pytest.mark.parametrize(
        "case_type",
        [
            # बेचबिखन is "sale/trade" — of land, and of controlled drugs.
            "जग्गा खरिद बेचबिखन",
            "बिना इजाजत नियन्त्रित औषधीको ओसार पसार बेचबिखन",
            # जबरजस्ती is "by force" — it qualifies coercion as often as करणी.
            "अपराधिक बल प्रयोग गरी जबरजस्ती चेक भर्न लगाई लिएको",
            "जबरजस्ती संस्थाको कार्यालयमा तालाबन्दी गरेको",
        ],
    )
    def test_ordinary_language_is_not_a_sensitive_offence(self, case_type):
        """Held out deliberately — hiding these costs accountability cases."""
        assert sv.names_a_sensitive_offence(case_type) is False

    def test_the_corruption_slice_is_untouched(self):
        """The index's actual purpose must survive the fix."""
        assert sv.court_case_public_visible(_case(case_type="भ्रष्टाचार")) is True
        assert sv.court_case_public_visible(_case(case_type="रिसवत(घुस)")) is True


class TestCourtAnonymisedComplainants:
    """The court's own pseudonym is a signal the charge cannot give us.

    Where a complainant is legally protected the register writes "परिवर्तित नाम
    <locality> <code>" in place of their name. Most such rows carry an entirely
    innocuous charge — plain ठगी or आपराधिक लाभ — so no case_type rule reaches
    them, and we were publishing the defendant's name, district, date and offence
    around a victim the court had already anonymised.
    """

    @pytest.mark.parametrize(
        "plaintiff",
        [
            "परिवर्तित नाम बौद्ध १७ (०८०/०८१)",
            "परिवर्तित नाम काठमाडौं ९ को जाहेरीले नेपाल सरकार",
            "परिवर्तित संकेत नाम ताजकोट",
            "नाम परिवर्तित सिंहदरबार २८",
            "नामथर परिवर्तित २२ जिल्ला मलंगवा",
        ],
    )
    def test_an_anonymised_complainant_hides_an_innocuous_charge(self, plaintiff):
        """The charge is in SHOW_CODES; only the party signal can catch this."""
        case = _case(case_type="ठगी", plaintiff=plaintiff)
        assert sv.case_type_code(case.case_type) in sv.SHOW_CODES
        assert sv.court_case_public_visible(case) is False

    @pytest.mark.parametrize(
        "plaintiff",
        [
            # A COMPANY rename — the same words, an entirely different meaning.
            "एबीसी इनभेस्टमेन्ट प्रा.लि.को परिवर्तित नाम एबीसी हायर पर्चेज प्रा.लि.",
            "परिवर्तित नाम उदाहरण इन्टरप्राइजेज काठमाडौं",
            # An ADDRESS change, which the registers also record with these words.
            "जिल्ला धनुषा साविक वडा नं. १ हाल परिवर्तित शहिदनगर न.पा. वडा नं. २",
        ],
    )
    def test_a_rename_is_not_an_anonymisation(self, plaintiff):
        """These sit on revenue and corruption cases that are core scope."""
        case = _case(case_type="आयकर", plaintiff=plaintiff)
        assert sv.has_anonymised_party(case) is False

    def test_an_anonymised_party_on_either_side_counts(self):
        """Defendants are not anonymised, but checking both sides costs nothing."""
        assert sv.has_anonymised_party(_case(defendant="परिवर्तित नाम ललितपुर ५")) is True

    def test_a_plain_party_is_not_anonymised(self):
        """An ordinary named party must not trip the check."""
        assert sv.has_anonymised_party(_case()) is False

    @pytest.mark.parametrize(
        "plaintiff",
        [
            "परिवर्तित नाम काठमाडौं ९ र एबीसी इनभेस्टमेन्ट प्रा.लि.",
            "एबीसी इनभेस्टमेन्ट प्रा.लि., परिवर्तित नाम काठमाडौं ९",
            "एबीसी प्रा.लि. | परिवर्तित नाम बौद्ध १७",
            "उदाहरण उद्योग प्रा.लि. को हाल परिवर्तित नाम नमुना प्रा.लि., "
            "परिवर्तित नाम ललितपुर ५ को जाहेरीले नेपाल सरकार",
        ],
    )
    def test_a_company_alongside_a_human_does_not_suppress_the_check(self, plaintiff):
        """The company exclusion is judged PER PARTY, never per cell.

        Judged per cell it was a bypass: one renamed company anywhere in the
        cell suppressed the check for a protected complainant named beside it.
        """
        assert sv.has_anonymised_party(_case(plaintiff=plaintiff)) is True
        assert (
            sv.court_case_public_visible(_case(case_type="ठगी", plaintiff=plaintiff))
            is False
        )

    @pytest.mark.parametrize(
        "plaintiff",
        [
            "परिवर्तित‍ नाम बौद्ध १७",  # stray ZWJ inside the pseudonym
            "परिबर्तित नाम बौद्ध १७",  # ब/व swap
            "परिवर्तित  नाम  बौद्ध १७",  # doubled spacing
            "परिवर्तित नामथर बौद्ध १७",
        ],
    )
    def test_the_pseudonym_is_matched_on_the_folded_form(self, plaintiff):
        """A typo in the pseudonym must not be a way past a privacy gate.

        The case_type floor already folds; matching parties on raw text left the
        weaker of the two axes as the way in.
        """
        assert sv.has_anonymised_party(_case(plaintiff=plaintiff)) is True


class TestTheMapWideInvariant:
    """No case_type in the shipped map that NAMES a protected offence is visible.

    This is the regression guard the gate never had. It runs over all ~130k keys of
    the committed map rather than a handful of examples, so a future map
    regeneration — or a new SHOW code — cannot quietly reopen the hole. Both
    defects this class was written for would have failed it at CI time.
    """

    def test_no_sensitive_case_type_is_publicly_visible(self):
        """Over every key in the shipped map, not a sample of it."""
        offenders = [
            case_type
            for case_type in sv._load_map()
            if sv.names_a_sensitive_offence(case_type)
            and sv.court_case_public_visible(_case(case_type=case_type))
        ]
        assert offenders == []

    def test_the_invariant_holds_inside_the_corruption_forum(self):
        """The forum rule is the widest show path, so check the invariant there."""
        offenders = [
            case_type
            for case_type in sv._load_map()
            if sv.names_a_sensitive_offence(case_type)
            and sv.court_case_public_visible(
                _case(
                    case_type=case_type, court_id="special", case_number="081-CR-0001"
                )
            )
        ]
        assert offenders == []

    def test_the_accountability_slice_is_not_collateral_damage(self):
        """Nothing coded CORRUPTION may be swept up by the text floor."""
        swept = [
            case_type
            for case_type, code in sv._load_map().items()
            if code == "CORRUPTION" and sv.names_a_sensitive_offence(case_type)
        ]
        assert swept == []
