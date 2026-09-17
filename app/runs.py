"""演练生命周期：创建根演练、在检查点分叉重放、时钟控制。"""
import uuid
from typing import Any

from . import config, db, execution
from .hub import hub


def _new_id() -> str:
    return uuid.uuid4().hex[:12]


def _snapshot_from_scenario(scenario: dict) -> dict:
    """场景 → 运行快照（演练创建后场景编辑不影响它，保证可回放）。"""
    return {
        "name": scenario["name"],
        "body": scenario["body"],
        "secret": scenario["secret"],
        "verify_signature": scenario["verify_signature"],
        "url_path": scenario["url_path"],
        "backoff": scenario["backoff"],
        "rules": scenario["rules"],
        "default_action": scenario["default_action"],
        "default_status_code": scenario["default_status_code"],
        "default_delay_ms": scenario["default_delay_ms"],
    }


def _validate_rules(rules: list[dict]) -> None:
    seen = set()
    for r in rules:
        if r["attempt"] in seen:
            raise ValueError(f"重复的尝试次数规则: {r['attempt']}")
        seen.add(r["attempt"])
        if r["action"] == "status" and r.get("status_code") is None:
            raise ValueError(f"第 {r['attempt']} 次规则为 status 但缺少 status_code")


def validate_scenario(data: dict) -> None:
    _validate_rules(data.get("rules", []))


def _initial_rule(snapshot: dict) -> dict:
    return execution.resolve_rule(snapshot, 1)


def create_run(scenario_id: str, speed: float | None = None) -> dict:
    conn = db.get_conn()
    try:
        scenario = db.get_scenario(conn, scenario_id)
        if not scenario:
            raise KeyError("scenario not found")
        _validate_rules(scenario["rules"])
        snapshot = _snapshot_from_scenario(scenario)

        run_id = _new_id()
        vt_start = db.now_ms()
        use_speed = config.DEFAULT_SPEED if (speed is None or speed == 0) else speed
        rule = _initial_rule(snapshot)
        idem = execution.make_idem_key(run_id, [], 1, snapshot["body"])

        conn.execute("BEGIN")
        db.insert_run(
            conn, run_id=run_id, root_run_id=run_id, parent_run_id=None, lineage=[],
            gen=0, scenario_id=scenario_id, name=snapshot["name"], label=None,
            snapshot=snapshot, status="running", vt_now=vt_start,
            anchor_ms=db.now_ms(), speed=float(use_speed), next_attempt=1,
            next_planned_ms=vt_start,
        )
        db.insert_attempt(
            conn, run_id=run_id, attempt=1, idem_key=idem,
            action=rule["action"], planned_status=rule["status_code"],
            planned_delay=rule["delay_ms"], scheduled_ms=vt_start)
        created_event = db.add_event_dict(conn, run_id, "run_created",
                     {"speed": float(use_speed), "rule": rule})
        conn.commit()

        hub.broadcast(created_event)
        row = db.get_run_row(conn, run_id)
        return db.serialize_run(conn, row)
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def fork_run(parent_run_id: str, payload: dict) -> dict:
    """在指定检查点（某一次尝试）分叉：继承此前的历史，用新规则重放之后的投递。"""
    conn = db.get_conn()
    try:
        parent = db.get_run_row(conn, parent_run_id)
        if not parent:
            raise KeyError("run not found")

        checkpoint = int(payload["checkpoint_attempt"])
        prior_attempts = db.list_attempts(conn, parent_run_id)
        prior_events = db.list_events(conn, parent_run_id)
        if not any(a["attempt"] == checkpoint for a in prior_attempts):
            raise ValueError(f"检查点 {checkpoint} 不存在")
        checkpoint_attempt = next(a for a in prior_attempts if a["attempt"] == checkpoint)
        if checkpoint_attempt["status"] != "done":
            raise ValueError("只能在已完成的检查点分叉")

        snapshot = db._loads(parent["snapshot"], {})
        # 应用规则覆盖
        if payload.get("rules") is not None:
            new_rules = [r for r in payload["rules"]]
            _validate_rules(new_rules)
            snapshot["rules"] = new_rules
        if payload.get("default_action") is not None:
            snapshot["default_action"] = payload["default_action"]
        if payload.get("default_status_code") is not None:
            snapshot["default_status_code"] = payload["default_status_code"]
        if payload.get("default_delay_ms") is not None:
            snapshot["default_delay_ms"] = payload["default_delay_ms"]

        child_id = _new_id()
        parent_lineage = db._loads(parent["lineage"], [])
        child_lineage = parent_lineage + [checkpoint]
        child_gen = int(parent["gen"]) + 1
        # 子分支时间线沿用父分支的虚拟时间轴
        vt_at_checkpoint = int(checkpoint_attempt["scheduled_ms"])
        max_attempts = int(snapshot["backoff"]["max_attempts"])

        # 检查点就是父演练的终局尝试：成功则子分支继承成功，失败且到顶则继承耗尽
        parent_outcome = db._loads(parent["outcome"], None)
        terminal_succeeded = (
            parent_outcome and parent_outcome.get("result") == "succeeded"
            and parent_outcome.get("attempts") == checkpoint)
        terminal_exhausted = (
            parent_outcome and parent_outcome.get("result") == "exhausted"
            and parent_outcome.get("attempts") == checkpoint)
        child_terminal = terminal_succeeded or terminal_exhausted or checkpoint >= max_attempts

        child_status = "succeeded" if terminal_succeeded else (
            "exhausted" if (terminal_exhausted or checkpoint >= max_attempts) else "paused")

        conn.execute("BEGIN")
        db.insert_run(
            conn, run_id=child_id, root_run_id=parent["root_run_id"],
            parent_run_id=parent_run_id, lineage=child_lineage, gen=child_gen,
            scenario_id=parent["scenario_id"], name=parent["name"],
            label=payload.get("label"), snapshot=snapshot, status=child_status,
            vt_now=vt_at_checkpoint, anchor_ms=None,
            speed=float(parent["speed"]),
            next_attempt=(checkpoint if child_terminal else checkpoint + 1),
            next_planned_ms=(None if child_terminal else vt_at_checkpoint),
            outcome=(parent_outcome if child_terminal else None),
        )

        # 继承检查点之前（含检查点）的尝试与事件：幂等键不变，标记 inherited
        for a in prior_attempts:
            if a["attempt"] > checkpoint:
                break
            db.insert_attempt(
                conn, run_id=child_id, attempt=a["attempt"], idem_key=a["idem_key"],
                action=a["action"], planned_status=a["planned_status"],
                planned_delay=a["planned_delay"], scheduled_ms=a["scheduled_ms"],
                inherited=True, status="done", result=a["result"],
                response_status=a["response_status"], sig_valid=a["sig_valid"],
                replay=a["replay"], side_effect_id=a["side_effect_id"],
                error=a["error"], started_ms=a["started_ms"],
                finished_ms=a["finished_ms"],
                real_duration_ms=a["real_duration_ms"],
            )
        for e in prior_events:
            if e["attempt"] is not None and e["attempt"] > checkpoint:
                continue
            db.add_event(conn, child_id, e["kind"], db._loads(e["data"], {}),
                         at_ms=e["at_ms"], attempt=e["attempt"], inherited=True)

        if child_terminal:
            # 继承结局，不再产生新投递
            forked_event = db.add_event_dict(
                conn, child_id, "forked",
                {"parent_run_id": parent_run_id, "checkpoint": checkpoint,
                 "label": payload.get("label"), "inherited_terminal": True})
            db.add_event_dict(conn, child_id, "run_finished",
                               parent_outcome or {"result": "exhausted",
                                                   "attempts": checkpoint},
                               at_ms=vt_at_checkpoint)
        else:
            # 下一次重放的尝试排在检查点虚拟时刻之后（resume 后按退避到期；
            # 首个重放尝试排在检查点时刻，恢复即到期）
            next_no = checkpoint + 1
            rule = execution.resolve_rule(snapshot, next_no)
            idem = execution.make_idem_key(
                parent["root_run_id"], child_lineage, next_no, snapshot["body"])
            db.insert_attempt(
                conn, run_id=child_id, attempt=next_no, idem_key=idem,
                action=rule["action"], planned_status=rule["status_code"],
                planned_delay=rule["delay_ms"], scheduled_ms=vt_at_checkpoint)
            forked_event = db.add_event_dict(
                conn, child_id, "forked",
                {"parent_run_id": parent_run_id, "checkpoint": checkpoint,
                 "label": payload.get("label"), "override": {
                     "rules": snapshot["rules"],
                     "default_action": snapshot["default_action"],
                     "default_status_code": snapshot["default_status_code"],
                     "default_delay_ms": snapshot["default_delay_ms"],
             }})
        conn.commit()

        hub.broadcast(forked_event)
        row = db.get_run_row(conn, child_id)
        return db.serialize_run(conn, row)
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def control_run(run_id: str, payload: dict) -> dict:
    conn = db.get_conn()
    try:
        row = db.get_run_row(conn, run_id)
        if not row:
            raise KeyError("run not found")
        status = row["status"]
        vt = row["vt_now"]
        real = db.now_ms()
        action = payload["action"]

        event = None
        conn.execute("BEGIN")
        if action == "pause":
            if status != "running":
                raise ValueError("只有运行中的演练可以暂停")
            cur_vt = _clock_vt(row, real)
            db.save_clock(conn, run_id, vt_now=cur_vt, anchor_ms=None, status="paused")
            event = db.add_event_dict(conn, run_id, "paused", {"vt_now": cur_vt})
        elif action == "resume":
            if status != "paused":
                raise ValueError("只有暂停的演练可以恢复")
            db.save_clock(conn, run_id, vt_now=vt, anchor_ms=real, status="running")
            event = db.add_event_dict(conn, run_id, "resumed", {"vt_now": vt})
        elif action == "speed":
            speed = payload.get("speed")
            if speed is None:
                raise ValueError("speed 操作需要 speed 字段")
            cur_vt = _clock_vt(row, real)
            anchor = None if status == "paused" else real
            db.save_clock(conn, run_id, vt_now=cur_vt, anchor_ms=anchor,
                          speed=float(speed), status=status)
            event = db.add_event_dict(conn, run_id, "speed_changed",
                         {"speed": float(speed), "vt_now": cur_vt})
        elif action == "advance":
            if status != "paused":
                raise ValueError("单步推进仅在暂停时可用")
            delta = int(payload.get("advance_ms") or 0)
            if delta < 0:
                raise ValueError("advance_ms 必须 >= 0")
            new_vt = vt + delta
            db.save_clock(conn, run_id, vt_now=new_vt, anchor_ms=None, status="paused")
            event = db.add_event_dict(conn, run_id, "advanced",
                                        {"by_ms": delta, "vt_now": new_vt})
        conn.commit()
        if event is not None:
            hub.broadcast(event)

        row = db.get_run_row(conn, run_id)
        return db.serialize_run(conn, row)
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def _clock_vt(row, real_ms: int) -> int:
    from .scheduler import clock_vt
    return clock_vt(row, real_ms)
