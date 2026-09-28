"""Phase 3 — candidate region data model tests (no onboarding/job wiring)."""

from __future__ import annotations

import unittest
from unittest import mock

from app.services.candidate_region import (
    CandidateRegionState,
    RegionSessionCreate,
    ResolvedRegion,
    build_resolved_region,
    empty_region_state,
    extract_region_state,
    region_fields_for_mongo,
)


class ResolvedRegionBuildTests(unittest.TestCase):
    def test_build_country_india(self):
        region = build_resolved_region("IN", "country")
        self.assertIsNotNone(region)
        self.assertEqual(region.regionType, "country")
        self.assertEqual(region.regionCode, "IN")
        self.assertTrue(-90 <= region.lat <= 90)
        self.assertTrue(-180 <= region.lon <= 180)

    def test_build_cluster_asia(self):
        region = build_resolved_region("asia", "cluster")
        self.assertIsNotNone(region)
        self.assertEqual(region.regionType, "cluster")
        self.assertEqual(region.regionCode, "asia")

    def test_build_unknown_returns_none(self):
        self.assertIsNone(build_resolved_region("ZZ", "country"))
        self.assertIsNone(build_resolved_region("not-real", "cluster"))


class RegionStateShapeTests(unittest.TestCase):
    def test_empty_state_defaults(self):
        state = empty_region_state()
        self.assertEqual(state.region_source, "none")
        self.assertFalse(state.is_manual_override)
        self.assertIsNone(state.resolved_region)

    def test_mongo_fields_round_trip(self):
        resolved = build_resolved_region("IN", "country")
        state = CandidateRegionState(
            phone_country_code="in",
            country_field="IN",
            resolved_region=resolved,
            is_manual_override=False,
            region_source="country_field",
        )
        fields = region_fields_for_mongo(state)
        self.assertEqual(fields["phone_country_code"], "IN")
        self.assertEqual(fields["country_field"], "IN")
        self.assertEqual(fields["region_source"], "country_field")
        self.assertFalse(fields["is_manual_override"])
        self.assertEqual(fields["resolved_region"]["regionCode"], "IN")

        restored = extract_region_state(fields)
        self.assertEqual(restored.phone_country_code, "IN")
        self.assertEqual(restored.country_field, "IN")
        self.assertEqual(restored.region_source, "country_field")
        self.assertIsNotNone(restored.resolved_region)

    def test_extract_falls_back_to_legacy_phoneCountry(self):
        state = extract_region_state({"phoneCountry": "US", "countryCode": "CA"})
        self.assertEqual(state.phone_country_code, "US")
        self.assertEqual(state.country_field, "CA")


class RegionSessionTests(unittest.TestCase):
    def test_upsert_region_session_writes_expected_shape(self):
        fake_coll = mock.MagicMock()
        fake_coll.find_one.return_value = None

        with mock.patch(
            "app.services.candidate_region.get_db",
            return_value={"geo_region_sessions": fake_coll},
        ):
            from app.services import candidate_region as cr

            resolved = ResolvedRegion(
                regionType="country",
                regionCode="IN",
                lat=20.0,
                lon=77.0,
            )
            doc = cr.upsert_region_session(
                RegionSessionCreate(
                    session_id="sess_test_1",
                    resolved_region=resolved,
                    is_manual_override=True,
                    region_source="manual_override",
                    country_field="IN",
                )
            )

        self.assertEqual(doc["session_id"], "sess_test_1")
        self.assertTrue(doc["is_manual_override"])
        self.assertEqual(doc["region_source"], "manual_override")
        self.assertEqual(doc["resolved_region"]["regionCode"], "IN")
        self.assertIn("expiresAt", doc)
        fake_coll.update_one.assert_called_once()


class OnboardingRegionPersistTests(unittest.TestCase):
    def test_country_name_resolves_to_india(self):
        from app.services.candidate_region import onboarding_region_persist_fields

        fields = onboarding_region_persist_fields(
            country_name_or_code="India",
            phone_country_code="US",
        )
        self.assertEqual(fields["country_field"], "IN")
        self.assertEqual(fields["phone_country_code"], "US")
        self.assertEqual(fields["region_source"], "country_field")
        self.assertFalse(fields["is_manual_override"])
        self.assertEqual(fields["resolved_region"]["regionCode"], "IN")

    def test_phone_code_used_when_country_missing(self):
        from app.services.candidate_region import onboarding_region_persist_fields

        fields = onboarding_region_persist_fields(phone_country_code="DE")
        self.assertEqual(fields["region_source"], "phone_code")
        self.assertEqual(fields["resolved_region"]["regionCode"], "DE")

    def test_manual_override_is_preserved(self):
        from app.services.candidate_region import onboarding_region_persist_fields

        existing = {
            "is_manual_override": True,
            "region_source": "manual_override",
            "country_field": "IN",
            "phone_country_code": "IN",
            "resolved_region": {
                "regionType": "cluster",
                "regionCode": "asia",
                "lat": 1.0,
                "lon": 2.0,
            },
        }
        fields = onboarding_region_persist_fields(
            country_name_or_code="Germany",
            phone_country_code="DE",
            existing_doc=existing,
        )
        self.assertTrue(fields["is_manual_override"])
        self.assertEqual(fields["region_source"], "manual_override")
        self.assertEqual(fields["resolved_region"]["regionCode"], "asia")
        self.assertEqual(fields["country_field"], "DE")
        self.assertEqual(fields["phone_country_code"], "DE")


if __name__ == "__main__":
    unittest.main()
