"""Unit tests for plan-based priority ranking (KV135 / KV136)."""

from __future__ import annotations

import unittest

from app.services.priority_engine import build_ranking, rotate_pool


def _job(job_id: str, employer_id: str, role: str = "Engineer") -> dict:
    return {
        "jobid": job_id,
        "employer_id": employer_id,
        "jobrole": role,
    }


class PriorityEngineTests(unittest.TestCase):
    def test_enterprise_lands_in_top_2(self):
        jobs = [
            _job("e1", "ent-a"),
            _job("s1", "std-a"),
            _job("s2", "std-b"),
            _job("s3", "std-c"),
        ]
        entitlements = {
            "ent-a": "enterprise",
            "std-a": "standard",
            "std-b": "standard",
            "std-c": "standard",
        }
        ranked = build_ranking(jobs, entitlements, round_counter=0)
        top2 = {r.job["jobid"] for r in ranked if r.position <= 2}
        self.assertIn("e1", top2)
        e1 = next(r for r in ranked if r.job["jobid"] == "e1")
        self.assertEqual(e1.badge, "Promoted")
        self.assertLessEqual(e1.position, 2)

    def test_premium_lands_in_top_10(self):
        jobs = [_job(f"p{i}", f"prem-{i}") for i in range(3)]
        jobs += [_job(f"s{i}", f"std-{i}") for i in range(8)]
        entitlements = {f"prem-{i}": "premium" for i in range(3)}
        entitlements.update({f"std-{i}": "standard" for i in range(8)})
        ranked = build_ranking(jobs, entitlements, round_counter=0)
        for r in ranked:
            if r.pool == "premium" and not r.shared_position:
                self.assertLessEqual(r.position, 10)
                self.assertEqual(r.badge, "Promoted")

    def test_non_matching_priority_never_injected(self):
        # Ranker only sees already-matched jobs — a Designer job is simply absent.
        jobs = [_job("acct", "prem-1", role="Accountant")]
        entitlements = {"prem-1": "premium"}
        ranked = build_ranking(jobs, entitlements, round_counter=0)
        ids = {r.job["jobid"] for r in ranked}
        self.assertNotIn("designer", ids)
        self.assertEqual(ids, {"acct"})

    def test_enterprise_above_premium(self):
        jobs = [
            _job("p1", "prem-a"),
            _job("e1", "ent-a"),
            _job("s1", "std-a"),
        ]
        entitlements = {
            "prem-a": "premium",
            "ent-a": "enterprise",
            "std-a": "standard",
        }
        ranked = build_ranking(jobs, entitlements, round_counter=0)
        by_id = {r.job["jobid"]: r for r in ranked}
        self.assertLess(by_id["e1"].position, by_id["p1"].position)

    def test_rotation_changes_order(self):
        pool = [_job(f"e{i}", f"ent-{i}") for i in range(4)]
        r0 = [j["jobid"] for j in rotate_pool(pool, 0)]
        r1 = [j["jobid"] for j in rotate_pool(pool, 1)]
        self.assertNotEqual(r0, r1)
        self.assertEqual(set(r0), set(r1))

    def test_employer_cap_marks_shared_position(self):
        jobs = [_job(f"e{i}", "ent-big") for i in range(5)]
        jobs += [_job(f"s{i}", f"std-{i}") for i in range(6)]
        entitlements = {"ent-big": "enterprise"}
        entitlements.update({f"std-{i}": "standard" for i in range(6)})
        ranked = build_ranking(
            jobs, entitlements, round_counter=0, max_per_employer=3
        )
        # Only 2 enterprise slots exist; cap still applies within slotted set.
        shared = [r for r in ranked if r.shared_position]
        # With 5 enterprise and only 2 slots, overflow is SHARED_POSITION.
        self.assertTrue(any(r.reason == "SHARED_POSITION" for r in shared))

    def test_organic_reserve_when_organic_exists(self):
        # Many premium + enough organic → at least 4 organic in top 10.
        jobs = [_job(f"p{i}", f"prem-{i}") for i in range(8)]
        jobs += [_job(f"s{i}", f"std-{i}") for i in range(6)]
        entitlements = {f"prem-{i}": "premium" for i in range(8)}
        entitlements.update({f"std-{i}": "standard" for i in range(6)})
        ranked = build_ranking(
            jobs, entitlements, round_counter=0, min_organic=4
        )
        top10 = [r for r in ranked if r.position <= 10]
        organic_in_top = sum(1 for r in top10 if r.pool == "standard")
        self.assertGreaterEqual(organic_in_top, 4)


if __name__ == "__main__":
    unittest.main()
