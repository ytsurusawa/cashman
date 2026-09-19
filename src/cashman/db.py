"""SQLite スキーマと接続ヘルパ.

外部DBは使わない. 顧客数が3桁に乗るまでSQLiteで足りるし、
ファイル1つで済むほうがバックアップも移行も速い.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

DEFAULT_DB = Path("data/cashman.db")

SCHEMA = """
PRAGMA journal_mode = WAL;
PRAGMA foreign_keys = ON;

-- 会社. domain を実質的な一意キーとして使う.
CREATE TABLE IF NOT EXISTS companies (
    id                INTEGER PRIMARY KEY,
    name              TEXT NOT NULL,
    domain            TEXT UNIQUE,
    corporate_number  TEXT,
    industry          TEXT,
    employees         INTEGER,
    prefecture        TEXT,
    source            TEXT NOT NULL,
    score             INTEGER NOT NULL DEFAULT 0,
    score_reason      TEXT,
    status            TEXT NOT NULL DEFAULT 'new',
    created_at        TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at        TEXT NOT NULL DEFAULT (datetime('now'))
);

-- シグナル: 「今この会社が困っている」ことの外形的な証拠.
-- これが無い会社には送らない. 文面の一行目がこれで決まる.
CREATE TABLE IF NOT EXISTS signals (
    id          INTEGER PRIMARY KEY,
    company_id  INTEGER NOT NULL REFERENCES companies(id) ON DELETE CASCADE,
    kind        TEXT NOT NULL,
    detail      TEXT NOT NULL,
    url         TEXT,
    weight      INTEGER NOT NULL DEFAULT 10,
    observed_at TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE(company_id, kind, detail)
);

CREATE TABLE IF NOT EXISTS contacts (
    id          INTEGER PRIMARY KEY,
    company_id  INTEGER NOT NULL REFERENCES companies(id) ON DELETE CASCADE,
    name        TEXT,
    title       TEXT,
    email       TEXT NOT NULL UNIQUE,
    is_public   INTEGER NOT NULL DEFAULT 1,  -- 公表アドレスか(特定電子メール法の適用除外判定)
    source      TEXT NOT NULL,
    created_at  TEXT NOT NULL DEFAULT (datetime('now'))
);

-- 送信予定/送信済みメール. step は何通目か(1=初回, 2以降=フォロー).
CREATE TABLE IF NOT EXISTS messages (
    id            INTEGER PRIMARY KEY,
    contact_id    INTEGER NOT NULL REFERENCES contacts(id) ON DELETE CASCADE,
    step          INTEGER NOT NULL DEFAULT 1,
    subject       TEXT NOT NULL,
    body          TEXT NOT NULL,
    status        TEXT NOT NULL DEFAULT 'draft',
    scheduled_for TEXT,
    sent_at       TEXT,
    replied_at    TEXT,
    reply_kind    TEXT,
    created_at    TEXT NOT NULL DEFAULT (datetime('now'))
);

-- 送ってはいけない宛先. 解除依頼・競合・既存顧客・バウンス.
-- email 完全一致か、@つきドメインで会社ごと除外できる.
CREATE TABLE IF NOT EXISTS suppressions (
    id         INTEGER PRIMARY KEY,
    pattern    TEXT NOT NULL UNIQUE,
    reason     TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS deals (
    id          INTEGER PRIMARY KEY,
    company_id  INTEGER NOT NULL REFERENCES companies(id) ON DELETE CASCADE,
    stage       TEXT NOT NULL DEFAULT 'meeting_set',
    mrr         INTEGER NOT NULL DEFAULT 0,
    setup_fee   INTEGER NOT NULL DEFAULT 0,
    lost_reason TEXT,
    created_at  TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at  TEXT NOT NULL DEFAULT (datetime('now')),
    closed_at   TEXT
);

-- 監査ログ. あとで「なぜこの数字になったか」を追うためだけに使う.
CREATE TABLE IF NOT EXISTS events (
    id          INTEGER PRIMARY KEY,
    entity      TEXT NOT NULL,
    entity_id   INTEGER,
    kind        TEXT NOT NULL,
    payload     TEXT,
    created_at  TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_signals_company  ON signals(company_id);
CREATE INDEX IF NOT EXISTS idx_contacts_company ON contacts(company_id);
CREATE INDEX IF NOT EXISTS idx_messages_contact ON messages(contact_id);
CREATE INDEX IF NOT EXISTS idx_messages_status  ON messages(status);
CREATE INDEX IF NOT EXISTS idx_companies_score  ON companies(score DESC);
CREATE INDEX IF NOT EXISTS idx_deals_stage      ON deals(stage);
"""

# 商談のステージ. 左から右にしか進まない(戻すときは lost にする).
STAGES = ["meeting_set", "meeting_done", "proposal", "won", "lost"]


def connect(path: Path | str = DEFAULT_DB) -> sqlite3.Connection:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init(path: Path | str = DEFAULT_DB) -> sqlite3.Connection:
    conn = connect(path)
    conn.executescript(SCHEMA)
    conn.commit()
    return conn


def log(conn: sqlite3.Connection, entity: str, entity_id: int | None,
        kind: str, payload: str | None = None) -> None:
    conn.execute(
        "INSERT INTO events (entity, entity_id, kind, payload) VALUES (?,?,?,?)",
        (entity, entity_id, kind, payload),
    )
