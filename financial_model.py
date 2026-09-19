#!/usr/bin/env python3
"""日給目標から逆算する収益シミュレータ.

    日給 = 月間粗利 / 30

このモデルが答えるのは次の3つだけ:

  1. いつ日給10万円(月粗利300万円)に到達するか
  2. いつ「工数の壁」にぶつかるか  -- 副業のまま回せなくなる月
  3. いつ資金が底を打つか          -- 手元資金が最小になる月と金額

依存ライブラリなし. `python3 financial_model.py` で実行できる.
"""

from __future__ import annotations

import argparse
import json
import math
from dataclasses import dataclass, field, asdict

DAYS_PER_MONTH = 30


# ---------------------------------------------------------------------------
# 設定
# ---------------------------------------------------------------------------


@dataclass
class Funnel:
    """アウトバウンド営業のファネル.

    数値は日本のB2Bコールドメールの実測レンジ. シグナルベースで
    1社ずつパーソナライズした場合の値を初期値にしている.
    汎用テンプレートの一斉送信なら返信率は 0.01 前後まで落ちる.
    """

    sends_per_workday: int = 40
    workdays_per_month: int = 20
    deliverability: float = 0.90        # 迷惑メール判定を除いた到達率
    reply_rate: float = 0.05            # 到達数に対する返信率
    positive_share: float = 0.35        # 返信のうち前向きな割合
    meeting_rate: float = 0.70          # 前向き返信が商談になる割合
    close_rate: float = 0.25            # 商談が成約になる割合
    ramp_months: int = 3                # 文面と的が固まるまでの助走期間

    def sends(self, month: int) -> float:
        """月あたり送信数. 初月からフルスロットルでは出さない."""
        ramp = min(1.0, (month + 1) / max(1, self.ramp_months))
        return self.sends_per_workday * self.workdays_per_month * ramp

    def meetings(self, month: int) -> float:
        reached = self.sends(month) * self.deliverability
        return reached * self.reply_rate * self.positive_share * self.meeting_rate

    def deals(self, month: int) -> float:
        return self.meetings(month) * self.close_rate


@dataclass
class Offer:
    """商品設計. ストック(月額)とフロー(初期費)の2階建て."""

    setup_fee: int = 300_000            # 初期構築費(単発)
    mrr: int = 150_000                  # 月額
    gross_margin: float = 0.85          # 粗利率(API代・インフラ代を引いた後)
    monthly_churn: float = 0.03         # 月次解約率


@dataclass
class Capacity:
    """自分の可処分工数. 副業ではここが最大の制約になる."""

    weekly_hours: float = 12.0
    onboarding_hours: float = 8.0       # 1社を立ち上げるのにかかる時間
    account_hours_per_month: float = 1.5  # 1社を維持するのにかかる月次時間
    sales_ops_hours: float = 8.0        # 営業システムの運用・改善(月次固定)
    outsource_hourly: int = 4_000       # 溢れた工数を外注する時の単価

    def monthly_hours(self) -> float:
        return self.weekly_hours * 52 / 12


@dataclass
class Costs:
    """固定費と変動費."""

    starting_capital: int = 1_000_000
    tools_monthly: int = 40_000         # メール基盤・LLM API・DB・ドメイン等
    list_building_monthly: int = 25_000  # リスト作成の外注(1社25円 x 月1,000社)
    ads_monthly: int = 0                # アウトバウンド主軸なので初期はゼロ
    fixed_other_monthly: int = 20_000   # 会計・登記維持・雑費
    owner_draw_monthly: int = 0         # 副業のため役員報酬は取らない前提


@dataclass
class Scenario:
    name: str
    funnel: Funnel = field(default_factory=Funnel)
    offer: Offer = field(default_factory=Offer)
    capacity: Capacity = field(default_factory=Capacity)
    costs: Costs = field(default_factory=Costs)
    months: int = 36


# ---------------------------------------------------------------------------
# シミュレーション
# ---------------------------------------------------------------------------


@dataclass
class MonthResult:
    month: int
    new_deals: float
    customers: float
    mrr: int
    setup_revenue: int
    revenue: int
    gross_profit: int
    daily_wage: int
    hours_needed: float
    hours_available: float
    outsource_cost: int
    opex: int
    net_cash: int
    cash_balance: int
    capacity_breached: bool


def simulate(s: Scenario) -> list[MonthResult]:
    results: list[MonthResult] = []
    customers = 0.0
    cash = float(s.costs.starting_capital)

    for m in range(s.months):
        # --- 獲得と解約 ---
        potential_deals = s.funnel.deals(m)

        # 工数の天井で新規獲得が頭打ちになる.
        # 既存維持と営業運用を先に確保し、残りを立ち上げに回す.
        available = s.capacity.monthly_hours()
        maintenance = customers * s.capacity.account_hours_per_month
        reserved = maintenance + s.capacity.sales_ops_hours
        free_hours = max(0.0, available - reserved)
        deals_self = free_hours / s.capacity.onboarding_hours

        new_deals = min(potential_deals, deals_self)
        # 自力で捌けない分は外注に回す(外注できる範囲は自力工数の2倍まで)
        overflow = max(0.0, min(potential_deals - new_deals, deals_self * 2))
        new_deals += overflow
        outsourced_hours = overflow * s.capacity.onboarding_hours
        # 維持工数も天井を超えた分は外注
        if reserved > available:
            outsourced_hours += reserved - available

        churned = customers * s.offer.monthly_churn
        customers = customers - churned + new_deals

        # --- 売上 ---
        mrr = customers * s.offer.mrr
        setup_revenue = new_deals * s.offer.setup_fee
        revenue = mrr + setup_revenue
        gross_profit = revenue * s.offer.gross_margin

        # --- 費用 ---
        outsource_cost = outsourced_hours * s.capacity.outsource_hourly
        opex = (
            s.costs.tools_monthly
            + s.costs.list_building_monthly
            + s.costs.ads_monthly
            + s.costs.fixed_other_monthly
            + s.costs.owner_draw_monthly
            + outsource_cost
        )
        net_cash = gross_profit - opex
        cash += net_cash

        hours_needed = (
            customers * s.capacity.account_hours_per_month
            + new_deals * s.capacity.onboarding_hours
            + s.capacity.sales_ops_hours
        )

        results.append(
            MonthResult(
                month=m + 1,
                new_deals=new_deals,
                customers=customers,
                mrr=int(mrr),
                setup_revenue=int(setup_revenue),
                revenue=int(revenue),
                gross_profit=int(gross_profit),
                daily_wage=int(gross_profit / DAYS_PER_MONTH),
                hours_needed=hours_needed,
                hours_available=available,
                outsource_cost=int(outsource_cost),
                opex=int(opex),
                net_cash=int(net_cash),
                cash_balance=int(cash),
                capacity_breached=hours_needed > available,
            )
        )

    return results


# ---------------------------------------------------------------------------
# 逆算
# ---------------------------------------------------------------------------


def required_customers(daily_wage: int, offer: Offer) -> int:
    """目標日給に必要な顧客数(月額のみで支える場合)."""
    monthly_gp = daily_wage * DAYS_PER_MONTH
    gp_per_customer = offer.mrr * offer.gross_margin
    return math.ceil(monthly_gp / gp_per_customer)


def required_sends(daily_wage: int, offer: Offer, funnel: Funnel) -> dict:
    """目標日給を維持するのに必要な月間送信数(解約を埋め続ける定常状態)."""
    n = required_customers(daily_wage, offer)
    replacement = n * offer.monthly_churn  # 毎月これだけ失う
    per_send = (
        funnel.deliverability
        * funnel.reply_rate
        * funnel.positive_share
        * funnel.meeting_rate
        * funnel.close_rate
    )
    return {
        "customers": n,
        "monthly_churn_count": round(replacement, 1),
        "sends_to_stay_flat": math.ceil(replacement / per_send) if per_send else 0,
        "deals_per_1000_sends": round(per_send * 1000, 2),
    }


def first_month_reaching(results: list[MonthResult], daily_wage: int) -> int | None:
    for r in results:
        if r.daily_wage >= daily_wage:
            return r.month
    return None


# ---------------------------------------------------------------------------
# 出力
# ---------------------------------------------------------------------------


def yen(n: int) -> str:
    if abs(n) >= 100_000_000:
        return f"{n / 100_000_000:.2f}億"
    if abs(n) >= 10_000:
        return f"{n / 10_000:,.0f}万"
    return f"{n:,}"


def print_table(s: Scenario, results: list[MonthResult], every: int = 1) -> None:
    print(f"\n{'=' * 104}")
    print(f"  シナリオ: {s.name}")
    print(
        f"  週{s.capacity.weekly_hours:.0f}h / 月額{yen(s.offer.mrr)}円 "
        f"/ 初期費{yen(s.offer.setup_fee)}円 / 粗利率{s.offer.gross_margin:.0%} "
        f"/ 月次解約{s.offer.monthly_churn:.0%} / 初期資金{yen(s.costs.starting_capital)}円"
    )
    print("=" * 104)
    header = (
        f"{'月':>3} {'新規':>5} {'顧客':>6} {'MRR':>8} {'売上':>8} "
        f"{'粗利':>8} {'日給':>8} {'要工数':>7} {'外注費':>7} {'月次CF':>8} {'現金残':>9}"
    )
    print(header)
    print("-" * 104)
    for r in results:
        if r.month % every and r.month != 1 and r.month != len(results):
            continue
        flag = " !" if r.capacity_breached else "  "
        print(
            f"{r.month:>3} {r.new_deals:>5.1f} {r.customers:>6.1f} "
            f"{yen(r.mrr):>8} {yen(r.revenue):>8} {yen(r.gross_profit):>8} "
            f"{yen(r.daily_wage):>8} {r.hours_needed:>5.0f}h{flag} "
            f"{yen(r.outsource_cost):>7} {yen(r.net_cash):>8} {yen(r.cash_balance):>9}"
        )
    print("-" * 104)
    print("  ! = 週の稼働時間を超過している月(外注 or 専業化の判断点)")


def print_milestones(s: Scenario, results: list[MonthResult]) -> None:
    print(f"\n  ■ マイルストーン")
    for wage, label in [
        (30_000, "日給 3万円  (月粗利  90万)"),
        (100_000, "日給10万円  (月粗利 300万)  ← 一次目標"),
        (300_000, "日給30万円  (月粗利 900万)"),
        (1_000_000, "日給100万円 (月粗利3000万) ← 最終目標"),
    ]:
        month = first_month_reaching(results, wage)
        when = f"{month}ヶ月目" if month else f"{s.months}ヶ月以内に未到達"
        need = required_customers(wage, s.offer)
        print(f"    {label} : {when:>16}  (必要顧客数 {need}社)")

    breach = next((r for r in results if r.capacity_breached), None)
    if breach:
        print(
            f"\n  ■ 工数の壁    : {breach.month}ヶ月目 "
            f"(必要{breach.hours_needed:.0f}h > 可処分{breach.hours_available:.0f}h, "
            f"顧客{breach.customers:.0f}社)"
        )
        print(f"                  この月までに外注化か専業化を決めないと成長が止まる")

    trough = min(results, key=lambda r: r.cash_balance)
    print(
        f"  ■ 資金の谷底  : {trough.month}ヶ月目に現金残 {yen(trough.cash_balance)}円"
        + ("  ← 資金ショート" if trough.cash_balance < 0 else "")
    )

    breakeven = next((r for r in results if r.net_cash > 0), None)
    if breakeven:
        print(f"  ■ 月次黒字化  : {breakeven.month}ヶ月目")


def print_reverse(s: Scenario) -> None:
    print(f"\n  ■ 日給100万円の構造(定常状態で必要な数字)")
    for mrr, label in [(150_000, "月額15万"), (300_000, "月額30万"), (500_000, "月額50万")]:
        offer = Offer(
            setup_fee=s.offer.setup_fee,
            mrr=mrr,
            gross_margin=s.offer.gross_margin,
            monthly_churn=s.offer.monthly_churn,
        )
        req = required_sends(1_000_000, offer, s.funnel)
        print(
            f"    {label}の場合: 顧客{req['customers']:>4}社を維持 / "
            f"毎月{req['monthly_churn_count']:>5}社が解約 / "
            f"補充に月{req['sends_to_stay_flat']:>6,}通の送信が必要"
        )
    per_1k = required_sends(1_000_000, s.offer, s.funnel)["deals_per_1000_sends"]
    print(f"    ※ 現在のファネルでは 1,000通 = {per_1k}件成約")


# ---------------------------------------------------------------------------
# フェーズ連結シミュレーション
# ---------------------------------------------------------------------------


@dataclass
class Phase:
    """事業フェーズ. 前フェーズの顧客と現金を引き継いで続きを走る."""

    name: str
    months: int
    funnel: Funnel
    offer: Offer
    capacity: Capacity
    costs: Costs


def default_phases() -> list[Phase]:
    """週12h・資金100万・実績ゼロから始める現実的な3フェーズ.

    フェーズを分ける理由は単純で、単価を上げないと日給100万に構造的に
    届かないから. 月額15万のままだと236社が必要になり、どんな工数でも
    維持できない.
    """
    return [
        Phase(
            name="P1 副業 / 月額15万で型を作る",
            months=10,
            funnel=Funnel(sends_per_workday=40, reply_rate=0.05, ramp_months=3),
            offer=Offer(setup_fee=300_000, mrr=150_000, monthly_churn=0.03),
            capacity=Capacity(weekly_hours=12.0),
            costs=Costs(tools_monthly=40_000, list_building_monthly=25_000,
                        fixed_other_monthly=20_000),
        ),
        Phase(
            name="P2 専業化 / 単価を月額50万に上げる",
            months=14,
            funnel=Funnel(sends_per_workday=60, reply_rate=0.06, ramp_months=2),
            offer=Offer(setup_fee=1_000_000, mrr=500_000, monthly_churn=0.025),
            capacity=Capacity(weekly_hours=40.0, onboarding_hours=20.0,
                              account_hours_per_month=3.0, sales_ops_hours=16.0),
            costs=Costs(tools_monthly=150_000, list_building_monthly=40_000,
                        fixed_other_monthly=80_000, owner_draw_monthly=600_000),
        ),
        Phase(
            name="P3 チーム化 / 納品を人に回す",
            months=18,
            funnel=Funnel(sends_per_workday=120, reply_rate=0.06, ramp_months=2),
            offer=Offer(setup_fee=1_000_000, mrr=500_000, monthly_churn=0.02,
                        gross_margin=0.62),
            capacity=Capacity(weekly_hours=160.0, onboarding_hours=20.0,
                              account_hours_per_month=3.0, sales_ops_hours=40.0),
            costs=Costs(tools_monthly=400_000, list_building_monthly=80_000,
                        fixed_other_monthly=300_000, owner_draw_monthly=1_500_000),
        ),
    ]


def simulate_phased(phases: list[Phase], starting_capital: int = 1_000_000):
    """フェーズをまたいで通算で走らせる.

    既存顧客は契約時の単価のまま据え置く(値上げは新規からしか効かない)ので、
    価格帯ごとのコホートで顧客を持つ.
    """
    cohorts: dict[int, float] = {}
    cash = float(starting_capital)
    results: list[MonthResult] = []
    labels: list[str] = []
    global_month = 0

    for ph in phases:
        for local in range(ph.months):
            global_month += 1
            customers = sum(cohorts.values())

            potential = ph.funnel.deals(local)
            available = ph.capacity.monthly_hours()
            maintenance = customers * ph.capacity.account_hours_per_month
            reserved = maintenance + ph.capacity.sales_ops_hours
            free_hours = max(0.0, available - reserved)
            deals_self = free_hours / ph.capacity.onboarding_hours

            new_deals = min(potential, deals_self)
            overflow = max(0.0, min(potential - new_deals, deals_self * 2))
            new_deals += overflow
            outsourced_hours = overflow * ph.capacity.onboarding_hours
            if reserved > available:
                outsourced_hours += reserved - available

            # 解約は全コホートに一律で効く
            for price in list(cohorts):
                cohorts[price] *= (1.0 - ph.offer.monthly_churn)
            cohorts[ph.offer.mrr] = cohorts.get(ph.offer.mrr, 0.0) + new_deals

            customers = sum(cohorts.values())
            mrr = sum(price * n for price, n in cohorts.items())
            setup_revenue = new_deals * ph.offer.setup_fee
            revenue = mrr + setup_revenue
            gross_profit = revenue * ph.offer.gross_margin

            outsource_cost = outsourced_hours * ph.capacity.outsource_hourly
            opex = (ph.costs.tools_monthly + ph.costs.list_building_monthly
                    + ph.costs.ads_monthly + ph.costs.fixed_other_monthly
                    + ph.costs.owner_draw_monthly + outsource_cost)
            net_cash = gross_profit - opex
            cash += net_cash

            hours_needed = (customers * ph.capacity.account_hours_per_month
                            + new_deals * ph.capacity.onboarding_hours
                            + ph.capacity.sales_ops_hours)

            results.append(MonthResult(
                month=global_month, new_deals=new_deals, customers=customers,
                mrr=int(mrr), setup_revenue=int(setup_revenue), revenue=int(revenue),
                gross_profit=int(gross_profit),
                daily_wage=int(gross_profit / DAYS_PER_MONTH),
                hours_needed=hours_needed, hours_available=available,
                outsource_cost=int(outsource_cost), opex=int(opex),
                net_cash=int(net_cash), cash_balance=int(cash),
                capacity_breached=hours_needed > available,
            ))
            labels.append(ph.name)

    return results, labels


def print_phased(phases: list[Phase], results: list[MonthResult], labels: list[str]) -> None:
    print(f"\n{'=' * 104}")
    print("  通算シミュレーション: 週12h・資金100万・実績ゼロ からの3フェーズ")
    print("=" * 104)
    print(f"{'月':>3} {'フェーズ':<28} {'顧客':>6} {'MRR':>8} {'売上':>8} "
          f"{'粗利':>8} {'日給':>8} {'月次CF':>8} {'現金残':>9}")
    print("-" * 104)
    seen = None
    for r, lab in zip(results, labels):
        boundary = lab != seen
        if boundary:
            print("-" * 104)
            seen = lab
        # フェーズ境界と3ヶ月ごと、最終月だけ表示
        if not boundary and r.month % 3 and r.month != len(results):
            continue
        print(f"{r.month:>3} {lab:<28} {r.customers:>6.1f} {yen(r.mrr):>8} "
              f"{yen(r.revenue):>8} {yen(r.gross_profit):>8} {yen(r.daily_wage):>8} "
              f"{yen(r.net_cash):>8} {yen(r.cash_balance):>9}")
    print("-" * 104)

    print("\n  ■ 通算マイルストーン")
    for wage, label in [(100_000, "日給10万円  (月粗利 300万)"),
                        (300_000, "日給30万円  (月粗利 900万)"),
                        (1_000_000, "日給100万円 (月粗利3000万)")]:
        m = first_month_reaching(results, wage)
        when = f"{m}ヶ月目 ({m // 12}年{m % 12}ヶ月)" if m else "未到達"
        print(f"    {label} : {when}")
    trough = min(results, key=lambda r: r.cash_balance)
    print(f"\n  ■ 資金の谷底  : {trough.month}ヶ月目 / 現金残 {yen(trough.cash_balance)}円"
          + ("  ← 資金ショート" if trough.cash_balance < 0 else "  (増資不要)"))


def main() -> None:
    p = argparse.ArgumentParser(description="日給目標の収益シミュレータ")
    p.add_argument("--weekly-hours", type=float, default=12.0)
    p.add_argument("--mrr", type=int, default=150_000)
    p.add_argument("--setup-fee", type=int, default=300_000)
    p.add_argument("--sends-per-day", type=int, default=40)
    p.add_argument("--reply-rate", type=float, default=0.05)
    p.add_argument("--churn", type=float, default=0.03)
    p.add_argument("--capital", type=int, default=1_000_000)
    p.add_argument("--months", type=int, default=36)
    p.add_argument("--every", type=int, default=1, help="N ヶ月ごとに表示")
    p.add_argument("--json", action="store_true", help="JSON で出力")
    p.add_argument("--phased", action="store_true", help="3フェーズ通算で走らせる")
    args = p.parse_args()

    if args.phased:
        phases = default_phases()
        results, labels = simulate_phased(phases, args.capital)
        print_phased(phases, results, labels)
        print()
        return

    base = Scenario(
        name="副業スタート / アウトバウンド主軸",
        funnel=Funnel(sends_per_workday=args.sends_per_day, reply_rate=args.reply_rate),
        offer=Offer(setup_fee=args.setup_fee, mrr=args.mrr, monthly_churn=args.churn),
        capacity=Capacity(weekly_hours=args.weekly_hours),
        costs=Costs(starting_capital=args.capital),
        months=args.months,
    )

    results = simulate(base)

    if args.json:
        print(
            json.dumps(
                {"scenario": asdict(base), "results": [asdict(r) for r in results]},
                ensure_ascii=False,
                indent=2,
            )
        )
        return

    print_table(base, results, every=args.every)
    print_milestones(base, results)
    print_reverse(base)
    print()


if __name__ == "__main__":
    main()
