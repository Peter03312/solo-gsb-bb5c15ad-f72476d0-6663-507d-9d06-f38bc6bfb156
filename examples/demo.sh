#!/usr/bin/env bash
# 供水管网隔离预演服务 —— 端到端调用示例
# 用法: ./examples/demo.sh [BASE_URL]   默认 http://127.0.0.1:8000
set -euo pipefail

BASE="${1:-http://127.0.0.1:8000}"
pp() { python3 -m json.tool --no-ensure-ascii; }
step() { echo; echo "===== $* ====="; }

step "0. 清空并重录（演示用）"
curl -s -X POST "$BASE/admin/reset" | pp

step "1. 录入水源（供水上限）"
curl -s -X POST "$BASE/sources" -H 'Content-Type: application/json' \
  -d '{"name":"S1","node":"SRC1","max_supply":100}' | pp
curl -s -X POST "$BASE/sources" -H 'Content-Type: application/json' \
  -d '{"name":"S2","node":"SRC2","max_supply":60}' | pp

step "2. 录入不可并联水源对"
curl -s -X POST "$BASE/incompatible-pairs" -H 'Content-Type: application/json' \
  -d '{"source_a":1,"source_b":2}' | pp

step "3. 录入单向管段（容量 + 阀门，响应中返回 valve.id）"
curl -s -X POST "$BASE/pipes" -H 'Content-Type: application/json' \
  -d '{"from_node":"SRC1","to_node":"J1","capacity":80}' | pp
curl -s -X POST "$BASE/pipes" -H 'Content-Type: application/json' \
  -d '{"from_node":"SRC2","to_node":"J2","capacity":60}' | pp
curl -s -X POST "$BASE/pipes" -H 'Content-Type: application/json' \
  -d '{"from_node":"J1","to_node":"J2","capacity":30,"valve_open":false}' | pp
curl -s -X POST "$BASE/pipes" -H 'Content-Type: application/json' \
  -d '{"from_node":"J1","to_node":"N1","capacity":40}' | pp
curl -s -X POST "$BASE/pipes" -H 'Content-Type: application/json' \
  -d '{"from_node":"J2","to_node":"N2","capacity":60}' | pp

step "4. 录入用水点（需求 + 优先级）"
curl -s -X POST "$BASE/demands" -H 'Content-Type: application/json' \
  -d '{"node":"N1","demand":50,"priority":10,"name":"医院"}' | pp
curl -s -X POST "$BASE/demands" -H 'Content-Type: application/json' \
  -d '{"node":"N2","demand":70,"priority":5,"name":"居民楼"}' | pp
curl -s -X POST "$BASE/demands" -H 'Content-Type: application/json' \
  -d '{"node":"N2","demand":20,"priority":10,"name":"消防"}' | pp

step "5. 基准预演（不关闭任何阀门，不写库）"
curl -s -X POST "$BASE/rehearsal" -H 'Content-Type: application/json' \
  -d '{"close_valves":[]}' | pp

step "6. 隔离预演：拟关闭阀门 2（S2 断供，不写库）"
curl -s -X POST "$BASE/rehearsal" -H 'Content-Type: application/json' \
  -d '{"close_valves":[2]}' | pp

step "7. 预演后阀门 2 仍为开启（预演不写库）"
curl -s "$BASE/valves" | pp

step "8. 阀门变更：开启阀门 3 -> 使 S1/S2 经开启管段连通，整批拒绝(409)"
curl -s -X POST "$BASE/valves/changes" -H 'Content-Type: application/json' \
  -d '{"changes":[{"valve_id":3,"open":true}]}' | pp

step "9. 阀门变更：合法批次（关闭阀门 1）原子生效"
curl -s -X POST "$BASE/valves/changes" -H 'Content-Type: application/json' \
  -d '{"changes":[{"valve_id":1,"open":false}]}' | pp
curl -s -X POST "$BASE/valves/changes" -H 'Content-Type: application/json' \
  -d '{"changes":[{"valve_id":1,"open":true}]}' > /dev/null   # 恢复

step "10. 错误定位示例"
echo "-- 未知端点 (404)";            curl -s "$BASE/no-such-endpoint" | pp
echo "-- 负容量 (422)";              curl -s -X POST "$BASE/pipes" -H 'Content-Type: application/json' \
  -d '{"from_node":"A","to_node":"B","capacity":-5}' | pp
echo "-- 非法阀门编号-变更 (404)";   curl -s -X POST "$BASE/valves/changes" -H 'Content-Type: application/json' \
  -d '{"changes":[{"valve_id":999,"open":true}]}' | pp
echo "-- 非法阀门编号-预演 (404)";   curl -s -X POST "$BASE/rehearsal" -H 'Content-Type: application/json' \
  -d '{"close_valves":[999]}' | pp
