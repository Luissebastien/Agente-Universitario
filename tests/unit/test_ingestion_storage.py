import tempfile
import unittest
from pathlib import Path

from ingestion.storage import FilesystemStorage, StorageError, StorageNotFoundError


class FilesystemStorageTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.storage = FilesystemStorage(Path(self._tmp.name) / "originals")

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_store_then_retrieve_returns_identical_bytes(self) -> None:
        data = b"hello world"
        ref = self.storage.store(data, "somehash")

        self.assertEqual(self.storage.retrieve(ref), data)

    def test_exists_true_after_store(self) -> None:
        ref = self.storage.store(b"x", "hashx")
        self.assertTrue(self.storage.exists(ref))

    def test_exists_false_for_unknown_ref(self) -> None:
        self.assertFalse(self.storage.exists("nope"))

    def test_retrieve_missing_raises_not_found(self) -> None:
        with self.assertRaises(StorageNotFoundError):
            self.storage.retrieve("does-not-exist")

    def test_store_is_idempotent_for_same_hash(self) -> None:
        ref1 = self.storage.store(b"same content", "hashy")
        ref2 = self.storage.store(b"same content", "hashy")

        self.assertEqual(ref1, ref2)
        self.assertEqual(self.storage.retrieve(ref1), b"same content")

    def test_administrative_delete_removes_data(self) -> None:
        ref = self.storage.store(b"to be deleted", "delhash")
        self.storage.delete(ref)

        self.assertFalse(self.storage.exists(ref))
        with self.assertRaises(StorageNotFoundError):
            self.storage.retrieve(ref)

    def test_delete_missing_raises_not_found(self) -> None:
        with self.assertRaises(StorageNotFoundError):
            self.storage.delete("never-existed")

    def test_rejects_path_traversal_in_ref(self) -> None:
        with self.assertRaises(StorageError):
            self.storage.retrieve("../../etc/passwd")

    def test_root_directory_is_created_if_missing(self) -> None:
        nested_root = Path(self._tmp.name) / "a" / "b" / "c"
        storage = FilesystemStorage(nested_root)
        self.assertTrue(nested_root.exists())
        ref = storage.store(b"data", "h1")
        self.assertEqual(storage.retrieve(ref), b"data")


if __name__ == "__main__":
    unittest.main()
