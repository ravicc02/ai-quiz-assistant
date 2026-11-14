from __future__ import annotations

import json
import os
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

from app.config import default_data_root, legacy_data_root, migrate_legacy_data_root, settings
from app.product import DATA_DIRECTORY, DATABASE_FILENAME, LEGACY_DATA_DIRECTORY, PRODUCT_NAME


class ProductCompatibilityTests(unittest.TestCase):
    def test_product_name_and_legacy_storage_identity_are_distinct(self):
        self.assertEqual(PRODUCT_NAME, "Mumu")
        self.assertEqual(DATA_DIRECTORY, ".mumu")
        self.assertEqual(LEGACY_DATA_DIRECTORY, "AIAssistantStudio")
        self.assertEqual(DATABASE_FILENAME, "answer-studio.sqlite3")

    def test_new_environment_name_wins_and_legacy_name_remains_supported(self):
        with tempfile.TemporaryDirectory() as directory:
            primary = Path(directory) / "primary"
            legacy = Path(directory) / "legacy"
            with patch.dict(
                os.environ,
                {
                    "MUMU_DATA": str(primary),
                    "ANSWER_STUDIO_DATA": str(legacy),
                },
                clear=False,
            ):
                self.assertEqual(default_data_root(), primary.resolve())
            with patch.dict(os.environ, {"ANSWER_STUDIO_DATA": str(legacy)}, clear=False):
                with patch.dict(os.environ, {"MUMU_DATA": ""}, clear=False):
                    self.assertEqual(default_data_root(), legacy.resolve())

    def test_default_data_root_uses_the_user_home_directory(self):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory) / "home"
            with patch("app.config.Path.home", return_value=home):
                with patch.dict(
                    os.environ,
                    {"MUMU_DATA": "", "ANSWER_STUDIO_DATA": ""},
                    clear=False,
                ):
                    self.assertEqual(default_data_root(), (home / ".mumu").resolve())

    def test_legacy_data_is_copied_to_the_new_root_without_removing_the_source(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "legacy"
            target = Path(directory) / "home" / ".mumu"
            (source / "profiles").mkdir(parents=True)
            (source / "profiles" / "account.json").write_text("session", encoding="utf-8")
            with closing(sqlite3.connect(source / DATABASE_FILENAME)) as connection:
                with connection:
                    connection.execute("CREATE TABLE accounts(profile_dir TEXT, metadata TEXT)")
                    connection.execute(
                        "INSERT INTO accounts VALUES(?, ?)",
                        (
                            str(source / "profiles" / "one"),
                            json.dumps({"artifact": str(source / "artifacts" / "one.png")}),
                        ),
                    )
            target.mkdir(parents=True)
            with patch.dict(
                os.environ,
                {"MUMU_DATA": "", "ANSWER_STUDIO_DATA": ""},
                clear=False,
            ):
                migrated = migrate_legacy_data_root(target, source=source)
            self.assertEqual(migrated, target.resolve())
            self.assertEqual((target / "profiles" / "account.json").read_text(encoding="utf-8"), "session")
            self.assertTrue((source / DATABASE_FILENAME).is_file())
            with closing(sqlite3.connect(target / DATABASE_FILENAME)) as connection:
                profile_dir, metadata = connection.execute(
                    "SELECT profile_dir, metadata FROM accounts"
                ).fetchone()
            self.assertEqual(profile_dir, str(target.resolve() / "profiles" / "one"))
            self.assertEqual(json.loads(metadata)["artifact"], str(target.resolve() / "artifacts" / "one.png"))

    def test_existing_new_data_root_is_never_overwritten(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "legacy"
            target = Path(directory) / "home" / ".mumu"
            source.mkdir()
            target.mkdir(parents=True)
            with closing(sqlite3.connect(source / DATABASE_FILENAME)) as connection:
                with connection:
                    connection.execute("CREATE TABLE source(value TEXT)")
                    connection.execute("INSERT INTO source VALUES('legacy')")
            with closing(sqlite3.connect(target / DATABASE_FILENAME)) as connection:
                with connection:
                    connection.execute("CREATE TABLE current(value TEXT)")
                    connection.execute("INSERT INTO current VALUES('current')")
            with patch.dict(
                os.environ,
                {"MUMU_DATA": "", "ANSWER_STUDIO_DATA": ""},
                clear=False,
            ):
                migrate_legacy_data_root(target, source=source)
            with closing(sqlite3.connect(target / DATABASE_FILENAME)) as connection:
                self.assertEqual(connection.execute("SELECT value FROM current").fetchone()[0], "current")

    def test_legacy_root_uses_local_app_data(self):
        with tempfile.TemporaryDirectory() as directory:
            with patch.dict(os.environ, {"LOCALAPPDATA": directory}, clear=False):
                self.assertEqual(
                    legacy_data_root(),
                    (Path(directory) / LEGACY_DATA_DIRECTORY).resolve(),
                )

    def test_database_keeps_the_legacy_filename(self):
        self.assertEqual(settings.database_path.name, DATABASE_FILENAME)


if __name__ == "__main__":
    unittest.main()
