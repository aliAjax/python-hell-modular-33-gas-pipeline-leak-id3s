from . import domain, rules
from .domain import DomainError, ConflictError


class Service:
    def __init__(self, repository):
        self.repository = repository

    # ---------- 反馈接入与合并 ----------

    def create_item(self, payload, actor, role, region=None):
        if not actor or not role:
            raise DomainError("identity_required", "需要用户身份和角色", 401)
        if role not in rules.CREATE_ROLES:
            raise DomainError("forbidden", "当前角色不能创建此类业务记录", 403)
        normalized = domain.normalize_create(payload)
        stable_key = normalized.pop("_stable_key")
        if region:
            normalized["region"] = region
        # 完全相同的重复送达：stable_key 一致，拒绝重复建单
        existing = self.repository.find_by_stable_key(rules.ENTITY_TYPE, stable_key)
        if existing:
            raise ConflictError("duplicate_item", "同一业务实体已经存在")
        # 同一管段半小时内的有效反馈：合并进已有事件
        candidate = self.repository.find_merge_candidate(
            normalized["pipeline_id"], normalized["segment_id"], normalized["reported_at"]
        )
        if candidate:
            return self._add_feedback(
                candidate,
                "feedback",
                normalized["reporter"],
                self._source_payload_from_create(normalized),
                normalized["reported_at"],
                actor,
                role,
                region,
            )
        # 新事件：建单并把首条反馈作为来源
        item = self.repository.create_item(
            rules.ENTITY_TYPE, stable_key, rules.INITIAL_STATUS, normalized, actor, role
        )
        self.repository.add_source(
            item["id"],
            "feedback",
            normalized["reporter"],
            self._source_payload_from_create(normalized),
            normalized["reported_at"],
            actor,
            role,
        )
        return self.get_item(item["id"])

    @staticmethod
    def _source_payload_from_create(normalized):
        result = {
            "sensor_value_ppm": normalized["sensor_value_ppm"],
            "pressure_drop_kpa": normalized["pressure_drop_kpa"],
            "odor_reports": normalized["odor_reports"],
            "note": "",
        }
        if normalized.get("segment_id"):
            result["segment_id"] = normalized["segment_id"]
        return result

    def add_source(self, item_id, payload, actor, role, region=None):
        if not actor or not role:
            raise DomainError("identity_required", "需要用户身份和角色", 401)
        if role not in rules.SOURCE_ROLES:
            raise DomainError("forbidden", "当前角色不能提交来源记录", 403)
        item = self.repository.get_item(item_id)
        normalized = domain.normalize_source(payload)
        source_type = normalized.pop("source_type")
        external_id = normalized.pop("external_id")
        observed_at = normalized.pop("observed_at")
        return self._add_feedback(item, source_type, external_id, normalized, observed_at, actor, role, region)

    def _add_feedback(self, item, source_type, external_id, source_payload, observed_at, actor, role, region=None):
        # 辖区校验：越权处理其他辖区要拒绝
        if region and rules.ENFORCE_REGION and role != "regulator":
            item_region = item["payload"].get("region")
            if item_region and item_region != region:
                raise DomainError("region_mismatch", "不能处理其他区域的记录", 403)
        old_segment = item["payload"].get("segment_id")
        old_pressure = item["payload"].get("pressure_drop_kpa", 0)
        # 写入来源（幂等：重复送达不新增来源）
        source = self.repository.add_source(
            item["id"], source_type, external_id, source_payload, observed_at, actor, role
        )
        # 重算聚合
        item = self.get_item(item["id"])
        sources = self.repository.list_sources(item["id"])
        pressure = max((s["payload"].get("pressure_drop_kpa") or 0) for s in sources) if sources else 0
        ppm = max((s["payload"].get("sensor_value_ppm") or 0) for s in sources) if sources else 0
        odor = sum(int((s["payload"].get("odor_reports") or 0)) for s in sources)
        item["payload"]["pressure_drop_kpa"] = pressure
        item["payload"]["sensor_value_ppm"] = ppm
        item["payload"]["odor_reports"] = odor
        # 晚到记录改动管段或压降？
        changed_segment = bool(source_payload.get("segment_id")) and source_payload["segment_id"] != old_segment
        changed_pressure = (
            source_payload.get("pressure_drop_kpa") is not None
            and source_payload["pressure_drop_kpa"] != old_pressure
        )
        event_type = "source_recorded"
        if changed_segment or changed_pressure:
            if changed_segment:
                item["payload"]["segment_id"] = source_payload["segment_id"]
            new_status, new_payload = rules.apply_correction(item)
            item["status"] = new_status
            item["payload"] = new_payload
            event_type = "correction_recorded"
        event_payload = {
            "source_id": source["id"],
            "source_type": source_type,
            "external_id": external_id,
            "changed_segment": changed_segment,
            "changed_pressure": changed_pressure,
        }
        self.repository.update_payload(
            item["id"],
            item["payload"],
            item["version"],
            event_type,
            actor,
            role,
            event_payload,
            new_status=item["status"] if (changed_segment or changed_pressure) else None,
        )
        return self.get_item(item["id"])

    # ---------- 事件合并 ----------

    def merge_events(self, from_item_id, to_item_id, actor, role, region=None):
        if not actor or not role:
            raise DomainError("identity_required", "需要用户身份和角色", 401)
        if role not in rules.CREATE_ROLES:
            raise DomainError("forbidden", "当前角色不能合并事件", 403)
        from_item = self.repository.get_item(from_item_id)
        to_item = self.repository.get_item(to_item_id)
        if region and rules.ENFORCE_REGION and role != "regulator":
            for candidate in (from_item, to_item):
                item_region = candidate["payload"].get("region")
                if item_region and item_region != region:
                    raise DomainError("region_mismatch", "不能处理其他区域的记录", 403)
        created = self.repository.record_merge_operation(from_item_id, to_item_id, actor, role)
        if created:
            self.repository.move_sources(from_item_id, to_item_id)
            to_item = self.get_item(to_item_id)
            sources = self.repository.list_sources(to_item_id)
            pressure = max((s["payload"].get("pressure_drop_kpa") or 0) for s in sources) if sources else 0
            ppm = max((s["payload"].get("sensor_value_ppm") or 0) for s in sources) if sources else 0
            odor = sum(int((s["payload"].get("odor_reports") or 0)) for s in sources)
            to_item["payload"]["pressure_drop_kpa"] = pressure
            to_item["payload"]["sensor_value_ppm"] = ppm
            to_item["payload"]["odor_reports"] = odor
            to_item["payload"]["assessment"] = rules.assess(to_item["payload"])
            self.repository.update_payload(
                to_item_id, to_item["payload"], to_item["version"], "merged", actor, role, {"from": from_item_id}
            )
            from_item["payload"]["merged_into"] = to_item_id
            self.repository.update_payload(
                from_item_id,
                from_item["payload"],
                from_item["version"],
                "merged",
                actor,
                role,
                {"into": to_item_id},
                new_status="merged",
            )
        return self.get_item(to_item_id)

    # ---------- 操作（核验/隔离/抢修/试压/恢复/撤销） ----------

    def act(self, item_id, action, payload, actor, role, expected_version=None, region=None):
        if not actor or not role:
            raise DomainError("identity_required", "需要用户身份和角色", 401)
        item = self.repository.get_item(item_id)
        allowed = rules.ACTION_ROLES.get(action, set())
        if role not in allowed:
            raise DomainError("forbidden", "当前角色不能执行该操作", 403)
        if rules.ENFORCE_REGION and action in rules.REGION_SENSITIVE_ACTIONS and region and role != "regulator":
            item_region = item["payload"].get("region")
            if item_region and item_region != region:
                raise DomainError("region_mismatch", "不能处理其他区域的记录", 403)
        if action in rules.ACTION_REQUIRES_VERSION and expected_version is None:
            raise DomainError("expected_version_required", "该操作需要 expected_version", 400)
        if action == "cancel":
            self.repository.cancel_idempotent(item_id, actor, role, payload, expected_version)
            return self.get_item(item_id)
        new_status, new_payload, event_payload = rules.apply_action(item, action, payload, actor, role)
        self.repository.apply_action(
            item_id, action, actor, role, new_status, new_payload, event_payload, expected_version
        )
        return self.get_item(item_id)

    # ---------- 查询与对账 ----------

    def get_item(self, item_id):
        item = self.repository.get_item(item_id)
        item["sources"] = self.repository.list_sources(item_id)
        item["audit"] = self.repository.audit_trail(item_id)
        item["assessment"] = rules.assess(item["payload"])
        return item

    def list_items(self, status=None):
        return self.repository.list_items(status)

    def state(self):
        return self.repository.state_summary()

    def verify_audit(self, item_id):
        return self.repository.verify_audit_chain(item_id)
