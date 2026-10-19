"""Documents the system has stopped trying to read.

A version that uses up its extraction attempts stops being offered and is
never mentioned again - the job reports '0 still pending' and looks healthy.
These tests pin down which versions count as abandoned, and that the two
queries that decide it cannot disagree.
"""
import sqlite3
import unittest

from database import extraction_repository as repo
from database.db import connect
from extraction.extract import MAX_FAILED_ATTEMPTS, UNSUPPORTED_EXTRACTOR_NAME

DEPTH = "basic"


class AbandonedVersionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.conn = connect(":memory:")
        self.conn.execute(
            "INSERT INTO courses (id, shortname, fullname, visible, created_at, last_synced_at)"
            " VALUES (1,'SW','Ingenieria de Software',1,'t','t')")

    def tearDown(self) -> None:
        self.conn.close()

    def add_version(self, version_id: int, name: str, *, versions: int = 1) -> None:
        """A resource with `versions` versions; version_id is the latest."""
        self.conn.execute(
            "INSERT INTO resources (id, origin, source_type, external_reference, course_id,"
            " name, source_url, created_at, last_synced_at)"
            " VALUES (?,'moodle','file',?,1,?,'url','t','t')",
            (version_id, f"ref{version_id}", name))
        for number in range(1, versions + 1):
            self.conn.execute(
                "INSERT INTO resource_versions (id, resource_id, version_number, content_hash,"
                " storage_ref, size_bytes, mimetype, ingested_at)"
                " VALUES (?,?,?,?,?,?, 'application/pdf','t')",
                (version_id * 100 + number, version_id, number,
                 f"h{version_id}{number}", f"h{version_id}{number}", 1048576))
        self.conn.commit()

    def attempt(self, version_id: int, status: str, *, error: str | None = None,
                extractor: str = "pdf", at: str = "2026-10-13T10:00:00+00:00") -> None:
        self.conn.execute(
            "INSERT INTO extracted_documents (resource_version_id, depth, extractor_name,"
            " extractor_version, status, error_reason, extracted_at)"
            " VALUES (?,?,?,'1',?,?,?)",
            (version_id, DEPTH, extractor, status, error, at))
        self.conn.commit()

    def abandoned(self) -> list[sqlite3.Row]:
        return repo.list_abandoned_versions(
            self.conn, DEPTH, MAX_FAILED_ATTEMPTS, UNSUPPORTED_EXTRACTOR_NAME)

    def pending(self) -> list[int]:
        return repo.get_pending_version_ids(
            self.conn, DEPTH, MAX_FAILED_ATTEMPTS, UNSUPPORTED_EXTRACTOR_NAME)

    # ---- what counts as abandoned ---------------------------------------

    def test_a_version_that_used_up_its_attempts_is_listed(self) -> None:
        self.add_version(1, "roto.pdf")
        for _ in range(MAX_FAILED_ATTEMPTS):
            self.attempt(101, "failed", error="PdfReadError: EOF marker not found")

        [row] = self.abandoned()
        self.assertEqual(row["name"], "roto.pdf")
        self.assertEqual(row["failures"], MAX_FAILED_ATTEMPTS)
        self.assertEqual(row["last_error"], "PdfReadError: EOF marker not found")
        self.assertEqual(row["course_id"], 1)

    def test_a_version_with_attempts_left_is_not_listed(self) -> None:
        self.add_version(1, "intermitente.pdf")
        for _ in range(MAX_FAILED_ATTEMPTS - 1):
            self.attempt(101, "failed", error="timeout")

        self.assertEqual(self.abandoned(), [])

    def test_a_version_that_eventually_succeeded_is_not_listed(self) -> None:
        # Transient failures are exactly what the retries are for.
        self.add_version(1, "recuperado.pdf")
        for _ in range(MAX_FAILED_ATTEMPTS):
            self.attempt(101, "failed", error="storage hiccup")
        self.attempt(101, "done")

        self.assertEqual(self.abandoned(), [])

    def test_an_unsupported_format_is_not_called_abandoned(self) -> None:
        # "We cannot read this kind of file" is a different, benign state from
        # "we tried to read this file and could not".
        self.add_version(1, "video.mp4")
        for _ in range(MAX_FAILED_ATTEMPTS):
            self.attempt(101, "failed", error="unsupported format",
                         extractor=UNSUPPORTED_EXTRACTOR_NAME)

        self.assertEqual(self.abandoned(), [])

    def test_only_the_current_version_is_reported(self) -> None:
        # An old version's failures are history; the file was replaced.
        self.add_version(1, "sustituido.pdf", versions=2)
        for _ in range(MAX_FAILED_ATTEMPTS):
            self.attempt(101, "failed", error="old failure")
        self.attempt(102, "done")

        self.assertEqual(self.abandoned(), [])

    def test_the_last_error_reported_is_the_most_recent_one(self) -> None:
        self.add_version(1, "roto.pdf")
        self.attempt(101, "failed", error="primero")
        self.attempt(101, "failed", error="segundo")
        self.attempt(101, "failed", error="tercero y ultimo")

        self.assertEqual(self.abandoned()[0]["last_error"], "tercero y ultimo")

    def test_the_worst_offenders_come_first(self) -> None:
        self.add_version(1, "tres-fallos.pdf")
        self.add_version(2, "cuatro-fallos.pdf")
        for _ in range(MAX_FAILED_ATTEMPTS):
            self.attempt(101, "failed", error="x")
        for _ in range(MAX_FAILED_ATTEMPTS + 1):
            self.attempt(201, "failed", error="x")

        self.assertEqual([r["name"] for r in self.abandoned()],
                         ["cuatro-fallos.pdf", "tres-fallos.pdf"])

    # ---- the two queries must agree -------------------------------------

    def test_abandoned_and_pending_are_complementary(self) -> None:
        """Nothing may be both still-offered and given-up-on."""
        self.add_version(1, "pendiente.pdf")          # never attempted
        self.add_version(2, "casi.pdf")               # attempts left
        self.add_version(3, "agotado.pdf")            # used them up
        self.add_version(4, "hecho.pdf")              # succeeded
        for _ in range(MAX_FAILED_ATTEMPTS - 1):
            self.attempt(201, "failed", error="x")
        for _ in range(MAX_FAILED_ATTEMPTS):
            self.attempt(301, "failed", error="x")
        self.attempt(401, "done")

        pending = set(self.pending())
        given_up = {row["version_id"] for row in self.abandoned()}

        self.assertEqual(pending, {101, 201})
        self.assertEqual(given_up, {301})
        self.assertEqual(pending & given_up, set())

    def test_a_healthy_system_reports_nothing(self) -> None:
        self.add_version(1, "bien.pdf")
        self.attempt(101, "done")

        self.assertEqual(self.abandoned(), [])


if __name__ == "__main__":
    unittest.main()
