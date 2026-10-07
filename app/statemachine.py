"""束流保护阈值规程裁决状态机。

核心约束：
* 裁决仅从序号 0 起按连续顺序进行；缺号时后续观测进入等待队列，水位不动；
  缺口补齐后在同一事务内依序排空。
* 规程按整数生效序号登记：裁决序号 i 时，固定采用 effective_from <= i
  中最大的那份规程。水位越过某序号后，影响该序号的规程登记将被拒绝，
  历史裁决永不重算。
* 投递标识稳定：同标识同内容重传回显原裁决；同标识改动序号/读数、或同
  序号出现不同投递，一律拒绝且不改动等待记录与既有裁决。
* 所有状态保存在 SQLite 中，入队与推进水位均为原子事务，重开服务后
  结果与不中断执行一致，每个序号仅有一份不可变裁决。
"""

from __future__ import annotations

import os
import sqlite3
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

DECISION_PASS = "PASS"
DECISION_INHIBIT = "INHIBIT"  # 读数越限，束流抑制

STATE_VERDICT = "VERDICT"
STATE_WAITING = "WAITING"


class StateMachineError(Exception):
    """状态机业务错误基类。"""


class NotFound(StateMachineError):
    """演练不存在。"""


class Conflict(StateMachineError):
    """请求与既有不可变状态冲突，必须明确拒绝。"""

    def __init__(self, reason: str, kind: str = "DELIVERY_CONFLICT",
                 delivery_id: str | None = None, seq: int | None = None):
        super().__init__(reason)
        self.reason = reason
        self.kind = kind
        self.delivery_id = delivery_id
        self.seq = seq


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def adjudicate(reading: float, threshold: float) -> tuple[str, str | None]:
    """按阈值规程裁决单次读数：越限则抑制束流。"""
    if reading > threshold:
        return DECISION_INHIBIT, f"reading {reading:g} > threshold {threshold:g}"
    return DECISION_PASS, None


@dataclass(frozen=True)
class SubmitOutcome:
    state: str                     # VERDICT / WAITING
    duplicate: bool                # 是否为同内容重传回显
    verdict: dict[str, Any] | None
    drained: list[dict[str, Any]]  # 本次顺带排空的后续序号
    water_level: int


class DrillStore:
    """线程安全的演练裁决存储，SQLite 持久化。"""

    SCHEMA = """
    CREATE TABLE IF NOT EXISTS drills (
        drill_id   TEXT PRIMARY KEY,
        name       TEXT NOT NULL,
        created_at TEXT NOT NULL
    );
    CREATE TABLE IF NOT EXISTS procedures (
        drill_id       TEXT NOT NULL,
        effective_from INTEGER NOT NULL,
        code           TEXT NOT NULL,
        threshold      REAL NOT NULL,
        created_at     TEXT NOT NULL,
        PRIMARY KEY (drill_id, effective_from)
    );
    CREATE TABLE IF NOT EXISTS deliveries (
        delivery_id TEXT NOT NULL,
        drill_id    TEXT NOT NULL,
        seq         INTEGER NOT NULL,
        reading     REAL NOT NULL,
        PRIMARY KEY (drill_id, delivery_id)
    );
    CREATE TABLE IF NOT EXISTS verdicts (
        drill_id       TEXT NOT NULL,
        seq            INTEGER NOT NULL,
        delivery_id    TEXT NOT NULL,
        reading        REAL NOT NULL,
        decision       TEXT NOT NULL,
        procedure_code TEXT NOT NULL,
        threshold      REAL NOT NULL,
        reason         TEXT,
        adjudicated_at TEXT NOT NULL,
        PRIMARY KEY (drill_id, seq)
    );
    CREATE TABLE IF NOT EXISTS waiting (
        drill_id    TEXT NOT NULL,
        seq         INTEGER NOT NULL,
        delivery_id TEXT NOT NULL,
        reading     REAL NOT NULL,
        received_at TEXT NOT NULL,
        PRIMARY KEY (drill_id, seq),
        UNIQUE (drill_id, delivery_id)
    );
    CREATE TABLE IF NOT EXISTS water (
        drill_id TEXT PRIMARY KEY,
        level    INTEGER NOT NULL
    );
    CREATE TABLE IF NOT EXISTS rejections (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        drill_id    TEXT NOT NULL,
        kind        TEXT NOT NULL,
        delivery_id TEXT,
        seq         INTEGER,
        reason      TEXT NOT NULL,
        rejected_at TEXT NOT NULL
    );
    """

    def __init__(self, db_path: str):
        self.db_path = db_path
        parent = os.path.dirname(os.path.abspath(db_path))
        os.makedirs(parent, exist_ok=True)
        self._lock = threading.RLock()
        with self._connect() as conn:
            conn.executescript(self.SCHEMA)
            self._migrate(conn)
            conn.commit()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, timeout=30, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA busy_timeout=30000")
        return conn

    def _migrate(self, conn) -> None:
        """把早期版本全局唯一的投递标识迁移为演练范围内唯一。"""
        ddl = conn.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND "
            "name='deliveries'").fetchone()["sql"] or ""
        if "PRIMARY KEY (drill_id, delivery_id)" not in ddl:
            conn.executescript(
                "CREATE TABLE deliveries_new("
                "delivery_id TEXT NOT NULL, drill_id TEXT NOT NULL,"
                "seq INTEGER NOT NULL, reading REAL NOT NULL,"
                "PRIMARY KEY (drill_id, delivery_id));"
                "INSERT OR IGNORE INTO deliveries_new"
                "(delivery_id,drill_id,seq,reading)"
                "SELECT delivery_id,drill_id,seq,reading FROM deliveries;"
                "DROP TABLE deliveries;"
                "ALTER TABLE deliveries_new RENAME TO deliveries;")
        ddl = conn.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND "
            "name='waiting'").fetchone()["sql"] or ""
        if "UNIQUE (drill_id, delivery_id)" not in ddl \
                and "drill_id, delivery_id" not in ddl:
            conn.executescript(
                "CREATE TABLE waiting_new("
                "drill_id TEXT NOT NULL, seq INTEGER NOT NULL,"
                "delivery_id TEXT NOT NULL, reading REAL NOT NULL,"
                "received_at TEXT NOT NULL,"
                "PRIMARY KEY (drill_id, seq),"
                "UNIQUE (drill_id, delivery_id));"
                "INSERT OR IGNORE INTO waiting_new"
                "(drill_id,seq,delivery_id,reading,received_at)"
                "SELECT drill_id,seq,delivery_id,reading,received_at "
                "FROM waiting;"
                "DROP TABLE waiting;"
                "ALTER TABLE waiting_new RENAME TO waiting;")

    # --------------------------------------------------------------- 演练

    def create_drill(self, drill_id: str, name: str,
                     base_code: str, base_threshold: float) -> dict[str, Any]:
        """创建演练并登记序号 0 起生效的基线规程。"""
        with self._lock, self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                conn.execute(
                    "INSERT INTO drills(drill_id,name,created_at) VALUES (?,?,?)",
                    (drill_id, name, _now()))
                conn.execute(
                    "INSERT INTO water(drill_id,level) VALUES (?,0)", (drill_id,))
                conn.execute(
                    "INSERT INTO procedures(drill_id,effective_from,code,"
                    "threshold,created_at) VALUES (?,?,?,?,?)",
                    (drill_id, 0, base_code, float(base_threshold), _now()))
                conn.commit()
            except Exception:
                conn.rollback()
                raise
        return self.drill_status(drill_id)

    def list_drills(self) -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT d.drill_id, d.name, d.created_at, w.level "
                "FROM drills d JOIN water w ON w.drill_id=d.drill_id "
                "ORDER BY d.created_at").fetchall()
        return [dict(r) for r in rows]

    def register_procedure(self, drill_id: str, effective_from: int,
                           code: str, threshold: float) -> dict[str, Any]:
        """登记新规程；只能影响尚未裁决的序号，否则拒绝。"""
        with self._lock, self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                if not self._drill_exists(conn, drill_id):
                    raise NotFound(f"drill {drill_id} not found")
                if effective_from < 1:
                    raise self._conflict(
                        "effective_from must be >= 1; baseline already "
                        "owns sequence 0",
                        kind="PROCEDURE_INVALID", seq=effective_from)
                level = conn.execute(
                    "SELECT level FROM water WHERE drill_id=?",
                    (drill_id,)).fetchone()["level"]
                if effective_from < level:
                    raise self._conflict(
                        f"water level {level} already passed sequence "
                        f"{effective_from}; adjudicated history is immutable",
                        kind="PROCEDURE_LATE", seq=effective_from)
                dup = conn.execute(
                    "SELECT 1 FROM procedures WHERE drill_id=? AND "
                    "effective_from=?", (drill_id, effective_from)).fetchone()
                if dup:
                    raise self._conflict(
                        f"procedure already registered at sequence "
                        f"{effective_from}",
                        kind="PROCEDURE_DUPLICATE", seq=effective_from)
                conn.execute(
                    "INSERT INTO procedures(drill_id,effective_from,code,"
                    "threshold,created_at) VALUES (?,?,?,?,?)",
                    (drill_id, effective_from, code, float(threshold), _now()))
                conn.commit()
            except Conflict as exc:
                conn.rollback()
                self._record_rejection(drill_id, exc)
                raise
            except Exception:
                conn.rollback()
                raise
        return {"drill_id": drill_id, "effective_from": effective_from,
                "code": code, "threshold": float(threshold)}

    # --------------------------------------------------------------- 投递

    def submit_observation(self, drill_id: str, delivery_id: str,
                           seq: int, reading: float) -> SubmitOutcome:
        with self._lock, self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                if not self._drill_exists(conn, drill_id):
                    raise NotFound(f"drill {drill_id} not found")
                level = conn.execute(
                    "SELECT level FROM water WHERE drill_id=?",
                    (drill_id,)).fetchone()["level"]

                existing = conn.execute(
                    "SELECT * FROM deliveries WHERE drill_id=? AND "
                    "delivery_id=?", (drill_id, delivery_id)).fetchone()
                if existing is not None:
                    outcome = self._handle_retransmit(
                        conn, drill_id, existing, seq, reading, level)
                    conn.commit()
                    return outcome

                # 新标识：序号必须尚未被占用（无论已裁决还是等待中）
                occupant = self._occupant(conn, drill_id, seq)
                if occupant is not None:
                    raise self._conflict(
                        f"sequence {seq} already bound to delivery "
                        f"{occupant}; each sequence accepts exactly one "
                        "immutable observation",
                        kind="SEQUENCE_TAKEN", delivery_id=delivery_id,
                        seq=seq)

                conn.execute(
                    "INSERT INTO deliveries(delivery_id,drill_id,seq,reading)"
                    " VALUES (?,?,?,?)",
                    (delivery_id, drill_id, seq, float(reading)))

                if seq > level:
                    # 缺号：持久化等待，水位不动
                    conn.execute(
                        "INSERT INTO waiting(drill_id,seq,delivery_id,reading,"
                        "received_at) VALUES (?,?,?,?,?)",
                        (drill_id, seq, delivery_id, float(reading), _now()))
                    conn.commit()
                    return SubmitOutcome(
                        STATE_WAITING, False, None, [], level)

                # seq == level：裁决并依序排空等待队列
                drained = self._adjudicate_chain(
                    conn, drill_id, level, delivery_id, float(reading))
                new_level = level + len(drained)
                conn.commit()
                return SubmitOutcome(
                    STATE_VERDICT, False, drained[0], drained[1:], new_level)
            except Conflict as exc:
                conn.rollback()
                self._record_rejection(drill_id, exc)
                raise
            except Exception:
                conn.rollback()
                raise

    def _handle_retransmit(self, conn, drill_id, existing, seq, reading,
                           level) -> SubmitOutcome:
        if existing["seq"] == seq and existing["reading"] == float(reading):
            # 同标识同内容：回显原裁决/原等待，不写任何新状态
            vrow = conn.execute(
                "SELECT * FROM verdicts WHERE drill_id=? AND seq=?",
                (drill_id, seq)).fetchone()
            if vrow is not None:
                return SubmitOutcome(
                    STATE_VERDICT, True, _verdict_dict(vrow), [], level)
            return SubmitOutcome(STATE_WAITING, True, None, [], level)

        bound = f"seq={existing['seq']}, reading={existing['reading']:g}"
        raise self._conflict(
            f"delivery id {existing['delivery_id']} already bound to "
            f"{bound}; changing seq/reading is forbidden",
            kind="DELIVERY_CONTENT_CHANGED",
            delivery_id=existing["delivery_id"], seq=seq)

    def _adjudicate_chain(self, conn, drill_id, level, first_delivery_id,
                          first_reading) -> list[dict[str, Any]]:
        """从水位起连续裁决，直到等待队列出现缺口；全程在同一事务内。"""
        pending: list[tuple[int, str, float]] = [
            (level, first_delivery_id, first_reading)]
        cursor = level + 1
        while True:
            row = conn.execute(
                "SELECT seq,delivery_id,reading FROM waiting "
                "WHERE drill_id=? AND seq=?", (drill_id, cursor)).fetchone()
            if row is None:
                break
            pending.append((row["seq"], row["delivery_id"], row["reading"]))
            cursor += 1

        verdicts: list[dict[str, Any]] = []
        for i, delivery_id, reading in pending:
            proc = conn.execute(
                "SELECT code,threshold FROM procedures "
                "WHERE drill_id=? AND effective_from<=? "
                "ORDER BY effective_from DESC LIMIT 1",
                (drill_id, i)).fetchone()
            decision, reason = adjudicate(reading, proc["threshold"])
            ts = _now()
            conn.execute(
                "INSERT INTO verdicts(drill_id,seq,delivery_id,reading,"
                "decision,procedure_code,threshold,reason,adjudicated_at)"
                " VALUES (?,?,?,?,?,?,?,?,?)",
                (drill_id, i, delivery_id, reading, decision, proc["code"],
                 proc["threshold"], reason, ts))
            conn.execute(
                "DELETE FROM waiting WHERE drill_id=? AND seq=?",
                (drill_id, i))
            verdicts.append({
                "drill_id": drill_id, "seq": i,
                "delivery_id": delivery_id, "reading": reading,
                "decision": decision, "procedure_code": proc["code"],
                "threshold": proc["threshold"], "reason": reason,
                "adjudicated_at": ts,
            })
        conn.execute("UPDATE water SET level=? WHERE drill_id=?",
                     (level + len(pending), drill_id))
        return verdicts

    # --------------------------------------------------------------- 恢复

    def recover(self) -> None:
        """重开服务时调用：排空所有连续可达的等待项。

        正常路径下入队与推进水位是原子事务，这里只需补排服务中断前
        已经连续可裁决的序号，结果与不中断执行一致。
        """
        with self._lock, self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                for drill in conn.execute("SELECT drill_id FROM water"):
                    drill_id = drill["drill_id"]
                    while True:
                        level = conn.execute(
                            "SELECT level FROM water WHERE drill_id=?",
                            (drill_id,)).fetchone()["level"]
                        nxt = conn.execute(
                            "SELECT COUNT(*) c FROM waiting "
                            "WHERE drill_id=? AND seq=?",
                            (drill_id, level)).fetchone()["c"]
                        if not nxt:
                            break
                        row = conn.execute(
                            "SELECT delivery_id,reading FROM waiting "
                            "WHERE drill_id=? AND seq=?",
                            (drill_id, level)).fetchone()
                        self._adjudicate_chain(
                            conn, drill_id, level,
                            row["delivery_id"], row["reading"])
                conn.commit()
            except Exception:
                conn.rollback()
                raise

    # --------------------------------------------------------------- 视图

    def drill_status(self, drill_id: str) -> dict[str, Any]:
        with self._connect() as conn:
            if not self._drill_exists(conn, drill_id):
                raise NotFound(f"drill {drill_id} not found")
            drill = conn.execute(
                "SELECT * FROM drills WHERE drill_id=?",
                (drill_id,)).fetchone()
            level = conn.execute(
                "SELECT level FROM water WHERE drill_id=?",
                (drill_id,)).fetchone()["level"]
            procedures = [dict(r) for r in conn.execute(
                "SELECT effective_from,code,threshold,created_at "
                "FROM procedures WHERE drill_id=? ORDER BY effective_from",
                (drill_id,))]
            verdicts = [_verdict_dict(r) for r in conn.execute(
                "SELECT * FROM verdicts WHERE drill_id=? ORDER BY seq",
                (drill_id,))]
            waiting = [dict(r) for r in conn.execute(
                "SELECT seq,delivery_id,reading,received_at FROM waiting "
                "WHERE drill_id=? ORDER BY seq", (drill_id,))]
            first = conn.execute(
                "SELECT kind,delivery_id,seq,reason,rejected_at FROM "
                "rejections WHERE drill_id=? ORDER BY id LIMIT 1",
                (drill_id,)).fetchone()
            rejections = [dict(r) for r in conn.execute(
                "SELECT kind,delivery_id,seq,reason,rejected_at FROM "
                "rejections WHERE drill_id=? ORDER BY id", (drill_id,))]
        return {
            "drill_id": drill_id,
            "name": drill["name"],
            "created_at": drill["created_at"],
            "water_level": level,
            "procedures": procedures,
            "verdicts": verdicts,
            "waiting": waiting,
            "first_rejection": dict(first) if first else None,
            "rejections": rejections,
        }

    # --------------------------------------------------------------- 工具

    @staticmethod
    def _drill_exists(conn, drill_id: str) -> bool:
        return conn.execute(
            "SELECT 1 FROM drills WHERE drill_id=?", (drill_id,)).fetchone() \
            is not None

    @staticmethod
    def _occupant(conn, drill_id: str, seq: int):
        row = conn.execute(
            "SELECT delivery_id FROM verdicts WHERE drill_id=? AND seq=?",
            (drill_id, seq)).fetchone()
        if row is not None:
            return row["delivery_id"]
        row = conn.execute(
            "SELECT delivery_id FROM waiting WHERE drill_id=? AND seq=?",
            (drill_id, seq)).fetchone()
        return row["delivery_id"] if row is not None else None

    @staticmethod
    def _conflict(reason, kind, delivery_id=None, seq=None) -> Conflict:
        return Conflict(reason, kind=kind, delivery_id=delivery_id, seq=seq)

    def _record_rejection(self, drill_id: str, exc: Conflict) -> None:
        """业务事务回滚后，用独立事务持久化拒因（拒因不可回滚丢失）。"""
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO rejections(drill_id,kind,delivery_id,seq,reason,"
                "rejected_at) VALUES (?,?,?,?,?,?)",
                (drill_id, exc.kind, exc.delivery_id, exc.seq,
                 exc.reason, _now()))


def _verdict_dict(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "drill_id": row["drill_id"], "seq": row["seq"],
        "delivery_id": row["delivery_id"], "reading": row["reading"],
        "decision": row["decision"],
        "procedure_code": row["procedure_code"],
        "threshold": row["threshold"], "reason": row["reason"],
        "adjudicated_at": row["adjudicated_at"],
    }
