"""Seed a minimal local-dev dataset + role users for the admin panel.

DEV ONLY. Creates the standard groups, three login users (admin / moderator /
caseworker, password == username), NES entities, NGM materials, courts, court
cases, cases (one per state), and a blocklisted firm — enough to exercise every
admin screen (incl. the case-edit entity linker + evidence/material picker).
Idempotent: safe to re-run.

Usage (with DEV_AUTH env, DEBUG=True):
    uv run python manage.py seed_dev
"""

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from django.core.management import call_command
from django.core.management.base import BaseCommand

from cases.models import (
    Case,
    CaseEntityRelationship,
    CaseState,
    CaseType,
    RelationshipType,
)
from courts.models import BlacklistedFirm, Court, CourtCase
from jawafdehi_shared.entities.ids import build_entity_iri, build_material_iri
from materials.jsonld import MATERIAL_CONTEXT, type_for
from materials.models import Material

User = get_user_model()

# NES entities to publish: (prefix, slug, @type, English name, Nepali name).
ENTITIES = [
    ("person", "ram-bahadur", "Person", "Ram Bahadur", "राम बहादुर"),
    ("person", "sita-sharma", "Person", "Sita Sharma", "सीता शर्मा"),
    ("organization", "ministry-of-works", "Organization", "Ministry of Works", "निर्माण मन्त्रालय"),
    ("organization", "board-y", "Organization", "Board Y", "बोर्ड वाई"),
    ("place", "kathmandu", "Place", "Kathmandu", "काठमाडौँ"),
]

# NGM materials: (source, ident, material_type, English name).
MATERIALS = [
    ("ciaa", "report-1", "official_report", "CIAA Investigation Report 2080"),
    ("ciaa", "chargesheet-1", "charge_sheet", "Charge Sheet — Ministry of Works"),
    ("oag", "audit-2079", "official_report", "OAG Audit Report FY2079/80"),
    ("court", "verdict-075-cr-0123", "court_order", "Verdict 075-CR-0123"),
]

USERS = [
    ("admin", [], True),
    ("moderator", ["Moderator"], False),
    ("caseworker", ["Caseworker"], False),
]

CASES = [
    ("seed-draft", "Draft: alleged kickbacks at Dept A", CaseState.DRAFT),
    ("seed-in-review", "Review: procurement fraud Ministry X", CaseState.IN_REVIEW),
    ("seed-published", "Published: embezzlement at Board Y", CaseState.PUBLISHED),
    ("seed-closed", "Closed: dismissed complaint Z", CaseState.CLOSED),
]


class Command(BaseCommand):
    help = "Seed minimal local-dev data + role users (DEV ONLY, idempotent)."

    def handle(self, *args, **options):
        call_command("create_groups")

        for name, groups, su in USERS:
            u, _ = User.objects.get_or_create(
                username=name, defaults={"email": f"{name}@local.test"}
            )
            u.set_password(name)
            u.is_staff = True
            u.is_superuser = su
            u.save()
            u.groups.clear()
            for g in groups:
                grp = Group.objects.filter(name=g).first()
                if grp:
                    u.groups.add(grp)
            self.stdout.write(f"user {name}: pw={name} superuser={su} groups={groups}")

        # ── NES entities (published, via the PublicationService write path) ──
        from entities.services.publication.service import PublicationService

        svc = PublicationService()
        for prefix, slug, etype, name_en, name_ne in ENTITIES:
            iri = build_entity_iri(prefix, slug)
            if svc.repo.get_entity(iri) is not None:
                self.stdout.write(f"entity {iri}: exists")
                continue
            doc = {
                "@context": "https://schema.org",
                "@id": iri,
                "@type": etype,
                "name": {"en": name_en, "ne": name_ne},
            }
            svc.create_entity(doc, author_id="seed_dev", change_description="seed")
            self.stdout.write(f"entity {iri}: created")

        # ── NGM materials (schema.org JSON-LD, via Material.from_jsonld) ──
        for source, ident, mtype, name_en in MATERIALS:
            iri = build_material_iri(source, ident)
            if Material.objects.filter(iri=iri).exists():
                self.stdout.write(f"material {iri}: exists")
                continue
            at_type, additional = type_for(mtype)
            doc = {
                "@context": MATERIAL_CONTEXT,
                "@id": iri,
                "@type": at_type,
                "name": {"en": name_en},
            }
            if additional:
                doc["additionalType"] = additional
            m = Material.from_jsonld(doc, material_type=mtype)
            m.save()
            self.stdout.write(f"material {iri}: created")

        court, _ = Court.objects.get_or_create(
            identifier="supreme-court",
            defaults=dict(
                court_type="SUPREME",
                full_name_nepali="सर्वोच्च अदालत",
                full_name_english="Supreme Court",
            ),
        )
        Court.objects.get_or_create(
            identifier="kathmandu-district-court",
            defaults=dict(
                court_type="DISTRICT",
                full_name_nepali="काठमाडौँ जिल्ला अदालत",
                full_name_english="Kathmandu District Court",
            ),
        )
        CourtCase.objects.get_or_create(
            court=court,
            case_number="075-CR-0123",
            defaults=dict(
                case_type="Corruption",
                case_status="Pending",
                plaintiff="State",
                defendant="Ram Bahadur",
            ),
        )
        BlacklistedFirm.objects.get_or_create(
            firm_name="Acme Builders Pvt Ltd",
            defaults=dict(
                proprietor_name="J. Doe",
                reason="Contract fraud",
                recommending_office="CIAA",
            ),
        )

        for slug, title, state in CASES:
            strict = state in (CaseState.IN_REVIEW, CaseState.PUBLISHED)
            c, created = Case.objects.get_or_create(
                slug=slug,
                defaults=dict(
                    title=title,
                    case_type=CaseType.CORRUPTION,
                    state=state,
                    short_description="Seed case for local testing.",
                    description="## Summary\n\nSeed **markdown** body.",
                    key_allegations=["Misappropriation", "Bid rigging"]
                    if strict
                    else [],
                ),
            )
            if created and strict:
                CaseEntityRelationship.objects.get_or_create(
                    case=c,
                    nes_id="https://jawafdehi.org/entity/person/ram-bahadur",
                    relationship_type=RelationshipType.ACCUSED,
                )
            self.stdout.write(f"case {slug}: state={c.state} created={created}")

        self.stdout.write(self.style.SUCCESS("seed_dev complete."))
