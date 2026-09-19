"""コマンドラインインターフェース.

日々の運用はこの5コマンドで回る:

    python3 -m cashman ingest data/leads.csv   # 朝: リードを入れる
    python3 -m cashman score                   # 朝: 点数を付け直す
    python3 -m cashman draft --limit 40        # 朝: 下書きを作る
    python3 -m cashman send                    # 朝: 枠の分だけ送る
    python3 -m cashman dashboard               # 夜: 数字を見る

返信が来たときだけ手を動かす:

    python3 -m cashman reply <message_id> "返信本文"
    python3 -m cashman deal open <company_id> --mrr 150000 --setup 300000
    python3 -m cashman deal advance <deal_id> won
"""

from __future__ import annotations

import argparse
import json
import os
import signal
import sys
import time
from pathlib import Path

from . import compose, db, metrics, pipeline, prospects, sender, signals


def _sender_identity() -> compose.SenderIdentity:
    """送信者情報は環境変数から読む. リポジトリに個人情報を置かないため."""
    return compose.SenderIdentity(
        person=os.environ.get("CASHMAN_SENDER_PERSON", "（氏名）"),
        company=os.environ.get("CASHMAN_SENDER_COMPANY", "（屋号 or 法人名）"),
        address=os.environ.get("CASHMAN_SENDER_ADDRESS", "（住所）"),
        email=os.environ.get("CASHMAN_FROM_EMAIL", "（送信元アドレス）"),
        optout_email=os.environ.get("CASHMAN_REPLY_TO",
                                    os.environ.get("CASHMAN_FROM_EMAIL", "（配信停止先）")),
        phone=os.environ.get("CASHMAN_SENDER_PHONE", ""),
        site=os.environ.get("CASHMAN_SENDER_SITE", ""),
    )


def _composer() -> compose.TemplateComposer:
    return compose.TemplateComposer(
        sender=_sender_identity(),
        offer_line=os.environ.get(
            "CASHMAN_OFFER_LINE",
            "受発注や請求まわりの転記作業を自動化し、月80時間の作業を"
            "3時間まで減らす仕組みを作っています。",
        ),
        case_line=os.environ.get(
            "CASHMAN_CASE_LINE",
            "同規模の卸売業で、受注メールから基幹システムへの転記を自動化し、"
            "月92時間かかっていた作業が4時間になりました。",
        ),
    )


# --- 各コマンド -------------------------------------------------------------


def cmd_init(args, conn):
    print(f"初期化しました: {args.db}")


def cmd_ingest(args, conn):
    result = prospects.ingest_csv(conn, args.path, args.source)
    print(result)
    n = signals.rescore_all(conn)
    print(f"{n} 社を再スコアリングしました")


def cmd_score(args, conn):
    icp = signals.ICP(employees_min=args.min_employees, employees_max=args.max_employees)
    n = signals.rescore_all(conn, icp)
    print(f"{n} 社をスコアリングしました\n")
    rows = conn.execute(
        "SELECT name, score, employees, industry, score_reason FROM companies "
        "ORDER BY score DESC LIMIT ?", (args.top,),
    ).fetchall()
    for r in rows:
        print(f"  [{r['score']:>3}] {r['name']}  ({r['industry'] or '-'} / "
              f"{r['employees'] or '?'}人)")
        print(f"        {r['score_reason']}")


def cmd_draft(args, conn):
    targets = prospects.sendable(conn, min_score=args.min_score, limit=args.limit)
    if not targets:
        print("送信対象がありません。ingest でリードを足すか --min-score を下げてください")
        return
    n = compose.draft_batch(conn, _composer(), targets, compose.SEQUENCE[0])
    f = compose.draft_followups(conn, _composer())
    print(f"初回 {n} 通 / フォロー {f} 通 の下書きを作成しました")
    if args.preview:
        row = conn.execute(
            "SELECT subject, body FROM messages WHERE status='draft' "
            "ORDER BY id DESC LIMIT 1").fetchone()
        print("\n" + "-" * 60)
        print(f"件名: {row['subject']}\n\n{row['body']}")
        print("-" * 60)


def cmd_send(args, conn):
    cfg = sender.SmtpConfig.from_env()
    live = args.live and cfg.is_configured()
    if args.live and not cfg.is_configured():
        print("SMTP が未設定です。CASHMAN_SMTP_HOST / _USER / _PASS / "
              "CASHMAN_FROM_EMAIL を設定してください。dry-run で続行します。",
              file=sys.stderr)
    if live:
        transport = sender.smtp_transport(cfg)
        pause = time.sleep
    else:
        transport = sender.dry_run_transport()
        pause = lambda _seconds: None  # dry-run では待たない
    report = sender.send_due(conn, transport, ceiling=args.cap, sleep=pause)
    print(("[本番]" if live else "[DRY-RUN]") + " " + str(report))
    cap = sender.daily_cap(sender.days_since_first_send(conn), args.cap)
    print(f"本日の上限 {cap} 通（送信開始から {sender.days_since_first_send(conn)} 日目）")


def cmd_reply(args, conn):
    kind = sender.record_reply(conn, args.message_id, args.text)
    print(f"返信を記録しました: {kind}")
    if kind == "optout":
        print("  -> 該当ドメインを配信停止に登録しました")
    elif kind == "positive":
        row = conn.execute(
            """SELECT c.company_id, co.name FROM messages m
               JOIN contacts c ON c.id=m.contact_id
               JOIN companies co ON co.id=c.company_id WHERE m.id=?""",
            (args.message_id,)).fetchone()
        print(f"  -> 商談化してください: "
              f"python3 -m cashman deal open {row['company_id']}  # {row['name']}")


def cmd_deal(args, conn):
    if args.action == "open":
        deal_id = pipeline.open_deal(conn, args.id, args.mrr, args.setup)
        print(f"商談を作成しました: deal_id={deal_id}")
    elif args.action == "advance":
        pipeline.advance(conn, args.id, args.stage, args.mrr or None,
                         args.setup or None, args.reason)
        print(f"deal {args.id} を {pipeline.STAGE_LABELS.get(args.stage, args.stage)} に更新しました")
    else:  # board
        rows = pipeline.board(conn)
        if not rows:
            print("進行中の商談はありません")
            return
        print(f"{'ID':>4} {'ステージ':<10} {'会社':<24} {'MRR':>10} {'放置':>5}")
        print("-" * 62)
        for r in rows:
            mark = " ←要対応" if r["stale_days"] >= 7 else ""
            print(f"{r['id']:>4} {pipeline.STAGE_LABELS[r['stage']]:<10} "
                  f"{r['company_name'][:22]:<24} {r['mrr']:>10,} "
                  f"{r['stale_days']:>3}日{mark}")
        fc = pipeline.forecast(conn)
        print("-" * 62)
        print(f"確度加重の見込みMRR: {fc.weighted_mrr:,}円 / "
              f"初期費 {fc.weighted_setup:,}円 / 進行中 {fc.open_count}件")


def cmd_dashboard(args, conn):
    print(metrics.dashboard(conn, args.target, args.margin))
    cal = signals.calibrate(conn)
    if cal:
        print("\n  ■ シグナル別の効き方（1.0 = 平均並み）")
        for kind, lift in sorted(cal.items(), key=lambda kv: -kv[1]):
            bar = "#" * int(min(lift, 3.0) * 10)
            print(f"    {kind:<16} {lift:>5.2f}  {bar}")


def cmd_suppress(args, conn):
    prospects.suppress(conn, args.pattern, args.reason)
    print(f"配信停止に登録しました: {args.pattern}")


def cmd_export(args, conn):
    rows = conn.execute(
        "SELECT * FROM companies ORDER BY score DESC").fetchall()
    print(json.dumps([dict(r) for r in rows], ensure_ascii=False, indent=2))


# --- パーサ -----------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="cashman", description="アウトバウンド営業システム")
    p.add_argument("--db", default=str(db.DEFAULT_DB))
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("init", help="DBを作る").set_defaults(func=cmd_init)

    g = sub.add_parser("ingest", help="CSVからリードを取り込む")
    g.add_argument("path", type=Path)
    g.add_argument("--source", default=None)
    g.set_defaults(func=cmd_ingest)

    g = sub.add_parser("score", help="スコアを計算し直す")
    g.add_argument("--top", type=int, default=10)
    g.add_argument("--min-employees", type=int, default=30)
    g.add_argument("--max-employees", type=int, default=300)
    g.set_defaults(func=cmd_score)

    g = sub.add_parser("draft", help="送信下書きを作る")
    g.add_argument("--limit", type=int, default=40)
    g.add_argument("--min-score", type=int, default=45)
    g.add_argument("--preview", action="store_true")
    g.set_defaults(func=cmd_draft)

    g = sub.add_parser("send", help="下書きを送る（既定はdry-run）")
    g.add_argument("--live", action="store_true", help="実際に送信する")
    g.add_argument("--cap", type=int, default=sender.WARMUP_CEILING)
    g.set_defaults(func=cmd_send)

    g = sub.add_parser("reply", help="返信を記録する")
    g.add_argument("message_id", type=int)
    g.add_argument("text")
    g.set_defaults(func=cmd_reply)

    g = sub.add_parser("deal", help="商談を操作する")
    g.add_argument("action", choices=["open", "advance", "board"])
    g.add_argument("id", type=int, nargs="?", default=0)
    g.add_argument("stage", nargs="?", default="")
    g.add_argument("--mrr", type=int, default=0)
    g.add_argument("--setup", type=int, default=0)
    g.add_argument("--reason", default=None)
    g.set_defaults(func=cmd_deal)

    g = sub.add_parser("dashboard", help="KPIを表示する")
    g.add_argument("--target", type=int, default=100_000, help="目標日給")
    g.add_argument("--margin", type=float, default=0.85)
    g.set_defaults(func=cmd_dashboard)

    g = sub.add_parser("suppress", help="配信停止に登録する")
    g.add_argument("pattern", help="アドレス、または @example.co.jp")
    g.add_argument("--reason", default="手動")
    g.set_defaults(func=cmd_suppress)

    sub.add_parser("export", help="会社一覧をJSONで出す").set_defaults(func=cmd_export)
    return p


def main(argv: list[str] | None = None) -> int:
    # `... | head` のように出力を途中で打ち切られても落ちないようにする。
    # 既定の Python は SIGPIPE を BrokenPipeError に変えるので、OS の既定に戻す。
    if hasattr(signal, "SIGPIPE"):
        signal.signal(signal.SIGPIPE, signal.SIG_DFL)

    args = build_parser().parse_args(argv)
    conn = db.init(args.db)
    try:
        args.func(args, conn)
    except (ValueError, KeyError) as exc:
        print(f"エラー: {exc}", file=sys.stderr)
        return 1
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
