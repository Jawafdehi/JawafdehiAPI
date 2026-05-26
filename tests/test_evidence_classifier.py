from cases.services.evidence_classifier import EvidenceClassifier, EvidenceSection


def test_evidence_classifier_routes_press_release_sections():
    result = EvidenceClassifier().classify(
        case_id="CASE-1",
        evidence_text="अख्तियार प्रेस विज्ञप्ति: बिगो मागदाबी र भ्रष्टाचार आरोप",
        title="CIAA press release",
    )

    assert EvidenceSection.KEY_ALLEGATIONS in result.sections
    assert EvidenceSection.BIGO in result.sections
    assert EvidenceSection.ENTITIES in result.sections
    assert result.source_type == "press_release"
    assert result.idempotency_key.startswith("case:CASE-1:evidence:")
    assert result.confidence > 0.5


def test_evidence_classifier_routes_court_source_type():
    result = EvidenceClassifier().classify(
        case_id="CASE-2",
        evidence_text="Special Court judgment dated 2080 with फैसला details",
        source_type="legal-court-order",
    )

    assert result.sections[0] == EvidenceSection.COURT_PROCEEDINGS
    assert EvidenceSection.TIMELINE in result.sections
    assert result.source_type == "legal_court_order"


def test_evidence_classifier_defaults_unknown_docs_to_source_documents():
    result = EvidenceClassifier().classify(
        case_id="CASE-3",
        evidence_text="generic attachment with no useful routing signal",
    )

    assert result.sections == (EvidenceSection.SOURCE_DOCUMENTS,)
    assert result.reasons == ("default:source_documents",)
