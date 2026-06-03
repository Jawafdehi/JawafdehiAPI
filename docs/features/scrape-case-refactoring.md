# Feature: scrape_case Refactoring & Database Import

## Overview

This document covers the complete refactoring of the `scrape_case` management command, including:
1. Extraction of GenAI scraping logic into reusable service
2. Timeline field rename (`event_date` → `date`)
3. Database import functionality with confirmation
4. Entity and source deduplication

## Implementation Status: ✅ Complete

All features have been implemented and are ready for testing.

---

## What Was Implemented

### 1. ✅ Renamed Timeline Field
- Changed `TimelineEntry.event_date` → `TimelineEntry.date` in the scraper
- Fixed Pydantic naming conflict by importing `date` as `date_type`
- Now matches Django model's expected field name
- No transformation needed during import

### 2. ✅ Extracted GenAI Logic to Service
Created `cases/services/case_scraper.py` with `CaseScraper` class:
- Encapsulates all Google AI scraping logic
- Handles credential management and client initialization
- Two-phase scraping process (extract → structure)
- Configurable language, model, location
- Progress logging support
- Reusable across commands, APIs, background tasks

### 3. ✅ Created Database Import Service
Created `cases/services/case_importer.py` with `CaseImporter` class:
- Entity deduplication by `display_name`
- Source deduplication by URL, then title
- Handles location dict/string formats
- Transaction-safe imports with rollback
- Statistics tracking and logging
- Comprehensive error handling

### 4. ✅ Added Database Import to Command
Enhanced `scrape_case` management command with:
- `--create-db-entry` flag to enable database import
- `--no-confirm` flag to skip confirmation prompt
- `--case-type` flag (CORRUPTION)
- `--case-state` flag (DRAFT, IN_REVIEW, or PUBLISHED)
- Interactive confirmation with detailed preview
- Import statistics and progress logging

---

## File Structure

```
services/jawafdehi-api/
├── cases/
│   ├── services/
│   │   ├── __init__.py              # Exports CaseScraper, CaseImporter
│   │   ├── case_scraper.py          # GenAI scraping service (NEW)
│   │   └── case_importer.py         # Database import service (NEW)
│   └── management/
│       └── commands/
│           └── scrape_case.py       # Refactored command using services
└── docs/
    └── features/
        └── scrape-case-refactoring.md   # This file
```

---

## Architecture

### Service Layer Pattern

```
┌─────────────────────────────────────────────────────────┐
│  Management Command (scrape_case.py)                    │
│  - CLI argument parsing                                 │
│  - User interaction (confirmation)                      │
│  - Progress logging                                     │
└────────────┬────────────────────────────┬───────────────┘
             │                            │
             ▼                            ▼
┌────────────────────────┐  ┌────────────────────────────┐
│  CaseScraper Service   │  │  CaseImporter Service      │
│  - Google AI client    │  │  - Entity deduplication    │
│  - Phase 1: Extract    │  │  - Source deduplication    │
│  - Phase 2: Structure  │  │  - Data transformation     │
│  - Pydantic validation │  │  - Django ORM operations   │
└────────────────────────┘  └────────────────────────────┘
```

### Data Flow

```
Source Files (MD/PDF/TXT)
    ↓
CaseScraper.scrape_case()
    ↓ Phase 1: Extract
Raw Information (Markdown)
    ↓ Phase 2: Structure
Pydantic Case Model (JSON)
    ↓ (if --create-db-entry)
Confirmation Preview
    ↓ (user confirms)
CaseImporter.import_from_json()
    ↓
Django Case Model (Database)
```

---

## Usage Examples

### Basic Usage

#### Scrape Only (Original Behavior)
```bash
python manage.py scrape_case case-details.md
```

#### Scrape + Import with Confirmation
```bash
python manage.py scrape_case case-details.md --create-db-entry
```

#### Scrape + Import without Confirmation (Automated)
```bash
python manage.py scrape_case case-details.md \
    --create-db-entry \
    --no-confirm
```

### Advanced Usage

#### Custom Case Type and State
```bash
python manage.py scrape_case case-details.md \
    --create-db-entry \
    --case-type CORRUPTION \
    --case-state IN_REVIEW
```

#### Multiple Sources
```bash
python manage.py scrape_case \
    source1.md source2.pdf source3.txt \
    --create-db-entry
```

#### Nepali Language
```bash
python manage.py scrape_case case-details.md \
    --language np \
    --create-db-entry
```

#### Custom Work Directory and Service Account
```bash
python manage.py scrape_case case-details.md \
    --work-dir /custom/path \
    --service-account /path/to/key.json \
    --create-db-entry
```

### Complete Example Output

```
/tmp/scrape-case-20241206-143022
Created work directory: /tmp/scrape-case-20241206-143022
Initializing case scraper...
Reading source files...
  Read 5432 characters from case-details.md

Phase 1: Extracting information...
  Extracting raw information from sources...
  Extracted 12543 characters

Phase 2: Structuring data...
  Converting raw data to structured format...
  Validation successful

Completed successfully!
  Case title: Ncell Tax Evasion Allegations
  Allegations: 3
  Timeline entries: 5
  Sources: 2
  Result saved to: /tmp/scrape-case-20241206-143022/case-result.json
  Total time elapsed: 45.23 seconds

============================================================
DATABASE IMPORT PREVIEW
============================================================
Title: Ncell Tax Evasion Allegations
Case Type: CORRUPTION
Initial State: DRAFT
Alleged entities: Ncell Pvt. Ltd., Reynolds Holdings
Related entities: Nepal Telecommunications Authority
Key allegations: 3
Timeline entries: 5
Sources: 2
Tags: telecommunications, tax-evasion, capital-gains
============================================================

Proceed with database import? [y/N]: y

Importing to database...
Importing case: Ncell Tax Evasion Allegations
Created case: case-a1b2c3d4e5f6
Processing alleged entities...
  Created entity: Ncell Pvt. Ltd.
  Created entity: Reynolds Holdings
Processing related entities...
  Created entity: Nepal Telecommunications Authority
Processing locations...
  Created entity: Kathmandu
Processing sources...
  Created source: Supreme Court Verdict 2075
  Created source: Office of the Auditor General Report

Import statistics:
  Entities created: 4
  Entities reused: 0
  Sources created: 2
  Sources reused: 0

✓ Database import successful!
  Case ID: case-a1b2c3d4e5f6
  Version: 1
  State: DRAFT
  Type: CORRUPTION
```

---

## Command Reference

### All Available Flags

```bash
python manage.py scrape_case [OPTIONS] SOURCE_PATH [SOURCE_PATH ...]

Required:
  SOURCE_PATH               One or more source file paths to scrape

Optional:
  --language {en,np}        Output language (default: en)
  --work-dir PATH           Base directory for work files (default: /tmp)
  --service-account PATH    Google service account key file (default: .service-account-key.json)
  --project PROJECT_ID      Google Cloud project ID (defaults to service account)
  --location LOCATION       Google Cloud location (default: us-central1)
  --model MODEL             Google AI model (default: gemini-2.5-pro)

Database Import:
  --create-db-entry         Create database entry after scraping
  --no-confirm              Skip confirmation prompt
  --case-type {CORRUPTION,CORRUPTION}    Case type (default: CORRUPTION)
  --case-state {DRAFT,IN_REVIEW,PUBLISHED}    Initial state (default: DRAFT)
```

---

## Data Mapping

### Pydantic Model → Django Model

| Pydantic Field | Django Field | Type | Notes |
|----------------|--------------|------|-------|
| `title` | `Case.title` | CharField | Direct mapping |
| `description` | `Case.description` | TextField | Rich HTML format |
| `key_allegations` | `Case.key_allegations` | TextListField | List of strings |
| `case_start_date` | `Case.case_start_date` | DateField | ISO format |
| `case_end_date` | `Case.case_end_date` | DateField | ISO format |
| `tags` | `Case.tags` | TextListField | List of strings |
| `timeline` | `Case.timeline` | TimelineListField | Field name: `date` |
| `alleged_entities` | `Case.alleged_entities` | ManyToMany | Via `get_or_create_entity()` |
| `related_entities` | `Case.related_entities` | ManyToMany | Via `get_or_create_entity()` |
| `locations` | `Case.locations` | ManyToMany | Handles dict/string formats |
| `sources` | `Case.evidence` | EvidenceListField | Via `get_or_create_source()` |
| N/A | `Case.case_type` | CharField | From `--case-type` flag |
| N/A | `Case.state` | CharField | From `--case-state` flag |
| N/A | `Case.case_id` | CharField | Auto-generated UUID |
| N/A | `Case.version` | IntegerField | Default: 1 |

### Special Handling

#### Timeline Events
Timeline field name matches Django model (no transformation needed):
```python
# Pydantic (case_scraper.py)
class TimelineEntry(BaseModel):
    date: date_type  # Serializes as "date"
    title: str
    description: str

# Django (models.py)
timeline = TimelineListField()  # Expects "date" field
```

#### Locations
Supports both string and dict formats:
```python
# String format
"locations": ["Pokhara", "Kathmandu"]

# Dict format (extracts: other > district > municipality)
"locations": [
    {"district": "Kaski", "municipality": "Pokhara"},
    {"province": "Bagmati", "district": "Kathmandu"}
]
```

#### Evidence
Transforms sources to evidence format:
```python
# Pydantic sources
"sources": [
    {"title": "Court Verdict", "url": "https://...", "description": "..."}
]

# Django evidence
"evidence": [
    {"source_id": "source:20241206:abc123", "description": "..."}
]
```

---

## Technical Details

### Entity Deduplication

**Strategy**: Match by `display_name` (case-sensitive)

```python
# First occurrence
entity = JawafEntity.objects.create(display_name="Ncell Pvt. Ltd.")
# Stats: entities_created += 1

# Subsequent occurrences
entity = JawafEntity.objects.filter(display_name="Ncell Pvt. Ltd.").first()
# Stats: entities_reused += 1
```

**Benefits**:
- Prevents duplicate entities
- Cached within import session
- Consistent entity references across cases

### Source Deduplication

**Strategy**: Match by URL (primary), then title (fallback)

```python
# Try URL match first
source = DocumentSource.objects.filter(url=url).first()

# Fallback to title match
if not source:
    source = DocumentSource.objects.filter(title=title).first()

# Create new if no match
if not source:
    source = DocumentSource.objects.create(...)
```

**Benefits**:
- Prevents duplicate sources
- Handles sources with/without URLs
- Reuses existing source records

### Transaction Safety

All database operations wrapped in `transaction.atomic()`:

```python
with transaction.atomic():
    # Create case
    case = Case(...)
    case.save()

    # Add relationships
    case.alleged_entities.add(entity1, entity2)
    case.locations.add(location1)

    # Update evidence
    case.evidence = [...]
    case.save()
```

**Benefits**:
- Rollback on any error
- Consistent database state
- No partial imports

### Pydantic Type Annotation Fix

Fixed naming conflict between field name and type:

```python
# ❌ Causes Pydantic error
from datetime import date
class TimelineEntry(BaseModel):
    date: date  # Field name clashes with type

# ✅ Fixed with alias
from datetime import date as date_type
class TimelineEntry(BaseModel):
    date: date_type  # Field serializes as "date", type is date_type
```

---

## Benefits

### For Developers
- **Reusable Services**: Use `CaseScraper` and `CaseImporter` in any context
- **Testable**: Easy to unit test services independently
- **Maintainable**: Clear separation of concerns
- **Type Safe**: Pydantic models with validation
- **Extensible**: Easy to add new features

### For Users
- **Single Command**: Scrape and import in one step
- **Safe**: Confirmation prompt prevents accidents
- **Transparent**: Preview before import with all details
- **Flexible**: Configure case type, state, language
- **Informative**: Detailed progress and statistics

### For Operations
- **Automated**: Use `--no-confirm` for scripts/CI
- **Auditable**: Detailed logging and statistics
- **Transactional**: Automatic rollback on errors
- **Deduplication**: Prevents duplicate entities/sources
- **Reliable**: Comprehensive error handling

---

## Migration Guide

### From seed_database.py

**Old workflow** (two steps):
```bash
# Step 1: Scrape
python manage.py scrape_case case.md

# Step 2: Import (separate script)
python laboratory/case-research/seed_database.py --dir /tmp/scrape-case-*
```

**New workflow** (single command):
```bash
python manage.py scrape_case case.md --create-db-entry
```

**Note**: The `seed_database.py` script can still be used for batch imports of existing scraped data.

---

## Testing Checklist

### Unit Tests Needed
- [ ] `CaseScraper` initialization
- [ ] `CaseScraper` phase 1 extraction
- [ ] `CaseScraper` phase 2 structuring
- [ ] `CaseImporter` entity deduplication
- [ ] `CaseImporter` source deduplication
- [ ] `CaseImporter` location handling (string/dict)
- [ ] `CaseImporter` data transformation
- [ ] `CaseImporter` error handling

### Integration Tests Needed
- [ ] Scrape with single source
- [ ] Scrape with multiple sources
- [ ] Database import with confirmation
- [ ] Database import with `--no-confirm`
- [ ] Duplicate case detection
- [ ] Entity reuse across imports
- [ ] Source reuse across imports
- [ ] Nepali language output
- [ ] Transaction rollback on error
- [ ] Keyboard interrupt during confirmation

### Manual Testing
- [ ] Test with real case data
- [ ] Test with various source formats (MD, PDF, TXT)
- [ ] Test error scenarios (invalid JSON, missing fields)
- [ ] Test with existing entities/sources in database
- [ ] Test confirmation prompt UX
- [ ] Verify import statistics accuracy

---

## Future Enhancements

### Short Term
- [ ] Add unit tests for services
- [ ] Add integration tests for command
- [ ] Add progress bars for long operations
- [ ] Support for batch import (multiple cases)

### Medium Term
- [ ] Update existing cases (version management)
- [ ] NES entity resolution integration
- [ ] Custom import hooks/callbacks
- [ ] Configurable validation rules
- [ ] Import dry-run mode

### Long Term
- [ ] Web UI for case scraping
- [ ] Async/background scraping with Celery
- [ ] Conflict resolution strategies
- [ ] Import templates/presets
- [ ] Machine learning for entity extraction

---

## Troubleshooting

### Common Issues

#### Import Error: "Case with title '...' already exists"
**Solution**: Case titles must be unique. Either:
- Modify the title in the source document
- Delete the existing case from database
- (Future) Use update mode instead of create

#### Import Error: "Validation error: ..."
**Solution**: Check that scraped data meets Django model requirements:
- Title is required and non-empty
- Dates are in ISO format (YYYY-MM-DD)
- At least one alleged entity for IN_REVIEW/PUBLISHED states

#### Pydantic Error: "unevaluable-type-annotation"
**Solution**: Already fixed by importing `date` as `date_type`

#### Google AI Error: "Authentication failed"
**Solution**: Verify service account key file:
- File exists at specified path
- File contains valid JSON
- Service account has necessary permissions

---

## References

### Code Files
- Command: `services/jawafdehi-api/cases/management/commands/scrape_case.py`
- Scraper Service: `services/jawafdehi-api/cases/services/case_scraper.py`
- Importer Service: `services/jawafdehi-api/cases/services/case_importer.py`
- Django Models: `services/jawafdehi-api/cases/models.py`
- Field Definitions: `services/jawafdehi-api/cases/fields.py`

### Related Documentation
- Django Models: See `.kiro/specs/accountability-platform-core/design.md`
- NES Integration: See `services/NepalEntityService/docs/`
- Google AI: https://cloud.google.com/vertex-ai/docs

---

## Changelog

### 2024-12-06 - Initial Implementation
- ✅ Renamed timeline field from `event_date` to `date`
- ✅ Extracted GenAI logic to `CaseScraper` service
- ✅ Created `CaseImporter` service with deduplication
- ✅ Added `--create-db-entry` flag with confirmation
- ✅ Added `--case-type` and `--case-state` flags
- ✅ Added progress logging throughout scraping
- ✅ Fixed Pydantic type annotation conflict
- ✅ Implemented transaction-safe imports
