"""指标口径与看板联动的共享治理规则。

这里只放两端共用、且需要明确阈值的规则，避免口径散落在两个 service 里对不上：
- 保存前校验：必填、格式、允许范围、上限；
- 版本/卡片的引用解析：看板卡片钉在「指标 + 口径版本」上，历史引用不被最新草稿覆盖；
- 审计留痕：所有状态写入、引用切换、发布拦截都落 audit 表。
"""
from __future__ import annotations

import re
from datetime import datetime
from typing import Any

from app.store import store

METRIC_MODULE = "metric"
DASHBOARD_MODULE = "dashboard"

# 状态只能沿该序列单向推进，不允许跳级、不允许回退。
METRIC_STATUS_ORDER = ["草稿", "口径评审中", "已发布", "已下线"]
DASHBOARD_STATUS_ORDER = ["草稿", "看板评审中", "已发布", "已下线"]

# 版本自身的状态集合（版本粒度，不与指标定义的四态混用）。
VERSION_STATUSES = ["草稿", "口径评审中", "已发布", "已下线"]
PUBLISHED_VERSION_STATUS = "已发布"
OFFLINE_ENTRY_STATUS = "已下线"

# ---- 保存前校验：明确的允许范围与上限，缺依据或越界一律拒绝保存 ----
METRIC_CODE_PATTERN = re.compile(r"^METR-\d{4}$")
DASHBOARD_CODE_PATTERN = re.compile(r"^DASH-\d{4}$")
METRIC_NAME_MAX = 50
FORMULA_MAX = 300
FILTER_MAX = 200
ALLOWED_GRAINS = ["分钟", "小时", "天", "周", "月"]
PRECISION_MIN = 0
PRECISION_MAX = 10
REFRESH_INTERVAL_MIN = 30          # 秒
REFRESH_INTERVAL_MAX = 86_400      # 秒（一天）
MAX_CARDS_PER_DASHBOARD = 20
VISIBLE_SCOPES = ["全员可见", "业务线内可见", "仅负责人可见"]

METRIC_REQUIRED_FIELDS = ["指标编号", "指标名称", "计算公式", "统计粒度"]
DASHBOARD_REQUIRED_FIELDS = ["看板编号", "看板名称", "布局模板", "刷新间隔", "可见范围", "看板负责人"]


class ValidationError(ValueError):
    """带明确规则说明的保存/发布拒绝。"""


def now_iso() -> str:
    return datetime.now().replace(microsecond=0).isoformat()


def audit(
    *,
    module: str,
    entry_id: int,
    action: str,
    ok: bool,
    detail: str,
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    record = {
        "module": module,
        "entry_id": entry_id,
        "action": action,
        "result": "成功" if ok else "拒绝",
        "detail": detail,
        "operator": "system",
        "created_at": now_iso(),
    }
    if extra:
        record["extra"] = extra
    return store.add_audit(record)


def _require_text(values: dict[str, Any], field: str, errors: list[str]) -> str:
    value = str(values.get(field) or "").strip()
    if not value:
        errors.append(f"{field}为必填项，不允许为空")
    return value


def validate_metric_values(values: dict[str, Any]) -> dict[str, Any]:
    """保存指标定义/草稿前的硬校验，返回清洗后的字段；任一项越界即抛错，不允许部分保存。"""
    errors: list[str] = []
    cleaned: dict[str, Any] = {}

    code = _require_text(values, "指标编号", errors)
    if code and not METRIC_CODE_PATTERN.match(code):
        errors.append("指标编号必须匹配 METR-四位数字（如 METR-0002），当前格式越界")
    cleaned["指标编号"] = code

    name = _require_text(values, "指标名称", errors)
    if name and len(name) > METRIC_NAME_MAX:
        errors.append(f"指标名称最长 {METRIC_NAME_MAX} 字，当前 {len(name)} 字越界")
    cleaned["指标名称"] = name

    formula = _require_text(values, "计算公式", errors)
    if formula and len(formula) > FORMULA_MAX:
        errors.append(f"计算公式最长 {FORMULA_MAX} 字，当前 {len(formula)} 字越界")
    cleaned["计算公式"] = formula

    grain = _require_text(values, "统计粒度", errors)
    if grain and grain not in ALLOWED_GRAINS:
        errors.append(f"统计粒度仅允许 {'/'.join(ALLOWED_GRAINS)}，收到「{grain}」越界")
    cleaned["统计粒度"] = grain

    window = str(values.get("时间窗口") or "").strip()
    if window:
        try:
            datetime.strptime(window, "%Y-%m-%d")
        except ValueError:
            errors.append("时间窗口必须是 YYYY-MM-DD 日期，缺少合法依据不允许保存")
    cleaned["时间窗口"] = window

    condition = str(values.get("筛选条件") or "").strip()
    if len(condition) > FILTER_MAX:
        errors.append(f"筛选条件最长 {FILTER_MAX} 字，当前 {len(condition)} 字越界")
    cleaned["筛选条件"] = condition

    precision_raw = values.get("精度位数")
    precision: int | None = None
    if precision_raw not in (None, ""):
        try:
            precision = int(precision_raw)
        except (TypeError, ValueError):
            errors.append("精度位数必须是整数，缺少合法依据不允许保存")
        else:
            if not PRECISION_MIN <= precision <= PRECISION_MAX:
                errors.append(f"精度位数允许范围 {PRECISION_MIN}-{PRECISION_MAX}，当前 {precision} 越界")
    cleaned["精度位数"] = precision

    if errors:
        raise ValidationError("；".join(errors))
    return cleaned


def validate_dashboard_values(values: dict[str, Any]) -> dict[str, Any]:
    """保存看板配置前的硬校验。"""
    errors: list[str] = []
    cleaned: dict[str, Any] = {}

    code = _require_text(values, "看板编号", errors)
    if code and not DASHBOARD_CODE_PATTERN.match(code):
        errors.append("看板编号必须匹配 DASH-四位数字（如 DASH-0002），当前格式越界")
    cleaned["看板编号"] = code

    name = _require_text(values, "看板名称", errors)
    if name and len(name) > METRIC_NAME_MAX:
        errors.append(f"看板名称最长 {METRIC_NAME_MAX} 字，当前 {len(name)} 字越界")
    cleaned["看板名称"] = name

    layout = _require_text(values, "布局模板", errors)
    cleaned["布局模板"] = layout

    interval_raw = values.get("刷新间隔")
    interval: int | None = None
    try:
        interval = int(interval_raw)
    except (TypeError, ValueError):
        errors.append("刷新间隔必须是整数秒数，缺少合法依据不允许保存")
    else:
        if not REFRESH_INTERVAL_MIN <= interval <= REFRESH_INTERVAL_MAX:
            errors.append(
                f"刷新间隔允许范围 {REFRESH_INTERVAL_MIN}-{REFRESH_INTERVAL_MAX} 秒，当前 {interval} 越界"
            )
    cleaned["刷新间隔"] = interval

    scope = _require_text(values, "可见范围", errors)
    if scope and scope not in VISIBLE_SCOPES:
        errors.append(f"可见范围仅允许 {'/'.join(VISIBLE_SCOPES)}，收到「{scope}」越界")
    cleaned["可见范围"] = scope

    owner = _require_text(values, "看板负责人", errors)
    if owner and len(owner) > METRIC_NAME_MAX:
        errors.append(f"看板负责人最长 {METRIC_NAME_MAX} 字越界")
    cleaned["看板负责人"] = owner

    business_line = str(values.get("所属业务线") or "").strip()
    if len(business_line) > METRIC_NAME_MAX:
        errors.append(f"所属业务线最长 {METRIC_NAME_MAX} 字越界")
    cleaned["所属业务线"] = business_line

    if errors:
        raise ValidationError("；".join(errors))
    return cleaned


# ---- 指标版本解析：看板卡片永远读钉住版本的不可变快照 ----

def find_metric(metric_id: int) -> dict[str, Any] | None:
    return store.find(METRIC_MODULE, metric_id)


def find_version(metric: dict[str, Any], version: str) -> dict[str, Any] | None:
    for item in metric.get("versions", []):
        if item.get("version") == version:
            return item
    return None


def next_version_label(metric: dict[str, Any]) -> str:
    numbers = [
        int(str(item.get("version", "v0")).lstrip("v"))
        for item in metric.get("versions", [])
        if str(item.get("version", "")).lstrip("v").isdigit()
    ]
    return f"v{max(numbers, default=0) + 1}"


def resolve_card(card: dict[str, Any]) -> dict[str, Any]:
    """把一张卡片的持久化引用解析成运行态视图。

    视图是每次读取即时计算的，不写回仓库——因此刷新页面不会重复累积失效标记，
    也不会出现列表与详情口径不一致。
    """
    view: dict[str, Any] = dict(card)
    metric = find_metric(int(card["metric_id"]))
    if metric is None:
        view.update(
            valid=False,
            refresh=False,
            reason="指标定义不存在或已归档，来源失效，卡片停止刷新",
            metric_code=card.get("metric_code"),
            metric_status=None,
            version_status=None,
            grain_matched=False,
        )
        return view

    version = find_version(metric, str(card["metric_version"]))
    view["metric_code"] = metric.get("指标编号")
    view["metric_status"] = metric.get("status")
    if version is None:
        view.update(
            valid=False,
            refresh=False,
            reason=f"口径版本 {card.get('metric_version')} 不存在，来源失效，卡片停止刷新",
            version_status=None,
            grain_matched=False,
        )
        return view

    snapshot = version.get("snapshot", {})
    view["version_status"] = version.get("status")
    view["指标名称"] = snapshot.get("指标名称")
    view["计算公式"] = snapshot.get("计算公式")
    view["口径粒度"] = snapshot.get("统计粒度")
    grain_matched = snapshot.get("统计粒度") == card.get("card_grain")
    view["grain_matched"] = grain_matched

    if metric.get("status") == OFFLINE_ENTRY_STATUS:
        view.update(
            valid=False,
            refresh=False,
            reason=f"指标 {metric.get('指标编号')} 已下线，来源失效，卡片保留原配置并停止刷新",
        )
    elif version.get("status") != PUBLISHED_VERSION_STATUS:
        view.update(
            valid=False,
            refresh=False,
            reason=(
                f"{metric.get('指标编号')} 口径版本 {version.get('version')} 状态为「{version.get('status')}」，"
                "尚未发布，卡片停止刷新"
            ),
        )
    elif not grain_matched:
        view.update(
            valid=False,
            refresh=False,
            reason=(
                f"{metric.get('指标编号')} 口径版本 {version.get('version')} 的计算粒度为"
                f"「{snapshot.get('统计粒度')}」，与卡片计算粒度「{card.get('card_grain')}」"
                "不一致，卡片停止刷新"
            ),
        )
    else:
        view.update(valid=True, refresh=bool(card.get("refresh_enabled", True)), reason=None)
    return view


def cards_referencing_metric(metric_id: int) -> list[dict[str, Any]]:
    """反向追溯：哪些看板的哪张卡片引用了该指标（含版本与粒度）。"""
    refs: list[dict[str, Any]] = []
    for dashboard in store.rows(DASHBOARD_MODULE):
        for card in dashboard.get("cards", []):
            if int(card.get("metric_id", -1)) == metric_id:
                refs.append({
                    "dashboard_id": dashboard["id"],
                    "看板编号": dashboard.get("看板编号"),
                    "看板名称": dashboard.get("看板名称"),
                    "dashboard_status": dashboard.get("status"),
                    "card_id": card.get("card_id"),
                    "metric_version": card.get("metric_version"),
                    "card_grain": card.get("card_grain"),
                })
    return refs
