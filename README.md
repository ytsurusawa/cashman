# cashman

日給100万円までの設計と、それを実行するためのアウトバウンド営業システム。

前提条件: **週12時間・資金100万円・実績ゼロ・開発力あり**

---

## 結論

| マイルストーン | 到達時期 |
|---|---|
| 初受注 | 2〜3ヶ月目 |
| 日給3万円（月粗利90万） | 3〜4ヶ月目 |
| **日給10万円（月粗利300万）** | **11ヶ月目** |
| 日給30万円（月粗利900万） | 13〜15ヶ月目 |
| **日給100万円（月粗利3,000万）** | **31ヶ月目（2年7ヶ月）** |

```bash
python3 financial_model.py --phased    # この数字の根拠
```

**日給100万円を1年で達成する設計は作れない。** 月粗利3,000万円は年商4〜5億円規模で、
週12時間の個人が1年で到達した再現可能な事例は存在しない。
2年7ヶ月という数字も、前提が全部当たった場合の値になる。

一方、**日給10万円は11ヶ月目に到達可能**で、ここは十分に現実的な目標になる。

### 事業の中身

> **求人に出ている定型業務を、AIで自動化して月額で運用代行する。**

求人票は「どの業務が回っていないか」を会社自身が公開してくれている。
これを検出してリストを作り、パーソナライズしたメールを自動で送る。
それが `src/cashman/` の全体になる。

### 最も重要な発見

前提を1つずつ動かして、日給10万円への到達月を測った結果:

| 条件 | 到達 |
|---|---|
| 基準（返信5% / 解約3% / 月額15万） | 11ヶ月目 |
| 返信率が半分（2.5%） | 30ヶ月目 |
| 解約が倍（6%/月） | 13ヶ月目 |
| **単価が半分（月額7.5万）** | **未到達** |
| **単価が2倍（月額30万）** | **6ヶ月目** |

**単価がすべてを支配する。** 安く売って数で埋めるルートは構造的に破綻する。
解約対策に時間を使うくらいなら、単価交渉に使うほうが効率が10倍いい。

---

## ドキュメント

| | 内容 |
|---|---|
| [docs/00-strategy.md](docs/00-strategy.md) | 全体戦略。なぜこのルートか、他の選択肢を外した理由 |
| [docs/01-roadmap.md](docs/01-roadmap.md) | フェーズ別の実行計画。Day 1から31ヶ月目まで。撤退基準 |
| [docs/02-offer.md](docs/02-offer.md) | 商品設計と価格設計。バリューラダー、値引き対応 |
| [docs/03-acquisition.md](docs/03-acquisition.md) | **集客と営業の仕組み**。リスト作り、文面、商談の型 |
| [docs/04-legal.md](docs/04-legal.md) | 特定電子メール法、個人情報保護法、契約書 |

---

## クイックスタート

Python 3.11以降。**外部ライブラリは不要**。

```bash
export PYTHONPATH=src

# 1. 収益モデルを確認する
python3 financial_model.py --phased

# 2. サンプルデータで一通り動かす
python3 -m cashman ingest data/sample_leads.csv    # リード取り込み + シグナル抽出
python3 -m cashman score --top 10                  # スコアリング結果を見る
python3 -m cashman draft --limit 40 --preview      # 下書き生成（送信はしない）
python3 -m cashman send                            # dry-run。--live で実送信
python3 -m cashman dashboard                       # KPI

# 3. テスト
python3 tests/test_cashman.py
```

### 送信前に必要な設定

```bash
export CASHMAN_SENDER_PERSON="山田太郎"
export CASHMAN_SENDER_COMPANY="合同会社サンプル"
export CASHMAN_SENDER_ADDRESS="東京都千代田区1-2-3"   # 特定電子メール法で必須
export CASHMAN_FROM_EMAIL="info@example-jp.com"
export CASHMAN_REPLY_TO="info@example-jp.com"
export CASHMAN_OFFER_LINE="受発注の転記を自動化し、月80時間を3時間に減らす仕組みを作っています。"
export CASHMAN_CASE_LINE="同規模の卸売業で、月92時間の作業が4時間になりました。"

# 実送信する場合のみ
export CASHMAN_SMTP_HOST="smtp.example.com"
export CASHMAN_SMTP_USER="..."
export CASHMAN_SMTP_PASS="..."
```

送信者情報を環境変数にしているのは、**個人情報をリポジトリに置かないため**。

---

## 日々の運用

毎朝20分:

```bash
python3 -m cashman ingest data/leads_$(date +%Y%m%d).csv
python3 -m cashman score
python3 -m cashman draft --limit 40
python3 -m cashman send --live
```

返信が来たときだけ:

```bash
python3 -m cashman reply 42 "資料希望です"           # 自動で分類・停止処理
python3 -m cashman deal open 7 --mrr 150000 --setup 300000
python3 -m cashman deal advance 3 won
```

週1回30分:

```bash
python3 -m cashman dashboard     # 返信率・商談化率・目標までの残り送信数
python3 -m cashman deal board    # 7日以上放置されている商談を潰す
```

---

## 構成

```
financial_model.py          収益シミュレータ（単体で動く / 依存なし）
src/cashman/
  db.py                     SQLiteスキーマ
  signals.py                シグナル検出とスコアリング ← ここが中核
  prospects.py              リード取り込み・名寄せ・配信停止
  compose.py                文面生成（テンプレート版 + LLM版）
  sender.py                 送信・ウォームアップ・返信分類
  pipeline.py               商談パイプライン
  metrics.py                KPI集計
  cli.py                    コマンドライン
tests/test_cashman.py       23件（配信停止・誤爆防止・上限を重点的に）
docs/                       戦略・ロードマップ・営業設計・法務
data/sample_leads.csv       動作確認用のサンプル
```

### 設計で重視したこと

このシステムは、**壊れると金と信用の両方を失う箇所**が3つある。
そこだけテストを厚くしてある。

1. **配信停止した相手に再送しない。** 特定電子メール法違反であり、
   停止依頼はドメイン単位で登録される（同じ会社の別部署への誤送信も防ぐ）。
2. **返信が来た相手に自動フォローを飛ばさない。**
   会話が始まった後に追撃メールが届くのが、この手の仕組みで最も信用を失う事故。
   返信を記録した時点で、未送信の下書きは全部 `cancelled` になる。
3. **ウォームアップの上限を超えて送らない。**
   新規ドメインで初日から40通送ると数日で焼ける。
   一度焼けたドメインは二度と使えず、リスト作りからやり直しになる。

---

## 注意

- **`docs/04-legal.md` を読んでから送信すること。** 特定電子メール法の表示義務を
  満たさないメールを送ると、措置命令の対象になる。
- **本業の就業規則の副業規定を先に確認すること。** ここが最大のリスク要因になりうる。
- **LLM APIのコストは処理件数に比例する。** 定額契約にする場合は必ず上限を設ける。
- シミュレータの数値は前提を置いた推計であり、保証ではない。
  前提を変えたときに結果がどう動くかを見るための道具として使う。
- ここに書かれている法務の整理は実務上の理解であり、法的助言ではない。
  本格稼働の前に専門家に確認すること。
