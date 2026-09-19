"""シグナル検出とスコアリング.

アウトバウンドの成否はほぼここで決まる. 文面を磨くより、
「今まさに困っている会社」を当てるほうが返信率への効き方が一桁大きい.

シグナルとは、外から観測できる「困っている証拠」のこと:

    - 事務・オペ職を募集している        -> その業務が人手で回っていない
    - 同じ職種を3ヶ月以上出し続けている  -> 採用で解決できていない
    - 直近で資金調達した                -> 予算がある / 急いでいる
    - SaaSを複数使っている              -> 決裁者にITリテラシーがある

逆に「シグナルが1つも無い会社には送らない」. 送る価値が無いからではなく、
書き出しの一行が書けないから. 一行目が書けない相手は返信もしない.
"""

from __future__ import annotations

import math
import re
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

# --- シグナル定義 -----------------------------------------------------------
# weight は「返信率への寄与」の相対値. 実績が溜まったら calibrate() で上書きする.

SIGNAL_WEIGHTS: dict[str, int] = {
    "hiring_ops":        30,  # 事務/オペレーション/カスタマーサポートの募集
    "hiring_repeat":     25,  # 同一職種を繰り返し募集(採用で解けていない)
    "funding":           35,  # 資金調達の公表
    "hiring_eng":        20,  # エンジニア募集(開発リソース不足)
    "job_volume":        20,  # 求人件数が多い(急拡大 or 高離職)
    "tech_stack":        15,  # SaaS利用の痕跡(導入の意思決定ができる組織)
    "new_service":       15,  # 新規事業・サービス開始のリリース
    "manual_process":    25,  # 求人票にExcel/手入力/転記といった語が出る
    "legacy_contact":     8,  # 問い合わせがフォームのみ・サイトが古い
}

SIGNAL_HALF_LIFE_DAYS = 45  # シグナルは古くなるほど効かない. 45日で効果半減.

# --- 求人票からシグナルを取り出すパターン ------------------------------------

_OPS_TITLES = re.compile(
    r"(事務|バックオフィス|オペレーション|データ入力|経理|総務|"
    r"カスタマーサポート|カスタマーサクセス|受付|アシスタント|業務委託スタッフ)"
)
_ENG_TITLES = re.compile(r"(エンジニア|開発|プログラマ|SRE|インフラ|情シス|社内SE)")
_MANUAL_WORDS = re.compile(
    r"(Excel|エクセル|手入力|転記|目視|コピー&?ペースト|コピペ|"
    r"紙|FAX|突合|名寄せ|手作業|マクロ|スプレッドシート)"
)


@dataclass
class Signal:
    kind: str
    detail: str
    url: str | None = None
    weight: int | None = None
    observed_at: str | None = None

    def resolved_weight(self) -> int:
        if self.weight is not None:
            return self.weight
        return SIGNAL_WEIGHTS.get(self.kind, 10)


def from_job_posting(title: str, body: str = "", url: str | None = None,
                     posted_at: str | None = None) -> list[Signal]:
    """求人票1件からシグナルを抽出する.

    求人はアウトバウンドで最も費用対効果の高い情報源. 「何に困っているか」を
    会社自身が公開で書いてくれている上に、予算が付いている証拠にもなる.
    """
    found: list[Signal] = []
    text = f"{title}\n{body}"

    if _OPS_TITLES.search(title):
        found.append(Signal("hiring_ops", f"「{title}」を募集中", url, observed_at=posted_at))
    elif _ENG_TITLES.search(title):
        found.append(Signal("hiring_eng", f"「{title}」を募集中", url, observed_at=posted_at))

    manual = sorted(set(m.group(0) for m in _MANUAL_WORDS.finditer(text)))
    if manual:
        found.append(Signal(
            "manual_process",
            f"求人票に手作業を示す語: {'/'.join(manual[:4])}",
            url, observed_at=posted_at,
        ))
    return found


def detect_repeat_hiring(postings: list[dict], window_days: int = 90) -> list[Signal]:
    """同じ職種を繰り返し出していないかを見る.

    3ヶ月で同一職種が2回以上出ている = 採用では解決しなかった業務.
    これは自動化提案が最も刺さる状態で、単体シグナルの中では返信率が高い.
    """
    buckets: dict[str, list[dict]] = {}
    cutoff = datetime.now(timezone.utc) - timedelta(days=window_days)
    for p in postings:
        posted = _parse(p.get("posted_at"))
        if posted and posted < cutoff:
            continue
        key = _normalize_title(p.get("title", ""))
        if key:
            buckets.setdefault(key, []).append(p)

    out: list[Signal] = []
    for key, group in buckets.items():
        if len(group) >= 2:
            out.append(Signal(
                "hiring_repeat",
                f"「{key}」を{window_days}日で{len(group)}回募集",
                group[0].get("url"),
            ))
    return out


def _normalize_title(title: str) -> str:
    t = re.sub(r"[【】\[\]（）()／/|｜].*$", "", title).strip()
    t = re.sub(r"(急募|未経験歓迎|正社員|契約社員|アルバイト|在宅|リモート|週\d日)", "", t)
    return t.strip()


def _parse(value) -> datetime | None:
    if not value:
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d", "%Y/%m/%d"):
        try:
            return datetime.strptime(str(value)[:19], fmt).replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    return None


# --- ICP(狙うべき会社像) ----------------------------------------------------


@dataclass
class ICP:
    """狙う会社の輪郭.

    従業員数のレンジが一番効く. 小さすぎると予算が無く、大きすぎると
    決裁に半年かかる. 30〜300人は「担当者が決裁者に直結している」帯で、
    副業の稼働時間でも商談が1〜2回で終わる.
    """

    employees_min: int = 30
    employees_max: int = 300
    employees_sweet: tuple[int, int] = (50, 200)
    industries: tuple[str, ...] = (
        "人材", "物流", "不動産", "医療", "介護", "建設", "卸売", "小売",
        "士業", "製造", "広告", "EC",
    )
    exclude_industries: tuple[str, ...] = ("官公庁", "学校", "宗教")

    def fit_score(self, company: sqlite3.Row | dict) -> tuple[int, list[str]]:
        """シグナルとは独立に、会社の属性だけで付ける点数."""
        get = company.__getitem__ if not isinstance(company, dict) else company.get
        score, reasons = 0, []

        emp = get("employees") if not isinstance(company, dict) else company.get("employees")
        if emp:
            lo, hi = self.employees_sweet
            if lo <= emp <= hi:
                score += 25
                reasons.append(f"従業員{emp}人(最適帯)")
            elif self.employees_min <= emp <= self.employees_max:
                score += 15
                reasons.append(f"従業員{emp}人(対象帯)")
            elif emp < self.employees_min:
                score -= 20
                reasons.append(f"従業員{emp}人(予算不足の懸念)")
            else:
                score -= 10
                reasons.append(f"従業員{emp}人(決裁が長期化)")

        industry = (get("industry") if not isinstance(company, dict)
                    else company.get("industry")) or ""
        if any(x in industry for x in self.exclude_industries):
            score -= 60
            reasons.append(f"除外業種({industry})")
        elif any(x in industry for x in self.industries):
            score += 15
            reasons.append(f"注力業種({industry})")

        return score, reasons


# --- スコアリング -----------------------------------------------------------


def decay(weight: int, observed_at: str | None,
          half_life: int = SIGNAL_HALF_LIFE_DAYS) -> float:
    """時間減衰. 半年前の求人は今の課題を表していない."""
    when = _parse(observed_at)
    if not when:
        return float(weight)
    days = max(0.0, (datetime.now(timezone.utc) - when).total_seconds() / 86400)
    return weight * math.pow(0.5, days / half_life)


def score_company(conn: sqlite3.Connection, company_id: int,
                  icp: ICP | None = None) -> tuple[int, str]:
    """会社1社のスコアと、その理由を返す.

    理由の文字列はそのまま商談準備メモとしても使うので、人が読める形にする.
    """
    icp = icp or ICP()
    company = conn.execute("SELECT * FROM companies WHERE id=?", (company_id,)).fetchone()
    if company is None:
        raise KeyError(f"company {company_id} not found")

    total, reasons = icp.fit_score(company)

    rows = conn.execute(
        "SELECT kind, detail, weight, observed_at FROM signals WHERE company_id=?",
        (company_id,),
    ).fetchall()
    for row in rows:
        w = decay(row["weight"], row["observed_at"])
        total += w
        reasons.append(f"{row['detail']} (+{w:.0f})")

    if not rows:
        # シグナル無しは送信対象にしない. 0点ではなく明確な負にして弾く.
        total -= 40
        reasons.append("シグナルなし(送信対象外)")

    total = int(max(0, min(100, total)))
    return total, " / ".join(reasons)


def rescore_all(conn: sqlite3.Connection, icp: ICP | None = None) -> int:
    ids = [r["id"] for r in conn.execute("SELECT id FROM companies")]
    for cid in ids:
        score, reason = score_company(conn, cid, icp)
        conn.execute(
            "UPDATE companies SET score=?, score_reason=?, updated_at=datetime('now') "
            "WHERE id=?",
            (score, reason, cid),
        )
    conn.commit()
    return len(ids)


def calibrate(conn: sqlite3.Connection, min_samples: int = 30) -> dict[str, float]:
    """実績からシグナルの重みを測り直す.

    「そのシグナルを持つ会社の返信率 / 全体の返信率」を返す. 1.0 より大きい
    シグナルは効いている. 送信数が min_samples に満たないシグナルは、
    数字がブレるだけなので返さない.
    """
    base = conn.execute("""
        SELECT COUNT(*) AS sent,
               SUM(CASE WHEN m.replied_at IS NOT NULL THEN 1 ELSE 0 END) AS replied
        FROM messages m WHERE m.status='sent'
    """).fetchone()
    if not base["sent"]:
        return {}
    base_rate = (base["replied"] or 0) / base["sent"]
    if base_rate == 0:
        return {}

    rows = conn.execute("""
        SELECT s.kind,
               COUNT(*) AS sent,
               SUM(CASE WHEN m.replied_at IS NOT NULL THEN 1 ELSE 0 END) AS replied
        FROM messages m
        JOIN contacts c ON c.id = m.contact_id
        JOIN signals  s ON s.company_id = c.company_id
        WHERE m.status='sent'
        GROUP BY s.kind
    """).fetchall()

    out = {}
    for r in rows:
        if r["sent"] < min_samples:
            continue
        out[r["kind"]] = round(((r["replied"] or 0) / r["sent"]) / base_rate, 2)
    return out
