"""Shared persistence helpers for the QuantPilot watchlist."""
from __future__ import annotations

import json
import os
import threading
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List


WATCHLIST_FILE = Path("runtime/data/quantpilot_watchlist.json")
_LOCK = threading.RLock()


def _legacy_date() -> str:
    try:
        return datetime.fromtimestamp(WATCHLIST_FILE.stat().st_mtime).strftime("%Y-%m-%d")
    except OSError:
        return datetime.now().strftime("%Y-%m-%d")


def read_watchlist() -> List[Dict[str, Any]]:
    """Read the list and normalize legacy rows without making them pending entries."""
    try:
        if not WATCHLIST_FILE.exists():
            return []
        with WATCHLIST_FILE.open("r", encoding="utf-8") as handle:
            payload = json.load(handle)
        if not isinstance(payload, list):
            return []
    except (OSError, json.JSONDecodeError):
        return []

    changed = False
    legacy_date = _legacy_date()
    normalized: List[Dict[str, Any]] = []
    for raw in payload:
        if not isinstance(raw, dict):
            continue
        item = dict(raw)
        code = str(item.get("code", "")).strip().zfill(6)
        if not code.isdigit() or len(code) != 6:
            continue
        if not item.get("added_at"):
            item["added_at"] = legacy_date
            # Existing rows predate the next-open workflow. Do not surprise the
            # user by treating them as newly selected today.
            item["entry_pending"] = False
            changed = True
        elif "entry_pending" not in item:
            item["entry_pending"] = False
            changed = True
        item["code"] = code
        normalized.append(item)

    if changed:
        write_watchlist(normalized)
    return normalized


def write_watchlist(items: List[Dict[str, Any]]) -> None:
    WATCHLIST_FILE.parent.mkdir(parents=True, exist_ok=True)
    temp_file = WATCHLIST_FILE.with_name(
        f".{WATCHLIST_FILE.name}.{os.getpid()}.{threading.get_ident()}.tmp"
    )
    with _LOCK:
        try:
            with temp_file.open("w", encoding="utf-8") as handle:
                json.dump(items, handle, ensure_ascii=False, indent=2)
            temp_file.replace(WATCHLIST_FILE)
        finally:
            temp_file.unlink(missing_ok=True)


def mark_entry_attempted(code: str, trade_date: str, reason: str = "") -> bool:
    """Consume a pending next-open attempt exactly once."""
    code = str(code).strip().zfill(6)
    with _LOCK:
        items = read_watchlist()
        changed = False
        for item in items:
            if item.get("code") != code or not item.get("entry_pending"):
                continue
            item["entry_pending"] = False
            item["entry_decision_date"] = str(trade_date)[:10]
            if reason:
                item["entry_decision"] = str(reason)[:200]
            changed = True
            break
        if changed:
            write_watchlist(items)
        return changed
