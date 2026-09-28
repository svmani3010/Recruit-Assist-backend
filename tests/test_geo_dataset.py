"""Phase 1 — static geo dataset consistency tests."""

from __future__ import annotations

import unittest

from app.services import geo_dataset


class GeoDatasetTests(unittest.TestCase):
    def test_countries_have_required_fields(self):
        countries = geo_dataset.list_countries()
        self.assertGreaterEqual(len(countries), 190)
        for row in countries:
            self.assertTrue(row["code"] and len(row["code"]) == 2)
            self.assertTrue(row["name"])
            self.assertIsInstance(row["lat"], (int, float))
            self.assertIsInstance(row["lon"], (int, float))
            self.assertGreaterEqual(float(row["lat"]), -90)
            self.assertLessEqual(float(row["lat"]), 90)
            self.assertGreaterEqual(float(row["lon"]), -180)
            self.assertLessEqual(float(row["lon"]), 180)
            self.assertIsInstance(row["clusters"], list)
            self.assertTrue(row["clusters"], msg=f"{row['code']} missing clusters")

    def test_india_exists_with_asia_membership(self):
        india = geo_dataset.get_country("IN")
        self.assertIsNotNone(india)
        self.assertEqual(india["name"], "India")
        self.assertIn("asia", india["clusters"])
        self.assertIsNotNone(geo_dataset.country_point("IN"))

    def test_clusters_cover_members_consistently(self):
        countries = {c["code"]: c for c in geo_dataset.list_countries()}
        clusters = geo_dataset.list_clusters()
        self.assertGreaterEqual(len(clusters), 10)

        for cluster in clusters:
            self.assertTrue(cluster["id"])
            self.assertTrue(cluster["name"])
            self.assertIn(cluster["type"], {"continent", "subregion"})
            members = cluster["memberCountryCodes"]
            self.assertTrue(members)
            for code in members:
                self.assertIn(code, countries)
                self.assertIn(cluster["id"], countries[code]["clusters"])

    def test_cluster_centroid_for_asia(self):
        point = geo_dataset.cluster_centroid("asia")
        self.assertIsNotNone(point)
        lat, lon = point
        self.assertGreaterEqual(lat, -90)
        self.assertLessEqual(lat, 90)
        self.assertGreaterEqual(lon, -180)
        self.assertLessEqual(lon, 180)

    def test_unknown_lookups_return_none_or_empty(self):
        self.assertIsNone(geo_dataset.get_country("ZZ"))
        self.assertIsNone(geo_dataset.get_cluster("not-a-cluster"))
        self.assertIsNone(geo_dataset.country_point("ZZ"))
        self.assertEqual(geo_dataset.cluster_member_codes("not-a-cluster"), ())
        self.assertIsNone(geo_dataset.cluster_centroid("not-a-cluster"))


    def test_lookup_country_code_by_name_and_iso(self):
        self.assertEqual(geo_dataset.lookup_country_code("IN"), "IN")
        self.assertEqual(geo_dataset.lookup_country_code("India"), "IN")
        self.assertEqual(geo_dataset.lookup_country_code("united states of america"), "")
        # Exact dataset name for US:
        us = geo_dataset.get_country("US")
        self.assertEqual(geo_dataset.lookup_country_code(us["name"]), "US")
        self.assertEqual(geo_dataset.lookup_country_code("not specified"), "")
        self.assertEqual(geo_dataset.lookup_country_code("ZZ"), "")


if __name__ == "__main__":
    unittest.main()
