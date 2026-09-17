"""端到端冒烟测试：通过真实 HTTP 驱动整个系统。"""
import json
import time
import urllib.request

BASE = "http://127.0.0.1:8000"


def req(method, path, body=None):
    data = json.dumps(body).encode() if body is not None else None
    r = urllib.request.Request(BASE + path, data=data, method=method,
                               headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(r) as resp:
        return json.loads(resp.read())


def wait_for(run_id, terminal=("succeeded", "exhausted"), timeout=40, pause_vt=None):
    deadline = time.time() + timeout
    while time.time() < deadline:
        run = req("GET", f"/api/runs/{run_id}")
        if run["status"] in terminal:
            return run
        time.sleep(0.2)
    raise AssertionError(f"run {run_id} did not finish; status={run['status']}")


def control(run_id, action, **kw):
    return req("POST", f"/api/runs/{run_id}/control", {"action": action, **kw})


results = []
def check(name, cond, extra=""):
    results.append((name, cond, extra))
    print(("PASS" if cond else "FAIL"), name, extra)


# 1) 场景：#1 timeout, #2 disconnect, #3 status 500, 之后默认成功
scenario_body = {
    "name": "E2E 故障矩阵",
    "body": {"event": "e2e", "n": 1},
    "secret": "topsecret",
    "verify_signature": True,
    "url_path": "/hook",
    "backoff": {"base_ms": 200, "factor": 2, "max_ms": 5000, "max_attempts": 6, "jitter": False},
    "rules": [
        {"attempt": 1, "action": "timeout"},
        {"attempt": 2, "action": "disconnect"},
        {"attempt": 3, "action": "status", "status_code": 500},
        {"attempt": 4, "action": "status", "status_code": 429},
    ],
    "default_action": "success",
    "default_status_code": 200,
    "default_delay_ms": 0,
}
s = req("POST", "/api/scenarios", scenario_body)
sid = s["id"]

# 2) 以 1000x 快速跑完（timeout 真实耗时约 5s 由 httpx 超时决定）
run = req("POST", f"/api/scenarios/{sid}/runs", {"speed": 1000})
rid = run["id"]
check("初始状态 running", run["status"] == "running")
check("首次尝试已排定", run["next_attempt"] == 1)

run = wait_for(rid)
check("最终成功（第5次）", run["outcome"]["result"] == "succeeded",
      f"attempts={run['outcome']['attempts']}")
check("成功发生在第 5 次", run["outcome"]["attempts"] == 5)

attempts = run["attempts"]
check("尝试1=timeout", attempts[0]["result"] == "timeout", attempts[0]["result"])
check("尝试2=disconnect", attempts[1]["result"] == "disconnect", attempts[1]["result"])
check("尝试3=failure 500", attempts[2]["result"] == "failure" and attempts[2]["response_status"] == 500)
check("尝试4=failure 429", attempts[3]["result"] == "failure" and attempts[3]["response_status"] == 429)
check("尝试5=success 200", attempts[4]["result"] == "success" and attempts[4]["response_status"] == 200)
check("成功尝试有副作用id", attempts[4]["side_effect_id"] is not None)
check("成功尝试非重放", attempts[4]["replay"] is False)
check("sig 有效", attempts[4]["sig_valid"] is True)

# 3) 退避时间单调（200,400,800,1600 虚拟 ms）
scheds = [a["scheduled_ms"] for a in attempts]
gaps = [scheds[i + 1] - scheds[i] for i in range(3)]
check("退避间隔=200/400/800", gaps == [200, 400, 800], str(gaps))

# 4) 幂等键稳定
from app.signing import idempotency_key
expected_key_1 = idempotency_key(rid, [], 1, scenario_body["body"])
check("幂等键确定性", attempts[0]["idempotency_key"] == expected_key_1)

# 5) 已确认投递只有 1 条
family = req("GET", f"/api/families/{rid}")
check("家族副作用=1", family["side_effects"] == 1, str(family["side_effects"]))
check("家族已确认投递=1", family["deliveries"] == 1)

# 6) 手动重放同一幂等键 -> 接收端返回 replay，不产生新副作用
import httpx
key5 = attempts[4]["idempotency_key"]
resp = httpx.post(
    BASE + "/receiver/hook",
    content=json.dumps(scenario_body["body"], separators=(",", ":"), sort_keys=True),
    headers={
        "Idempotency-Key": key5,
        "X-Webhook-Timestamp": str(attempts[4]["scheduled_ms"]),
        "X-Signature": __import__("app.signing", fromlist=["sign"]).sign(
            "topsecret", attempts[4]["scheduled_ms"], scenario_body["body"]),
    }, timeout=10)
check("重放返回200", resp.status_code == 200)
check("重放标记 X-Replay=1", resp.headers.get("X-Replay") == "1")
family = req("GET", f"/api/families/{rid}")
check("重放后副作用仍=1", family["side_effects"] == 1)

# 7) 错误签名 -> 401
bad = httpx.post(BASE + "/receiver/hook", content=b"{}",
                 headers={"Idempotency-Key": key5, "X-Webhook-Timestamp": "1",
                          "X-Signature": "sha256=deadbeef"})
check("坏签名 401", bad.status_code == 401)

# 8) 分叉：从检查点 #2 分叉，把规则改成 #3 success
child = req("POST", f"/api/runs/{rid}/fork", {
    "checkpoint_attempt": 2,
    "label": "#3 起改为成功",
    "rules": [
        {"attempt": 1, "action": "timeout"},
        {"attempt": 2, "action": "disconnect"},
        {"attempt": 3, "action": "success"},
    ],
    "default_action": "success",
})
cid = child["id"]
check("子分支初始 paused", child["status"] == "paused")
check("继承前2次尝试", len(child["attempts"]) == 3 and
      all(a["inherited"] for a in child["attempts"][:2]))
check("继承尝试幂等键一致",
      child["attempts"][0]["idempotency_key"] == attempts[0]["idempotency_key"])
check("新尝试幂等键不同",
      child["attempts"][2]["idempotency_key"] != attempts[2]["idempotency_key"])
check("谱系", child["lineage"] == [2], str(child["lineage"]))
forked_events = [e for e in child["events"] if e["kind"] == "forked"]
check("有 forked 事件", len(forked_events) == 1)

# 恢复 -> 应在 #3 成功
control(cid, "resume")
child_done = wait_for(cid)
check("分叉第3次成功", child_done["outcome"]["result"] == "succeeded"
      and child_done["outcome"]["attempts"] == 3,
      str(child_done["outcome"]))
check("成功的是新尝试（非继承）",
      child_done["attempts"][2]["result"] == "success"
      and not child_done["attempts"][2]["inherited"])

# 家族副作用变为 2（继承的两次失败无副作用，#3 是全新成功投递）
family = req("GET", f"/api/families/{rid}")
check("分叉后家族副作用=2", family["side_effects"] == 2, str(family["side_effects"]))

# 9) 分叉继承成功尝试的情形：从检查点 #5（成功）分叉，继承结局，立即成功，不重放
child2 = req("POST", f"/api/runs/{rid}/fork", {
    "checkpoint_attempt": 5, "label": "终点分叉"})
check("终点分叉继承全部5次", len(child2["attempts"]) == 5)
check("终点分叉直接继承成功", child2["status"] == "succeeded"
      and child2["outcome"]["result"] == "succeeded")
check("继承的成功尝试带原副作用", child2["attempts"][4]["side_effect_id"]
      == attempts[4]["side_effect_id"])
family = req("GET", f"/api/families/{rid}")
check("终点分叉不新增副作用", family["side_effects"] == 2)
check("终点分叉无待处理尝试",
      all(a["status"] == "done" for a in child2["attempts"]))

# 10) 暂停 / 单步推进 / 变速：极慢倍率下，首次失败后的退避窗口很长，暂停必然生效
s2 = req("POST", "/api/scenarios", {
    "name": "暂停测试", "body": {"x": 1}, "secret": "k", "verify_signature": True,
    "url_path": "/hook",
    "backoff": {"base_ms": 100000, "factor": 2, "max_ms": 400000, "max_attempts": 3},
    "rules": [{"attempt": 1, "action": "status", "status_code": 500}],
    "default_action": "success", "default_status_code": 200, "default_delay_ms": 0})
r2 = req("POST", f"/api/scenarios/{s2['id']}/runs", {"speed": 0.0001})
time.sleep(1.0)
control(r2["id"], "pause")
paused = req("GET", f"/api/runs/{r2['id']}")
vt_paused = paused["vt_now"]
time.sleep(0.8)
still = req("GET", f"/api/runs/{r2['id']}")
check("暂停后虚拟时钟冻结", still["vt_now"] == vt_paused and still["status"] == "paused")
control(r2["id"], "advance", advance_ms=200_000)
adv = req("GET", f"/api/runs/{r2['id']}")
check("单步推进 +200000", adv["vt_now"] == vt_paused + 200_000)
# 不恢复则不会触发
time.sleep(0.5)
frozen = req("GET", f"/api/runs/{r2['id']}")
check("未恢复仍暂停", frozen["status"] == "paused")
control(r2["id"], "resume")
done2 = wait_for(r2["id"])
check("恢复后完成", done2["status"] == "succeeded", done2["outcome"])

# 11) 事件持久化数量
check("根演练事件数 >= 10", len(run["events"]) >= 10, str(len(run["events"])))
kinds = {e["kind"] for e in run["events"]}
check("事件种类齐全",
      {"run_created", "attempt_started", "attempt_finished",
       "attempt_scheduled", "run_finished"} <= kinds, str(kinds))

print()
failed = [r for r in results if not r[1]]
print(f"TOTAL {len(results)}  PASS {len(results) - len(failed)}  FAIL {len(failed)}")
if failed:
    raise SystemExit(1)
