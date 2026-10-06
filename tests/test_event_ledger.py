import os
import sys
import tempfile
import threading
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from src.repository import Repository
from src.service import Service
from src.domain import ConflictError, DomainError


def make_payload(**overrides):
    payload = {
        "pipeline_id": "P-9",
        "segment_id": "S-1",
        "reported_at": "2026-10-06T08:00:00+00:00",
        "pressure_drop_kpa": 20,
        "sensor_value_ppm": 80,
        "odor_reports": 1,
        "reporter": "dispatch-1",
    }
    payload.update(overrides)
    return payload


class EventLedgerTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.tmp.close()
        self.repo = Repository(self.tmp.name)
        self.repo.initialize()
        self.service = Service(self.repo)

    def tearDown(self):
        os.unlink(self.tmp.name)

    def test_feedback_within_half_hour_merges_into_one_event(self):
        first = self.service.create_item(
            make_payload(source_type="phone", external_id="phone-1"), "d", "dispatcher"
        )
        second = self.service.create_item(
            make_payload(
                reported_at="2026-10-06T08:20:00+00:00",
                pressure_drop_kpa=35,
                source_type="sensor",
                external_id="sensor-7",
            ),
            "d",
            "dispatcher",
        )
        self.assertTrue(first["created_new"])
        self.assertFalse(second["created_new"])
        self.assertEqual(second["id"], first["id"])
        self.assertEqual(len(self.service.list_items()), 1)
        sources = second["sources"]
        self.assertEqual(len(sources), 2)
        self.assertEqual(
            {(s["source_type"], s["observed_at"]) for s in sources},
            {("phone", "2026-10-06T08:00:00+00:00"), ("sensor", "2026-10-06T08:20:00+00:00")},
        )
        # 晚到记录更新了有效压降
        self.assertEqual(second["payload"]["pressure_drop_kpa"], 35.0)

    def test_merge_window_boundary(self):
        first = self.service.create_item(make_payload(), "d", "dispatcher")
        edge = self.service.create_item(
            make_payload(reported_at="2026-10-06T08:30:00+00:00", external_id="e-2"), "d", "dispatcher"
        )
        # 恰好 30 分钟：并入同一事件
        self.assertEqual(edge["id"], first["id"])
        late = self.service.create_item(
            make_payload(reported_at="2026-10-06T09:01:00+00:00", external_id="e-3"), "d", "dispatcher"
        )
        # 距事件所有已知反馈都超过 30 分钟：另开新事件
        self.assertNotEqual(late["id"], first["id"])
        self.assertTrue(late["created_new"])

    def test_concurrent_create_merges_to_single_event(self):
        results = []
        errors = []

        def submit(index):
            try:
                results.append(
                    self.service.create_item(
                        make_payload(external_id="call-%d" % index, reporter="reporter-%d" % index),
                        "d",
                        "dispatcher",
                    )
                )
            except Exception as exc:  # pragma: no cover
                errors.append(exc)

        threads = [threading.Thread(target=submit, args=(i,)) for i in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(errors, [])
        self.assertEqual(len({item["id"] for item in results}), 1)
        self.assertEqual(len(self.service.list_items()), 1)
        self.assertEqual(len(self.service.get_item(results[0]["id"])["sources"]), 2)

    def test_late_correction_invalidates_and_keeps_valves_closed(self):
        item = self.service.create_item(make_payload(pressure_drop_kpa=30, sensor_value_ppm=120, odor_reports=3), "d", "dispatcher")
        item = self.service.act(item["id"], "verify", {"field_confirmed": True}, "r", "responder", item["version"])
        item = self.service.act(item["id"], "isolate", {"valve_sequence": ["V-1", "V-2"]}, "s", "supervisor", item["version"])
        self.assertEqual(item["payload"]["closed_valves"], ["V-1", "V-2"])

        result = self.service.add_source(
            item["id"],
            {
                "source_type": "patrol",
                "external_id": "patrol-7",
                "observed_at": "2026-10-06T08:25:00+00:00",
                "segment_id": "S-2",
                "pressure_drop_kpa": 55,
            },
            "patrol-1",
            "patrol",
        )
        self.assertTrue(result["created"])
        self.assertTrue(result["invalidated"])
        self.assertEqual(result["status"], "reported")

        item = self.service.get_item(item["id"])
        self.assertEqual(item["status"], "reported")
        self.assertEqual(item["payload"]["segment_id"], "S-2")
        self.assertEqual(item["payload"]["pressure_drop_kpa"], 55.0)
        self.assertEqual(item["payload"]["valve_sequence"], [])
        self.assertNotIn("verification", item["payload"])
        # 已关阀门保持关闭
        self.assertEqual(item["payload"]["closed_valves"], ["V-1", "V-2"])
        # 旧评分失效重算：55*2 + 120*0.1 + 3*5 = 137 -> 100
        self.assertEqual(item["payload"]["assessment"], {"score": 100.0, "level": "critical"})

        # 退回待核验后可以重新走流程，关闭阀门累计保留
        item = self.service.act(item["id"], "verify", {"field_confirmed": True}, "r", "responder", item["version"])
        item = self.service.act(item["id"], "isolate", {"valve_sequence": ["V-3", "V-4"]}, "s", "supervisor", item["version"])
        self.assertEqual(item["payload"]["closed_valves"], ["V-1", "V-2", "V-3", "V-4"])

    def test_duplicate_source_redelivery_is_idempotent(self):
        item = self.service.create_item(make_payload(), "d", "dispatcher")
        source = {
            "source_type": "sensor",
            "external_id": "sensor-1",
            "observed_at": "2026-10-06T08:05:00+00:00",
            "pressure_drop_kpa": 25,
        }
        first = self.service.add_source(item["id"], source, "sensor-1", "sensor")
        self.assertTrue(first["created"])
        retry = self.service.add_source(item["id"], source, "sensor-1", "sensor")
        self.assertFalse(retry["created"])
        self.assertEqual(retry["id"], first["id"])
        self.assertEqual(len(self.service.get_item(item["id"])["sources"]), 2)

    def test_concurrent_cancel_leaves_single_record(self):
        item = self.service.create_item(make_payload(), "d", "dispatcher")
        outcomes = []

        def cancel(actor):
            try:
                self.service.act(item["id"], "cancel", {"reason": "重复开单"}, actor, "supervisor", item["version"])
                outcomes.append("ok")
            except DomainError as exc:
                outcomes.append(exc.code)

        threads = [threading.Thread(target=cancel, args=("sup-%d" % i,)) for i in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(outcomes.count("ok"), 1)
        self.assertEqual(len(outcomes), 2)
        trail = self.service.get_item(item["id"])["audit"]
        self.assertEqual(len([e for e in trail if e["event_type"] == "cancel"]), 1)

    def test_region_enforcement(self):
        item = self.service.create_item(make_payload(region="north"), "d", "dispatcher", "north")
        with self.assertRaises(DomainError) as context:
            self.service.act(item["id"], "verify", {"field_confirmed": True}, "r", "responder", item["version"], "south")
        self.assertEqual(context.exception.status, 403)
        with self.assertRaises(DomainError):
            self.service.add_source(
                item["id"],
                {"source_type": "sensor", "external_id": "s-1", "observed_at": "2026-10-06T08:10:00+00:00"},
                "sensor-1",
                "sensor",
                "south",
            )
        with self.assertRaises(DomainError):
            self.service.create_item(make_payload(region="south", external_id="x-1"), "d", "dispatcher", "north")
        # 同辖区可以正常处理
        item = self.service.act(item["id"], "verify", {"field_confirmed": True}, "r", "responder", item["version"], "north")
        self.assertEqual(item["status"], "verified")

    def test_restart_preserves_event_valves_and_audit(self):
        item = self.service.create_item(make_payload(pressure_drop_kpa=30), "d", "dispatcher")
        item = self.service.act(item["id"], "verify", {"field_confirmed": True}, "r", "responder", item["version"])
        item = self.service.act(item["id"], "isolate", {"valve_sequence": ["V-1", "V-2"]}, "s", "supervisor", item["version"])
        self.service.add_source(
            item["id"],
            {
                "source_type": "patrol",
                "external_id": "patrol-9",
                "observed_at": "2026-10-06T08:15:00+00:00",
                "pressure_drop_kpa": 48,
            },
            "patrol-1",
            "patrol",
        )
        # 模拟服务重启：同一数据库文件重新建仓
        restarted = Service(Repository(self.tmp.name))
        restored = restarted.get_item(item["id"])
        self.assertEqual(restored["status"], "reported")
        self.assertEqual(restored["payload"]["closed_valves"], ["V-1", "V-2"])
        self.assertEqual(restored["payload"]["pressure_drop_kpa"], 48.0)
        self.assertEqual(len(restored["sources"]), 2)
        self.assertTrue(restarted.verify_audit(item["id"]))


if __name__ == "__main__":
    unittest.main()
