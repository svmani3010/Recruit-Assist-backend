"""Tests for KV141–KV149 analytics platform helpers."""

from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone
from typing import Any, Optional
from unittest.mock import MagicMock

from bson import ObjectId

from app.services import analytics as analytics_service
from app.services import analytics_platform as platform
from app.services.metric_definitions import NOT_ENOUGH_DATA


class _FakeCollection:
    def __init__(self, docs=None):
        self._docs = list(docs or [])

    def find(self, query=None, projection=None, sort=None):
        matched = [dict(d) for d in self._docs if self._match(d, query or {})]
        if sort:
            for key, direction in reversed(list(sort)):
                matched.sort(
                    key=lambda d: d.get(key)
                    or datetime.min.replace(tzinfo=timezone.utc),
                    reverse=direction < 0,
                )
        return matched

    def find_one(self, query=None, projection=None, sort=None):
        rows = self.find(query, projection, sort=sort)
        return rows[0] if rows else None

    def count_documents(self, query=None):
        return len(self.find(query))

    def insert_one(self, doc):
        doc = dict(doc)
        if "_id" not in doc:
            doc["_id"] = ObjectId()
        self._docs.append(doc)
        result = MagicMock()
        result.inserted_id = doc["_id"]
        return result

    def update_one(self, query, update, upsert=False):
        doc = self.find_one(query)
        result = MagicMock()
        if doc is None:
            if not upsert:
                result.modified_count = 0
                return result
            new_doc = {}
            new_doc.update(update.get("$setOnInsert") or {})
            new_doc.update(update.get("$set") or {})
            for k, v in (query or {}).items():
                if not str(k).startswith("$"):
                    new_doc.setdefault(k, v)
            self.insert_one(new_doc)
            result.modified_count = 1
            return result
        idx = next(i for i, d in enumerate(self._docs) if d.get("_id") == doc.get("_id"))
        if "$set" in update:
            self._docs[idx].update(update["$set"])
        result.modified_count = 1
        return result

    def create_index(self, *args, **kwargs):
        return "ok"

    def sort(self, *args, **kwargs):
        return self

    def _match(self, doc: dict, query: dict) -> bool:
        if not query:
            return True
        if "$and" in query:
            return all(self._match(doc, q) for q in query["$and"])
        ok = True
        if "$or" in query:
            ok = ok and any(self._match(doc, q) for q in query["$or"])
        if "$nor" in query:
            ok = ok and not any(self._match(doc, q) for q in query["$nor"])
        for key, expected in query.items():
            if key.startswith("$"):
                continue
            actual = doc.get(key)
            if isinstance(expected, dict):
                if not self._match_ops(actual, expected, key in doc):
                    return False
            elif actual != expected:
                return False
        return ok

    def _match_ops(self, actual: Any, ops: dict, key_present: bool = True) -> bool:
        if "$exists" in ops and bool(ops["$exists"]) != key_present:
            return False
        if "$in" in ops:
            allowed = ops["$in"]
            if actual not in allowed and str(actual) not in [str(x) for x in allowed]:
                return False
        if "$nin" in ops and actual in ops["$nin"]:
            return False
        if "$ne" in ops and actual == ops["$ne"]:
            return False
        if "$gte" in ops and (actual is None or actual < ops["$gte"]):
            return False
        if "$lt" in ops and (actual is None or actual >= ops["$lt"]):
            return False
        if "$regex" in ops:
            import re

            flags = re.I if "i" in str(ops.get("$options") or "") else 0
            if not re.search(ops["$regex"], str(actual or ""), flags):
                return False
        return True


class _FakeDB:
    def __init__(self):
        self._cols = {}

    def __getitem__(self, name: str) -> _FakeCollection:
        if name not in self._cols:
            self._cols[name] = _FakeCollection()
        return self._cols[name]

    def seed(self, name: str, docs: list) -> None:
        self._cols[name] = _FakeCollection(docs)


class PlatformTests(unittest.TestCase):
    def setUp(self):
        self.db = _FakeDB()
        self.now = datetime(2026, 9, 15, 12, 0, tzinfo=timezone.utc)
        self.start = self.now - timedelta(days=30)
        self.end = self.now + timedelta(days=1)
        self.db.seed(
            "jobview",
            [
                {
                    "jobid": "job-1",
                    "status": "Open",
                    "show": "on",
                    "posted_timestamp": self.now - timedelta(days=10),
                    "hr_user_id": "hr-1",
                    "jobrole": "Engineer",
                    "technical_interview": True,
                }
            ],
        )
        self.db.seed(
            "candidateInformation",
            [
                {
                    "_id": ObjectId(),
                    "candidate_id": "c1",
                    "job_id": "job-1",
                    "application_status": "applied",
                    "applied_date": self.now - timedelta(days=7),
                    "found_us": "LinkedIn",
                },
                {
                    "_id": ObjectId(),
                    "candidate_id": "c2",
                    "job_id": "job-1",
                    "application_status": "applied",
                    "applied_date": self.now - timedelta(days=6),
                    "attribution_source": "featured",
                },
            ],
        )
        self.db.seed(
            "Scores",
            [
                {
                    "candidate_id": "c1",
                    "job_id": "job-1",
                    "l0_passed": True,
                    "status": "approved",
                    "l1_approved_at": self.now - timedelta(days=5),
                },
                {
                    "candidate_id": "c2",
                    "job_id": "job-1",
                    "l0_passed": True,
                    "status": "completed",
                },
            ],
        )
        self.db.seed("interviewInvites", [])
        self.db.seed("offers", [])
        self.db.seed("job_detail_views", [])
        self.db.seed("pipeline_stage_events", [])
        self.db.seed("analytics_refresh_meta", [])
        self.db.seed("analytics_insight_dismissals", [])

    def test_time_to_hire_rise_is_negative_sentiment(self):
        cur = {
            "name": "Time-to-hire",
            "value": 25.0,
            "display": "25 days",
            "definition": {"usesMedian": True},
        }
        prev = {"value": 10.0, "display": "10 days"}
        out = platform.attach_period_change(cur, prev, key="time_to_hire")
        self.assertEqual(out["periodChangeDirection"], "up")
        self.assertEqual(out["periodChangeSentiment"], "negative")

    def test_applications_rise_is_positive(self):
        cur = {"name": "Total applications", "value": 20, "display": "20"}
        prev = {"value": 10, "display": "10"}
        out = platform.attach_period_change(cur, prev, key="total_applications")
        self.assertEqual(out["periodChangeSentiment"], "positive")

    def test_funnel_highlights_largest_drop(self):
        jobs = list(self.db["jobview"].find({}))
        # Add views so top of funnel is large
        for _ in range(20):
            analytics_service.record_job_view(
                self.db, job_id="job-1", duration_seconds=10
            )
        for doc in self.db["job_detail_views"]._docs:
            doc["viewed_at"] = self.now - timedelta(days=1)
        funnel = platform.compute_funnel(
            self.db, jobs=jobs, start=self.start, end=self.end
        )
        self.assertTrue(funnel["stages"])
        drops = [s for s in funnel["stages"] if s.get("isLargestDrop")]
        self.assertEqual(len(drops), 1)

    def test_source_quality_ranking(self):
        # Mark c1 hired so LinkedIn ranks above featured by quality
        for doc in self.db["candidateInformation"]._docs:
            if doc["candidate_id"] == "c1":
                doc["hired_at"] = self.now - timedelta(days=1)
                doc["pipeline_status"] = "hired"
        ranked = platform.compute_source_performance_ranked(
            self.db, ["job-1"], self.start, self.end
        )
        self.assertEqual(ranked["sources"][0]["source"], "Shared link — LinkedIn")
        self.assertEqual(ranked["sources"][0]["qualityRank"], 1)

    def test_empty_dashboard_state(self):
        empty_db = _FakeDB()
        empty_db.seed("jobview", [])
        empty_db.seed("candidateInformation", [])
        empty_db.seed("Scores", [])
        empty_db.seed("interviewInvites", [])
        empty_db.seed("offers", [])
        empty_db.seed("job_detail_views", [])
        empty_db.seed("pipeline_stage_events", [])
        empty_db.seed("analytics_refresh_meta", [])
        empty_db.seed("featured_events", [])
        filters = platform.resolve_filters(
            start=self.start, end=self.end, compare=False
        )
        dash = platform.assemble_home_dashboard(
            empty_db,
            job_scope_query={"hr_user_id": "hr-1"},
            filters=filters,
            org_id=None,
            include_insights=False,
        )
        self.assertTrue(dash["isEmpty"])
        self.assertIsNotNone(dash["emptyState"])

    def test_export_csv_has_filter_header(self):
        filters = platform.resolve_filters(preset="last_30_days", compare=False)
        csv_bytes = platform.export_metrics_csv(
            title="Test",
            filters=filters,
            metrics={
                "hires": {"name": "Hires", "value": 1, "display": "1"},
            },
        )
        text = csv_bytes.decode("utf-8")
        self.assertIn("Period start", text)
        self.assertIn("Hires", text)

    def test_reconcile_ok(self):
        result = platform.reconcile_headline_counts(
            self.db,
            job_scope_query={"hr_user_id": "hr-1"},
            start=self.start,
            end=self.end,
        )
        self.assertTrue(result["ok"])


if __name__ == "__main__":
    unittest.main()
