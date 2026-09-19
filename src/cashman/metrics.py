"""KPI集計.

見る数字は5つだけに絞る. 増やすと毎日見なくなる.

    送信数 -> 返信率 -> 商談化率 -> 受注率 -> MRR

このうち手で動かせるのは送信数と返信率だけ. 商談化率から先は
商品と価格の問題なので、数字が悪いときにどこを直すかがこの順で決まる.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass

DAYS_PER_MONTH = 30


@dataclass
class Funnel:
    sent: int
    replied: int
    positive: int
    optout: int
    meetings: int
    won: int

    @property
    def reply_rate(self) -> float:
        return self.replied / self.sent if self.sent else 0.0

    @property
    def positive_rate(self) -> float:
        return self.positive / self.sent if self.sent else 0.0

    @property
    def optout_rate(self) -> float:
        return self.optout / self.sent if self.sent else 0.0

    @property
    def meeting_rate(self) -> float:
        return self.meetings / self.sent if self.sent else 0.0

    @property
    def win_rate(self) -> float:
        return self.won / self.meetings if self.meetings else 0.0

    @property
    def sends_per_deal(self) -> float:
        return self.sent / self.won if self.won else float("inf")


def funnel(conn: sqlite3.Connection, days: int | None = None) -> Funnel:
    where = f"AND sent_at >= date('now', '-{int(days)} day')" if days else ""
    m = conn.execute(f"""
        SELECT COUNT(*) AS sent,
               SUM(CASE WHEN replied_at IS NOT NULL THEN 1 ELSE 0 END) AS replied,
               SUM(CASE WHEN reply_kind='positive' THEN 1 ELSE 0 END) AS positive,
               SUM(CASE WHEN reply_kind='optout'   THEN 1 ELSE 0 END) AS optout
        FROM messages WHERE status='sent' {where}
    """).fetchone()
    d = conn.execute("SELECT COUNT(*) AS n FROM deals").fetchone()
    w = conn.execute("SELECT COUNT(*) AS n FROM deals WHERE stage='won'").fetchone()
    return Funnel(
        sent=m["sent"] or 0, replied=m["replied"] or 0,
        positive=m["positive"] or 0, optout=m["optout"] or 0,
        meetings=d["n"] or 0, won=w["n"] or 0,
    )


def revenue(conn: sqlite3.Connection, gross_margin: float = 0.85) -> dict:
    """現在のMRRと、そこから逆算した日給."""
    row = conn.execute(
        "SELECT COALESCE(SUM(mrr),0) mrr, COUNT(*) n FROM deals WHERE stage='won'"
    ).fetchone()
    setup = conn.execute(
        """SELECT COALESCE(SUM(setup_fee),0) s FROM deals
           WHERE stage='won' AND closed_at >= date('now','-30 day')"""
    ).fetchone()
    mrr = int(row["mrr"])
    monthly_revenue = mrr + int(setup["s"])
    gp = monthly_revenue * gross_margin
    return {
        "customers": int(row["n"]),
        "mrr": mrr,
        "setup_last_30d": int(setup["s"]),
        "monthly_revenue": monthly_revenue,
        "monthly_gross_profit": int(gp),
        "daily_wage": int(gp / DAYS_PER_MONTH),
        "arpa": int(mrr / row["n"]) if row["n"] else 0,
    }


def gap_to_target(conn: sqlite3.Connection, target_daily_wage: int,
                  gross_margin: float = 0.85) -> dict:
    """目標日給までの差分を、必要な「送信数」まで翻訳して返す.

    ここが一番使う関数. 「あと何通送れば届くか」が出ないと、
    日々の行動に落ちない.
    """
    cur = revenue(conn, gross_margin)
    f = funnel(conn)

    need_gp = target_daily_wage * DAYS_PER_MONTH
    gap_gp = max(0, need_gp - cur["monthly_gross_profit"])
    arpa = cur["arpa"] or 150_000
    gp_per_customer = arpa * gross_margin
    need_customers = int(-(-gap_gp // gp_per_customer)) if gp_per_customer else 0

    # 実績の受注効率. 実績が薄いうちは仮置きの値を使う.
    sends_per_deal = f.sends_per_deal if f.won >= 3 else 360.0
    return {
        "current_daily_wage": cur["daily_wage"],
        "target_daily_wage": target_daily_wage,
        "gap_monthly_gross_profit": gap_gp,
        "additional_customers_needed": need_customers,
        "sends_per_deal": round(sends_per_deal, 1),
        "sends_needed": int(need_customers * sends_per_deal),
        "months_at_40_per_day": round(need_customers * sends_per_deal / 800, 1),
        "basis": "実績" if f.won >= 3 else "仮置き(受注3件未満)",
    }


def _pct(x: float) -> str:
    return f"{x * 100:.1f}%"


def _yen(n: int) -> str:
    if abs(n) >= 100_000_000:
        return f"{n / 100_000_000:.2f}億円"
    if abs(n) >= 10_000:
        return f"{n / 10_000:,.0f}万円"
    return f"{n:,}円"


def dashboard(conn: sqlite3.Connection, target_daily_wage: int = 100_000,
              gross_margin: float = 0.85) -> str:
    f30 = funnel(conn, days=30)
    fall = funnel(conn)
    rev = revenue(conn, gross_margin)
    gap = gap_to_target(conn, target_daily_wage, gross_margin)

    lines = [
        "=" * 66,
        "  cashman ダッシュボード",
        "=" * 66,
        "",
        "  ■ 現在地",
        f"    顧客数         : {rev['customers']} 社",
        f"    MRR            : {_yen(rev['mrr'])}",
        f"    ARPA           : {_yen(rev['arpa'])}",
        f"    月間粗利       : {_yen(rev['monthly_gross_profit'])}",
        f"    日給           : {_yen(rev['daily_wage'])}",
        "",
        "  ■ ファネル（直近30日 / 累計）",
        f"    送信           : {f30.sent:>6} / {fall.sent}",
        f"    返信率         : {_pct(f30.reply_rate):>6} / {_pct(fall.reply_rate)}"
        "   （目安 4〜8%）",
        f"    前向き返信率   : {_pct(f30.positive_rate):>6} / {_pct(fall.positive_rate)}"
        "   （目安 1.5〜3%）",
        f"    配信停止率     : {_pct(f30.optout_rate):>6} / {_pct(fall.optout_rate)}"
        "   （2%超で文面かリストを疑う）",
        f"    商談化率       : {_pct(fall.meeting_rate):>6}"
        "           （目安 1.5〜2.5%）",
        f"    商談→受注      : {_pct(fall.win_rate):>6}"
        "           （目安 20〜30%）",
        "",
        f"  ■ 目標 日給{_yen(target_daily_wage)} まで（根拠: {gap['basis']}）",
        f"    粗利の不足     : {_yen(gap['gap_monthly_gross_profit'])}/月",
        f"    必要な追加顧客 : {gap['additional_customers_needed']} 社",
        f"    1件受注に必要な送信数 : {gap['sends_per_deal']} 通",
        f"    必要な送信数   : {gap['sends_needed']:,} 通"
        f"（40通/日なら約 {gap['months_at_40_per_day']} ヶ月）",
        "=" * 66,
    ]
    return "\n".join(lines)
