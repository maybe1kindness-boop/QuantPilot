import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from utils.db_manager import DBManager
from utils.stock_theme_service import StockThemeService


class StockThemeServiceTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        db_path = Path(self.temp_dir.name) / "themes.db"
        self.db = DBManager(str(db_path))
        self.db.execute(
            "CREATE TABLE stock_sector ("
            "sector_code TEXT PRIMARY KEY, sector_name TEXT NOT NULL, "
            "sector_type TEXT, sector_change REAL, rank_position INTEGER)"
        )
        self.db.execute(
            "CREATE TABLE stock_sector_mapping ("
            "id INTEGER PRIMARY KEY AUTOINCREMENT, stock_code TEXT NOT NULL, "
            "sector_code TEXT NOT NULL, "
            "mapping_date TEXT NOT NULL, created_date TEXT, "
            "UNIQUE(stock_code, sector_code, mapping_date))"
        )
        self.db.execute(
            "CREATE TABLE stock_basic (code TEXT PRIMARY KEY, industry TEXT)"
        )
        sectors = [
            ("BK0968", "固态电池", "概念", 2.5, 2),
            ("BK0800", "人工智能", "概念", 1.8, 5),
            ("BK0596", "融资融券", "概念", 0.2, 1),
        ]
        for row in sectors:
            self.db.execute(
                "INSERT INTO stock_sector VALUES (?, ?, ?, ?, ?)", row
            )
        self.db.connect().commit()

    def tearDown(self):
        self.db.close()
        self.temp_dir.cleanup()

    def test_fetches_persists_and_filters_display_themes(self):
        service = StockThemeService(self.db)

        def fake_profile(_code, catalog):
            ordered = []
            for relevance_rank, sector_code in enumerate(
                ["BK0596", "BK0968", "BK0800"], start=1
            ):
                theme = dict(catalog[sector_code])
                theme["relevance_rank"] = relevance_rank
                ordered.append(theme)
            return {"industry": "电池", "themes": ordered}

        with patch.object(service, "_fetch_stock_profile", side_effect=fake_profile):
            result = service.get_stock_themes(["300750"])

        self.assertEqual(
            [item["name"] for item in result["300750"]],
            ["固态电池", "人工智能"],
        )
        count = self.db.query(
            "SELECT COUNT(*) AS count FROM stock_sector_mapping "
            "WHERE stock_code = ?",
            ("300750",),
        )[0]["count"]
        self.assertEqual(count, 3)

        with patch.object(
            service,
            "_fetch_stock_profile",
            side_effect=AssertionError("同日缓存不应重复请求"),
        ):
            cached = service.get_stock_themes(["300750"])
        self.assertEqual(cached, result)

    def test_market_code_normalization(self):
        self.assertEqual(StockThemeService._eastmoney_market_code("600519"), "SH600519")
        self.assertEqual(StockThemeService._eastmoney_market_code("300750"), "SZ300750")
        self.assertEqual(StockThemeService._eastmoney_market_code("920012"), "BJ920012")


if __name__ == "__main__":
    unittest.main()
