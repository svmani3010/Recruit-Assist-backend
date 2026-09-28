"""Phase 2 — distance engine unit tests (isolated from jobs/onboarding)."""

from __future__ import annotations

import math
import unittest

from app.services.distance_engine import (
    EARTH_RADIUS_KM,
    haversine_km,
    resolve_region_point,
    sort_by_proximity,
    sort_country_codes_by_proximity,
    validate_lat_lon,
)


class HaversineTests(unittest.TestCase):
    def test_identical_points_are_zero(self):
        self.assertEqual(haversine_km(20.0, 77.0, 20.0, 77.0), 0.0)

    def test_symmetry(self):
        a = haversine_km(40.7128, -74.0060, 51.5074, -0.1278)  # NYC ↔ London
        b = haversine_km(51.5074, -0.1278, 40.7128, -74.0060)
        self.assertAlmostEqual(a, b, places=6)

    def test_known_nyc_london_distance(self):
        # Approximate great-circle distance ~5570 km
        distance = haversine_km(40.7128, -74.0060, 51.5074, -0.1278)
        self.assertGreater(distance, 5400)
        self.assertLess(distance, 5800)

    def test_short_distance_reasonable(self):
        # ~111 km per degree latitude near equator
        distance = haversine_km(0.0, 0.0, 1.0, 0.0)
        self.assertAlmostEqual(distance, (math.pi / 180.0) * EARTH_RADIUS_KM, delta=1.0)

    def test_invalid_latitude_raises(self):
        with self.assertRaises(ValueError):
            haversine_km(100.0, 0.0, 0.0, 0.0)

    def test_validate_lat_lon(self):
        self.assertEqual(validate_lat_lon("12.5", "-69.9"), (12.5, -69.9))
        with self.assertRaises(ValueError):
            validate_lat_lon("nan", 0)


class ProximitySortTests(unittest.TestCase):
    def test_sorts_ascending_by_distance(self):
        reference = (20.0, 77.0)  # near India centroid
        points = [
            {"id": "far", "lat": 51.5, "lon": -0.1},  # London
            {"id": "near", "lat": 23.8, "lon": 90.4},  # Bangladesh-ish
            {"id": "mid", "lat": 33.7, "lon": 73.0},  # Pakistan-ish
        ]
        ranked = sort_by_proximity(reference[0], reference[1], points)
        self.assertEqual([row["id"] for row in ranked], ["near", "mid", "far"])
        self.assertTrue(
            ranked[0]["distanceKm"]
            <= ranked[1]["distanceKm"]
            <= ranked[2]["distanceKm"]
        )

    def test_preserves_extra_fields(self):
        ranked = sort_by_proximity(
            0.0,
            0.0,
            [{"id": "a", "lat": 1.0, "lon": 0.0, "jobId": "J1", "title": "Dev"}],
        )
        self.assertEqual(ranked[0]["jobId"], "J1")
        self.assertEqual(ranked[0]["title"], "Dev")
        self.assertIn("distanceKm", ranked[0])

    def test_tuple_points_supported(self):
        ranked = sort_by_proximity(0.0, 0.0, [("x", 2.0, 0.0), ("y", 1.0, 0.0)])
        self.assertEqual([row["id"] for row in ranked], ["y", "x"])

    def test_skip_invalid_points(self):
        ranked = sort_by_proximity(
            0.0,
            0.0,
            [
                {"id": "ok", "lat": 1.0, "lon": 0.0},
                {"id": "bad", "lat": 999.0, "lon": 0.0},
            ],
            skip_invalid=True,
        )
        self.assertEqual([row["id"] for row in ranked], ["ok"])

    def test_invalid_point_raises_by_default(self):
        with self.assertRaises(ValueError):
            sort_by_proximity(0.0, 0.0, [{"id": "bad", "lat": 999.0, "lon": 0.0}])


class CountryConvenienceTests(unittest.TestCase):
    def test_sort_country_codes_with_injected_points(self):
        fake_points = {
            "IN": (20.0, 77.0),
            "BD": (23.8, 90.4),
            "GB": (54.0, -2.0),
        }

        def fake_country_point(code: str):
            return fake_points.get(str(code).upper())

        ranked = sort_country_codes_by_proximity(
            "IN",
            ["GB", "BD", "IN"],
            country_point_fn=fake_country_point,
        )
        self.assertEqual([row["code"] for row in ranked], ["IN", "BD", "GB"])
        self.assertEqual(ranked[0]["distanceKm"], 0.0)

    def test_resolve_region_point_country_and_cluster(self):
        country = resolve_region_point(
            "IN",
            "country",
            country_point_fn=lambda _code: (20.0, 77.0),
        )
        self.assertEqual(country, (20.0, 77.0))

        cluster = resolve_region_point(
            "asia",
            "cluster",
            cluster_centroid_fn=lambda _cid: (30.0, 90.0),
        )
        self.assertEqual(cluster, (30.0, 90.0))

    def test_resolve_region_point_unknown_type(self):
        with self.assertRaises(ValueError):
            resolve_region_point("IN", "city")


if __name__ == "__main__":
    unittest.main()
