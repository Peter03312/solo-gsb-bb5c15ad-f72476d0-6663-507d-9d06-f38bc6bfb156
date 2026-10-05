"""供水管网隔离预演服务 —— FastAPI 入口。

功能：
- 录入水源（供水上限）、不可并联水源对、单向管段（容量+阀门）、用水点（需求+优先级）
- 阀门变更：整批校验、原子生效；导致不可并联水源经开启管段连通的批次整体拒绝
- 隔离预演：给定拟关闭阀门，不写库，按优先级分配各点最大流量并给出欠供原因与受限管段
"""
from __future__ import annotations

import collections
import sqlite3
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from .db import connect, init_db
from .network import compute_allocation, find_parallel_violations
from .schemas import (
    DemandCreate,
    IncompatiblePairCreate,
    PipeCreate,
    RehearsalRequest,
    SourceCreate,
    ValveChangeBatch,
)


class ApiError(Exception):
    """业务错误：携带 HTTP 状态码、错误码与定位信息。"""

    def __init__(self, status_code, code, message, location=None, details=None):
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.message = message
        self.location = location
        self.details = details


def _error_body(code, message, location=None, details=None):
    err = {"code": code, "message": message}
    if location:
        err["location"] = location
    if details is not None:
        err["details"] = details
    return {"error": err}


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()  # 启动时自动完成数据库初始化（幂等）
    yield


app = FastAPI(
    title="供水管网隔离预演服务",
    description="物业抢修场景：阀门变更整批校验 + 停水隔离预演（不写库）",
    version="1.0.0",
    lifespan=lifespan,
)


# ---------------- 错误处理：统一返回带定位的错误体 ----------------

@app.exception_handler(ApiError)
async def api_error_handler(request: Request, exc: ApiError):
    return JSONResponse(
        status_code=exc.status_code,
        content=_error_body(exc.code, exc.message, exc.location, exc.details),
    )


@app.exception_handler(StarletteHTTPException)
async def http_exception_handler(request: Request, exc: StarletteHTTPException):
    if exc.status_code == 404:
        return JSONResponse(status_code=404, content=_error_body(
            "ENDPOINT_NOT_FOUND",
            f"未知端点: {request.method} {request.url.path}",
            location="path",
        ))
    if exc.status_code == 405:
        return JSONResponse(status_code=405, content=_error_body(
            "METHOD_NOT_ALLOWED",
            f"方法不允许: {request.method} {request.url.path}",
            location="method",
        ))
    return JSONResponse(status_code=exc.status_code,
                        content=_error_body("HTTP_ERROR", str(exc.detail)))


@app.exception_handler(RequestValidationError)
async def validation_exception_handler(request: Request, exc: RequestValidationError):
    details = [
        {
            "location": ".".join(str(x) for x in e.get("loc", [])),
            "message": e.get("msg", ""),
            "type": e.get("type", ""),
        }
        for e in exc.errors()
    ]
    return JSONResponse(
        status_code=422,
        content=_error_body("VALIDATION_ERROR", "请求参数校验失败", details=details),
    )


# ---------------- 基础 ----------------

@app.get("/")
def root():
    return {
        "service": "供水管网隔离预演服务",
        "version": "1.0.0",
        "docs": "/docs",
        "endpoints": [
            "POST /sources", "GET /sources",
            "POST /incompatible-pairs", "GET /incompatible-pairs",
            "POST /pipes", "GET /pipes",
            "GET /valves", "POST /valves/changes",
            "POST /demands", "GET /demands",
            "POST /rehearsal",
        ],
    }


# ---------------- 水源 ----------------

@app.post("/sources", status_code=201)
def create_source(body: SourceCreate):
    with connect() as conn:
        try:
            cur = conn.execute(
                "INSERT INTO sources (name, node, max_supply) VALUES (?, ?, ?)",
                (body.name, body.node, body.max_supply),
            )
        except sqlite3.IntegrityError:
            raise ApiError(409, "SOURCE_EXISTS",
                           f"水源名称已存在: {body.name}", location="body.name")
        row = conn.execute("SELECT * FROM sources WHERE id = ?",
                           (cur.lastrowid,)).fetchone()
    return {"source": dict(row)}


@app.get("/sources")
def list_sources():
    with connect() as conn:
        rows = conn.execute("SELECT * FROM sources ORDER BY id").fetchall()
    return {"sources": [dict(r) for r in rows]}


# ---------------- 不可并联水源对 ----------------

@app.post("/incompatible-pairs", status_code=201)
def create_incompatible_pair(body: IncompatiblePairCreate):
    with connect() as conn:
        for sid, loc in ((body.source_a, "body.source_a"),
                         (body.source_b, "body.source_b")):
            if not conn.execute("SELECT 1 FROM sources WHERE id = ?",
                                (sid,)).fetchone():
                raise ApiError(404, "SOURCE_NOT_FOUND",
                               f"水源不存在: id={sid}",
                               location=loc, details={"source_id": sid})
        a, b = sorted((body.source_a, body.source_b))
        try:
            cur = conn.execute(
                "INSERT INTO incompatible_pairs (source_a, source_b) VALUES (?, ?)",
                (a, b),
            )
        except sqlite3.IntegrityError:
            raise ApiError(409, "PAIR_EXISTS",
                           f"不可并联水源对已存在: ({a}, {b})",
                           location="body",
                           details={"source_a": a, "source_b": b})
        row = conn.execute("SELECT * FROM incompatible_pairs WHERE id = ?",
                           (cur.lastrowid,)).fetchone()
    return {"pair": dict(row)}


@app.get("/incompatible-pairs")
def list_incompatible_pairs():
    with connect() as conn:
        rows = conn.execute(
            "SELECT * FROM incompatible_pairs ORDER BY id").fetchall()
    return {"pairs": [dict(r) for r in rows]}


# ---------------- 管段（自动附带一个阀门） ----------------

@app.post("/pipes", status_code=201)
def create_pipe(body: PipeCreate):
    with connect() as conn:
        cur = conn.execute(
            "INSERT INTO pipes (from_node, to_node, capacity) VALUES (?, ?, ?)",
            (body.from_node, body.to_node, body.capacity),
        )
        pipe_id = cur.lastrowid
        vcur = conn.execute(
            "INSERT INTO valves (pipe_id, open) VALUES (?, ?)",
            (pipe_id, 1 if body.valve_open else 0),
        )
        valve_id = vcur.lastrowid
    return {
        "pipe": {
            "id": pipe_id,
            "from_node": body.from_node,
            "to_node": body.to_node,
            "capacity": body.capacity,
        },
        "valve": {"id": valve_id, "pipe_id": pipe_id, "open": body.valve_open},
    }


@app.get("/pipes")
def list_pipes():
    with connect() as conn:
        rows = conn.execute(
            "SELECT p.id, p.from_node, p.to_node, p.capacity, "
            "       v.id AS valve_id, v.open AS valve_open "
            "FROM pipes p JOIN valves v ON v.pipe_id = p.id ORDER BY p.id"
        ).fetchall()
    return {"pipes": [
        {**dict(r), "valve_open": bool(r["valve_open"])} for r in rows
    ]}


# ---------------- 阀门 ----------------

@app.get("/valves")
def list_valves():
    with connect() as conn:
        rows = conn.execute(
            "SELECT v.id, v.pipe_id, v.open, p.from_node, p.to_node "
            "FROM valves v JOIN pipes p ON p.id = v.pipe_id ORDER BY v.id"
        ).fetchall()
    return {"valves": [
        {
            "valve_id": r["id"],
            "pipe_id": r["pipe_id"],
            "open": bool(r["open"]),
            "from_node": r["from_node"],
            "to_node": r["to_node"],
        }
        for r in rows
    ]}


def _open_pipes(conn, valve_state):
    """按给定阀门状态表，返回开启且容量>0 的管段 (from_node, to_node) 列表。"""
    rows = conn.execute(
        "SELECT p.from_node, p.to_node, p.capacity, v.id AS valve_id "
        "FROM pipes p JOIN valves v ON v.pipe_id = p.id"
    ).fetchall()
    return [
        (r["from_node"], r["to_node"])
        for r in rows
        if valve_state.get(r["valve_id"]) and r["capacity"] > 0
    ]


@app.post("/valves/changes")
def change_valves(batch: ValveChangeBatch):
    """整批校验、原子生效：任一变更导致不可并联水源经开启管段连通，则整批拒绝。"""
    ids = [c.valve_id for c in batch.changes]
    dupes = sorted(v for v, n in collections.Counter(ids).items() if n > 1)
    if dupes:
        raise ApiError(400, "DUPLICATE_VALVE_IN_BATCH",
                       "同一批次中阀门编号重复",
                       location="body.changes",
                       details={"duplicate_valve_ids": dupes})

    with connect() as conn:
        # 写事务先行加锁，保证并发批次之间串行校验、原子生效
        conn.execute("BEGIN IMMEDIATE")
        existing = {r["id"]: r["open"]
                    for r in conn.execute("SELECT id, open FROM valves")}
        unknown = sorted(i for i in ids if i not in existing)
        if unknown:
            raise ApiError(404, "VALVE_NOT_FOUND",
                           "阀门编号不存在，整批变更已拒绝",
                           location="body.changes",
                           details={"unknown_valve_ids": unknown,
                                    "applied": False})

        new_state = dict(existing)
        for c in batch.changes:
            new_state[c.valve_id] = 1 if c.open else 0

        sources = conn.execute("SELECT * FROM sources").fetchall()
        pairs = conn.execute("SELECT * FROM incompatible_pairs").fetchall()
        before = {(v["source_a"], v["source_b"]) for v in find_parallel_violations(
            sources, pairs, _open_pipes(conn, existing))}
        after = find_parallel_violations(
            sources, pairs, _open_pipes(conn, new_state))
        introduced = [v for v in after
                      if (v["source_a"], v["source_b"]) not in before]
        if introduced:
            raise ApiError(409, "PARALLEL_SOURCES_CONFLICT",
                           "变更将使不可并联的水源经开启管段连通，"
                           "整批变更已拒绝，阀门保持原状态",
                           location="body.changes",
                           details={"violations": introduced, "applied": False})

        for c in batch.changes:
            conn.execute("UPDATE valves SET open = ? WHERE id = ?",
                         (1 if c.open else 0, c.valve_id))
        states = conn.execute(
            "SELECT id, pipe_id, open FROM valves ORDER BY id").fetchall()

    return {
        "applied": True,
        "changes": [c.model_dump() for c in batch.changes],
        "valves": [
            {"valve_id": r["id"], "pipe_id": r["pipe_id"], "open": bool(r["open"])}
            for r in states
        ],
    }


# ---------------- 用水点 ----------------

@app.post("/demands", status_code=201)
def create_demand(body: DemandCreate):
    with connect() as conn:
        cur = conn.execute(
            "INSERT INTO demands (name, node, demand, priority) VALUES (?, ?, ?, ?)",
            (body.name, body.node, body.demand, body.priority),
        )
        row = conn.execute("SELECT * FROM demands WHERE id = ?",
                           (cur.lastrowid,)).fetchone()
    return {"demand_point": dict(row)}


@app.get("/demands")
def list_demands():
    with connect() as conn:
        rows = conn.execute("SELECT * FROM demands ORDER BY id").fetchall()
    return {"demands": [dict(r) for r in rows]}


# ---------------- 隔离预演（不写库） ----------------

@app.post("/rehearsal")
def rehearsal(body: RehearsalRequest):
    closed = sorted(set(body.close_valves))
    with connect() as conn:
        if closed:
            existing = {r["id"] for r in conn.execute("SELECT id FROM valves")}
            unknown = [v for v in closed if v not in existing]
            if unknown:
                raise ApiError(404, "VALVE_NOT_FOUND",
                               "阀门编号不存在",
                               location="body.close_valves",
                               details={"unknown_valve_ids": unknown})
        sources = [dict(r) for r in conn.execute("SELECT * FROM sources")]
        pipes = [dict(r) for r in conn.execute(
            "SELECT p.id, p.from_node, p.to_node, p.capacity, "
            "       v.id AS valve_id, v.open AS valve_open "
            "FROM pipes p JOIN valves v ON v.pipe_id = p.id")]
        demands = [dict(r) for r in conn.execute("SELECT * FROM demands")]

    results = compute_allocation(sources, pipes, demands, set(closed))
    return {
        "state_written": False,
        "closed_valves": closed,
        "allocation_order": "priority_desc,demand_id_asc",
        "results": results,
        "total_demand": round(sum(r["demand"] for r in results), 6),
        "total_allocated": round(sum(r["allocated"] for r in results), 6),
    }


# ---------------- 管理：清空重录（演示/测试用） ----------------

@app.post("/admin/reset")
def admin_reset():
    with connect() as conn:
        for table in ("demands", "valves", "pipes",
                      "incompatible_pairs", "sources"):
            conn.execute(f"DELETE FROM {table}")
            conn.execute("DELETE FROM sqlite_sequence WHERE name = ?", (table,))
    return {"reset": True}
