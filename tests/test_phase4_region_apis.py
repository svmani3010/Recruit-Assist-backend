"""Phase 4 — API contract tests for geo + region + jobs proximity."""

from __future__ import annotations

import unittest
from unittest import mock

from app.services.candidate_region import resolve_region_from_signals
from app.services.jobs_proximity import sort_jobs_by_resolved_region


class ResolveApiContractTests(unittest.TestCase):
    def test_country_field_wins_over_phone(self):
        result = resolve_region_from_signals(
            country_field="IN",
            phone_country_code="US",
        )
        self.assertEqual(result["regionSource"], "country_field")
        self.assertEqual(result["resolvedRegion"]["regionCode"], "IN")

    def test_phone_code_used_when_country_missing(self):
        result = resolve_region_from_signals(
            country_field="",
            phone_country_code="US",
        )
        self.assertEqual(result["regionSource"], "phone_code")
        self.assertEqual(result["resolvedRegion"]["regionCode"], "US")

    def test_none_when_both_missing(self):
        result = resolve_region_from_signals("", "")
        self.assertEqual(result["regionSource"], "none")
        self.assertIsNone(result["resolvedRegion"])

    def test_idempotent_pure_function(self):
        a = resolve_region_from_signals("IN", "US")
        b = resolve_region_from_signals("IN", "US")
        self.assertEqual(a, b)


class JobsCountryFilterTests(unittest.TestCase):
    def test_keeps_only_selected_country(self):
        jobs = [
            {"jobid": "gb", "locationCountryCode": "GB", "jobrole": "UK"},
            {"jobid": "in", "locationCountryCode": "IN", "jobrole": "IN"},
            {"jobid": "us", "locationCountryCode": "US", "jobrole": "US"},
        ]
        filtered = sort_jobs_by_resolved_region(
            jobs,
            resolved_region="IN",
            region_type="country",
        )
        self.assertEqual([j["jobid"] for j in filtered], ["in"])

    def test_parses_country_from_location_text(self):
        jobs = [
            {"jobid": "us", "location": "Austin, United States"},
            {"jobid": "in", "location": "Bengaluru, India"},
            {"jobid": "remote", "location": "Work From Home"},
        ]
        filtered = sort_jobs_by_resolved_region(
            jobs,
            resolved_region="IN",
            region_type="country",
        )
        self.assertEqual([j["jobid"] for j in filtered], ["in"])

    def test_missing_country_returns_full_list(self):
        jobs = [{"jobid": "a"}, {"jobid": "b"}]
        filtered = sort_jobs_by_resolved_region(
            jobs,
            resolved_region="",
            region_type="country",
        )
        self.assertEqual([j["jobid"] for j in filtered], ["a", "b"])

    def test_all_countries_returns_full_list(self):
        jobs = [
            {"jobid": "in", "locationCountryCode": "IN"},
            {"jobid": "us", "locationCountryCode": "US"},
        ]
        filtered = sort_jobs_by_resolved_region(
            jobs,
            resolved_region="ALL",
            region_type="country",
        )
        self.assertEqual([j["jobid"] for j in filtered], ["in", "us"])

    def test_remote_wfh_location_text_with_country(self):
        jobs = [
            {
                "jobid": "remote-in",
                "location": "Work From Home · Bengaluru, India",
                "locationCountryCode": "IN",
                "workMode": "Remote",
            },
            {
                "jobid": "remote-us",
                "location": "Work From Home · Austin, United States",
                "locationCountryCode": "US",
                "workMode": "Remote",
            },
        ]
        filtered = sort_jobs_by_resolved_region(
            jobs,
            resolved_region="IN",
            region_type="country",
        )
        self.assertEqual([j["jobid"] for j in filtered], ["remote-in"])


class GeoDatasetApiShapeTests(unittest.TestCase):
    def test_countries_payload_has_coords_and_clusters(self):
        from app.services import geo_dataset

        row = geo_dataset.get_country("IN")
        self.assertIsNotNone(row)
        self.assertIn("lat", row)
        self.assertIn("lon", row)
        self.assertTrue(row["clusters"])

    def test_clusters_payload_has_members(self):
        from app.services import geo_dataset

        cluster = geo_dataset.get_cluster("asia")
        self.assertIsNotNone(cluster)
        self.assertIn("IN", cluster["memberCountryCodes"])


class OverrideWritePathTests(unittest.TestCase):
    def test_override_mints_session_when_anonymous(self):
        fake_coll = mock.MagicMock()
        fake_coll.find_one.return_value = None

        with mock.patch(
            "app.services.candidate_region.get_db",
            return_value={"geo_region_sessions": fake_coll},
        ):
            from app.services.candidate_region import apply_manual_override

            result = apply_manual_override(
                region_code="IN",
                region_type="country",
                session_id="",
            )

        self.assertTrue(result["isManualOverride"])
        self.assertEqual(result["resolvedRegion"]["regionCode"], "IN")
        self.assertTrue(result["sessionId"])
        fake_coll.update_one.assert_called_once()


if __name__ == "__main__":
    unittest.main()
