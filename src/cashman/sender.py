"""送信. レート制御・ドメインウォームアップ・配信停止の自動処理.

ここで一番大事なのはウォームアップ. 新しいドメインからいきなり1日40通
出すと、数日でスパム判定されてドメインが焼ける. 一度焼けると同じドメインは
二度と使えず、リスト作りからやり直しになる.

  Day 1-3    :  5通/日
  Day 4-7    : 10通/日
  Day 8-14   : 20通/日
  Day 15-21  : 30通/日
  Day 22-    : 40通/日(上限)

送信ドメインは本番サイトと分ける. 例えば本番が example.co.jp なら
営業には example-jp.com のような別ドメインを使い、焼けても本業のメールが
届かなくなる事故を避ける.
"""

from __future__ import annotations

import os
import random
import re
import smtplib
import sqlite3
import time
from dataclasses import dataclass
from email.message import EmailMessage
from email.utils import formataddr, formatdate, make_msgid
from typing import Callable

from . import db, prospects

# --- ウォームアップ ---------------------------------------------------------

WARMUP_SCHEDULE: list[tuple[int, int]] = [
    (3, 5), (7, 10), (14, 20), (21, 30),
]
WARMUP_CEILING = 40


def daily_cap(days_since_start: int, ceiling: int = WARMUP_CEILING) -> int:
    """送信開始からの経過日数に応じた1日の上限通数."""
    for until_day, cap in WARMUP_SCHEDULE:
        if days_since_start <= until_day:
            return min(cap, ceiling)
    return ceiling


def sent_today(conn: sqlite3.Connection) -> int:
    row = conn.execute(
        "SELECT COUNT(*) AS n FROM messages "
        "WHERE status='sent' AND date(sent_at)=date('now')"
    ).fetchone()
    return int(row["n"])


def days_since_first_send(conn: sqlite3.Connection) -> int:
    row = conn.execute(
        "SELECT CAST(julianday('now') - julianday(MIN(sent_at)) AS INTEGER) AS d "
        "FROM messages WHERE status='sent'"
    ).fetchone()
    return int(row["d"]) if row and row["d"] is not None else 0


def remaining_quota(conn: sqlite3.Connection, ceiling: int = WARMUP_CEILING) -> int:
    return max(0, daily_cap(days_since_first_send(conn), ceiling) - sent_today(conn))


# --- 送信トランスポート -----------------------------------------------------


@dataclass
class SmtpConfig:
    host: str = ""
    port: int = 587
    username: str = ""
    password: str = ""
    from_email: str = ""
    from_name: str = ""
    reply_to: str = ""

    @classmethod
    def from_env(cls) -> "SmtpConfig":
        return cls(
            host=os.environ.get("CASHMAN_SMTP_HOST", ""),
            port=int(os.environ.get("CASHMAN_SMTP_PORT", "587")),
            username=os.environ.get("CASHMAN_SMTP_USER", ""),
            password=os.environ.get("CASHMAN_SMTP_PASS", ""),
            from_email=os.environ.get("CASHMAN_FROM_EMAIL", ""),
            from_name=os.environ.get("CASHMAN_FROM_NAME", ""),
            reply_to=os.environ.get("CASHMAN_REPLY_TO", ""),
        )

    def is_configured(self) -> bool:
        return bool(self.host and self.username and self.from_email)


def build_message(cfg: SmtpConfig, to_email: str, subject: str, body: str) -> EmailMessage:
    msg = EmailMessage()
    msg["From"] = formataddr((cfg.from_name, cfg.from_email)) if cfg.from_name else cfg.from_email
    msg["To"] = to_email
    msg["Subject"] = subject
    msg["Date"] = formatdate(localtime=True)
    msg["Message-ID"] = make_msgid()
    if cfg.reply_to:
        msg["Reply-To"] = cfg.reply_to
    # 一斉配信であることを機械可読にしておく. 受信側のフィルタに対して誠実に振る舞うほど
    # 長期の到達率は上がる.
    msg["List-Unsubscribe"] = f"<mailto:{cfg.reply_to or cfg.from_email}?subject=unsubscribe>"
    msg.set_content(body)
    return msg


def smtp_transport(cfg: SmtpConfig) -> Callable[[str, str, str], None]:
    def send(to_email: str, subject: str, body: str) -> None:
        msg = build_message(cfg, to_email, subject, body)
        with smtplib.SMTP(cfg.host, cfg.port, timeout=30) as smtp:
            smtp.starttls()
            smtp.login(cfg.username, cfg.password)
            smtp.send_message(msg)
    return send


def dry_run_transport(out=print) -> Callable[[str, str, str], None]:
    def send(to_email: str, subject: str, body: str) -> None:
        out(f"[DRY-RUN] to={to_email} subject={subject}")
    return send


# --- 送信ループ -------------------------------------------------------------


@dataclass
class SendReport:
    sent: int = 0
    skipped_suppressed: int = 0
    failed: int = 0
    quota_left: int = 0

    def __str__(self) -> str:
        return (f"送信 {self.sent} / 除外 {self.skipped_suppressed} / "
                f"失敗 {self.failed} / 本日の残枠 {self.quota_left}")


def send_due(conn: sqlite3.Connection, transport: Callable[[str, str, str], None],
             ceiling: int = WARMUP_CEILING, min_gap: float = 8.0,
             max_gap: float = 45.0, sleep: Callable[[float], None] = time.sleep,
             ) -> SendReport:
    """送信予定日が来た下書きを、上限の範囲で送る.

    送信間隔はランダムに空ける. 一定間隔で秒単位に並ぶと機械送信だと
    判定されやすい. dry-run では sleep を差し替えて待たずに流せる.
    """
    report = SendReport()
    quota = remaining_quota(conn, ceiling)
    if quota <= 0:
        report.quota_left = 0
        return report

    rows = conn.execute(
        """SELECT m.id, m.subject, m.body, c.email
           FROM messages m JOIN contacts c ON c.id = m.contact_id
           WHERE m.status='draft'
             AND (m.scheduled_for IS NULL OR date(m.scheduled_for) <= date('now'))
           ORDER BY m.step DESC, m.id ASC
           LIMIT ?""",
        (quota,),
    ).fetchall()

    for i, row in enumerate(rows):
        reason = prospects.is_suppressed(conn, row["email"])
        if reason:
            conn.execute("UPDATE messages SET status='suppressed' WHERE id=?", (row["id"],))
            report.skipped_suppressed += 1
            continue
        try:
            transport(row["email"], row["subject"], row["body"])
        except Exception as exc:
            conn.execute("UPDATE messages SET status='failed' WHERE id=?", (row["id"],))
            db.log(conn, "messages", row["id"], "send_failed", str(exc)[:500])
            report.failed += 1
            continue

        conn.execute(
            "UPDATE messages SET status='sent', sent_at=datetime('now') WHERE id=?",
            (row["id"],),
        )
        report.sent += 1
        if i < len(rows) - 1:
            sleep(random.uniform(min_gap, max_gap))

    conn.commit()
    report.quota_left = remaining_quota(conn, ceiling)
    return report


# --- 返信の取り込み ---------------------------------------------------------

_OPTOUT = re.compile(
    r"(配信停止|配信不要|今後.{0,6}不要|送らないで|停止してください|"
    r"お断り|受信拒否|unsubscribe|remove me)"
)
_POSITIVE = re.compile(
    r"(資料.{0,4}(希望|ください|お願い|送っ)|興味|詳し[くい]|話.{0,4}聞|"
    r"打ち合わせ|面談|商談|日程|ミーティング|お時間)"
)
_NEGATIVE = re.compile(r"(間に合って|不要|見送り|他社|予定はありま|必要ありま)")


def classify_reply(text: str) -> str:
    """返信を4種類に分ける.

    optout は最優先. 「興味はあるが今は不要」のような文面でも、
    停止の意思が読めたら必ず止める側に倒す.
    """
    if _OPTOUT.search(text):
        return "optout"
    if _NEGATIVE.search(text):
        return "negative"
    if _POSITIVE.search(text):
        return "positive"
    return "neutral"


def record_reply(conn: sqlite3.Connection, message_id: int, text: str) -> str:
    """返信を記録する. optout なら即座に配信停止へ登録する."""
    row = conn.execute(
        """SELECT m.id, c.email, c.company_id FROM messages m
           JOIN contacts c ON c.id=m.contact_id WHERE m.id=?""",
        (message_id,),
    ).fetchone()
    if row is None:
        raise KeyError(f"message {message_id} not found")

    kind = classify_reply(text)
    conn.execute(
        "UPDATE messages SET replied_at=datetime('now'), reply_kind=? WHERE id=?",
        (kind, message_id),
    )
    # 返信が来た相手への未送信フォローは全部止める. 会話が始まった後に
    # 自動フォローが飛ぶのが、この手の仕組みで最も信用を失う事故.
    conn.execute(
        """UPDATE messages SET status='cancelled'
           WHERE contact_id=(SELECT contact_id FROM messages WHERE id=?)
             AND status='draft'""",
        (message_id,),
    )
    if kind == "optout":
        domain = row["email"].split("@")[-1]
        prospects.suppress(conn, f"@{domain}", "返信による配信停止依頼")
    db.log(conn, "messages", message_id, "reply", kind)
    conn.commit()
    return kind
