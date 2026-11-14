import unittest

from app.services.attendance.coordinates import (
    gcj02_to_bd09,
    out_of_china,
    wgs84_to_bd09,
    wgs84_to_gcj02,
)


class AttendanceCoordinateTests(unittest.TestCase):
    def test_wgs84_gcj02_bd09_conversion_for_china_location(self):
        gcj_lng, gcj_lat = wgs84_to_gcj02(116.397428, 39.90923)
        self.assertAlmostEqual(gcj_lng, 116.40367, places=4)
        self.assertAlmostEqual(gcj_lat, 39.91063, places=4)
        bd_lng, bd_lat = gcj02_to_bd09(gcj_lng, gcj_lat)
        self.assertAlmostEqual(bd_lng, 116.41004, places=4)
        self.assertAlmostEqual(bd_lat, 39.91697, places=4)
        final_lng, final_lat = wgs84_to_bd09(116.397428, 39.90923)
        self.assertAlmostEqual(final_lng, bd_lng, places=8)
        self.assertAlmostEqual(final_lat, bd_lat, places=8)

    def test_coordinates_outside_china_do_not_apply_gcj_offset(self):
        self.assertTrue(out_of_china(-73.9857, 40.7484))
        self.assertEqual(wgs84_to_gcj02(-73.9857, 40.7484), (-73.9857, 40.7484))
