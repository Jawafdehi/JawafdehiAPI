"""The backfill walks the archive without being able to destroy it.

What is pinned here is the safety argument rather than the redactor — that is
``test_redaction.py``'s job. These tests say: a dry run writes nothing, a stored
original is never overwritten, a document whose redaction came out incomplete is
refused instead of published, and a re-run is a no-op rather than a second pass
over the command's own output.

    DATABASE_URL=sqlite:// NES_DB_URL=sqlite:// NGM_DATABASE_URL=sqlite:// \
        uv run pytest materials/tests/test_backfill_redaction.py
"""

from __future__ import annotations

from io import StringIO
from unittest import mock

from django.core.management import CommandError, call_command
from django.test import TestCase

from materials import redaction
from materials.jsonld import MATERIAL_CONTEXT
from materials.models import Material

CITIZENSHIP_VALUE = "१२-०१-७५-०४५६७"
WITH_CITIZENSHIP = f"नागरिकता नं. {CITIZENSHIP_VALUE} रहेको व्यक्ति"
MOBILE_VALUE = "९८४१२३४५६७"
WITH_MOBILE = f"सम्पर्क {MOBILE_VALUE}"


def _material(ident: str, text, *, material_type="charge_sheet", **extra) -> Material:
    """A material carrying ``text``, written the way the archive already holds it.

    Deliberately ``objects.create`` rather than the upsert primitive: the whole
    population this command exists for is rows that never went through the
    primitive, because it did not redact when they were written.
    """
    iri = f"https://jawafdehi.org/material/probe/{ident}"
    return Material.objects.create(
        iri=iri,
        material_type=material_type,
        source="probe",
        ident=ident,
        data={
            "@context": MATERIAL_CONTEXT,
            "@type": "DigitalDocument",
            "@id": iri,
            "name": {"ne": "परीक्षण"},
            "text": text,
        },
        **extra,
    )


def _run(*args, **kwargs):
    out, err = StringIO(), StringIO()
    call_command("backfill_redaction", *args, stdout=out, stderr=err, **kwargs)
    return out.getvalue() + err.getvalue()


class DryRunTests(TestCase):
    databases = "__all__"

    def test_a_dry_run_changes_nothing(self):
        material = _material("dry", WITH_CITIZENSHIP)

        output = _run("--material-type", "charge_sheet")

        material.refresh_from_db()
        self.assertEqual(material.data["text"], WITH_CITIZENSHIP)
        self.assertEqual(material.source_text, "")
        self.assertIn("would_redact=1", output)
        self.assertIn("dry run", output)

    def test_a_document_with_nothing_to_remove_is_reported_clean(self):
        _material("clean", "यो कागजातमा व्यक्तिगत विवरण छैन।")

        output = _run("--material-type", "charge_sheet")

        self.assertIn("clean=1", output)


class ApplyTests(TestCase):
    databases = "__all__"

    def test_apply_redacts_the_text_and_keeps_the_original(self):
        material = _material("apply", WITH_CITIZENSHIP)

        _run("--material-type", "charge_sheet", "--apply")

        material.refresh_from_db()
        self.assertIn("[REDACTED:CITIZENSHIP-NO]", material.data["text"])
        self.assertNotIn(CITIZENSHIP_VALUE, material.data["text"])
        # The original is preserved rather than re-derivable: re-transcribing a
        # document is an OCR run and is not byte-for-byte reproducible.
        self.assertIn(CITIZENSHIP_VALUE, material.source_text)

    def test_a_language_map_is_redacted_in_every_language(self):
        material = _material("langmap", {"ne": WITH_CITIZENSHIP, "en": WITH_MOBILE})

        _run("--material-type", "charge_sheet", "--apply")

        material.refresh_from_db()
        self.assertNotIn(CITIZENSHIP_VALUE, material.data["text"]["ne"])
        self.assertNotIn(MOBILE_VALUE, material.data["text"]["en"])

    def test_running_twice_is_a_no_op_and_keeps_the_first_original(self):
        material = _material("twice", WITH_CITIZENSHIP)

        _run("--material-type", "charge_sheet", "--apply")
        material.refresh_from_db()
        first_original = material.source_text
        output = _run("--material-type", "charge_sheet", "--apply")

        material.refresh_from_db()
        self.assertEqual(material.source_text, first_original)
        self.assertIn(CITIZENSHIP_VALUE, material.source_text)
        self.assertIn("clean=1", output)

    def test_a_stored_original_is_never_overwritten(self):
        """The one irreversible mistake available to this command.

        A row that already carries an original holds text that is itself a
        redacted derivative. Writing that over ``source_text`` would replace the
        pristine copy with a partly-redacted one — and nothing could get it back.
        """
        pristine = '"नागरिकता नं. ९९-९९-९९-९९९९९ र सम्पर्क ९८००००००००"'
        material = _material("stored", WITH_MOBILE, source_text=pristine)

        _run("--material-type", "charge_sheet", "--apply")

        material.refresh_from_db()
        self.assertEqual(material.source_text, pristine)
        # …and the text was still redacted, which is the half that makes keeping
        # the original safe rather than an excuse to skip the document.
        self.assertNotIn(MOBILE_VALUE, material.data["text"])

    def test_a_soft_deleted_row_is_left_alone(self):
        material = _material("deleted", WITH_CITIZENSHIP, is_deleted=True)

        _run("--apply")

        material.refresh_from_db()
        self.assertEqual(material.data["text"], WITH_CITIZENSHIP)


class GateTests(TestCase):
    databases = "__all__"

    def test_residual_personal_data_refuses_the_document(self):
        """A gate that cannot be made to fail is decoration.

        The scanner is neutralised into always reporting a survivor, which is what
        an incomplete redaction looks like from here: the document must be left
        exactly as it was, and the run must not exit 0.
        """
        material = _material("refuse", WITH_CITIZENSHIP)

        with mock.patch(
            "likhit.privacy.scan_text",
            return_value=({"citizenship": 1}, {}),
        ):
            with self.assertRaises(CommandError) as caught:
                _run("--material-type", "charge_sheet", "--apply")

        self.assertEqual(caught.exception.returncode, 2)
        material.refresh_from_db()
        self.assertEqual(material.data["text"], WITH_CITIZENSHIP)
        self.assertEqual(material.source_text, "")

    def test_the_gate_knows_every_class_the_scanner_reports(self):
        """An unknown class is an ungated one, so upstream adding one must fail here.

        ``_removal_tiers`` records which tier first removes each class. If likhit
        starts reporting a class that table does not know, the gate would silently
        stop checking it — the one failure mode a gate must not have.
        """
        from likhit.privacy import scan_text

        detections, _names = scan_text(f"{WITH_CITIZENSHIP} {WITH_MOBILE}")
        unknown = (
            set(detections) - set(redaction._removal_tiers()) - redaction.NOT_REMOVED
        )

        self.assertEqual(unknown, set(), f"likhit reports ungated class(es): {unknown}")

    def test_nothing_exempt_from_the_gate_is_ever_actually_removed(self):
        """The test above passes if a REMOVABLE class is wrongly listed as exempt.

        That is not hypothetical — it is the defect the first corpus run found.
        ``phone_labelled`` was read as a shape diagnostic and exempted, and the
        coverage test above was satisfied *by the exemption*. likhit removes it:
        an English-labelled phone number is journalled under that kind, 193 times
        in one charge-sheet sweep, so residual ones were going ungated.

        So assert the exemption from the other side: anything we refuse to gate on
        must be something the redactor never takes out.
        """
        from likhit.privacy import RedactionMode
        from likhit.privacy import redact_text as likhit_redact

        probe = (
            f"{WITH_CITIZENSHIP} {WITH_MOBILE} Phone No.: 014234567 "
            "इमेल: ram@example.com.np खाता नं. ०१२३४५६७८९०१२३ "
            "स्थायी लेखा नम्बर (PAN): ६०१२३४५६७"
        )
        removed = {
            row["kind"]
            for row in likhit_redact(probe, mode=RedactionMode.STRICT).journal
        }

        wrongly_exempt = removed & redaction.NOT_REMOVED
        self.assertEqual(
            wrongly_exempt,
            set(),
            f"the gate exempts {wrongly_exempt}, which the redactor does remove",
        )


class SelectionTests(TestCase):
    databases = "__all__"

    def test_the_most_sensitive_types_are_walked_first(self):
        """An aborted run should have done the documents that matter most."""
        _material("news", WITH_CITIZENSHIP, material_type="news")
        _material("sheet", WITH_CITIZENSHIP, material_type="charge_sheet")

        output = _run("--limit", "1", "--apply")

        self.assertIn("charge_sheet", output)
        self.assertEqual(
            Material.objects.get(ident="news").data["text"], WITH_CITIZENSHIP
        )

    def test_a_single_document_can_be_named(self):
        named = _material("named", WITH_CITIZENSHIP)
        other = _material("other", WITH_CITIZENSHIP)

        _run("--iri", named.iri, "--apply")

        other.refresh_from_db()
        self.assertEqual(other.data["text"], WITH_CITIZENSHIP)
        named.refresh_from_db()
        self.assertNotIn(CITIZENSHIP_VALUE, named.data["text"])


class WithheldVisibilityTests(TestCase):
    """A withheld transcript must be countable, or the gate is a silent drop.

    It is deliberately not announced in the public payload — see
    ``redaction.withheld_by_the_write_path`` for why that would be a map to
    unredacted data while the original PDFs are still served. So the operator
    signal has to be exact, and it is: an original kept outside ``data`` while
    ``data`` serves no text is a combination nothing else produces.
    """

    databases = "__all__"

    def test_a_withheld_row_is_counted_and_a_normal_one_is_not(self):
        withheld = _material("withheld", WITH_CITIZENSHIP, source_text='"original"')
        withheld.data.pop("text")
        withheld.save()
        _material("ordinary", WITH_CITIZENSHIP)
        _material("redacted-normally", "कुनै पाठ", source_text='"original"')

        found = list(redaction.withheld_by_the_write_path())

        self.assertEqual([m.iri for m in found], [withheld.iri])

    def test_the_count_is_reported_even_when_nothing_else_is(self):
        withheld = _material("withheld2", WITH_CITIZENSHIP, source_text='"original"')
        withheld.data.pop("text")
        withheld.save()

        output = _run()

        self.assertIn("1 document(s) are carrying a withheld transcript", output)


class TranscriptHalfTests(TestCase):
    """The sweep publishes a redacted transcript, or says it did not.

    🛑 **Why this half exists at all.** ``materials.artifacts.is_withheld``
    withholds our original MARKDOWN only when a redacted counterpart exists. A
    sweep that rewrites ``data["text"]`` and stops there therefore redacts the
    search index and leaves the full transcript published one link away — the
    exposure looks closed from every surface except the one a reader can click.
    """

    databases = "__all__"

    def _with_transcript(self, ident, *, stored, indexed=None):
        """A row whose MARKDOWN artifact says ``stored`` and whose text says ``indexed``.

        The two differ on purpose in most of these tests: they are not the same
        bytes in the archive either, and a redacted transcript built from the
        indexed text would be a redaction of a document we do not serve.
        """
        material = _material(ident, indexed if indexed is not None else stored)
        material.data["associatedMedia"] = [
            {
                "@type": "MediaObject",
                "contentUrl": f"https://s3.jawafdehi.org/case_uploads/{ident}.md",
                "jawafdehi:linkRole": "MARKDOWN",
            }
        ]
        material.save()
        return material

    def _fetch(self, body: bytes):
        return mock.patch(
            "materials.extract.fetch_document", return_value=body
        )

    def _stored(self, material):
        roles = {
            mo.get("jawafdehi:linkRole"): mo
            for mo in material.data.get("associatedMedia", [])
        }
        return roles

    def test_a_redacted_transcript_is_published_beside_the_text(self):
        material = self._with_transcript("t1", stored=WITH_CITIZENSHIP)

        with self._fetch(WITH_CITIZENSHIP.encode("utf-8")):
            output = _run("--apply")

        material.refresh_from_db()
        roles = self._stored(material)
        self.assertIn("REDACTED_MARKDOWN", roles)
        self.assertIn("MARKDOWN", roles, "the original must survive, withheld")
        self.assertIn("transcripts: published=1", output)

    def test_the_transcript_is_redacted_from_the_artifact_not_the_index(self):
        """The distinguishing test: the two inputs carry different values."""
        other = "नागरिकता नं. ९९-९९-९९-९९९९९ को अर्को कागज"
        material = self._with_transcript("t2", stored=other, indexed=WITH_CITIZENSHIP)

        with self._fetch(other.encode("utf-8")) as fetch:
            _run("--apply")

        material.refresh_from_db()
        fetch.assert_called_once()
        self.assertEqual(
            fetch.call_args[0][0],
            "https://s3.jawafdehi.org/case_uploads/t2.md",
            "it must read the stored MARKDOWN, not re-use data['text']",
        )
        self.assertIn("REDACTED_MARKDOWN", self._stored(material))

    def test_a_document_with_no_transcript_still_gets_its_text_redacted(self):
        material = _material("t3", WITH_CITIZENSHIP)

        output = _run("--apply")

        material.refresh_from_db()
        self.assertNotIn(CITIZENSHIP_VALUE, str(material.data["text"]))
        self.assertIn("no_transcript=1", output)

    def test_an_empty_stored_object_is_reported_not_published(self):
        """~7% of the AG shelf answers 200 with zero bytes."""
        material = self._with_transcript("t4", stored=WITH_CITIZENSHIP)

        with self._fetch(b""), self.assertRaises(CommandError) as caught:
            _run("--apply")

        material.refresh_from_db()
        self.assertNotIn(CITIZENSHIP_VALUE, str(material.data["text"]))
        self.assertNotIn("REDACTED_MARKDOWN", self._stored(material))
        self.assertEqual(caught.exception.returncode, 1)

    def test_a_transcript_that_still_scans_positive_is_not_published(self):
        material = self._with_transcript("t5", stored=WITH_CITIZENSHIP)
        residual = redaction.Counter({"bank_account": 1})

        with (
            self._fetch(WITH_CITIZENSHIP.encode("utf-8")),
            mock.patch(
                "materials.management.commands.backfill_redaction.residual_classes",
                return_value=residual,
            ),
            self.assertRaises(CommandError),
        ):
            _run("--apply")

        material.refresh_from_db()
        self.assertNotIn("REDACTED_MARKDOWN", self._stored(material))
        self.assertNotIn(
            CITIZENSHIP_VALUE,
            str(material.data["text"]),
            "the text half still goes ahead — strictly better than leaving both",
        )

    def test_running_twice_replaces_the_transcript_rather_than_adding_one(self):
        material = self._with_transcript("t6", stored=WITH_CITIZENSHIP)

        with self._fetch(WITH_CITIZENSHIP.encode("utf-8")):
            _run("--apply")
            _run("--apply")

        material.refresh_from_db()
        roles = [
            mo.get("jawafdehi:linkRole")
            for mo in material.data.get("associatedMedia", [])
        ]
        self.assertEqual(roles.count("REDACTED_MARKDOWN"), 1)
        self.assertEqual(roles.count("MARKDOWN"), 1)

    def test_a_dry_run_fetches_but_stores_nothing(self):
        material = self._with_transcript("t7", stored=WITH_CITIZENSHIP)

        with self._fetch(WITH_CITIZENSHIP.encode("utf-8")):
            output = _run()

        material.refresh_from_db()
        self.assertNotIn("REDACTED_MARKDOWN", self._stored(material))
        self.assertIn("would_publish=1", output)

    def test_no_transcript_skips_the_half_entirely(self):
        material = self._with_transcript("t8", stored=WITH_CITIZENSHIP)

        with self._fetch(WITH_CITIZENSHIP.encode("utf-8")) as fetch:
            _run("--apply", "--no-transcript")

        material.refresh_from_db()
        fetch.assert_not_called()
        self.assertNotIn("REDACTED_MARKDOWN", self._stored(material))


class ConcurrentWriteTests(TestCase):
    """A write that lands between the read and the lock is not clobbered.

    The command re-reads under ``select_for_update`` and then assigns the blob it
    built from the PRE-lock snapshot, so without this check the sweep would
    overwrite a scraper's fresher document wholesale — text, media and all — on
    its way to redacting text that is already redacted.
    """

    databases = "__all__"

    def test_a_document_rewritten_under_the_lock_is_left_alone(self):
        from collections import Counter

        from materials.management.commands.backfill_redaction import Command

        material = _material("race", WITH_CITIZENSHIP)
        stale = Material.objects.using("ngm").get(pk=material.iri)
        fresher = dict(stale.data)
        fresher["text"] = {"ne": "कसैले बीचमा लेख्यो"}
        Material.objects.using("ngm").filter(pk=material.iri).update(data=fresher)

        command = Command()
        command._transcript_notes = Counter()
        outcome, _removed = command._one(stale, True, skip_transcript=True)

        self.assertEqual(outcome, "changed_underfoot")
        material.refresh_from_db()
        self.assertEqual(material.data["text"], {"ne": "कसैले बीचमा लेख्यो"})


class TranscriptRoleGuardTests(TestCase):
    """``transcripts.replace`` will not touch a role it does not own."""

    def test_a_non_transcript_role_is_refused(self):
        from materials import transcripts

        with self.assertRaises(ValueError):
            transcripts.replace([], [{"jawafdehi:linkRole": "RAW"}])

    def test_only_the_roles_being_written_are_replaced(self):
        from materials import transcripts

        existing = [
            {"jawafdehi:linkRole": "RAW", "contentUrl": "a"},
            {"jawafdehi:linkRole": "MARKDOWN", "contentUrl": "b"},
            {"jawafdehi:linkRole": "REDACTED_MARKDOWN", "contentUrl": "old"},
        ]

        out = transcripts.replace(
            existing, [{"jawafdehi:linkRole": "REDACTED_MARKDOWN", "contentUrl": "new"}]
        )

        self.assertEqual(
            [(mo["jawafdehi:linkRole"], mo["contentUrl"]) for mo in out],
            [("RAW", "a"), ("MARKDOWN", "b"), ("REDACTED_MARKDOWN", "new")],
        )


class CleanRowsStillGetATranscriptTests(TestCase):
    """🛑 A row clean in `data["text"]` can carry values in its MARKDOWN artifact.

    That is the premise of this whole command — the two are not guaranteed to be
    the same bytes, which is why the transcript is redacted from the artifact and
    not from the indexed text. Returning early on `clean` therefore skipped
    exactly the rows the distinction was drawn for, and made the documented
    remedy a permanent no-op: a row an earlier text-only sweep already redacted
    is clean on every later run.
    """

    databases = "__all__"

    def _row(self, ident: str, *, indexed: str, stored: str):
        material = _material(ident, indexed)
        material.data["associatedMedia"] = [
            {
                "@type": "MediaObject",
                "contentUrl": f"https://s3.jawafdehi.org/case_uploads/{ident}.md",
                "jawafdehi:linkRole": "MARKDOWN",
            }
        ]
        material.save()
        return material

    def _roles(self, material):
        material.refresh_from_db()
        return [
            mo.get("jawafdehi:linkRole")
            for mo in material.data.get("associatedMedia", [])
        ]

    def test_a_clean_row_with_a_dirty_transcript_is_published(self):
        material = self._row("c1", indexed="कुनै पाठ", stored=WITH_CITIZENSHIP)

        with mock.patch(
            "materials.extract.fetch_document",
            return_value=WITH_CITIZENSHIP.encode("utf-8"),
        ):
            output = _run("--apply")

        assert "REDACTED_MARKDOWN" in self._roles(material)
        assert "MARKDOWN" in self._roles(material)
        self.assertIn("clean=1", output)

    def test_a_second_run_over_an_already_redacted_row_still_publishes_one(self):
        """The remedy the command documents has to actually work."""
        material = self._row("c2", indexed=WITH_CITIZENSHIP, stored=WITH_CITIZENSHIP)
        fetched = mock.patch(
            "materials.extract.fetch_document",
            return_value=WITH_CITIZENSHIP.encode("utf-8"),
        )
        with fetched:
            _run("--apply", "--no-transcript")  # the text-only sweep that came first
        assert "REDACTED_MARKDOWN" not in self._roles(material)

        with fetched:
            _run("--apply")

        assert "REDACTED_MARKDOWN" in self._roles(material)

    def test_an_upload_failure_does_not_end_the_sweep(self):
        material = self._row("c3", indexed=WITH_CITIZENSHIP, stored=WITH_CITIZENSHIP)

        with (
            mock.patch(
                "materials.extract.fetch_document",
                return_value=WITH_CITIZENSHIP.encode("utf-8"),
            ),
            mock.patch(
                "materials.transcripts.store", side_effect=OSError("R2 is having a day")
            ),
            self.assertRaises(CommandError),
        ):
            _run("--apply")

        material.refresh_from_db()
        self.assertNotIn(CITIZENSHIP_VALUE, str(material.data["text"]))


class TheSweepIsIdempotentTests(TestCase):
    """🛑 A second run over a finished archive must write nothing.

    The command's docstring claims re-running is "a no-op rather than a second
    pass over its own output", and the whole remediation story for every other
    gap in this work is "re-run it". That claim stopped being true when the
    transcript half moved ahead of the `clean` return: nothing checked whether a
    REDACTED_MARKDOWN already existed, so every clean row re-fetched its
    MARKDOWN, re-uploaded a derivative, and re-saved — and `post_save` re-indexes
    the row, so a second sweep is a corpus-wide reindex on an estate that already
    OOMs on those.

    `provenance.build_provenance` stamps `captured_at = now()` when none is
    passed, so the MediaObject differed every time and the write always stuck.
    """

    databases = "__all__"

    def _row(self, ident: str, text: str):
        material = _material(ident, text)
        material.data["associatedMedia"] = [
            {
                "@type": "MediaObject",
                "contentUrl": f"https://s3.jawafdehi.org/case_uploads/{ident}.md",
                "jawafdehi:linkRole": "MARKDOWN",
            }
        ]
        material.save()
        return material

    def test_a_second_run_fetches_nothing_and_writes_nothing(self):
        material = self._row("idem", WITH_CITIZENSHIP)
        fetch = mock.patch(
            "materials.extract.fetch_document",
            return_value=WITH_CITIZENSHIP.encode("utf-8"),
        )
        with fetch:
            _run("--apply")
        material.refresh_from_db()
        first = (material.data, material.updated_at)

        with fetch as second_fetch, mock.patch(
            "materials.transcripts.store"
        ) as second_store:
            _run("--apply")

        second_fetch.assert_not_called()
        second_store.assert_not_called()
        material.refresh_from_db()
        self.assertEqual(material.data, first[0])
        self.assertEqual(material.updated_at, first[1], "no write, so no reindex")


class StalePairingTests(TestCase):
    """A redaction of a transcript that was replaced mid-flight is not published.

    🛑 The fetch and the redaction happen BEFORE the lock, by design — two
    network round trips are not something to hold a row lock across. If a
    scraper re-converts the document in that window, publishing our derivative
    anyway makes `is_withheld` withhold the NEW original on the strength of a
    redaction of the OLD one.
    """

    databases = "__all__"

    def test_a_transcript_replaced_during_the_fetch_is_not_paired(self):
        material = _material("race-md", WITH_CITIZENSHIP)
        material.data["associatedMedia"] = [
            {
                "@type": "MediaObject",
                "contentUrl": "https://s3.jawafdehi.org/case_uploads/old.md",
                "jawafdehi:linkRole": "MARKDOWN",
            }
        ]
        material.save()

        def swap_the_original(url, **kwargs):
            """Stand in for a scraper re-converting while we are fetching."""
            row = Material.objects.using("ngm").get(pk=material.iri)
            row.data["associatedMedia"] = [
                {
                    "@type": "MediaObject",
                    "contentUrl": "https://s3.jawafdehi.org/case_uploads/new.md",
                    "jawafdehi:linkRole": "MARKDOWN",
                }
            ]
            row.save(using="ngm")
            return WITH_CITIZENSHIP.encode("utf-8")

        # A changed pairing is a SKIP, not a success: the row is left exactly as
        # it was and the run exits 1 so the count cannot be read as done.
        with mock.patch(
            "materials.extract.fetch_document", side_effect=swap_the_original
        ), self.assertRaises(CommandError):
            _run("--apply")

        material.refresh_from_db()
        roles = [
            mo.get("jawafdehi:linkRole")
            for mo in material.data.get("associatedMedia", [])
        ]
        self.assertNotIn("REDACTED_MARKDOWN", roles)


class ConcurrentPublishTests(TestCase):
    """A second sweep must not silently replace the first one's transcript.

    Both runs see no REDACTED_MARKDOWN before the lock — the short-circuit is
    pre-lock by design, since asking is free and fetching is not — so both fetch,
    redact and upload. Without a locked re-check the second `transcripts.replace`
    drops the first's link by role, possibly swapping a redaction made at a
    different tier (the tier is resolved per process), and fires a second
    `post_save` reindex.
    """

    databases = "__all__"

    def test_a_publish_that_landed_first_is_not_overwritten(self):
        material = _material("concurrent", "कुनै पाठ")
        material.data["associatedMedia"] = [
            {
                "@type": "MediaObject",
                "contentUrl": "https://s3.jawafdehi.org/case_uploads/c.md",
                "jawafdehi:linkRole": "MARKDOWN",
            }
        ]
        material.save()

        def publish_first(text: str, *args, **kwargs):
            """Stand in for another run taking the lock before this one."""
            row = Material.objects.using("ngm").get(pk=material.iri)
            row.data["associatedMedia"] = row.data["associatedMedia"] + [
                {
                    "@type": "MediaObject",
                    "contentUrl": "https://s3.jawafdehi.org/case_uploads/theirs.md",
                    "jawafdehi:linkRole": "REDACTED_MARKDOWN",
                }
            ]
            row.save(using="ngm")
            return {"link": "https://s3.jawafdehi.org/case_uploads/ours.md"}

        with (
            mock.patch(
                "materials.extract.fetch_document",
                return_value=WITH_CITIZENSHIP.encode("utf-8"),
            ),
            mock.patch("materials.transcripts.store", side_effect=publish_first),
            self.assertRaises(CommandError),
        ):
            _run("--apply")

        material.refresh_from_db()
        urls = [
            mo.get("contentUrl")
            for mo in material.data.get("associatedMedia", [])
            if mo.get("jawafdehi:linkRole") == "REDACTED_MARKDOWN"
        ]
        self.assertEqual(urls, ["https://s3.jawafdehi.org/case_uploads/theirs.md"])


class StalePairingAcrossRunsTests(TestCase):
    """A redaction left behind when its original was replaced is repairable.

    🛑 The short-circuit asks about the PAIRING, not about existence. If a writer
    replaces the MARKDOWN alone — which `transcripts.replace` supports by design,
    since its drop set comes from what is being written — the old redaction
    survives beside the new original and `is_withheld` withholds the new one on
    the strength of it. An existence check short-circuited every later run before
    noticing, so the documented remedy could never repair the one state that most
    needed it.
    """

    databases = "__all__"

    def test_a_redaction_of_a_replaced_original_is_re_derived(self):
        material = _material("stale-across", WITH_CITIZENSHIP)
        material.data["associatedMedia"] = [
            {
                "@type": "MediaObject",
                "contentUrl": "https://s3.jawafdehi.org/case_uploads/new.md",
                "jawafdehi:linkRole": "MARKDOWN",
            },
            {
                "@type": "MediaObject",
                "contentUrl": "https://s3.jawafdehi.org/case_uploads/old-redacted.md",
                "jawafdehi:linkRole": "REDACTED_MARKDOWN",
                # made from an original this row no longer carries
                "jawafdehi:provenance": {
                    "fetch_method": "redaction",
                    "source_url": "https://s3.jawafdehi.org/case_uploads/old.md",
                },
            },
        ]
        material.save()

        with mock.patch(
            "materials.extract.fetch_document",
            return_value=WITH_CITIZENSHIP.encode("utf-8"),
        ) as fetch:
            _run("--apply")

        fetch.assert_called_once()
        self.assertEqual(
            fetch.call_args[0][0], "https://s3.jawafdehi.org/case_uploads/new.md"
        )
        material.refresh_from_db()
        redacted = [
            mo
            for mo in material.data["associatedMedia"]
            if mo.get("jawafdehi:linkRole") == "REDACTED_MARKDOWN"
        ]
        self.assertEqual(len(redacted), 1)
        self.assertEqual(
            redacted[0]["jawafdehi:provenance"]["source_url"],
            "https://s3.jawafdehi.org/case_uploads/new.md",
            "the fresh redaction records the original it was actually made of",
        )
