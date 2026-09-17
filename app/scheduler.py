"""调度器：可暂停的虚拟时钟 + 到期投递 + 退避 + 崩溃恢复。

状态全部持久化在 SQLite。调度循环只负责：

1. 推进每个非终结演练的虚拟时间（真实时间 * speed，可暂停 / 变速 / 单步）；
2. 当虚拟时间到达下次尝试的计划时刻，先把尝试持久化为 ``running`` 再发起
   真实 HTTP 投递（崩溃恢复语义）；
3. 依据结果与退避策略排定下一次尝试，或把演练标记为 succeeded / exhausted。
"""
import asyncio
from typing import Any

from . import config, db, execution
from .hub import hub


def clock_vt(row, real_ms: int) -> int:
    """根据持久化的锚点计算当前虚拟时间。"""
    speed = float(row["speed"])
    vt_now = row["vt_now"]
    anchor_ms = row["anchor_ms"]
    if row["status"] != "running" or anchor_ms is None:
        return vt_now
    return vt_now + int((real_ms - anchor_ms) * speed)


def _serialize_row(conn, row) -> dict:
    return db.serialize_run(conn, row, with_timeline=False)


class Scheduler:
    def __init__(self) -> None:
        self._task: asyncio.Task | None = None
        self._stop = asyncio.Event()
        self._in_progress: set[str] = set()
        # 强引用：create_task 的任务可能被 GC 提前回收，必须自己持有
        self._bg_tasks: set[asyncio.Task] = set()
        self._last_clock_push: int = 0

    # ------------------------------------------------------------ 生命周期

    async def start(self) -> None:
        self._recover()
        self._stop.clear()
        self._task = asyncio.create_task(self._loop(), name="scheduler")

    async def stop(self) -> None:
        self._stop.set()
        if self._task:
            await asyncio.wait_for(self._task, timeout=5)

    # ------------------------------------------------------------ 启动恢复

    def _recover(self) -> None:
        """容器重启 / 刷新后：重新锚定仍活跃的演练。

        被标记为 running 的尝试说明上次进程可能在投递中途死亡，保持其
        pending 语义即可——它仍是非 done 的下一个到期尝试，会被重新投递，
        接收端按幂等键去重，不产生重复副作用。
        """
        conn = db.get_conn()
        try:
            rows = conn.execute("SELECT * FROM runs").fetchall()
            for row in rows:
                if row["status"] == "running":
                    conn.execute(
                        "UPDATE runs SET anchor_ms=?, updated_at=? WHERE id=?",
                        (db.now_ms(), db.now_ms(), row["id"]),
                    )
                elif row["status"] == "paused" and row["anchor_ms"] is not None:
                    conn.execute(
                        "UPDATE runs SET anchor_ms=NULL, updated_at=? WHERE id=?",
                        (db.now_ms(), row["id"]),
                    )
            conn.commit()
        finally:
            conn.close()

    # ------------------------------------------------------------ 主循环

    async def _loop(self) -> None:
        tick_s = config.TICK_MS / 1000.0
        while not self._stop.is_set():
            try:
                await self._scan()
            except Exception as exc:  # noqa: BLE001
                # 单轮异常不杀死调度循环
                print(f"[scheduler] scan error: {exc!r}")
            try:
                self._maybe_push_clock()
            except Exception as exc:  # noqa: BLE001
                print(f"[scheduler] clock push error: {exc!r}")
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=tick_s)
            except asyncio.TimeoutError:
                pass

    async def _scan(self) -> None:
        conn = db.get_conn()
        try:
            rows = conn.execute(
                "SELECT * FROM runs WHERE status IN ('running','paused') ORDER BY created_at"
            ).fetchall()
        finally:
            conn.close()

        for row in rows:
            if row["id"] in self._in_progress:
                continue
            real_ms = db.now_ms()
            vt = clock_vt(row, real_ms)
            # 每轮把虚拟时间推进落盘（轻量写）
            if row["status"] == "running" and vt != row["vt_now"]:
                c2 = db.get_conn()
                try:
                    c2.execute("UPDATE runs SET vt_now=?, updated_at=? WHERE id=?",
                               (vt, real_ms, row["id"]))
                    c2.commit()
                finally:
                    c2.close()

            pending = None
            c3 = db.get_conn()
            try:
                pending = db.get_pending_attempt(c3, row["id"])
            finally:
                c3.close()
            if not pending:
                continue

            # 暂停时绝不投递：单步推进越过计划时刻后，也要等恢复才触发
            if row["status"] != "running":
                continue
            due = pending["scheduled_ms"] <= vt
            instant = float(row["speed"]) == 0
            if not due and not instant:
                continue

            self._in_progress.add(row["id"])
            task = asyncio.create_task(self._process(row["id"]))
            self._bg_tasks.add(task)
            task.add_done_callback(self._bg_tasks.discard)

    def _maybe_push_clock(self) -> None:
        real_ms = db.now_ms()
        if real_ms - self._last_clock_push < config.CLOCK_PUSH_MS:
            return
        self._last_clock_push = real_ms
        conn = db.get_conn()
        try:
            rows = conn.execute(
                "SELECT * FROM runs WHERE status IN ('running','paused')"
            ).fetchall()
            clocks = []
            for row in rows:
                clocks.append({
                    "run_id": row["id"],
                    "vt_now": clock_vt(row, real_ms),
                    "speed": row["speed"],
                    "status": row["status"],
                    "next_planned_ms": row["next_planned_ms"],
                    "next_attempt": row["next_attempt"],
                })
        finally:
            conn.close()
        # 纯瞬态事件，不落库
        hub.broadcast({
            "id": None, "run_id": "_clock", "seq": 0, "kind": "clock",
            "data": {"clocks": clocks}, "at_ms": real_ms,
            "attempt": None, "inherited": False,
        })

    # ------------------------------------------------------------ 单次投递处理

    async def _process(self, run_id: str) -> None:
        pending_events: list[dict] = []
        try:
            # 1) 到期瞬间把虚拟时钟对齐到计划时刻并冻结，再持久化 running、发请求。
            #    这样真实 HTTP 投递的墙钟耗时（超时可能数秒）不会被倍率放大到
            #    虚拟时间里；崩溃恢复时该尝试仍是 pending，会被重新投递。
            conn = db.get_conn()
            attempt_row = None
            run_dict = None
            try:
                run_row = db.get_run_row(conn, run_id)
                if not run_row or run_row["status"] not in ("running", "paused"):
                    return
                pending = db.get_pending_attempt(conn, run_id)
                if not pending:
                    return
                attempt_row = pending
                vt_due = int(pending["scheduled_ms"])
                db.save_clock(conn, run_id, vt_now=vt_due, anchor_ms=None)
                db.mark_attempt_running(conn, attempt_row["id"], db.now_ms())
                pending_events.append(db.add_event_dict(
                    conn, run_id, "attempt_started",
                    {"attempt": attempt_row["attempt"],
                     "idempotency_key": attempt_row["idem_key"],
                     "vt_ms": vt_due},
                    attempt=attempt_row["attempt"], at_ms=vt_due))
                conn.commit()
                run_dict = db.serialize_run(conn, db.get_run_row(conn, run_id),
                                            with_timeline=False)
            finally:
                conn.close()
            for e in pending_events:
                hub.broadcast(e)
            pending_events.clear()

            # 2) 真实 HTTP 投递（可能耗时数秒，期间虚拟时钟保持冻结）
            result = await execution.execute_attempt(run_dict, attempt_row)

            # 3) 记录结果并排定后续 / 终结
            conn = db.get_conn()
            try:
                pending_events = self._settle(conn, run_id, attempt_row, result)
                conn.commit()
            except Exception:
                conn.rollback()
                raise
            finally:
                conn.close()
            for e in pending_events:
                hub.broadcast(e)
        except Exception as exc:  # noqa: BLE001
            print(f"[scheduler] process {run_id} error: {exc!r}")
        finally:
            self._in_progress.discard(run_id)

    def _settle(self, conn, run_id: str, attempt_row, result: dict) -> list[dict]:
        """在同一事务内完成尝试、排定后续/终结，返回待广播事件。"""
        events: list[dict] = []
        run_row = db.get_run_row(conn, run_id)
        if not run_row:
            return events
        snapshot = db._loads(run_row["snapshot"], {})
        attempt_no = attempt_row["attempt"]
        vt_due = int(run_row["vt_now"])  # 投递期间冻结在到期时刻
        finished_ms = db.now_ms()

        db.complete_attempt(
            conn, attempt_row["id"],
            result=result["result"],
            response_status=result["status"],
            sig_valid=result["sig_valid"],
            replay=result["replay"],
            side_effect_id=result["side_effect_id"],
            error=result["error"],
            finished_ms=finished_ms,
            real_duration_ms=result["real_duration_ms"],
        )

        attempt_dict = db.serialize_attempt(
            conn.execute("SELECT * FROM attempts WHERE id=?", (attempt_row["id"],)).fetchone()
        )
        events.append(db.add_event_dict(
            conn, run_id, "attempt_finished",
            {"attempt": attempt_no, **attempt_dict, "vt_ms": vt_due},
            attempt=attempt_no, at_ms=vt_due))

        max_attempts = int(snapshot["backoff"]["max_attempts"])
        succeeded = result["result"] == "success"

        if succeeded:
            outcome = {"result": "succeeded", "attempts": attempt_no,
                       "last_status": result["status"],
                       "replay": result["replay"],
                       "side_effect_id": result["side_effect_id"]}
            db.save_clock(conn, run_id, vt_now=vt_due, anchor_ms=None,
                          status="succeeded", next_planned_ms=None,
                          outcome=outcome)
            events.append(db.add_event_dict(
                conn, run_id, "run_finished", outcome, at_ms=vt_due))
            return events

        if attempt_no >= max_attempts:
            outcome = {"result": "exhausted", "attempts": attempt_no,
                       "last_result": result["result"],
                       "last_status": result["status"],
                       "error": result["error"]}
            db.save_clock(conn, run_id, vt_now=vt_due, anchor_ms=None,
                          status="exhausted", next_planned_ms=None,
                          outcome=outcome)
            events.append(db.add_event_dict(
                conn, run_id, "run_finished", outcome, at_ms=vt_due))
            return events

        delay = execution.backoff_delay_ms(snapshot, attempt_no)
        scheduled = vt_due + delay
        next_attempt_no = attempt_no + 1
        rule = execution.resolve_rule(snapshot, next_attempt_no)
        idem = execution.make_idem_key(
            run_row["root_run_id"], db._loads(run_row["lineage"], []),
            next_attempt_no, snapshot["body"])
        db.insert_attempt(
            conn, run_id=run_id, attempt=next_attempt_no, idem_key=idem,
            action=rule["action"], planned_status=rule["status_code"],
            planned_delay=rule["delay_ms"], scheduled_ms=scheduled)
        # 若投递期间用户暂停了演练，保持暂停（不重新锚定），恢复后才到期
        still_running = run_row["status"] == "running"
        db.save_clock(
            conn, run_id, vt_now=vt_due,
            anchor_ms=(finished_ms if still_running else None),
            next_attempt=next_attempt_no, next_planned_ms=scheduled)
        events.append(db.add_event_dict(
            conn, run_id, "attempt_scheduled",
            {"attempt": next_attempt_no, "scheduled_ms": scheduled,
             "delay_ms": delay, "rule": rule, "vt_ms": scheduled},
            attempt=next_attempt_no, at_ms=scheduled))
        return events


# 单例
scheduler = Scheduler()
