"""审计查询接口：把指标口径与看板发布的状态写入、引用切换、拦截记录统一暴露。"""
from __future__ import annotations

from fastapi import APIRouter, Query

from app.schemas import PageResult
from app.store import store

router = APIRouter(prefix="/api/audit", tags=["审计记录"])


@router.get("", response_model=PageResult[dict])
def list_audits(
    module: str | None = Query(default=None, description="metric / dashboard"),
    entry_id: int | None = Query(default=None, description="业务记录 id"),
    page: int = 1,
    size: int = 50,
) -> PageResult[dict]:
    """按模块与记录 id 过滤审计流水，默认按写入时间倒序返回。"""
    if size > 200:
        size = 200
    items = store.audits(module=module, entry_id=entry_id)
    items = sorted(items, key=lambda row: int(row.get("id", 0)), reverse=True)
    total = len(items)
    start = max(page - 1, 0) * size
    return PageResult(items=items[start:start + size], total=total, page=page, size=size)
