"""壊れると金か信用を失う箇所だけをテストする.

網羅率は狙わない. 「配信停止した相手に再送する」「返信済みに追撃を送る」は
一度やると取り返しがつかないので、そこを重点的に押さえる.
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from cashman import compose, db, metrics, pipeline, prospects, sender, signals  # noqa: E402


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.conn = db.init(Path(self.tmp.name) / "t.db")

    def tearDown(self):
        self.conn.close()
        self.tmp.cleanup()

    def make_company(self, name="テスト商事", domain="test.example.jp",
                     employees=100, industry="卸売", email="info@test.example.jp"):
        cid, _ = prospects.upsert_company(
            self.conn, name=name, domain=domain, source="test",
            industry=industry, employees=employees)
        contact_id = prospects.add_contact(self.conn, cid, email, source="test")
        self.conn.commit()
        return cid, contact_id

    def composer(self):
        s = compose.SenderIdentity(person="テスト", company="テスト社",
                                   address="東京", email="a@b.jp", optout_email="a@b.jp")
        return compose.TemplateComposer(s, "オファー文", "事例文")


class TestSuppression(Base):
    def test_suppressed_email_is_never_drafted(self):
        cid, _ = self.make_company()
        prospects.add_signal(self.conn, cid, signals.Signal("hiring_ops", "事務募集"))
        signals.rescore_all(self.conn)
        self.assertTrue(prospects.sendable(self.conn))

        prospects.suppress(self.conn, "info@test.example.jp", "本人希望")
        self.assertEqual(prospects.sendable(self.conn), [],
                         "配信停止済みのアドレスが送信候補に残っている")

    def test_domain_level_suppression_blocks_all_addresses(self):
        cid, _ = self.make_company(email="info@blocked.example.jp",
                                   domain="blocked.example.jp")
        prospects.suppress(self.conn, "@blocked.example.jp", "会社ごと停止")
        self.assertIsNotNone(
            prospects.is_suppressed(self.conn, "sales@blocked.example.jp"),
            "同一ドメインの別アドレスが素通りしている")
        self.assertIsNone(prospects.add_contact(
            self.conn, cid, "newperson@blocked.example.jp", source="test"))

    def test_suppressed_draft_is_not_sent(self):
        cid, contact_id = self.make_company()
        prospects.add_signal(self.conn, cid, signals.Signal("hiring_ops", "事務募集"))
        signals.rescore_all(self.conn)
        compose.draft_batch(self.conn, self.composer(),
                            prospects.sendable(self.conn))
        # 下書きを作った後に停止依頼が来た場合でも送ってはいけない
        prospects.suppress(self.conn, "@test.example.jp", "停止依頼")
        report = sender.send_due(self.conn, sender.dry_run_transport(lambda *_: None),
                                 sleep=lambda _: None)
        self.assertEqual(report.sent, 0)
        self.assertEqual(report.skipped_suppressed, 1)

    def test_optout_reply_suppresses_domain(self):
        cid, _ = self.make_company()
        prospects.add_signal(self.conn, cid, signals.Signal("hiring_ops", "事務募集"))
        signals.rescore_all(self.conn)
        compose.draft_batch(self.conn, self.composer(), prospects.sendable(self.conn))
        sender.send_due(self.conn, sender.dry_run_transport(lambda *_: None),
                        sleep=lambda _: None)
        msg_id = self.conn.execute("SELECT id FROM messages").fetchone()["id"]

        sender.record_reply(self.conn, msg_id, "配信停止をお願いします")
        self.assertIsNotNone(prospects.is_suppressed(self.conn, "info@test.example.jp"))


class TestFollowupSafety(Base):
    def test_no_followup_after_reply(self):
        cid, _ = self.make_company()
        prospects.add_signal(self.conn, cid, signals.Signal("hiring_ops", "事務募集"))
        signals.rescore_all(self.conn)
        compose.draft_batch(self.conn, self.composer(), prospects.sendable(self.conn))
        sender.send_due(self.conn, sender.dry_run_transport(lambda *_: None),
                        sleep=lambda _: None)
        msg_id = self.conn.execute("SELECT id FROM messages").fetchone()["id"]
        # 20日前に送ったことにして、フォローの条件を満たさせる
        self.conn.execute(
            "UPDATE messages SET sent_at=datetime('now','-20 day') WHERE id=?", (msg_id,))
        self.conn.commit()

        sender.record_reply(self.conn, msg_id, "興味があります")
        made = compose.draft_followups(self.conn, self.composer())
        self.assertEqual(made, 0, "返信済みの相手にフォローの下書きが作られた")

    def test_followup_generated_when_no_reply(self):
        cid, _ = self.make_company()
        prospects.add_signal(self.conn, cid, signals.Signal("hiring_ops", "事務募集"))
        signals.rescore_all(self.conn)
        compose.draft_batch(self.conn, self.composer(), prospects.sendable(self.conn))
        sender.send_due(self.conn, sender.dry_run_transport(lambda *_: None),
                        sleep=lambda _: None)
        self.conn.execute("UPDATE messages SET sent_at=datetime('now','-5 day')")
        self.conn.commit()
        self.assertEqual(compose.draft_followups(self.conn, self.composer()), 1)

    def test_reply_cancels_pending_drafts(self):
        cid, contact_id = self.make_company()
        self.conn.execute(
            "INSERT INTO messages (contact_id, step, subject, body, status) "
            "VALUES (?,1,'s','b','sent')", (contact_id,))
        self.conn.execute(
            "UPDATE messages SET sent_at=datetime('now') WHERE contact_id=?", (contact_id,))
        self.conn.execute(
            "INSERT INTO messages (contact_id, step, subject, body, status) "
            "VALUES (?,2,'s2','b2','draft')", (contact_id,))
        self.conn.commit()
        msg_id = self.conn.execute(
            "SELECT id FROM messages WHERE step=1").fetchone()["id"]

        sender.record_reply(self.conn, msg_id, "詳しく聞かせてください")
        pending = self.conn.execute(
            "SELECT status FROM messages WHERE step=2").fetchone()["status"]
        self.assertEqual(pending, "cancelled", "返信後も未送信のフォローが残っている")


class TestWarmup(Base):
    def test_cap_grows_over_time(self):
        self.assertEqual(sender.daily_cap(1), 5)
        self.assertEqual(sender.daily_cap(5), 10)
        self.assertEqual(sender.daily_cap(12), 20)
        self.assertEqual(sender.daily_cap(20), 30)
        self.assertEqual(sender.daily_cap(30), 40)

    def test_cap_is_monotonic(self):
        caps = [sender.daily_cap(d) for d in range(0, 60)]
        self.assertEqual(caps, sorted(caps), "上限が途中で下がっている")

    def test_send_respects_daily_cap(self):
        for i in range(20):
            cid, contact_id = self.make_company(
                name=f"社{i}", domain=f"d{i}.example.jp", email=f"info@d{i}.example.jp")
            self.conn.execute(
                "INSERT INTO messages (contact_id, step, subject, body, status) "
                "VALUES (?,1,'s','b','draft')", (contact_id,))
        self.conn.commit()
        report = sender.send_due(self.conn, sender.dry_run_transport(lambda *_: None),
                                 sleep=lambda _: None)
        self.assertEqual(report.sent, 5, "初日から上限を超えて送信している")


class TestScoring(Base):
    def test_company_without_signal_is_not_sendable(self):
        self.make_company()
        signals.rescore_all(self.conn)
        self.assertEqual(prospects.sendable(self.conn), [])

    def test_excluded_industry_is_filtered(self):
        cid, _ = self.make_company(name="学校法人A", domain="school.example.jp",
                                   industry="学校", email="info@school.example.jp")
        prospects.add_signal(self.conn, cid, signals.Signal("hiring_ops", "事務募集"))
        signals.rescore_all(self.conn)
        self.assertEqual(prospects.sendable(self.conn), [])

    def test_private_address_is_not_sendable(self):
        cid, _ = prospects.upsert_company(
            self.conn, name="個人宛", domain="priv.example.jp", source="t",
            industry="卸売", employees=100)
        prospects.add_contact(self.conn, cid, "yamada.taro@priv.example.jp", source="t")
        prospects.add_signal(self.conn, cid, signals.Signal("hiring_ops", "事務募集"))
        self.conn.commit()
        signals.rescore_all(self.conn)
        self.assertEqual(prospects.sendable(self.conn), [],
                         "個人アドレスが送信候補に入っている")

    def test_signal_decays_over_time(self):
        fresh = signals.decay(30, None)
        old = signals.decay(30, "2020-01-01")
        self.assertLess(old, fresh * 0.01, "古いシグナルが減衰していない")

    def test_repeat_hiring_detected(self):
        found = signals.detect_repeat_hiring([
            {"title": "営業事務【急募】", "posted_at": "2026-09-01"},
            {"title": "営業事務（正社員）", "posted_at": "2026-08-01"},
        ])
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0].kind, "hiring_repeat")


class TestPipeline(Base):
    def test_cannot_move_backwards(self):
        cid, _ = self.make_company()
        deal_id = pipeline.open_deal(self.conn, cid, mrr=150_000)
        pipeline.advance(self.conn, deal_id, "proposal")
        with self.assertRaises(ValueError):
            pipeline.advance(self.conn, deal_id, "meeting_done")

    def test_cannot_reopen_closed_deal(self):
        cid, _ = self.make_company()
        deal_id = pipeline.open_deal(self.conn, cid, mrr=150_000)
        pipeline.advance(self.conn, deal_id, "won")
        with self.assertRaises(ValueError):
            pipeline.advance(self.conn, deal_id, "proposal")

    def test_won_deal_counts_into_mrr(self):
        cid, _ = self.make_company()
        deal_id = pipeline.open_deal(self.conn, cid, mrr=150_000, setup_fee=300_000)
        pipeline.advance(self.conn, deal_id, "won")
        rev = metrics.revenue(self.conn)
        self.assertEqual(rev["mrr"], 150_000)
        self.assertEqual(rev["customers"], 1)

    def test_forecast_is_probability_weighted(self):
        cid, _ = self.make_company()
        pipeline.open_deal(self.conn, cid, mrr=1_000_000)
        fc = pipeline.forecast(self.conn)
        # meeting_set の受注率 25% なので 100万 -> 25万
        self.assertEqual(fc.weighted_mrr, 250_000)


class TestReplyClassification(Base):
    def test_optout_beats_positive(self):
        # 「興味はあるが配信は止めて」は必ず止める側に倒す
        self.assertEqual(
            sender.classify_reply("興味はありますが、配信停止でお願いします"), "optout")

    def test_classifications(self):
        cases = {
            "資料希望です": "positive",
            "一度お話を聞かせてください": "positive",
            "今は間に合っています": "negative",
            "unsubscribe": "optout",
            "承知しました": "neutral",
        }
        for text, want in cases.items():
            self.assertEqual(sender.classify_reply(text), want, f"{text!r}")


class TestLegalFooter(Base):
    def test_footer_contains_required_disclosures(self):
        s = compose.SenderIdentity(person="山田", company="株式会社A",
                                   address="東京都千代田区1-1", email="info@a.jp",
                                   optout_email="stop@a.jp")
        footer = s.footer()
        # 特定電子メール法4条: 名称・住所・受信拒否の通知先の表示が必要
        for required in ["株式会社A", "山田", "東京都千代田区1-1", "stop@a.jp", "不要"]:
            self.assertIn(required, footer, f"フッタに {required} が無い")

    def test_every_message_carries_footer(self):
        cid, _ = self.make_company()
        prospects.add_signal(self.conn, cid, signals.Signal("hiring_ops", "事務募集"))
        signals.rescore_all(self.conn)
        c = self.composer()
        for step in compose.SEQUENCE:
            _, body = c.compose({"company_name": "A社", "headline_signal": "x",
                                 "subject_hook": "y"}, step)
            self.assertIn("不要な場合", body, f"step{step.step} にフッタが無い")


if __name__ == "__main__":
    unittest.main(verbosity=2)
