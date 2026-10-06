import hashlib
from datetime import datetime


class DomainError(Exception):
    def __init__(self, code, message, status=400):
        super().__init__(message)
        self.code = code
        self.status = status


class ConflictError(DomainError):
    def __init__(self, code, message):
        super().__init__(code, message, 409)


class NotFoundError(DomainError):
    def __init__(self, code, message):
        super().__init__(code, message, 404)


def require_text(payload, name):
    value = payload.get(name)
    if not isinstance(value, str) or not value.strip():
        raise DomainError("field_required", "%s 不能为空" % name)
    return value.strip()


def optional_text(payload, name):
    value = payload.get(name)
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise DomainError("invalid_field", "%s 格式无效" % name)
    return value.strip()


def number(payload, name, minimum=None):
    value = payload.get(name)
    if isinstance(value, bool):
        raise DomainError("invalid_number", "%s 必须是数字" % name)
    try:
        value = float(value)
    except (TypeError, ValueError):
        raise DomainError("invalid_number", "%s 必须是数字" % name)
    if minimum is not None and value < minimum:
        raise DomainError("invalid_number", "%s 不能小于 %s" % (name, minimum))
    return value


def parse_timestamp(payload, name):
    value = require_text(payload, name)
    try:
        datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        raise DomainError("invalid_timestamp", "%s 必须是 ISO 时间" % name)
    return value


def normalize_create(payload):
    pipeline_id = require_text(payload, "pipeline_id")
    segment_id = require_text(payload, "segment_id")
    reported_at = parse_timestamp(payload, "reported_at")
    pressure_drop = number(payload, "pressure_drop_kpa", 0)
    sensor_ppm = number(payload, "sensor_value_ppm", 0)
    odor_reports = int(payload.get("odor_reports", 0) or 0)
    if odor_reports < 0:
        raise DomainError("invalid_odor_reports", "异味报告数不能为负数")
    reporter = require_text(payload, "reporter")
    source_type = optional_text(payload, "source_type") or "dispatch"
    external_id = optional_text(payload, "external_id")
    region = optional_text(payload, "region")
    stable_key = "%s|%s|%s" % (pipeline_id, segment_id, reported_at)
    if not external_id:
        # 未给来源编号时按内容生成稳定编号，写盘失败后原样重试不会重复入账。
        fingerprint = "%s|%s|%s|%s|%s|%s|%s" % (
            pipeline_id,
            segment_id,
            reported_at,
            pressure_drop,
            sensor_ppm,
            odor_reports,
            reporter,
        )
        external_id = "auto-" + hashlib.sha256(fingerprint.encode("utf-8")).hexdigest()[:16]
    return {
        "pipeline_id": pipeline_id,
        "segment_id": segment_id,
        "reported_at": reported_at,
        "pressure_drop_kpa": pressure_drop,
        "sensor_value_ppm": sensor_ppm,
        "odor_reports": odor_reports,
        "reporter": reporter,
        "region": region,
        "source_comparison": [],
        "valve_sequence": [],
        "closed_valves": [],
        "hazards_clear": False,
        "_stable_key": stable_key,
        "_source_type": source_type,
        "_external_id": external_id,
    }


def normalize_source(payload):
    source_type = require_text(payload, "source_type")
    external_id = require_text(payload, "external_id")
    observed_at = parse_timestamp(payload, "observed_at")
    odor = payload.get("odor_reports")
    if odor is None:
        odor_reports = None
    else:
        try:
            odor_reports = int(odor)
        except (TypeError, ValueError):
            raise DomainError("invalid_odor_reports", "异味报告数必须是整数")
        if odor_reports < 0:
            raise DomainError("invalid_odor_reports", "异味报告数不能为负数")
    return {
        "source_type": source_type,
        "external_id": external_id,
        "observed_at": observed_at,
        "sensor_value_ppm": number(payload, "sensor_value_ppm", 0) if "sensor_value_ppm" in payload else None,
        "pressure_drop_kpa": number(payload, "pressure_drop_kpa", 0) if "pressure_drop_kpa" in payload else None,
        "odor_reports": odor_reports,
        "segment_id": optional_text(payload, "segment_id"),
        "pipeline_id": optional_text(payload, "pipeline_id"),
        "note": payload.get("note", ""),
    }
