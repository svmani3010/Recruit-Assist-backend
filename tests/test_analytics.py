"""Unit tests for KV140 E19 Job Performance Analytics."""

from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone
from typing import Any, Optional
from unittest.mock import MagicMock

from bson import ObjectId

from app.services.metric_definitions import (
    NOT_ENOUGH_DATA,
    METRIC_DEFINITIONS,
    definitions_sign_off_status,
    get_definition,
    list_definitions,
)
from app.services import analytics as analytics_service


class _FakeCollection:
    def __init__(self, docs: Optional[list] = None):
        self._docs = list(docs or [])

    def find(self, query=None, projection=None, sort=None):
        matched = [dict(d) for d in self._docs if self._match(d, query or {})]
        if sort:
            for key, direction in reversed(list(sort)):
                matched.sort(key=lambda d: d.get(key) or datetime.min.replace(tzinfo=timezone.utc), reverse=direction < 0)
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
            if upsert:
                new_doc = {}
                new_doc.update(update.get("$setOnInsert") or {})
                new_doc.update(update.get("$set") or {})
                for k, v in (query or {}).items():
                    if not str(k).startswith("$"):
                        new_doc.setdefault(k, v)
                self.insert_one(new_doc)
                result.modified_count = 1
            else:
                result.modified_count = 0
            return result
        idx = next(i for i, d in enumerate(self._docs) if d.get("_id") == doc.get("_id"))
        if "$set" in update:
            self._docs[idx].update(update["$set"])
        result.modified_count = 1
        return result

    def update_many(self, query, update):
        matched = 0
        for i, d in enumerate(self._docs):
            if self._match(d, query or {}):
                if "$set" in update:
                    self._docs[i].update(update["$set"])
                matched += 1
        result = MagicMock()
        result.matched_count = matched
        result.modified_count = matched
        return result

    def create_index(self, *args, **kwargs):
        return "ok"

    def _match(self, doc: dict, query: dict) -> bool:
        if not query:
            return True
        if "$and" in query:
            return all(self._match(doc, q) for q in query["$and"])

        # Combine $or / $nor / field predicates (Mongo ANDs top-level keys)
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
        if "$exists" in ops:
            exists = key_present
            if bool(ops["$exists"]) != exists:
                return False
        if "$in" in ops:
            allowed = ops["$in"]
            if actual not in allowed and str(actual) not in [str(x) for x in allowed]:
                return False
        if "$nin" in ops:
            if actual in ops["$nin"]:
                return False
        if "$ne" in ops:
            if actual == ops["$ne"]:
                return False
        if "$gte" in ops:
            if actual is None or actual < ops["$gte"]:
                return False
        if "$lt" in ops:
            if actual is None or actual >= ops["$lt"]:
                return False
        if "$regex" in ops:
            import re

            flags = re.I if "i" in str(ops.get("$options") or "") else 0
            if not re.search(ops["$regex"], str(actual or ""), flags):
                return False
        return True


class _FakeDB:
    def __init__(self):
        self._cols: dict[str, _FakeCollection] = {}

    def __getitem__(self, name: str) -> _FakeCollection:
        if name not in self._cols:
            self._cols[name] = _FakeCollection()
        return self._cols[name]

    def seed(self, name: str, docs: list) -> None:
        self._cols[name] = _FakeCollection(docs)


class MetricDefinitionsTests(unittest.TestCase):
    def test_all_sheet_metrics_present(self):
        keys = {m.key for m in METRIC_DEFINITIONS}
        expected = {
            "total_jobs_posted",
            "active_jobs",
            "job_views",
            "apply_rate",
            "total_applications",
            "qualified_candidates",
            "screening_rate",
            "technical_interview_completion_rate",
            "approval_rate",
            "interview_conversion_rate",
            "interview_to_offer_rate",
            "offer_acceptance_rate",
            "hires",
            "offers_made",
            "time_to_hire",
            "time_to_fill",
            "stage_duration",
            "source_performance",
            "featured_impressions",
            "featured_click_through_rate",
        }
        self.assertEqual(keys, expected)

    def test_time_metrics_use_median(self):
        for key in ("time_to_hire", "time_to_fill", "stage_duration"):
            self.assertTrue(get_definition(key).uses_median)

    def test_exclusions_mention_withdrawn_duplicates_test(self):
        apps = get_definition("total_applications")
        text = apps.excluded.lower()
        self.assertIn("withdrawn", text)
        self.assertIn("duplicate", text)
        self.assertIn("test", text)

    def test_sign_off_ready(self):
        status = definitions_sign_off_status()
        self.assertTrue(status["readyForBuild"])
        self.assertEqual(status["metricCount"], len(list_definitions()))


class AnalyticsServiceTests(unittest.TestCase):
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
                    "technical_interview": True,
                },
                {
                    "jobid": "job-draft",
                    "status": "Draft",
                    "show": "off",
                    "posted_timestamp": self.now - timedelta(days=5),
                    "hr_user_id": "hr-1",
                },
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
                    "found_us": "LinkedIn",
                },
                {
                    "_id": ObjectId(),
                    "candidate_id": "c3",
                    "job_id": "job-1",
                    "application_status": "applied",
                    "applied_date": self.now - timedelta(days=5),
                    "is_test": True,
                    "found_us": "Referral",
                },
                {
                    "_id": ObjectId(),
                    "candidate_id": "c4",
                    "job_id": "job-1",
                    "application_status": "superseded",
                    "applied_date": self.now - timedelta(days=4),
                    "found_us": "Indeed",
                },
                {
                    "_id": ObjectId(),
                    "candidate_id": "c5",
                    "job_id": "job-1",
                    "application_status": "withdrawn",
                    "applied_date": self.now - timedelta(days=3),
                    "withdrawn_at": self.now - timedelta(days=3, hours=-2),
                    "found_us": "Website",
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
                    "l0_completed_at": self.now - timedelta(days=6),
                },
                {
                    "candidate_id": "c2",
                    "job_id": "job-1",
                    "l0_passed": False,
                    "compatibility": 20,
                    "status": "completed",
                },
            ],
        )

        self.db.seed(
            "interviewInvites",
            [
                {
                    "candidate_id": "c1",
                    "job_id": "job-1",
                    "status": "completed",
                    "created_at": self.now - timedelta(days=4),
                    "outcome_at": self.now - timedelta(days=3),
                },
                {
                    "candidate_id": "c2",
                    "job_id": "job-1",
                    "status": "pending",
                    "created_at": self.now - timedelta(days=2),
                },
            ],
        )

    def test_excludes_test_duplicate_withdrawn(self):
        apps = analytics_service.compute_total_applications(
            self.db, ["job-1"], self.start, self.end
        )
        # c1 + c2 only
        self.assertEqual(apps["value"], 2)

    def test_screening_rate(self):
        metric = analytics_service.compute_screening_rate(
            self.db, ["job-1"], self.start, self.end
        )
        self.assertEqual(metric["numerator"], 1)
        self.assertEqual(metric["denominator"], 2)
        self.assertAlmostEqual(metric["value"], 0.5)

    def test_rate_not_enough_data(self):
        metric = analytics_service.compute_apply_rate(
            self.db, ["job-1"], self.start, self.end
        )
        # no views recorded → Not enough data
        self.assertIsNone(metric["value"])
        self.assertEqual(metric["display"], NOT_ENOUGH_DATA)

    def test_job_views_min_duration_and_exclusions(self):
        analytics_service.record_job_view(
            self.db, job_id="job-1", duration_seconds=3
        )
        analytics_service.record_job_view(
            self.db, job_id="job-1", duration_seconds=10, is_org_user=True
        )
        analytics_service.record_job_view(
            self.db, job_id="job-1", duration_seconds=10, is_bot=True
        )
        good = analytics_service.record_job_view(
            self.db, job_id="job-1", duration_seconds=10
        )
        self.assertTrue(good["countable"])
        # Fix viewed_at into period for the countable one
        for doc in self.db["job_detail_views"]._docs:
            doc["viewed_at"] = self.now - timedelta(days=1)
        metric = analytics_service.compute_job_views(
            self.db, ["job-1"], self.start, self.end
        )
        self.assertEqual(metric["value"], 1)

    def test_time_to_hire_uses_median(self):
        # Three hires with 10, 20, 100 days → median 20 (average is skewed)
        base_apply = self.now - timedelta(days=120)
        self.db.seed(
            "candidateInformation",
            [
                {
                    "candidate_id": f"h{i}",
                    "job_id": "job-1",
                    "application_status": "applied",
                    "applied_date": base_apply,
                    "hired_at": base_apply + timedelta(days=days),
                    "pipeline_status": "hired",
                }
                for i, days in enumerate((10, 20, 100))
            ],
        )
        for i, days in enumerate((10, 20, 100)):
            analytics_service.upsert_offer(
                self.db,
                candidate_id=f"h{i}",
                job_id="job-1",
                status="accepted",
                offered_at=base_apply + timedelta(days=days - 1),
                decided_at=base_apply + timedelta(days=days),
            )
        metric = analytics_service.compute_time_to_hire(
            self.db, ["job-1"], self.now - timedelta(days=130), self.end
        )
        self.assertEqual(metric["value"], 20.0)
        self.assertEqual(metric["average"], round((10 + 20 + 100) / 3, 1))
        self.assertIn("definition", metric)
        self.assertTrue(metric["definition"]["usesMedian"])

    def test_offer_acceptance_excludes_pending(self):
        analytics_service.upsert_offer(
            self.db, candidate_id="c1", job_id="job-1", status="accepted",
            offered_at=self.now - timedelta(days=2),
            decided_at=self.now - timedelta(days=1),
        )
        analytics_service.upsert_offer(
            self.db, candidate_id="c2", job_id="job-1", status="declined",
            offered_at=self.now - timedelta(days=2),
            decided_at=self.now - timedelta(days=1),
        )
        analytics_service.upsert_offer(
            self.db, candidate_id="c9", job_id="job-1", status="pending",
            offered_at=self.now - timedelta(days=1),
        )
        # Fix decided_at into collection docs
        for doc in self.db["offers"]._docs:
            if doc.get("status") in {"accepted", "declined"}:
                doc["decided_at"] = self.now - timedelta(days=1)
        metric = analytics_service.compute_offer_acceptance_rate(
            self.db, ["job-1"], self.start, self.end
        )
        self.assertEqual(metric["numerator"], 1)
        self.assertEqual(metric["denominator"], 2)

    def test_source_performance(self):
        metric = analytics_service.compute_source_performance(
            self.db, ["job-1"], self.start, self.end
        )
        sources = {row["source"]: row for row in metric["sources"]}
        self.assertIn("LinkedIn", sources)
        self.assertEqual(sources["LinkedIn"]["applications"], 2)
        self.assertEqual(sources["LinkedIn"]["qualified"], 1)
        self.assertNotIn("Indeed", sources)  # superseded excluded
        self.assertNotIn("Referral", sources)  # test excluded

    def test_dashboard_includes_definitions_and_highlights(self):
        dash = analytics_service.compute_dashboard(
            self.db,
            job_scope_query={"hr_user_id": "hr-1"},
            start=self.start,
            end=self.end,
        )
        self.assertIn("metrics", dash)
        self.assertIn("highlights", dash)
        self.assertIn("definitions", dash)
        self.assertEqual(dash["notEnoughDataLabel"], NOT_ENOUGH_DATA)
        self.assertIn("screening_rate", dash["metrics"])
        self.assertIn("time_to_hire", dash["metrics"])
        self.assertIn("source_performance", dash["metrics"])

    def test_active_jobs_excludes_drafts(self):
        metric = analytics_service.compute_active_jobs(
            self.db, {"hr_user_id": "hr-1"}
        )
        self.assertEqual(metric["value"], 1)

    def test_featured_ctr(self):
        for _ in range(4):
            analytics_service.record_featured_event(
                self.db, job_id="job-1", event_type="impression"
            )
        analytics_service.record_featured_event(
            self.db, job_id="job-1", event_type="click"
        )
        for doc in self.db["featured_events"]._docs:
            doc["occurred_at"] = self.now - timedelta(days=1)
        metric = analytics_service.compute_featured_ctr(
            self.db, ["job-1"], self.start, self.end
        )
        self.assertEqual(metric["numerator"], 1)
        self.assertEqual(metric["denominator"], 4)
        self.assertAlmostEqual(metric["value"], 0.25)


if __name__ == "__main__":
    unittest.main()
