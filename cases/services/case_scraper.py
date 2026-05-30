"""
Service for scraping case information using Google AI.

Extracts case data from source documents using Gemini models with a two-phase approach:
1. Phase 1: Extract raw information from sources
2. Phase 2: Structure data into validated schema
"""

import json
from datetime import date as date_type
from pathlib import Path
from typing import List

from google import genai
from google.genai.types import GenerateContentConfig, GoogleSearch, HttpOptions, Tool
from google.oauth2 import service_account
from pydantic import BaseModel, Field


class DocumentSource(BaseModel):
    """Documents, sources, and evidences."""

    title: str = Field(description="Source title")
    url: str = Field(description="URL to the source")
    description: str = Field(default="", description="Brief description of the source")


class Location(BaseModel):
    other: str | None = Field(default=None, description="Other location details")
    ward_no: int | None = Field(default=None, description="Ward number")
    municipality: str | None = Field(default=None, description="Municipality name")
    district: str | None = Field(default=None, description="District name")
    province: str | None = Field(default=None, description="Province name")


class TimelineEntry(BaseModel):
    date: date_type = Field(description="Date of the event")
    title: str = Field(description="Title of the event")
    description: str = Field(description="Description of what happened")


class Case(BaseModel):
    title: str = Field(description="Case title")
    description: str = Field(
        description="Detailed description of the case in rich HTML format with proper formatting, paragraphs, lists, and emphasis"
    )
    unverified_info: str = Field(
        default="",
        description="Unverified or incomplete information in rich HTML format",
    )
    key_allegations: list[str] = Field(description="List of key allegations")
    alleged_entities: list[str] = Field(description="Names of entities being accused")
    related_entities: list[str] = Field(
        default_factory=list, description="Names of related entities"
    )
    locations: list[Location] = Field(
        default_factory=list, description="Locations related to the case"
    )
    tags: list[str] = Field(default_factory=list, description="Tags for categorization")
    case_start_date: date_type | None = Field(
        default=None, description="When the incident began"
    )
    case_end_date: date_type | None = Field(
        default=None, description="When the incident ended"
    )
    timeline: list[TimelineEntry] = Field(
        default_factory=list, description="Timeline of events"
    )
    sources: list[DocumentSource] = Field(
        description="Document sources supporting the case"
    )


SYSTEM_PROMPT_TEMPLATE = """You are a research assistant for a world-class corruption and accountability database focused on Nepali public entities.

LANGUAGE: {language_instruction}

FORMATTING: The description and unverified_info fields must be in rich HTML format:
- Use <p> tags for paragraphs
- Use <strong> for emphasis on important points
- Use <ul> and <li> for bullet lists
- Use <ol> and <li> for numbered lists
- Use <a href="..."> for links to sources
- Use proper HTML structure for readability

ACCURACY IS CRITICAL. Every piece of information must be:
- Factually correct and verifiable
- Backed by credible sources with URLs whenever possible
- Cross-referenced when possible

For sources:
- ALWAYS provide URLs/links whenever available
- Prioritize first-hand evidence (court documents, official reports, government records) over news reporting
- Secondary priority: reputable news outlets
- Include the exact URL where information was found
- If no URL is available, clearly state the source type (e.g., "Internal document", "Interview", etc.)
- Provide clear descriptions of what each source contains

For structured fields (dates, names, amounts, timeline, evidence):
- Only include information that is precise and verifiable
- All dates must be exact (YYYY-MM-DD format)
- All names must be complete and accurate
- All amounts must be specific

For uncertain or incomplete information:
- Place it in the unverified_info field
- Include approximate dates, partial names, estimated amounts, or unconfirmed details here
- This allows us to track leads while maintaining data integrity in primary fields

This database will be used for public accountability.
"""

PHASE1_PROMPT = """Research assistant: Extract ALL information about this case from available sources.

Include:
- Facts, allegations, and claims
- Names, dates, amounts, locations
- Timeline of events
- Quotes and specific details
- Source URLs

Provide comprehensive raw findings."""

PHASE2_PROMPT = """Structure the research into a verified accountability case.

RULES:
- Only verified, precise information in structured fields
- Exact dates (YYYY-MM-DD), complete names, specific amounts
- Uncertain/incomplete information → unverified_info field
- All sources must have URLs and descriptions
- Format description and unverified_info as rich HTML with proper tags (<p>, <strong>, <ul>, <li>, <a>, etc.)

Maintain data integrity for public accountability."""


class CaseScraper:
    """Service for scraping case information using Google AI."""

    def __init__(
        self,
        service_account_path: str,
        project: str | None = None,
        location: str = "us-central1",
        model: str = "gemini-2.5-pro",
        language: str = "en",
        logger=None,
    ):
        """
        Initialize the case scraper.

        Args:
            service_account_path: Path to Google service account key file
            project: Google Cloud project ID (defaults to project_id from service account)
            location: Google Cloud location
            model: Google AI model to use
            language: Output language ('en' or 'np')
            logger: Optional logger for progress messages
        """
        self.service_account_path = Path(service_account_path)
        self.project = project
        self.location = location
        self.model = model
        self.language = language
        self.logger = logger

        # Validate service account file
        if not self.service_account_path.exists():
            raise FileNotFoundError(
                f"Service account key file not found: {service_account_path}"
            )

        # Initialize credentials
        self.credentials = service_account.Credentials.from_service_account_file(
            str(self.service_account_path),
            scopes=["https://www.googleapis.com/auth/cloud-platform"],
        )

        # Read project_id from service account if not provided
        if not self.project:
            with open(self.service_account_path, "r") as f:
                service_account_info = json.load(f)
                self.project = service_account_info.get("project_id")
                if not self.project:
                    raise ValueError("project_id not found in service account key file")

        # Initialize Google AI client
        self.client = genai.Client(
            http_options=HttpOptions(api_version="v1"),
            vertexai=True,
            credentials=self.credentials,
            project=self.project,
            location=self.location,
        )

        # Set language instruction
        if self.language == "np":
            language_instruction = "Provide all responses in Nepali language (नेपाली भाषामा जवाफ दिनुहोस्)."
        else:
            language_instruction = "Provide all responses in English language."

        # Generate system prompt
        self.system_prompt = SYSTEM_PROMPT_TEMPLATE.format(
            language_instruction=language_instruction
        )

    def log(self, message):
        """Log a message if logger is available."""
        if self.logger:
            if hasattr(self.logger, "write"):
                self.logger.write(message)
            else:
                self.logger(message)

    def scrape_case(
        self, source_paths: List[str | Path], work_dir: Path
    ) -> tuple[Case, Path, Path]:
        """
        Scrape case information from source files.

        Args:
            source_paths: List of source file paths to scrape
            work_dir: Directory to save intermediate and final results

        Returns:
            Tuple of (Case object, phase1 file path, result file path)

        Raises:
            FileNotFoundError: If source file doesn't exist
            ValueError: If source path is not a file
        """
        # Validate source paths
        validated_paths = []
        for path_str in source_paths:
            path = Path(path_str)
            if not path.exists():
                raise FileNotFoundError(f"Source path does not exist: {path_str}")
            if not path.is_file():
                raise ValueError(f"Source path is not a file: {path_str}")
            validated_paths.append(path)

        # Create work directory
        work_dir.mkdir(parents=True, exist_ok=True)

        # Read source files
        source_contents = []
        for path in validated_paths:
            content = path.read_text(encoding="utf-8")
            source_contents.append({"path": str(path), "content": content})

        # Phase 1: Extract raw information
        raw_data = self._phase1_extract(source_contents)

        # Save phase 1 results
        phase1_file = work_dir / "phase1-raw.md"
        phase1_file.write_text(raw_data, encoding="utf-8")

        # Phase 2: Structure the data
        case = self._phase2_structure(raw_data)

        # Save final result
        result_file = work_dir / "case-result.json"
        result_file.write_text(case.model_dump_json(indent=2), encoding="utf-8")

        return case, phase1_file, result_file

    def _phase1_extract(self, source_contents: List[dict]) -> str:
        """
        Phase 1: Extract raw information from sources.

        Args:
            source_contents: List of dicts with 'path' and 'content' keys

        Returns:
            Raw extracted data as markdown text
        """
        self.log("  Extracting raw information from sources...")

        # Concatenate all source contents
        combined_sources = "\n\n---SOURCE DOCUMENT---\n\n".join(
            [f"File: {s['path']}\n\n{s['content']}" for s in source_contents]
        )

        query = f"Extract all relevant information from these documents:\n\n{combined_sources}"

        response = self.client.models.generate_content(
            model=self.model,
            contents=f"{PHASE1_PROMPT}\n\n{query}",
            config=GenerateContentConfig(
                system_instruction=self.system_prompt,
                tools=[Tool(google_search=GoogleSearch())],
            ),
        )

        self.log(f"  Extracted {len(response.text)} characters")
        return response.text

    def _phase2_structure(self, raw_data: str) -> Case:
        """
        Phase 2: Structure raw data into validated Case schema.

        Args:
            raw_data: Raw extracted data from phase 1

        Returns:
            Validated Case object
        """
        self.log("\nPhase 2: Structuring data...")
        self.log("  Converting raw data to structured format...")

        prompt = f"{PHASE2_PROMPT}\n\nRaw Research:\n{raw_data}"

        response = self.client.models.generate_content(
            model=self.model,
            contents=prompt,
            config=GenerateContentConfig(
                response_mime_type="application/json",
                response_schema=Case,
                system_instruction=self.system_prompt,
            ),
        )

        # Parse and validate the case
        case = Case.model_validate_json(response.text)

        self.log("  Validation successful")
        return case
