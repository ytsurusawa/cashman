"""リードの取り込み・名寄せ・送信除外.

リスト作りで事故る箇所は3つしかない:
  1. 同じ会社に二重に送る          -> domain で名寄せする
  2. 配信停止した相手に再送する    -> suppressions を送信前に必ず引く
  3. 個人のアドレスに送る          -> is_public=0 は送信対象から外す

3つ目は法律の話. 特定電子メール法は原則オプトインだが、
「自己の電子メールアドレスを公表している営業を営む団体・個人」宛ては
適用除外(法3条1項4号). つまり会社サイトに載っている info@ や
部署代表アドレスは送ってよく、個人が非公開で使うアドレスは駄目.
このモジュールはその線引きをデータ構造として持つ.
"""

from __future__ import annotations

import csv
import re
import sqlite3
from dataclasses import dataclass
from pathlib import Path

from . import db

_EMAIL = re.compile(r"^[^@\s]+@[^@\s]+\.[A-Za-z]{2,}$")

# 公開の代表アドレスとみなせるローカルパート.
# ここに該当しないものは個人アドレスの可能性があるので既定で送らない.
_PUBLIC_LOCALPARTS = {
    "info", "contact", "support", "sales", "inquiry", "office", "mail",
    "recruit", "hr", "jinji", "soumu", "otoiawase", "cs", "help", "admin",
}


@dataclass
class IngestResult:
    companies_added: int = 0
    companies_updated: int = 0
    contacts_added: int = 0
    signals_added: int = 0
    skipped_suppressed: int = 0
    skipped_invalid: int = 0

    def __str__(self) -> str:
        return (
            f"会社 +{self.companies_added} (更新 {self.companies_updated}) / "
            f"連絡先 +{self.contacts_added} / シグナル +{self.signals_added} / "
            f"除外 {self.skipped_suppressed} / 不正 {self.skipped_invalid}"
        )


def normalize_domain(value: str | None) -> str | None:
    if not value:
        return None
    v = value.strip().lower()
    v = re.sub(r"^https?://", "", v)
    v = re.sub(r"^www\.", "", v)
    v = v.split("/")[0].split("?")[0]
    return v or None


def is_public_address(email: str) -> bool:
    local = email.split("@")[0].lower()
    local = re.sub(r"[._-]?\d+$", "", local)
    return local in _PUBLIC_LOCALPARTS


def is_suppressed(conn: sqlite3.Connection, email: str) -> str | None:
    """送信除外に該当すれば理由を返す. アドレス完全一致とドメイン一致を見る."""
    email = email.strip().lower()
    domain = email.split("@")[-1]
    row = conn.execute(
        "SELECT reason FROM suppressions WHERE pattern=? OR pattern=?",
        (email, f"@{domain}"),
    ).fetchone()
    return row["reason"] if row else None


def suppress(conn: sqlite3.Connection, pattern: str, reason: str) -> None:
    """配信停止に登録する. 会社ごと止めるときは '@example.co.jp' の形で渡す.

    特定電子メール法は受信拒否の通知を受けたら以後の送信を禁じている.
    返信に「不要」「配信停止」が含まれていたら必ずここを通すこと.
    """
    conn.execute(
        "INSERT OR IGNORE INTO suppressions (pattern, reason) VALUES (?,?)",
        (pattern.strip().lower(), reason),
    )
    db.log(conn, "suppression", None, "added", f"{pattern}: {reason}")
    conn.commit()


def upsert_company(conn: sqlite3.Connection, *, name: str, domain: str | None,
                   source: str, industry: str | None = None,
                   employees: int | None = None, prefecture: str | None = None,
                   corporate_number: str | None = None) -> tuple[int, bool]:
    """会社を追加または更新する. 戻り値は (id, 新規かどうか)."""
    domain = normalize_domain(domain)
    existing = None
    if domain:
        existing = conn.execute(
            "SELECT id FROM companies WHERE domain=?", (domain,)
        ).fetchone()
    if existing is None:
        existing = conn.execute(
            "SELECT id FROM companies WHERE name=? AND domain IS NULL", (name,)
        ).fetchone()

    if existing:
        conn.execute(
            """UPDATE companies SET
                 industry        = COALESCE(?, industry),
                 employees       = COALESCE(?, employees),
                 prefecture      = COALESCE(?, prefecture),
                 corporate_number= COALESCE(?, corporate_number),
                 updated_at      = datetime('now')
               WHERE id=?""",
            (industry, employees, prefecture, corporate_number, existing["id"]),
        )
        return existing["id"], False

    cur = conn.execute(
        """INSERT INTO companies
             (name, domain, corporate_number, industry, employees, prefecture, source)
           VALUES (?,?,?,?,?,?,?)""",
        (name, domain, corporate_number, industry, employees, prefecture, source),
    )
    return int(cur.lastrowid), True


def add_contact(conn: sqlite3.Connection, company_id: int, email: str,
                name: str | None = None, title: str | None = None,
                source: str = "manual") -> int | None:
    """連絡先を追加する. 除外対象と不正アドレスはここで弾く."""
    email = email.strip().lower()
    if not _EMAIL.match(email):
        return None
    if is_suppressed(conn, email):
        return None
    existing = conn.execute("SELECT id FROM contacts WHERE email=?", (email,)).fetchone()
    if existing:
        return int(existing["id"])
    cur = conn.execute(
        """INSERT INTO contacts (company_id, name, title, email, is_public, source)
           VALUES (?,?,?,?,?,?)""",
        (company_id, name, title, email, 1 if is_public_address(email) else 0, source),
    )
    return int(cur.lastrowid)


def add_signal(conn: sqlite3.Connection, company_id: int, sig) -> bool:
    """シグナルを1件保存する. 同一内容の重複は無視される."""
    cur = conn.execute(
        """INSERT OR IGNORE INTO signals
             (company_id, kind, detail, url, weight, observed_at)
           VALUES (?,?,?,?,?, COALESCE(?, datetime('now')))""",
        (company_id, sig.kind, sig.detail, sig.url,
         sig.resolved_weight(), sig.observed_at),
    )
    return cur.rowcount > 0


def ingest_csv(conn: sqlite3.Connection, path: Path | str,
               source: str | None = None) -> IngestResult:
    """CSVからリードを取り込む.

    想定する列(足りないものは空でよい):
        name, domain, email, contact_name, title, industry, employees,
        prefecture, corporate_number, job_title, job_body, job_url, posted_at

    job_* が入っていれば求人票としてシグナル抽出まで一気に通す.
    """
    from .signals import from_job_posting, detect_repeat_hiring

    path = Path(path)
    source = source or path.name
    result = IngestResult()
    # 会社ごとに求人を溜めておき、取り込みの最後に「繰り返し募集」を判定する。
    # これは1行だけ見ても分からず、同じ会社の複数行を突き合わせて初めて出る。
    postings_by_company: dict[int, list[dict]] = {}

    with path.open(encoding="utf-8-sig", newline="") as fh:
        for row in csv.DictReader(fh):
            name = (row.get("name") or "").strip()
            if not name:
                result.skipped_invalid += 1
                continue

            employees = row.get("employees")
            try:
                employees = int(employees) if employees else None
            except ValueError:
                employees = None

            cid, created = upsert_company(
                conn, name=name, domain=row.get("domain"), source=source,
                industry=(row.get("industry") or "").strip() or None,
                employees=employees,
                prefecture=(row.get("prefecture") or "").strip() or None,
                corporate_number=(row.get("corporate_number") or "").strip() or None,
            )
            if created:
                result.companies_added += 1
            else:
                result.companies_updated += 1

            email = (row.get("email") or "").strip()
            if email:
                if is_suppressed(conn, email):
                    result.skipped_suppressed += 1
                elif add_contact(conn, cid, email, row.get("contact_name"),
                                 row.get("title"), source):
                    result.contacts_added += 1
                else:
                    result.skipped_invalid += 1

            job_title = (row.get("job_title") or "").strip()
            if job_title:
                for sig in from_job_posting(
                    job_title, row.get("job_body") or "",
                    row.get("job_url"), row.get("posted_at"),
                ):
                    if add_signal(conn, cid, sig):
                        result.signals_added += 1
                postings_by_company.setdefault(cid, []).append({
                    "title": job_title,
                    "url": row.get("job_url"),
                    "posted_at": row.get("posted_at"),
                })

    for cid, postings in postings_by_company.items():
        for sig in detect_repeat_hiring(postings):
            if add_signal(conn, cid, sig):
                result.signals_added += 1

    conn.commit()
    return result


def sendable(conn: sqlite3.Connection, min_score: int = 45,
             limit: int = 50) -> list[sqlite3.Row]:
    """今日送ってよい相手をスコア順に返す.

    条件は4つ全部を満たすもの:
      - スコアが閾値以上(= シグナルがあり、ICPに合う)
      - 公表アドレスである
      - 除外リストに載っていない
      - まだ一度も送っていない
    """
    return conn.execute(
        """
        SELECT c.id AS contact_id, c.email, c.name AS contact_name, c.title,
               co.id AS company_id, co.name AS company_name, co.score,
               co.score_reason, co.industry, co.employees
        FROM contacts c
        JOIN companies co ON co.id = c.company_id
        WHERE c.is_public = 1
          AND co.score >= ?
          AND NOT EXISTS (SELECT 1 FROM messages m WHERE m.contact_id = c.id)
          AND NOT EXISTS (
                SELECT 1 FROM suppressions s
                WHERE s.pattern = c.email
                   OR s.pattern = '@' || substr(c.email, instr(c.email,'@') + 1)
          )
        ORDER BY co.score DESC, co.updated_at DESC
        LIMIT ?
        """,
        (min_score, limit),
    ).fetchall()
