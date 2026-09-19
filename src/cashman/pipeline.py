"""商談パイプライン.

ステージは左から右にしか進まない. 失注は lost に落とす.
「検討中」を無限に置けるようにすると、パイプラインが希望的観測の
置き場になって数字が読めなくなるので、置けるステージを5つに固定している.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass

from . import db

STAGES = db.STAGES  # meeting_set -> meeting_done -> proposal -> won / lost

STAGE_LABELS = {
    "meeting_set":  "商談設定",
    "meeting_done": "商談実施",
    "proposal":     "提案中",
    "won":          "受注",
    "lost":         "失注",
}

# 各ステージから受注に至る確率. 予測の重み付けに使う.
# 実績が30件を超えたら measured_conversion() の実測値に置き換える.
DEFAULT_WIN_RATES = {
    "meeting_set":  0.25,
    "meeting_done": 0.40,
    "proposal":     0.60,
}


def open_deal(conn: sqlite3.Connection, company_id: int,
              mrr: int = 0, setup_fee: int = 0) -> int:
    cur = conn.execute(
        "INSERT INTO deals (company_id, stage, mrr, setup_fee) VALUES (?,?,?,?)",
        (company_id, "meeting_set", mrr, setup_fee),
    )
    deal_id = int(cur.lastrowid)
    conn.execute("UPDATE companies SET status='in_pipeline' WHERE id=?", (company_id,))
    db.log(conn, "deals", deal_id, "opened")
    conn.commit()
    return deal_id


def advance(conn: sqlite3.Connection, deal_id: int, stage: str,
            mrr: int | None = None, setup_fee: int | None = None,
            lost_reason: str | None = None) -> None:
    """ステージを進める. 後戻りは受け付けない(失注にするしかない)."""
    if stage not in STAGES:
        raise ValueError(f"unknown stage: {stage!r}. use one of {STAGES}")
    row = conn.execute("SELECT stage FROM deals WHERE id=?", (deal_id,)).fetchone()
    if row is None:
        raise KeyError(f"deal {deal_id} not found")
    current = row["stage"]
    if current in ("won", "lost"):
        raise ValueError(f"deal {deal_id} はすでに {current} で確定している")
    if stage != "lost" and STAGES.index(stage) <= STAGES.index(current):
        raise ValueError(f"{current} から {stage} へは戻せない")

    closed = "datetime('now')" if stage in ("won", "lost") else "NULL"
    conn.execute(
        f"""UPDATE deals SET stage=?,
              mrr=COALESCE(?, mrr), setup_fee=COALESCE(?, setup_fee),
              lost_reason=?, updated_at=datetime('now'), closed_at={closed}
            WHERE id=?""",
        (stage, mrr, setup_fee, lost_reason, deal_id),
    )
    if stage == "won":
        conn.execute(
            "UPDATE companies SET status='customer' WHERE id="
            "(SELECT company_id FROM deals WHERE id=?)", (deal_id,))
    db.log(conn, "deals", deal_id, f"{current}->{stage}", lost_reason)
    conn.commit()


@dataclass
class Forecast:
    weighted_mrr: int
    weighted_setup: int
    open_count: int
    won_mrr: int
    detail: list[tuple[str, int, int]]  # (stage, count, weighted_mrr)


def forecast(conn: sqlite3.Connection,
             win_rates: dict[str, float] | None = None) -> Forecast:
    """進行中の商談を受注確率で重み付けした見込み."""
    rates = win_rates or DEFAULT_WIN_RATES
    detail, w_mrr, w_setup, total = [], 0.0, 0.0, 0
    for stage in ("meeting_set", "meeting_done", "proposal"):
        row = conn.execute(
            "SELECT COUNT(*) n, COALESCE(SUM(mrr),0) mrr, COALESCE(SUM(setup_fee),0) s "
            "FROM deals WHERE stage=?", (stage,),
        ).fetchone()
        r = rates.get(stage, 0.0)
        stage_mrr = row["mrr"] * r
        detail.append((stage, int(row["n"]), int(stage_mrr)))
        w_mrr += stage_mrr
        w_setup += row["s"] * r
        total += int(row["n"])

    won = conn.execute(
        "SELECT COALESCE(SUM(mrr),0) m FROM deals WHERE stage='won'"
    ).fetchone()
    return Forecast(int(w_mrr), int(w_setup), total, int(won["m"]), detail)


def measured_conversion(conn: sqlite3.Connection,
                        min_samples: int = 10) -> dict[str, float]:
    """実績から各ステージの受注率を測る.

    決着した(won/lost)商談だけを母数にする. 進行中を分母に入れると
    受注率が実態より低く出て、判断を誤る.
    """
    out: dict[str, float] = {}
    for stage in ("meeting_set", "meeting_done", "proposal"):
        row = conn.execute(
            """SELECT COUNT(DISTINCT entity_id) AS reached FROM events
               WHERE entity='deals' AND kind LIKE ?""",
            (f"{stage}->%",),
        ).fetchone()
        reached = row["reached"] or 0
        if reached < min_samples:
            continue
        won = conn.execute(
            """SELECT COUNT(DISTINCT e.entity_id) AS n FROM events e
               JOIN deals d ON d.id = e.entity_id
               WHERE e.entity='deals' AND e.kind LIKE ? AND d.stage='won'""",
            (f"{stage}->%",),
        ).fetchone()
        out[stage] = round((won["n"] or 0) / reached, 3)
    return out


def board(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute(
        """SELECT d.id, d.stage, d.mrr, d.setup_fee, d.updated_at,
                  co.name AS company_name, co.score,
                  CAST(julianday('now') - julianday(d.updated_at) AS INTEGER) AS stale_days
           FROM deals d JOIN companies co ON co.id = d.company_id
           WHERE d.stage NOT IN ('won','lost')
           ORDER BY CASE d.stage WHEN 'proposal' THEN 0 WHEN 'meeting_done'
                    THEN 1 ELSE 2 END, d.updated_at ASC"""
    ).fetchall()
