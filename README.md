# nagi-ledger

[![CI](https://github.com/watasisaikou/nagi-ledger/actions/workflows/ci.yml/badge.svg)](https://github.com/watasisaikou/nagi-ledger/actions/workflows/ci.yml)
[![Python 3.10+](https://img.shields.io/badge/python-3.10%2B-blue)](https://www.python.org/downloads/)
[![License: MIT](https://img.shields.io/badge/license-MIT-green)](LICENSE)

**日本語** | [English](README.en.md)

**自律型 AI コーディングエージェントのための監査台帳とガードレール。**
MCP サーバー + Claude Code hooks。MCP SDK を除いて標準ライブラリのみで動きます。

AI エージェントに単独で作業をさせると、2 つの問いに答えられなくなります。

- **実際に何をしたのか。** どのサブエージェントを派遣し、何回リトライし、検証は何と結論したのか。
- **同じ失敗をどう止めるのか。** 月曜に判明した行き止まりが、金曜にまた試される。何も覚えていないからです。

nagi-ledger はこの両方に答えます。そして重要なのは、**エージェントの自制心に頼らず、機構として**答える点です。派遣はエージェント自身が制御できない hook が記録します。既知の行き止まりは次の試行の**前**に照会され、該当すれば試行そのものが阻止されます。

> 記憶するよう指示された規則は、単なる提案にすぎない。
> ハーネスが強制する規則だけが、制約になる。

---

## 何をするものか

```mermaid
flowchart LR
    subgraph session["コーディングセッション"]
        direction TB
        S["セッション開始"] --> D["サブエージェントを<br/>派遣しようとする"]
        D --> R["サブエージェントが動く"]
        R --> F["ツールが失敗する"]
        F --> E["ターンを終えようとする"]
    end

    S -. "未解決の作業を注入" .-> B["session_brief"]
    D -. "履歴を照会" .-> G["dispatch_guard<br/>繰り返しを阻止"]
    R -. "自動記録" .-> H["hook_ingest"]
    F -. "自動記録" .-> H
    E -. "まだ終わってない?" .-> Q["goal_gate<br/>停止を阻止"]

    B --> L[("SQLite 台帳")]
    G --> L
    H --> L
    Q --> L
```

構成要素は 5 つ。それぞれエージェントのライフサイクルの別々の地点に接続されます。

| 構成要素 | フック | 役割 |
|---|---|---|
| **`session_brief.py`** | `SessionStart`, `PostCompact` | **未解決の作業**をセッション開始時に注入します。進行中の目標、検証待ちの派遣、直近の行き止まり、未コミットの変更が残るリポジトリ。すべて片付いているときは**何も出力しません**。つまり、綺麗な状態のセッションではコンテキストを 1 バイトも消費しません。 |
| **`dispatch_guard.py`** | `PreToolUse` (Agent) | サブエージェントを派遣する直前に、そのタスクのリトライ回数と記録済みの行き止まりを照会します。リトライ上限を超えている場合、または既知の行き止まりに該当する場合は、理由を添えて**阻止**します (exit 2)。それ以外のときは沈黙します。 |
| **`hook_ingest.py`** | `PostToolUse`, `PostToolUseFailure` | すべてのサブエージェント派遣とツール失敗を台帳に記録します。非同期で走り、エージェントに拒否権はありません。 |
| **`goal_gate.py`** | `Stop` | 目標が設定されている間、**エージェントがターンを終えることを阻止**します。明示的に「完了」を宣言するまで止まれません。ただしターン予算があるため、無限ループにはなりません。 |
| **`server.py`** | MCP (stdio) | 台帳を 8 個の MCP ツールとして公開します。エージェントが意図的に台帳を読み書きするための口です。検証結果の記録、行き止まりの登録、セッションレポートの生成など。 |

台帳の本体 (`ledger.py`) は MCP のコードもフックのコードも一切含まない依存ゼロのモジュールです。そのため、すべての関数を直接ユニットテストできます。

---

## なぜ「指示」ではなく「フック」なのか

このプロジェクトが立脚している設計原則です。

> **エージェントが覚えていなければならない規則は、提案にすぎない。**
> **ハーネスが強制する規則だけが、制約になる。**

プロンプトに *「同じ失敗する手法を 2 回を超えて繰り返さないこと」* と書くことはできます。それはコンテキストが長くなるまで、あるいはモデルが自信を持つまで、あるいは要約がその一行を落とすまでは有効です。`dispatch_guard` は同じ規則を `exit 2` にします。

この方針から、実装上の判断が 2 つ導かれました。

### ガードは必ず「開く方向」に倒れる (fail open)

すべてのフックは、**内部エラーが起きたら必ず exit 0 で終了**します。全部の派遣を阻止する壊れたガードは、ガードが無いより悪いからです。クラッシュも、データベースの欠損も、ファイルロックも、すべて「通す。ただし stderr に文句を書く」に解決されます。

ただし fail open が原理的に不可能な箇所が 1 つあります。**`SessionStart` フックが標準入力で待ちに入った場合、例外ハンドラでは救えません。** 例外が発生しないからです。ただ固まり、セッションが永遠に始まらないだけです。この経路は「標準入力を一切読まない」ことで塞いであり、**開いたまま閉じられていないパイプに対してスクリプトを起動する回帰テスト**で守っています。

### 阻止は JSON ではなく終了コードで伝える

初期の実装では `permissionDecision: "ask"` という JSON を返していました。しかし Claude Code の `auto` 権限モードでは、**この判断は黙って握りつぶされます。** ガードは正しく判断したのに、派遣はそのまま実行され、理由は誰にも届きませんでした。

終了コード 2 はすべての権限モードで尊重されます。**届かないガードは、ガードではありません。**

---

## 使い方

Python 3.10 以上が必要です。

```bash
git clone https://github.com/watasisaikou/nagi-ledger.git
cd nagi-ledger
python -m venv .venv
.venv/bin/pip install -r requirements-dev.txt   # Windows: .venv\Scripts\pip
.venv/bin/pytest -q
```

`requirements.txt` には実行時の唯一の依存 (MCP SDK、`server.py` のみが必要とします) が入っています。`requirements-dev.txt` はそれに pytest を加えたものです。

### MCP サーバーを登録する

```bash
claude mcp add nagi-ledger -s user -- /abs/path/.venv/bin/python /abs/path/server.py
```

MCP サーバーには `mcp` パッケージが必要なので、仮想環境側のインタプリタを指定してください。一方フックのスクリプトは**意図的に標準ライブラリのみ**で書かれているため、どの Python でも動きます。

### フックを接続する

`~/.claude/settings.json` に以下を追加します。`PY` をインタプリタのパス、`DIR` をチェックアウト先のパスに置き換えてください。

```json
{
  "hooks": {
    "SessionStart": [
      { "hooks": [{ "type": "command", "command": "PY DIR/session_brief.py", "timeout": 15 }] }
    ],
    "PostCompact": [
      { "hooks": [{ "type": "command", "command": "PY DIR/session_brief.py", "timeout": 15 }] }
    ],
    "PreToolUse": [
      { "matcher": "Agent",
        "hooks": [{ "type": "command", "command": "PY DIR/dispatch_guard.py", "timeout": 15 }] }
    ],
    "PostToolUse": [
      { "matcher": "Agent",
        "hooks": [{ "type": "command", "command": "PY DIR/hook_ingest.py agent-dispatch", "timeout": 30, "async": true }] }
    ],
    "PostToolUseFailure": [
      { "hooks": [{ "type": "command", "command": "PY DIR/hook_ingest.py tool-failure", "timeout": 30, "async": true }] }
    ],
    "Stop": [
      { "hooks": [{ "type": "command", "command": "PY DIR/goal_gate.py stop-gate", "timeout": 15 }] }
    ]
  }
}
```

`SessionStart` と `PostCompact` に同じスクリプトを渡しています。圧縮はセッション開始と同じ状況、つまり**未解決の作業が文脈から失われた状態**を作るため、必要な処置も同じです。

各要素は独立しています。**必要なものだけ接続して構いません。**

### 目標ゲートを使う

```bash
python goal_gate.py set "全テストが緑で CHANGELOG が更新されていること" --max-turns 20
python goal_gate.py status
python goal_gate.py extend 10          # バックグラウンド処理の完了待ちで予算が足りないとき
python goal_gate.py done "142 テスト緑、CHANGELOG を a1b2c3d でコミット"
```

`done` を宣言するまで (あるいは `abort` するか、ターン予算が尽きるまで)、エージェントはターンを終えられません。

---

## MCP ツール一覧

| ツール | 用途 |
|---|---|
| `ledger_log_action(tier, category, description, project=None)` | 自律実行した操作を記録する。影響度を 0〜2 の 3 段階で区別。 |
| `ledger_log_dispatch(task, agent_type, model, brief_summary)` | サブエージェントの派遣を記録し、**そのタスクのそれまでのリトライ回数を返す**。 |
| `ledger_log_verdict(dispatch_id, verdict, notes=None)` | 派遣に `CONFIRMED` / `REFUTED` / `PARTIAL` の検証結果を紐づける。 |
| `ledger_task_status(task)` | そのタスクのリトライ回数、直近の検証結果、リトライ上限超過フラグを返す。 |
| `ledger_log_approach(task, approach, outcome, reason)` | 試した手法を `DEAD_END` / `NO_GO` / `WORKS` として登録する。 |
| `ledger_check_approaches(task)` | そのタスクで既に何を試し、結果がどうだったかを返す。 |
| `ledger_session_report(since_hours=24)` | 直近の操作と派遣を Markdown レポートにする。 |
| `ledger_stats(days=7)` | 影響度・分類・検証結果ごとの集計。 |

派遣の記録は**自動**です (フックがやります)。一方、**検証結果の記録は意図的に手動のまま**にしてあります。「その仕事が本当に正しいか」を決めるのは判断であり、そこを自動化したらこの仕組みの意味が失われるからです。

---

## データの保存先

`~/.nagi/ledger.db` に SQLite で保存します。テーブルは `actions` / `dispatches` / `approaches` の 3 つ。

WAL モードを使っています。非同期フックが同時に発火しうるためで、**書き込みが 1 件失われることは監査証跡に穴が開くこと**を意味するからです。

すべてのパスは環境変数で上書きできます。テストスイートが実際の台帳に触れないのも、この仕組みを使っています。

| 環境変数 | 既定値 |
|---|---|
| `NAGI_LEDGER_DB` | `~/.nagi/ledger.db` |
| `NAGI_GOAL_FILE` | `~/.nagi/goal.json` |
| `NAGI_GOAL_HISTORY` | `~/.nagi/goal_history.jsonl` |
| `NAGI_BRIEF_REPOS` | 現在の git リポジトリ (あれば) |

---

## テスト

```bash
pytest -q                     # 142 テスト
python tests/smoke_stdio.py   # MCP サーバーを stdio で起動して実際に呼ぶ
```

CI は Linux と Windows の両方で、Python 3.10 と 3.12 に対してこれらを実行します。

テストは正常系よりも**異常系に厚く**書いてあります。この種のツールでは、壊れ方こそが本題だからです。

- **破損状態での fail open** — 読めないデータベース、ディレクトリがあるべき場所にファイルがある状態、標準入力に流し込まれたゴミ、オブジェクトではなく JSON 配列。**すべてのケースで操作は通らなければなりません。**
- **「書き込まないこと」の証明** — 読み取り専用の要素については、全テーブルの行数を前後で記録して一致を検査します。**監査対象を書き換える監査ツールには価値がありません。**
- **非 ASCII 文字のサブプロセス往復** — 記録される理由は英語でないことが多く、Windows のコンソールコードページはそれを表現できません。これは実際に起きたバグでした。`json.dumps` が非 ASCII をエスケープして問題を隠しており、stderr への出力を平文に変えた瞬間に露出しました。
- **標準入力での固まり** — 開いたまま閉じられていない標準入力パイプを与えてサブプロセスを起動し、速やかに終了することを検査します。

---

## 現状と適用範囲

**日常的に使っている実働ツール**であり、フレームワークではありません。意図的に小さく作ってあります。SQLite と標準ライブラリのみ、1 つの関心事につき 1 ファイル、プラグイン機構なし。

[Claude Code](https://code.claude.com) のフックを対象にしていますが、MCP サーバー側は任意の MCP クライアントで動きます。

既知の制約を、影響の大きい順に挙げます。

- **目標ゲートに「待機」の概念がない。** 「バックグラウンド処理の完了を待っている」と「早々に諦めた」を区別できないため、待機がターン予算を消費します。現状の回避策は `extend` です。
- **`stop-gate` の同時実行が直列化されていない。** 状態ファイルはロックなしの read-modify-write です。単一セッションでの利用 (これが唯一のサポート対象です) では発生しません。

## ライセンス

MIT — [LICENSE](LICENSE) を参照してください。
