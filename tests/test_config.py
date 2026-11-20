import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import config


class DatabaseUrlTests(unittest.TestCase):
    def test_explicit_database_url_wins(self):
        with patch.dict(os.environ, {"DATABASE_URL": "sqlite+aiosqlite:////tmp/custom.db"}, clear=False):
            self.assertEqual(config._resolve_database_url(), "sqlite+aiosqlite:////tmp/custom.db")

    def test_default_path_is_inside_data_dir(self):
        with tempfile.TemporaryDirectory() as tmp:
            with patch.dict(os.environ, {"DATABASE_URL": "", "DATA_DIR": tmp}, clear=False):
                url = config._resolve_database_url()
            self.assertTrue(url.startswith("sqlite+aiosqlite:///"))
            self.assertTrue(url.endswith("vellio_bot.db"))
            self.assertIn("vellio_bot", url)
            self.assertTrue((Path(tmp) / "vellio_bot.db").parent.exists())


if __name__ == "__main__":
    unittest.main()
