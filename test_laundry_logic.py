import datetime
import json
import tempfile
import unittest
from pathlib import Path

import laundry_logic


class CalculatePriceTests(unittest.TestCase):
    def test_calculates_base_and_weight_charges(self):
        self.assertEqual(laundry_logic.calculate_price("Wash & Fold", 2.5), 230.0)
        self.assertEqual(
            laundry_logic.get_price_breakdown("Comforter Wash", 2),
            {"base_price": 250.0, "weight_charge": 40.0, "total_price": 290.0},
        )

    def test_rejects_invalid_service_and_weight(self):
        for service, weight in (
            ("Unknown service", 1),
            ("Wash & Fold", 0),
            ("Wash & Fold", -1),
            ("Wash & Fold", float("inf")),
            ("Wash & Fold", float("nan")),
            ("Wash & Fold", laundry_logic.MAX_WEIGHT_KG + 0.1),
        ):
            with self.subTest(service=service, weight=weight):
                with self.assertRaises(ValueError):
                    laundry_logic.calculate_price(service, weight)


class ValidatePickupDateTests(unittest.TestCase):
    def test_rejects_missing_malformed_past_and_too_far_dates(self):
        today = datetime.date(2026, 1, 1)
        for value in (
            "",
            "not-a-date",
            "2026-02-30",
            (today - datetime.timedelta(days=1)).isoformat(),
            (today + datetime.timedelta(days=31)).isoformat(),
        ):
            with self.subTest(value=value):
                result, error = laundry_logic.validate_pickup_date(value, today=today)
                self.assertIsNone(result)
                self.assertTrue(error)

    def test_accepts_dates_inside_the_booking_window(self):
        today = datetime.date(2026, 1, 1)
        valid_date = (today + datetime.timedelta(days=30)).isoformat()
        self.assertEqual(
            laundry_logic.validate_pickup_date(valid_date, today=today),
            (valid_date, None),
        )

    def test_checks_weekday_and_sunday_operating_slots(self):
        today = datetime.date.today()
        weekday_date = today + datetime.timedelta(days=1)
        while weekday_date.weekday() == 6:
            weekday_date += datetime.timedelta(days=1)
        sunday_date = today + datetime.timedelta(days=(6 - today.weekday()) % 7 or 7)
        weekday = weekday_date.isoformat()
        sunday = sunday_date.isoformat()
        self.assertEqual(
            laundry_logic.validate_pickup_date(weekday, "07:00", today=today),
            (weekday, None),
        )
        self.assertEqual(
            laundry_logic.validate_pickup_date(sunday, "08:00", today=today),
            (sunday, None),
        )
        self.assertIsNotNone(
            laundry_logic.validate_pickup_date(sunday, "07:00", today=today)[1]
        )
        self.assertIsNotNone(
            laundry_logic.validate_pickup_date(weekday, "20:00", today=today)[1]
        )

    def test_same_day_pickup_slot_must_still_be_in_the_future(self):
        today = datetime.date.today()
        today_string = today.isoformat()
        accepted_slot = "13:00"
        rejected_slot = "11:00"
        self.assertEqual(
            laundry_logic.validate_pickup_date(
                today_string,
                accepted_slot,
                today=today,
                now=datetime.time(12, 0),
            ),
            (today_string, None),
        )
        self.assertIsNotNone(
            laundry_logic.validate_pickup_date(
                today_string,
                rejected_slot,
                today=today,
                now=datetime.time(12, 0),
            )[1]
        )


class JsonBackupTests(unittest.TestCase):
    def test_load_save_and_create_timestamped_previous_backup(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "orders.json"
            orders = [{"id": 1, "customer_name": "Test"}]
            self.assertEqual(laundry_logic.load_json_backup(path), [])
            self.assertTrue(laundry_logic.save_json_backup(orders, path))
            self.assertEqual(laundry_logic.load_json_backup(path), orders)
            self.assertTrue(laundry_logic.save_json_backup([{"id": 2}], path))
            self.assertEqual(laundry_logic.load_json_backup(path), [{"id": 2}])
            backups = list((Path(directory) / "backups").glob("orders_*.json"))
            self.assertEqual(len(backups), 1)
            with backups[0].open(encoding="utf-8") as backup:
                self.assertEqual(json.load(backup), orders)

    def test_retains_only_twenty_timestamped_backups(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "orders.json"
            for order_id in range(25):
                self.assertTrue(
                    laundry_logic.save_json_backup([{"id": order_id}], path)
                )
            backups = list((Path(directory) / "backups").glob("orders_*.json"))
            self.assertEqual(len(backups), laundry_logic.BACKUP_RETENTION_COUNT)

    def test_corrupt_and_invalid_utf8_files_return_empty_lists(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "orders.json"
            for data in (b"{not valid json", b"\xff\xfe"):
                path.write_bytes(data)
                with self.subTest(data=data):
                    self.assertEqual(laundry_logic.load_json_backup(path), [])


if __name__ == "__main__":
    unittest.main()
