"""管网演算核心：不可并联水源连通性校验 + 按优先级的最大流分配。

- 校验：若某对不可并联水源经"开启且容量>0"的单向管段可达同一节点，视为并联冲突。
- 预演：以超级源点汇总各水源上限，在同一残量网络上按
  优先级从高到低、同级按用水点编号从小到大依次求最大流，
  共享管段容量与水源上限随分配逐步消耗，不会重复占用。
- 欠供定位：在残量网络最小割上找出"位于该用水点上游"的饱和
  水源边与管段边，分别给出 SOURCE_LIMIT / PIPE_CAPACITY 原因。
"""
from __future__ import annotations

import collections
import sys

sys.setrecursionlimit(100000)

EPS = 1e-9
SUPER_SOURCE = "__super_source__"


class _Edge:
    __slots__ = ("to", "cap", "orig", "rev", "meta")

    def __init__(self, to, cap, rev, meta=None):
        self.to = to
        self.cap = cap          # 残量容量
        self.orig = cap         # 原始容量
        self.rev = rev          # 反向边在 adj[to] 中的下标
        self.meta = meta        # ("source", id) 或 ("pipe", id)


class Dinic:
    def __init__(self):
        self.adj = collections.defaultdict(list)

    def add_edge(self, u, v, cap, meta=None):
        fwd = _Edge(v, cap, len(self.adj[v]), meta)
        rev = _Edge(u, 0.0, len(self.adj[u]), None)
        self.adj[u].append(fwd)
        self.adj[v].append(rev)

    def _bfs(self, s, t):
        self.level = {s: 0}
        q = collections.deque([s])
        while q:
            u = q.popleft()
            for e in self.adj[u]:
                if e.cap > EPS and e.to not in self.level:
                    self.level[e.to] = self.level[u] + 1
                    q.append(e.to)
        return t in self.level

    def _dfs(self, u, t, f):
        if u == t:
            return f
        edges = self.adj[u]
        while self.it[u] < len(edges):
            i = self.it[u]
            e = edges[i]
            if e.cap > EPS and self.level.get(e.to) == self.level[u] + 1:
                d = self._dfs(e.to, t, min(f, e.cap))
                if d > EPS:
                    e.cap -= d
                    self.adj[e.to][e.rev].cap += d
                    return d
            self.it[u] += 1
        return 0.0

    def max_flow(self, s, t, limit=float("inf")):
        flow = 0.0
        while flow < limit - EPS and self._bfs(s, t):
            self.it = collections.defaultdict(int)
            while flow < limit - EPS:
                f = self._dfs(s, t, limit - flow)
                if f <= EPS:
                    break
                flow += f
        return flow

    def residual_reachable(self, s):
        adj = collections.defaultdict(list)
        for u, edges in self.adj.items():
            for e in edges:
                if e.cap > EPS:
                    adj[u].append(e.to)
        return _bfs_set(adj, [s])


def _bfs_set(adj, starts):
    seen = set(starts)
    q = collections.deque(seen)
    while q:
        u = q.popleft()
        for v in adj.get(u, ()):
            if v not in seen:
                seen.add(v)
                q.append(v)
    return seen


def find_parallel_violations(sources, pairs, open_pipes):
    """检查不可并联水源对是否经开启管段连通（存在共同可达节点）。

    sources/pairs: 形如 dict 的行（含 id/node、source_a/source_b）。
    open_pipes: (from_node, to_node) 可迭代，仅含开启且容量>0 的管段。
    返回冲突列表：[{"source_a", "source_b", "shared_nodes"}]。
    """
    adj = collections.defaultdict(list)
    for u, v in open_pipes:
        adj[u].append(v)
    reach = {s["id"]: _bfs_set(adj, [s["node"]]) for s in sources}
    violations = []
    for p in pairs:
        a, b = p["source_a"], p["source_b"]
        ra, rb = reach.get(a), reach.get(b)
        if ra is None or rb is None:
            continue
        shared = sorted(ra & rb)
        if shared:
            violations.append({"source_a": a, "source_b": b, "shared_nodes": shared})
    return violations


def compute_allocation(sources, pipes, demands, closed_valves):
    """按优先级分配各用水点可获得的最大流量（纯演算，不写库）。

    pipes 需含 valve_id / valve_open；closed_valves 为本次预演拟关闭的阀门集合。
    返回按分配顺序排列的结果列表。
    """
    closed_valves = set(closed_valves)
    dinic = Dinic()
    for s in sources:
        if s["max_supply"] > EPS:
            dinic.add_edge(SUPER_SOURCE, s["node"], float(s["max_supply"]),
                           meta=("source", s["id"]))

    open_adj = collections.defaultdict(list)
    rev_adj = collections.defaultdict(list)
    for p in pipes:
        if p["valve_open"] and p["valve_id"] not in closed_valves and p["capacity"] > EPS:
            dinic.add_edge(p["from_node"], p["to_node"], float(p["capacity"]),
                           meta=("pipe", p["id"]))
            open_adj[p["from_node"]].append(p["to_node"])
            rev_adj[p["to_node"]].append(p["from_node"])

    # 任一水源经开启管段可达的节点集合（用于判定"无路可供"）
    reachable_any = _bfs_set(open_adj, [s["node"] for s in sources])

    results = []
    ordered = sorted(demands, key=lambda d: (-d["priority"], d["id"]))
    for d in ordered:
        want = float(d["demand"])
        got = dinic.max_flow(SUPER_SOURCE, d["node"], limit=want) if want > EPS else 0.0
        got = round(got, 6)
        shortage = round(max(0.0, want - got), 6)

        reasons, limited_pipes = [], []
        if shortage > EPS:
            if d["node"] not in reachable_any:
                reasons.append({
                    "code": "NO_PATH",
                    "message": "阀门关闭或管网断开，无可用供水路径",
                })
            else:
                # 最小割：残量网络中源点侧可达集 -> 不可达集 的饱和边，
                # 仅保留位于该用水点上游（可经开启管段到达用水点）的边。
                can_reach_t = _bfs_set(rev_adj, [d["node"]])
                reach = dinic.residual_reachable(SUPER_SOURCE)
                cut_pipes, cut_sources = set(), set()
                for u in reach:
                    for e in dinic.adj[u]:
                        if (e.meta and e.orig > EPS
                                and e.to not in reach and e.to in can_reach_t):
                            kind, ident = e.meta
                            (cut_pipes if kind == "pipe" else cut_sources).add(ident)
                if cut_sources:
                    reasons.append({
                        "code": "SOURCE_LIMIT",
                        "message": "水源供水上限受限",
                        "source_ids": sorted(cut_sources),
                    })
                if cut_pipes:
                    reasons.append({
                        "code": "PIPE_CAPACITY",
                        "message": "管段容量受限",
                        "pipe_ids": sorted(cut_pipes),
                    })
                limited_pipes = sorted(cut_pipes)
                if not reasons:
                    reasons.append({
                        "code": "SOURCE_LIMIT",
                        "message": "可用供水总量不足",
                        "source_ids": [],
                    })

        results.append({
            "demand_id": d["id"],
            "name": d.get("name", ""),
            "node": d["node"],
            "priority": d["priority"],
            "demand": want,
            "allocated": got,
            "shortage": shortage,
            "reasons": reasons,
            "limited_pipes": limited_pipes,
        })
    return results
