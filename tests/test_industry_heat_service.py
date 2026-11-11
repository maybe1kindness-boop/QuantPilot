import unittest
from contextlib import contextmanager
from datetime import datetime

import pandas as pd

from utils.industry_heat_service import IndustryHeatService


class _FakeDb:
    def __init__(self):
        self.rows = []

    @contextmanager
    def transaction(self):
        yield

    def execute(self, _sql, params):
        self.rows.append(params)

    def query(self, _sql, _params):
        return []


class _FakeSource:
    name = "测试主源"

    def fetch(self):
        return pd.DataFrame([
            {"行业代码": "BK2", "行业名称": "电子元件", "涨跌幅": 2.1, "上涨家数": 8, "下跌家数": 2},
            {"行业代码": "BK1", "行业名称": "软件开发", "涨跌幅": 3.2, "上涨家数": 7, "下跌家数": 3},
            {"行业代码": "BK3", "行业名称": "化学制品", "涨跌幅": -1.0, "上涨家数": 1, "下跌家数": 9},
        ])


class IndustryHeatServiceTests(unittest.TestCase):
    def test_refresh_sorts_and_persists_industry_heat(self):
        db = _FakeDb()
        service = IndustryHeatService(
            db,
            source=_FakeSource(),
            now_provider=lambda: datetime(2026, 8, 30, 15, 25),
        )

        top = service.refresh("2026-08-30", limit=2)

        self.assertEqual([item["name"] for item in top], ["软件开发", "电子元件"])
        self.assertEqual([item["rank"] for item in top], [1, 2])
        self.assertEqual(len(db.rows), 3)
        self.assertEqual(db.rows[0][0], "BK1")
        self.assertEqual(db.rows[0][4], 1)

    def test_refresh_falls_back_after_empty_primary_source(self):
        class EmptySource:
            name = "空主源"

            def fetch(self):
                return pd.DataFrame()

        db = _FakeDb()
        service = IndustryHeatService(
            db,
            now_provider=lambda: datetime(2026, 8, 30, 15, 25),
            sleep_fn=lambda _seconds: None,
            retries=1,
        )
        service.sources = [EmptySource(), _FakeSource()]

        top = service.refresh("2026-08-28", limit=1)

        self.assertEqual(top[0]["name"], "软件开发")
        self.assertEqual(top[0]["source"], "测试主源")
        self.assertTrue(all(str(row[-1]).startswith("2026-08-28") for row in db.rows))
