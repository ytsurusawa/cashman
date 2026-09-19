"""メール文面の生成.

コールドメールで返信が取れるかは、ほぼ次の3点で決まる:

  1. 一行目が「あなたを調べました」になっているか
     -> テンプレ感が出た瞬間に読まれない. シグナルをそのまま引用する.
  2. 依頼が小さいか
     -> 初回で商談を求めると返信率が半分以下になる. 「資料送りましょうか」
        程度の、Yes/No が1秒で決まる依頼まで落とす.
  3. 4通目まで送っているか
     -> 返信の6〜7割は2通目以降に来る. 1通で諦めると母数を捨てることになる.

LLM は「シグナルを自然な日本語の一行に変換する」ためだけに使う.
文面の骨格はテンプレートで固定しておかないと、品質が送信のたびにブレて
何が効いたのか測れなくなる.
"""

from __future__ import annotations

import sqlite3
import textwrap
from dataclasses import dataclass
from typing import Callable, Protocol

from . import db

# --- 送信者情報(特定電子メール法 4条の表示義務) ------------------------------
# 氏名/名称・受信拒否の通知先・住所・問い合わせ先の4点は必須.


@dataclass
class SenderIdentity:
    person: str = "（氏名）"
    company: str = "（屋号 or 法人名）"
    address: str = "（登記住所 or 事業所住所）"
    email: str = "（送信元アドレス）"
    optout_email: str = "（配信停止を受け付けるアドレス）"
    phone: str = ""
    site: str = ""

    def footer(self) -> str:
        lines = [
            "--",
            f"{self.company}　{self.person}",
            self.address,
        ]
        if self.phone:
            lines.append(f"TEL: {self.phone}")
        lines.append(f"Mail: {self.email}")
        if self.site:
            lines.append(self.site)
        lines += [
            "",
            "※本メールは貴社ウェブサイトで公開されているアドレス宛にお送りしています。",
            f"　今後の送付が不要な場合は、本メールへの返信または {self.optout_email} 宛に",
            "　その旨をご連絡ください。以後一切お送りいたしません。",
        ]
        return "\n".join(lines)


# --- シーケンス設計 ---------------------------------------------------------
# 何日空けて、どの角度から当てるか. 角度を変えないと同じ人に4回無視される.

@dataclass
class Step:
    step: int
    delay_days: int
    angle: str
    note: str


SEQUENCE: list[Step] = [
    Step(1, 0,  "signal",    "観測したシグナルを引用し、課題の仮説を1つ置く"),
    Step(2, 3,  "evidence",  "同業・同規模の具体事例を1本、数字付きで出す"),
    Step(3, 8,  "lower_ask", "依頼をさらに小さくする(資料 -> 1枚の試算)"),
    Step(4, 16, "breakup",   "クローズを宣言する. 返信率が最も高いのはここ"),
]


class Composer(Protocol):
    def compose(self, ctx: dict, step: Step) -> tuple[str, str]:
        """(件名, 本文) を返す."""
        ...


# --- テンプレート版(APIキー不要・これ単体で運用できる) -----------------------


# 本文テンプレートはモジュール定数として持つ.
# f-string の中でインデントすると dedent が効かないので、
# 先に整形済みの文字列を用意して format() で差し込む.

_T_SIGNAL = """\
{greeting}

突然のご連絡失礼いたします。{sender_company}の{sender_person}と申します。

{signal}、という貴社の公開情報を拝見してご連絡しました。
差し支えなければ伺いたいのですが、この業務は今、人手で回されている状態でしょうか。

{offer_line}

もしご興味があれば、{company}さまの状況に当てはめた資料をお送りします。
「資料希望」とだけご返信いただければ、こちらで用意します。"""

_T_EVIDENCE = """\
{greeting}

先日ご連絡した{sender_company}の{sender_person}です。

参考までに1件だけ共有させてください。
{case_line}

{company}さまでも近い構造かもしれないと思い、お送りしました。
不要でしたら本メールは破棄いただいて構いません。"""

_T_LOWER_ASK = """\
{greeting}

{sender_person}です。度々失礼します。

資料をお送りするほどでもないかと思い直しまして、かわりに
「その業務に月何時間・いくらかかっているか」の試算だけを1枚にまとめて
お送りできます。作成に費用はいただきません。

ご不要でしたら、その旨だけ教えていただけると助かります。"""

_T_BREAKUP = """\
{greeting}

{sender_person}です。
何度かご連絡しましたが、タイミングが合わなかったようですので
こちらからのご連絡は今回で最後にいたします。

もし今後、同様の業務でお困りのことがあれば、そのときに本メールへ
ご返信ください。すぐに対応いたします。

お忙しいところ失礼いたしました。"""

_TEMPLATES = {
    "signal": _T_SIGNAL,
    "evidence": _T_EVIDENCE,
    "lower_ask": _T_LOWER_ASK,
    "breakup": _T_BREAKUP,
}


class TemplateComposer:
    """差し込みだけで組み立てる版.

    LLM を使わなくても、シグナルの引用さえ正確なら返信率は実用域に入る.
    最初の数百通はこれで回して、何が効くかを測ってから LLM に置き換える.
    """

    def __init__(self, sender: SenderIdentity, offer_line: str, case_line: str):
        self.sender = sender
        self.offer_line = offer_line
        self.case_line = case_line

    def compose(self, ctx: dict, step: Step) -> tuple[str, str]:
        company = ctx["company_name"]
        hook = ctx.get("subject_hook") or "業務自動化のご提案"

        if step.angle == "signal":
            subject = f"{company}さま｜{hook}"
        elif step.angle == "breakup":
            subject = f"Re: {hook}（最後のご連絡）"
        else:
            subject = f"Re: {hook}"

        body = _TEMPLATES[step.angle].format(
            greeting=f"{company}\nご担当者さま",
            company=company,
            signal=ctx.get("headline_signal") or "貴社サイトの記載",
            sender_person=self.sender.person,
            sender_company=self.sender.company,
            offer_line=self.offer_line,
            case_line=self.case_line,
        )
        return subject, body.strip() + "\n\n" + self.sender.footer()


# --- LLM版 ------------------------------------------------------------------


PROMPT = """あなたは日本のB2B向けコールドメールを書く担当者です。
以下の会社に送る{step_no}通目のメールを書いてください。

# 相手
会社名: {company_name}
業種: {industry}
従業員数: {employees}
観測したシグナル: {signals}

# こちらの商品
{offer}

# この通の役割
{angle_note}

# 制約
- 件名は25文字以内。会社名か、観測した事実を必ず含める。
- 本文は250文字以内。
- 一行目で「あなたを個別に調べた」ことが伝わること。シグナルを具体的に引用する。
- 誇張・断定をしない。数字は与えられたものだけ使い、創作しない。
- 依頼は1つだけ。初回は商談ではなく資料送付の可否にとどめる。
- 敬語。ただし定型的な挨拶文（「時下ますますご清栄の」等）は入れない。
- 署名・フッタは書かない。システム側で付与する。

件名と本文を次の形式で出力してください:
SUBJECT: <件名>
BODY:
<本文>
"""


class LLMComposer:
    """LLM に文面を書かせる版.

    生成関数を外から渡す形にしてある. Anthropic でも OpenAI でもローカル
    モデルでも、`(prompt: str) -> str` を満たせば何でも差せる.
    生成が失敗したら黙ってテンプレート版に落ちる -- 送信が止まるほうが損失が大きい.
    """

    def __init__(self, generate: Callable[[str], str], fallback: TemplateComposer,
                 offer_description: str):
        self.generate = generate
        self.fallback = fallback
        self.offer_description = offer_description

    def compose(self, ctx: dict, step: Step) -> tuple[str, str]:
        prompt = PROMPT.format(
            step_no=step.step,
            company_name=ctx["company_name"],
            industry=ctx.get("industry") or "不明",
            employees=ctx.get("employees") or "不明",
            signals=ctx.get("all_signals") or "なし",
            offer=self.offer_description,
            angle_note=step.note,
        )
        try:
            raw = self.generate(prompt)
            subject, body = _parse_llm_output(raw)
            if not subject or len(body) < 40:
                raise ValueError("生成結果が短すぎる")
        except Exception:
            return self.fallback.compose(ctx, step)
        return subject, body.strip() + "\n\n" + self.fallback.sender.footer()


def _parse_llm_output(raw: str) -> tuple[str, str]:
    subject, body_lines, in_body = "", [], False
    for line in raw.splitlines():
        if line.startswith("SUBJECT:"):
            subject = line[len("SUBJECT:"):].strip()
        elif line.startswith("BODY:"):
            in_body = True
        elif in_body:
            body_lines.append(line)
    return subject, "\n".join(body_lines)


# --- 実行 -------------------------------------------------------------------


def context_for(conn: sqlite3.Connection, contact_id: int) -> dict:
    """1件分の差し込みコンテキストを組み立てる."""
    row = conn.execute(
        """SELECT c.id, c.email, c.name AS contact_name, c.title,
                  co.id AS company_id, co.name AS company_name,
                  co.industry, co.employees, co.score
           FROM contacts c JOIN companies co ON co.id=c.company_id
           WHERE c.id=?""",
        (contact_id,),
    ).fetchone()
    if row is None:
        raise KeyError(f"contact {contact_id} not found")

    sigs = conn.execute(
        """SELECT kind, detail FROM signals WHERE company_id=?
           ORDER BY weight DESC, observed_at DESC""",
        (row["company_id"],),
    ).fetchall()

    ctx = dict(row)
    ctx["headline_signal"] = sigs[0]["detail"] if sigs else None
    ctx["all_signals"] = " / ".join(s["detail"] for s in sigs) if sigs else None
    ctx["subject_hook"] = _hook(sigs)
    return ctx


def _hook(sigs) -> str:
    kinds = {s["kind"] for s in sigs}
    if "hiring_repeat" in kinds:
        return "繰り返しのご募集について"
    if "manual_process" in kinds:
        return "手入力・転記業務について"
    if "hiring_ops" in kinds:
        return "事務業務のご負荷について"
    if "funding" in kinds:
        return "調達後の体制づくりについて"
    return "業務自動化のご提案"


def draft_batch(conn: sqlite3.Connection, composer: Composer,
                contacts: list[sqlite3.Row], step: Step = SEQUENCE[0]) -> int:
    """下書きを作って messages に積む. この時点では送信しない."""
    made = 0
    for c in contacts:
        cid = c["contact_id"] if "contact_id" in c.keys() else c["id"]
        ctx = context_for(conn, cid)
        subject, body = composer.compose(ctx, step)
        conn.execute(
            """INSERT INTO messages (contact_id, step, subject, body, status,
                                     scheduled_for)
               VALUES (?,?,?,?, 'draft', date('now', ?))""",
            (cid, step.step, subject, body, f"+{step.delay_days} day"),
        )
        made += 1
    db.log(conn, "messages", None, "drafted", f"step={step.step} count={made}")
    conn.commit()
    return made


def draft_followups(conn: sqlite3.Connection, composer: Composer) -> int:
    """返信が無く、次の通の送信日が来た相手にフォローの下書きを作る.

    返信済み・配信停止済みはここで確実に外す. フォローの誤爆は
    1通でも信用を失うので、条件は全部 SQL 側で閉じている.
    """
    made = 0
    for step in SEQUENCE[1:]:
        prev = step.step - 1
        rows = conn.execute(
            """
            SELECT m.contact_id
            FROM messages m
            JOIN contacts c ON c.id = m.contact_id
            WHERE m.step = ?
              AND m.status = 'sent'
              AND m.replied_at IS NULL
              AND julianday('now') - julianday(m.sent_at) >= ?
              AND NOT EXISTS (
                    SELECT 1 FROM messages n
                    WHERE n.contact_id = m.contact_id AND n.step >= ?)
              AND NOT EXISTS (
                    SELECT 1 FROM messages r
                    WHERE r.contact_id = m.contact_id AND r.replied_at IS NOT NULL)
              AND NOT EXISTS (
                    SELECT 1 FROM suppressions s
                    WHERE s.pattern = c.email
                       OR s.pattern = '@' || substr(c.email, instr(c.email,'@') + 1))
            """,
            (prev, step.delay_days - SEQUENCE[prev - 1].delay_days, step.step),
        ).fetchall()
        for r in rows:
            ctx = context_for(conn, r["contact_id"])
            subject, body = composer.compose(ctx, step)
            conn.execute(
                """INSERT INTO messages (contact_id, step, subject, body, status,
                                         scheduled_for)
                   VALUES (?,?,?,?, 'draft', date('now'))""",
                (r["contact_id"], step.step, subject, body),
            )
            made += 1
    conn.commit()
    return made
