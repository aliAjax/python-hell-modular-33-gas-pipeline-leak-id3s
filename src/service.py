from . import domain, rules
from .domain import DomainError


class Service:
    def __init__(self, repository):
        self.repository = repository

    def _require_identity(self, actor, role):
        if not actor or not role:
            raise DomainError("identity_required", "需要用户身份和角色", 401)

    def _check_region(self, item, region, role):
        if not rules.ENFORCE_REGION or role == "regulator":
            return
        item_region = item["payload"].get("region")
        if item_region and item_region != region:
            raise DomainError("region_mismatch", "不能处理其他辖区的记录", 403)

    def create_item(self, payload, actor, role, region=None):
        self._require_identity(actor, role)
        if role not in rules.CREATE_ROLES:
            raise DomainError("forbidden", "当前角色不能创建此类业务记录", 403)
        normalized = domain.normalize_create(payload)
        stable_key = normalized.pop("_stable_key")
        source_type = normalized.pop("_source_type")
        external_id = normalized.pop("_external_id")
        if rules.ENFORCE_REGION and role != "regulator":
            wanted = normalized.get("region")
            if wanted and wanted != region:
                raise DomainError("region_mismatch", "不能在其他辖区创建事件", 403)
            if not wanted and region:
                normalized["region"] = region
        source = {
            "source_type": source_type,
            "external_id": external_id,
            "observed_at": normalized["reported_at"],
            "pressure_drop_kpa": normalized["pressure_drop_kpa"],
            "sensor_value_ppm": normalized["sensor_value_ppm"],
            "odor_reports": normalized["odor_reports"],
            "segment_id": None,
            "pipeline_id": None,
            "note": "initial report",
        }
        result = self.repository.create_or_merge(
            rules.ENTITY_TYPE,
            stable_key,
            rules.INITIAL_STATUS,
            normalized,
            source,
            actor,
            role,
            rules.MERGE_WINDOW_SECONDS,
            rules.apply_feedback,
        )
        item = self.get_item(result["item"]["id"])
        item["created_new"] = result["created_new"]
        item["merged"] = not result["created_new"]
        item["duplicate"] = (not result["created_new"]) and (not result["source_created"])
        return item

    def add_source(self, item_id, payload, actor, role, region=None):
        self._require_identity(actor, role)
        if role not in rules.SOURCE_ROLES:
            raise DomainError("forbidden", "当前角色不能提交来源记录", 403)
        item = self.repository.get_item(item_id)
        self._check_region(item, region, role)
        normalized = domain.normalize_source(payload)
        result = self.repository.apply_feedback(item_id, normalized, actor, role, rules.apply_feedback)
        return {
            "id": result["source_id"],
            "item_id": item_id,
            "source_type": normalized["source_type"],
            "external_id": normalized["external_id"],
            "observed_at": normalized["observed_at"],
            "created": result["source_created"],
            "invalidated": result["invalidated"],
            "status": result["item"]["status"],
        }

    def act(self, item_id, action, payload, actor, role, expected_version=None, region=None):
        self._require_identity(actor, role)
        item = self.repository.get_item(item_id)
        allowed = rules.ACTION_ROLES.get(action, set())
        if role not in allowed:
            raise DomainError("forbidden", "当前角色不能执行该操作", 403)
        if action in rules.REGION_SENSITIVE_ACTIONS:
            self._check_region(item, region, role)
        if action in rules.ACTION_REQUIRES_VERSION and expected_version is None:
            raise DomainError("expected_version_required", "该操作需要 expected_version", 400)
        new_status, new_payload, event_payload = rules.apply_action(item, action, payload, actor, role)
        self.repository.apply_action(
            item_id, action, actor, role, new_status, new_payload, event_payload, expected_version
        )
        return self.get_item(item_id)

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
        return self.repository.verify_audit(item_id)
