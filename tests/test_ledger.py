import os
import sys
import tempfile
import threading
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from src.repository import Repository
from src.service import Service
from src.domain import DomainError


def make_service():
    tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    tmp.close()
    repo = Repository(tmp.name)
    repo.initialize()
    return Service(repo), tmp.name


def feedback(segment, ts, pressure=20, ppm=50, odor=1, reporter="r-1"):
    return {
        "pipeline_id": "P-1",
        "segment_id": segment,
        "reported_at": ts,
        "pressure_drop_kpa": pressure,
        "sensor_value_ppm": ppm,
        "odor_reports": odor,
        "reporter": reporter,
    }


class MergeTest(unittest.TestCase):
    def setUp(self):
        self.service, self.path = make_service()

    def tearDown(self):
        os.unlink(self.path)

    def test_same_segment_within_window_merges(self):
        a = self.service.create_item(feedback("S-1", "2026-09-27T09:00:00+00:00"), "d", "dispatcher")
        b = self.service.create_item(feedback("S-1", "2026-09-27T09:05:00+00:00", reporter="r-2"), "d", "dispatcher")
        self.assertEqual(a["id"], b["id"])
        self.assertEqual(len(b["sources"]), 2)

    def test_outside_window_creates_new_event(self):
        a = self.service.create_item(feedback("S-1", "2026-09-27T09:00:00+00:00"), "d", "dispatcher")
        b = self.service.create_item(feedback("S-1", "2026-09-27T10:00:00+00:00", reporter="r-2"), "d", "dispatcher")
        self.assertNotEqual(a["id"], b["id"])

    def test_different_segment_creates_new_event(self):
        a = self.service.create_item(feedback("S-1", "2026-09-27T09:00:00+00:00"), "d", "dispatcher")
        b = self.service.create_item(feedback("S-2", "2026-09-27T09:05:00+00:00", reporter="r-2"), "d", "dispatcher")
        self.assertNotEqual(a["id"], b["id"])

    def test_source_and_time_kept(self):
        a = self.service.create_item(feedback("S-1", "2026-09-27T09:00:00+00:00"), "d", "dispatcher")
        b = self.service.create_item(feedback("S-1", "2026-09-27T09:05:00+00:00", reporter="r-2"), "d", "dispatcher")
        types = {s["source_type"] for s in b["sources"]}
        self.assertIn("feedback", types)
        observed = {s["observed_at"] for s in b["sources"]}
        self.assertIn("2026-09-27T09:00:00+00:00", observed)
        self.assertIn("2026-09-27T09:05:00+00:00", observed)


class LateCorrectionTest(unittest.TestCase):
    def setUp(self):
        self.service, self.path = make_service()

    def tearDown(self):
        os.unlink(self.path)

    def test_pressure_change_recomputes_and_reverts(self):
        item = self.service.create_item(feedback("S-2", "2026-09-27T09:00:00+00:00", pressure=20), "d", "dispatcher")
        item = self.service.act(item["id"], "verify", {"field_confirmed": True}, "resp", "responder", item["version"])
        self.assertEqual(item["status"], "verified")
        old_score = item["assessment"]["score"]
        item = self.service.add_source(item["id"], {
            "source_type": "sensor",
            "external_id": "SENS-1",
            "observed_at": "2026-09-27T09:10:00+00:00",
            "pressure_drop_kpa": 60,
            "sensor_value_ppm": 200,
        }, "d", "dispatcher")
        self.assertEqual(item["status"], "reported")
        self.assertEqual(item["payload"]["pressure_drop_kpa"], 60)
        self.assertNotEqual(item["assessment"]["score"], old_score)

    def test_segment_change_invalidates_unexecuted_plan(self):
        item = self.service.create_item(feedback("S-3", "2026-09-27T09:00:00+00:00"), "d", "dispatcher")
        item = self.service.act(item["id"], "verify", {"field_confirmed": True, "valve_sequence": ["V-1", "V-2"]}, "resp", "responder", item["version"])
        self.assertEqual(item["payload"].get("planned_valve_sequence"), ["V-1", "V-2"])
        item = self.service.add_source(item["id"], {
            "source_type": "patrol",
            "external_id": "PAT-1",
            "observed_at": "2026-09-27T09:08:00+00:00",
            "segment_id": "S-3B",
            "pressure_drop_kpa": 22,
        }, "d", "dispatcher")
        self.assertEqual(item["status"], "reported")
        self.assertEqual(item["payload"]["segment_id"], "S-3B")
        self.assertNotIn("planned_valve_sequence", item["payload"])

    def test_closed_valves_keep_closed_on_correction(self):
        item = self.service.create_item(feedback("S-4", "2026-09-27T09:00:00+00:00"), "d", "dispatcher")
        item = self.service.act(item["id"], "verify", {"field_confirmed": True}, "resp", "responder", item["version"])
        item = self.service.act(item["id"], "isolate", {"valve_sequence": ["V-10", "V-11"]}, "sup", "supervisor", item["version"])
        self.assertEqual(item["status"], "isolated")
        self.assertEqual(item["payload"]["valve_states"]["V-10"]["state"], "closed")
        item = self.service.add_source(item["id"], {
            "source_type": "phone",
            "external_id": "PHONE-1",
            "observed_at": "2026-09-27T09:12:00+00:00",
            "pressure_drop_kpa": 55,
        }, "d", "dispatcher")
        self.assertEqual(item["status"], "reported")
        self.assertEqual(item["payload"]["valve_sequence"], ["V-10", "V-11"])
        self.assertEqual(item["payload"]["valve_states"]["V-10"]["state"], "closed")
        self.assertEqual(item["payload"]["valve_states"]["V-11"]["state"], "closed")

    def test_source_update_recomputes_aggregate(self):
        item = self.service.create_item(feedback("S-5", "2026-09-27T09:00:00+00:00", pressure=20), "d", "dispatcher")
        self.service.add_source(item["id"], {
            "source_type": "sensor",
            "external_id": "SENS-9",
            "observed_at": "2026-09-27T09:05:00+00:00",
            "pressure_drop_kpa": 30,
        }, "d", "dispatcher")
        item = self.service.get_item(item["id"])
        self.assertEqual(item["payload"]["pressure_drop_kpa"], 30)
        # late correction to the same source number
        self.service.add_source(item["id"], {
            "source_type": "sensor",
            "external_id": "SENS-9",
            "observed_at": "2026-09-27T09:05:00+00:00",
            "pressure_drop_kpa": 45,
        }, "d", "dispatcher")
        item = self.service.get_item(item["id"])
        self.assertEqual(item["payload"]["pressure_drop_kpa"], 45)
        sens = [s for s in item["sources"] if s["external_id"] == "SENS-9"]
        self.assertEqual(len(sens), 1)


class RegionTest(unittest.TestCase):
    def setUp(self):
        self.service, self.path = make_service()

    def tearDown(self):
        os.unlink(self.path)

    def test_cross_region_action_rejected(self):
        item = self.service.create_item(feedback("S-6", "2026-09-27T09:00:00+00:00"), "d", "dispatcher", region="A")
        with self.assertRaises(DomainError) as ctx:
            self.service.act(item["id"], "verify", {"field_confirmed": True}, "resp", "responder", item["version"], region="B")
        self.assertEqual(ctx.exception.code, "region_mismatch")

    def test_same_region_action_allowed(self):
        item = self.service.create_item(feedback("S-6", "2026-09-27T09:00:00+00:00"), "d", "dispatcher", region="A")
        item = self.service.act(item["id"], "verify", {"field_confirmed": True}, "resp", "responder", item["version"], region="A")
        self.assertEqual(item["status"], "verified")

    def test_cross_region_source_rejected(self):
        item = self.service.create_item(feedback("S-6", "2026-09-27T09:00:00+00:00"), "d", "dispatcher", region="A")
        with self.assertRaises(DomainError) as ctx:
            self.service.add_source(item["id"], {
                "source_type": "patrol",
                "external_id": "PAT-9",
                "observed_at": "2026-09-27T09:20:00+00:00",
            }, "d", "dispatcher", region="B")
        self.assertEqual(ctx.exception.code, "region_mismatch")


class RetryTest(unittest.TestCase):
    def setUp(self):
        self.service, self.path = make_service()

    def tearDown(self):
        os.unlink(self.path)

    def test_duplicate_delivery_no_new_source(self):
        item = self.service.create_item(feedback("S-7", "2026-09-27T09:00:00+00:00"), "d", "dispatcher")
        src1 = self.service.add_source(item["id"], {
            "source_type": "sensor",
            "external_id": "SENS-100",
            "observed_at": "2026-09-27T09:05:00+00:00",
            "pressure_drop_kpa": 30,
        }, "d", "dispatcher")
        src2 = self.service.add_source(item["id"], {
            "source_type": "sensor",
            "external_id": "SENS-100",
            "observed_at": "2026-09-27T09:05:00+00:00",
            "pressure_drop_kpa": 30,
        }, "d", "dispatcher")
        self.assertEqual(src1["id"], src2["id"])
        item = self.service.get_item(item["id"])
        sens = [s for s in item["sources"] if s["external_id"] == "SENS-100"]
        self.assertEqual(len(sens), 1)


class ConcurrencyTest(unittest.TestCase):
    def setUp(self):
        self.service, self.path = make_service()

    def tearDown(self):
        os.unlink(self.path)

    def test_concurrent_cancel_only_one_record(self):
        item = self.service.create_item(feedback("S-8", "2026-09-27T09:00:00+00:00"), "d", "dispatcher")
        results = []
        errors = []

        def do_cancel(version):
            try:
                results.append(self.service.act(item["id"], "cancel", {"reason": "dup"}, "sup", "supervisor", version))
            except Exception as exc:
                errors.append(exc)

        t1 = threading.Thread(target=do_cancel, args=(item["version"],))
        t2 = threading.Thread(target=do_cancel, args=(item["version"],))
        t1.start()
        t2.start()
        t1.join()
        t2.join()
        self.assertEqual(len(errors), 0, errors)
        self.assertEqual(len(results), 2)
        cancelled = self.service.get_item(item["id"])
        self.assertEqual(cancelled["status"], "cancelled")
        cancel_audits = [a for a in cancelled["audit"] if a["event_type"] == "cancel"]
        self.assertEqual(len(cancel_audits), 1)


class RestartTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.tmp.close()
        self.repo = Repository(self.tmp.name)
        self.repo.initialize()
        self.service = Service(self.repo)

    def tearDown(self):
        os.unlink(self.tmp.name)

    def test_state_survives_restart(self):
        item = self.service.create_item(feedback("S-9", "2026-09-27T09:00:00+00:00"), "d", "dispatcher")
        item = self.service.act(item["id"], "verify", {"field_confirmed": True}, "resp", "responder", item["version"])
        item = self.service.act(item["id"], "isolate", {"valve_sequence": ["V-20", "V-21"]}, "sup", "supervisor", item["version"])
        item_id = item["id"]
        # simulate restart: new repository + service on the same db file
        repo2 = Repository(self.tmp.name)
        svc2 = Service(repo2)
        restored = svc2.get_item(item_id)
        self.assertEqual(restored["status"], "isolated")
        self.assertEqual(restored["payload"]["valve_sequence"], ["V-20", "V-21"])
        self.assertEqual(restored["payload"]["valve_states"]["V-20"]["state"], "closed")
        self.assertEqual(restored["payload"]["valve_states"]["V-21"]["state"], "closed")
        self.assertEqual(len(restored["sources"]), 1)
        ok, error = svc2.verify_audit(item_id)
        self.assertTrue(ok, error)

    def test_audit_chain_valid_after_many_ops(self):
        item = self.service.create_item(feedback("S-10", "2026-09-27T09:00:00+00:00"), "d", "dispatcher")
        item = self.service.act(item["id"], "verify", {"field_confirmed": True}, "resp", "responder", item["version"])
        item = self.service.act(item["id"], "isolate", {"valve_sequence": ["V-30", "V-31"]}, "sup", "supervisor", item["version"])
        item = self.service.act(item["id"], "repair", {"work_order": "WO-1"}, "tech", "technician", item["version"])
        ok, error = self.service.verify_audit(item["id"])
        self.assertTrue(ok, error)


class MergeEventsTest(unittest.TestCase):
    def setUp(self):
        self.service, self.path = make_service()

    def tearDown(self):
        os.unlink(self.path)

    def test_merge_events_idempotent(self):
        a = self.service.create_item(feedback("S-11", "2026-09-27T09:00:00+00:00", reporter="r-a"), "d", "dispatcher")
        b = self.service.create_item(feedback("S-11", "2026-09-27T10:00:00+00:00", reporter="r-b"), "d", "dispatcher")
        merged = self.service.merge_events(b["id"], a["id"], "d", "dispatcher")
        self.assertEqual(merged["id"], a["id"])
        self.assertEqual(len(merged["sources"]), 2)
        merged2 = self.service.merge_events(b["id"], a["id"], "d", "dispatcher")
        self.assertEqual(len(merged2["sources"]), 2)

    def test_feedback_after_merge_goes_to_target(self):
        a = self.service.create_item(feedback("S-12", "2026-09-27T09:00:00+00:00", reporter="r-a"), "d", "dispatcher")
        b = self.service.create_item(feedback("S-12", "2026-09-27T10:00:00+00:00", reporter="r-b"), "d", "dispatcher")
        self.service.merge_events(b["id"], a["id"], "d", "dispatcher")
        # new feedback within window of a (the target)
        c = self.service.create_item(feedback("S-12", "2026-09-27T09:15:00+00:00", reporter="r-c"), "d", "dispatcher")
        self.assertEqual(c["id"], a["id"], "feedback should merge into the target event, not the merged-away one")
        self.assertEqual(len(c["sources"]), 3)


if __name__ == "__main__":
    unittest.main()
