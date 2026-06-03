# Case Draft Template

> **Language:** Fill in all content fields in **Nepali** unless otherwise noted. English is acceptable for technical terms, proper nouns (e.g. company names), and fields that are explicitly English-only (tags, dates, URLs).
>
> Reference: https://portal.jawafdehi.org/api/cases/210/

---

## Case Metadata

- **Case Type:** CORRUPTION  <!-- CORRUPTION | OTHER -->
- **State:** DRAFT  <!-- DRAFT | IN_REVIEW -->
- **Title:** <!-- Nepali title with the CIAA/Special Court case number as a suffix in parentheses, e.g. "राष्ट्रिय सूचना प्रविधि केन्द्रमा High Compute Infrastructure खरिदमा भ्रष्टाचार ({{CASE_NUMBER}})" -->
- **Case Start Date:** <!-- AD date (YYYY-MM-DD) — typically the CIAA decision/filing date -->
- **Case End Date:** <!-- AD date if closed; leave blank if still active -->
- **Bigo Amount:** <!-- Integer NPR amount of disputed/embezzled funds (बिगो), e.g. 15880000. Leave blank if unknown. -->

---

## Entities

Entities are the people, organizations, and locations involved in the case.

Entity types:
- **Accused** — Primary defendant(s) named in the charge sheet
- **Related** — Supporting parties, companies, intermediaries, or institutions involved
- **Location** — Geographic locations (district, address, landmark)

### Accused

| Name | Position / Role | NES ID |
|------|----------------|--------|
|  |  |  |

### Related Parties

| Name | Role / Notes | NES ID |
|------|-------------|--------|
|  |  |  |

### Locations

<!-- Locations are linked to the case as Jawaf Entities with relationship_type LOCATION.
     This is a system-specific design: the platform stores locations as regular entities
     (same model as people and organizations), not in a separate location table. -->

| Name |
|------|
|  |

---

## Description

Write the full case narrative in Nepali. Should include:
- Background on the accused and their role
- Nature of the corruption/irregularity
- How the scheme worked
- Financial impact (amount involved, who benefited, who was harmed)
- Legal basis and charges filed

<!-- Write narrative here -->

---

## Key Allegations

2–5 concise Nepali sentences summarising the core allegations. These appear as bullet points on the case page.

- <!-- e.g. "specification विपरीत कम गुणस्तरको उपकरण आपूर्ति गर्दा रु. १५,८८,५०,००० को गैरकानूनी लाभ" -->
-
-

---

## Timeline

Significant events in chronological order. Include both AD and BS dates where known.

### [Event Title] — [BS date] ([AD date])

<!-- Description (optional) -->

### [Event Title] — [BS date] ([AD date])

<!-- Description (optional) -->

---

## Evidence / Sources

Each **source** has two distinct description fields:
- **Source description** — describes what the document *is*: its content, origin, and key metadata. Set when uploading via `upload_document_source`.
- **Evidence description** — explains *how* this source connects to and supports this specific case. Set when attaching the source to the case via the evidence patch operation.

### 1. [Document Title]

- **Type:** OFFICIAL_GOVERNMENT
- **Source Description:** <!-- Nepali. What this document is: e.g. "अख्तियारद्वारा विशेष अदालतमा दायर गरिएको अभियोग पत्र — मुद्दा {{CASE_NUMBER}}, मिति २०८१-०५-१५, प्रतिवादी Ram Prasad Sharma" -->
- **Evidence Description:** <!-- Nepali. How it supports this case: e.g. "यो अभियोग पत्रले घुसखोरीको आरोप र रु. ९.२२ करोडको बिगो रकम पुष्टि गर्दछ।" -->
- **URL:** <!-- https://s3.jawafdehi.org/case_uploads/... or web URL -->

### 2. [Document Title]

- **Type:** MEDIA_NEWS
- **Publication Date:** <!-- YYYY-MM-DD — required for all news sources -->
- **Source Description:** <!-- Nepali. What the article reports -->
- **Evidence Description:** <!-- Nepali. How this article relates to the case -->
- **URL:** <!-- Original newspaper article URL -->

---

## Tags

2–5 topic tags in English. Pick from existing tags where possible (e.g. `Procurement`, `CIAA`, `IT`, `Land`, `Banking`, `Revenue`) or add a new one if needed.

-
-

---

## Missing Details

List any information that could not be found or verified at the time of drafting. This helps reviewers and future caseworkers know what still needs to be sourced.

- [ ] <!-- e.g. Charge sheet / अभियोग पत्र -->
- [ ] <!-- e.g. Written defence / प्रतिवादीहरूको लिखित जवाफ -->
- [ ] <!-- e.g. Witness statements / साक्षीको वकपत्र -->
- [ ] <!-- e.g. Special court verdict / फैसलाको प्रतिलिपि -->
- [ ] <!-- e.g. Photos of accused -->

---

## Internal Notes

Internal working notes — not published. Include caseworker name and date drafted.

<!-- Case drafted by [Name]. [Date]. -->

---

## Images

Case-relevant images identified during news research (photos of accused, co-defendants, official press conference photos, property/crime-scene photos). Populated from `MEMORY.md ## Images` by the caseworker.

- <!-- ![Caption](url) — context, source outlet -->
