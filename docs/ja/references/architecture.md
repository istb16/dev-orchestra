<!-- translated-from: references/architecture.md sha256:30dd8eaf9bcf7e7a4207932926eb25d8a1cc700215b8719e2d76a25a42e96f56 -->

> この文書は [references/architecture.md](../../../references/architecture.md) の日本語訳です。内容が食い違うときは英語版が正です。

<a id="architecture"></a>

# アーキテクチャ

<!-- contents: start -->

**目次**

- [全体の形](#the-shape-of-the-thing)
- [レイヤー](#layers)
- [なぜこの分け方なのか](#why-this-split)
- [データフロー](#data-flow)
- [拡張ポイント](#extension-points)
- [プラグインのフック](#plugin-hooks)
- [セキュリティ](#security)
- [意図的に目標としないこと](#deliberate-non-goals)

<!-- contents: end -->

<a id="the-shape-of-the-thing"></a>

## 全体の形

このスキルは、**薄い決定的なレイヤーの上に載ったプロンプト駆動のオーケストレーション**です。判断はモデルに任せ、機械的な処理はコードに任せます。

```mermaid
flowchart TD
    U[User request] --> O[Orchestrator<br/>the skill + configured CLI]
    O -->|classify| D{Design needed?}
    D -->|no| I
    D -->|yes| A[Architect<br/>read-only]
    A --> P[(.ai/plan.md)]
    P --> DR{design review runs?<br/>on, or auto and risky/large}
    DR -->|yes| DP[[Design review<br/>same panel, read-only]]
    DP --> DT[Triage + revise<br/>run architect again]
    DT --> P
    DR -->|no| AP{plan approved<br/>by the user?}
    DP --> AP
    AP -->|no| ASK[Ask the user<br/>design approve on a yes]
    ASK --> AP
    AP -->|yes| I[Implementer<br/>writes code + tests]
    I --> T[Test<br/>project's own commands]
    T --> S[[review snapshot<br/>.ai/reviews/review-target.diff]]
    S --> R1[Reviewer 1<br/>read-only]
    S --> R2[Reviewer 2<br/>read-only]
    S --> R3[Reviewer N<br/>read-only]
    R1 --> C[Consolidate<br/>parse + dedupe]
    R2 --> C
    R3 --> C
    C --> TR[Triage<br/>orchestrator judgement]
    TR -->|accepted only| F[Review Fixer<br/>writes code]
    F --> T2[Re-test]
    T2 --> Q{critical/high left<br/>and budget remains?}
    Q -->|yes| S
    Q -->|no| REP[Final report]
```

<a id="layers"></a>

## レイヤー

| レイヤー | 置き場所 | 責務 |
| --- | --- | --- |
| スキル | `skills/dev-orchestra/SKILL.md`, `references/` | オーケストレーターが何をいつ決めるか |
| CLI | `scripts/dev_orchestra.py`、`scripts/orchestrator/cli.py`（引数の解析と入口）と `cli_*.py`（コマンドのまとまりごとのモジュール） | エージェントが呼び出せる決定的な操作 |
| ドメイン | `config.py`、`config_policy.py`、`review_*.py`（`review.py` が再公開する）、`workspace.py`、`wizard.py`、`doctor.py` | 設定のレイヤリング、プロジェクトファイル由来の席と書き込みオプションを拒否する方針、スナップショット取得、パース、重複排除、トリアージ、診断 |
| provider | `scripts/orchestrator/providers/` | CLI の構文とモデル名を知っている唯一のコード |
| プラグインのフック | `hooks/`、`scripts/hooks/`、`reply_language.py` | Claude Code のみ: 返答を `language.reply` の言語に保つ（[後述](#plugin-hooks)） |

provider レイヤーより上のコードは、`claude` が `--model` を使い `codex` が `-m` を使うことを一切知りません。スキルレイヤーより下のコードは、設計ステージが必要かどうかを一切判断しません。

<a id="why-this-split"></a>

## なぜこの分け方なのか

**判断は再現できませんが、配管は再現できなければなりません。** 指摘の重複排除、diff の固定、設定ファイルのレイヤリングは毎回同じ答えを返すので、コードとして実装され、テストされています。ある指摘が*この*コードベースにおける本物のバグかどうかを判断することは、まさにモデルが得意とすることなので、プロンプトに残します。

**レビューは独立していなければ価値がありません。** 互いの出力を見る 3 つのモデルは同じ結論に収束しますが、互いを見ない 3 つのモデルは有益な形で意見が分かれます。そのため、ファンアウトはコードで行います。同じ固定スナップショット、分離されたプロセス、共有コンテキストなし、CLI 自身の強制による読み取り専用（Claude ではツールの許可リストと `--restricted`、Codex ではサンドボックス）です。これは、プロンプトが編集されたからといって誤って破られることはありません。

**モデルはスキルよりも速く変わります。** 日付付きのモデル ID は何も永続化されません。設定には `family` + `version: latest` を保存し、adapter がそれを、インストール済みの CLI がその時点で示す内容に照らして解決します。モデルを検証できない adapter は、推測せずに例外を送出します。

**認証は CLI の責任です。** スキルは `claude` と `codex` を、ユーザーの環境を継承したサブプロセスとして実行するので、既存のサブスクリプションやログインがそのまま使えます。スキルは認証情報を保存せず、読み取りもしません。

<a id="data-flow"></a>

## データフロー

```
project/
├── .dev-orchestra.yaml         # optional per-project override
└── .ai/                          # working artifacts (self-ignoring)
    ├── plan.md                   # Architect output
    ├── execution/                # prompts you wrote, fix brief, role outputs
    ├── reviews/
    │   ├── review-target.diff    # the frozen snapshot every reviewer sees
    │   ├── review-target.json    # strategy, files, sha256
    │   ├── review-surrounding.json  # enclosing symbols, only with review.context.surrounding: enclosing
    │   ├── <reviewer-id>.md      # one report per reviewer
    │   ├── consolidated.md       # deduped findings, human readable
    │   ├── consolidated.json     # deduped findings + triage state
    │   ├── rounds/               # consolidated.json of every round, kept after the next
    │   └── design/               # the same files for the design review, so
    │                             # its rounds and triage stay its own
    └── state.json                # stage events with resolved model ids
```

`consolidated.json` はステージ間の受け渡しに使われます。`review run` がこれを書き出し、トリアージがこれに注記を加え、`review fix-brief` がこれを読み込み、`review status` がもう 1 ラウンド必要かどうかを判断します。

<a id="extension-points"></a>

## 拡張ポイント

- **新しい CLI**: `providers/` にモジュールを 1 つ追加し、`register()` を 1 回呼び出すだけです。あるいは、プラグインを編集せずに `<config dir>/providers/` にモジュールを 1 つ置くこともできます。これは組み込みの provider の後に import され、プラグインを更新しても残ります。`references/providers.md` を参照してください。
- **新しいレビュアーロール**: 任意の文字列が使えます。組み込みのロールには、より的確なプロンプトのガイダンス（`review_common.py` の `ROLE_GUIDANCE`）が付くだけです。
- **別のワークスペースの場所**: 設定の `workspace.dir` で指定します。
- **別のレビュープロンプト**: `build_review_prompt` はテンプレートを受け取れます。

<a id="plugin-hooks"></a>

## プラグインのフック

ルール 11 はオーケストレーターにユーザーの言語で答えるよう求めていますが、それを確かめるものはありません。英語の計画書、指摘、CLI の出力を何ページも読んだあとでは、返答が英語に流れていきます。`language.reply` を設定すると（[設定](configuration.md#field-reference)）、Claude Code のプラグインがフックでこのルールを支えます。設定していなければ、どのフックも何も出力しません。

**何が動くか。** `.claude-plugin/plugin.json` は `hooks/claude-code.json` を指しています。ルートの `hooks.json` ではありません。それは Antigravity が自分で読み込んでしまうからです。どのフックも `sh hooks/run <event>` を実行し、これが `bin/dev-orchestra` と同じ方法で Python 3.11 以上を探して、`scripts/hooks/reply_language.py` を `-I` 付きで実行します。処理の本体は `orchestrator/reply_language.py` です。標準ライブラリだけを使い、設定は 2 つのファイルから直接読み、provider のレジストリには触れません。そのため、フックがユーザーの adapter を読み込むことはありません。

- **UserPromptSubmit** と、コンパクションや再開のあとの **SessionStart** は、言語を名指しした短いリマインダー（約 60 トークン）を加えます。進捗、質問、指摘、報告、ツール呼び出しの説明をその言語で書くように、という内容です。
- **Stop** は書き終えたばかりの返答（最後のツール呼び出しのあとの文章）を読み、明らかに別の言語で書かれていれば一度だけブロックします。その理由として、同じ返答を省略せず、設定した言語で、ツールを実行せずにもう一度書くよう求めます。これを止めるのが `language.rewrite: false` です。

**いつ動くか。** `language.reply` が設定されていて、そのセッションが dev-orchestra を使っていて、このツールが委譲した実行の中ではないときだけです。セッションが dev-orchestra を使ったとみなすのは、トランスクリプトにスキルの読み込み、`/dev-orchestra` コマンドの入力、Bash や PowerShell からの `dev_orchestra.py` / `bin/dev-orchestra` の実行が残っているとき（コマンドとして実行したものに限り、`cat` や `git diff` などで名前を挙げただけのものは数えません。サブエージェントの記録も数えません）、またはそのセッションのワークフローディレクトリ `.ai/workflows/<sha256(session id)[:12]>` があるときです。`.ai/` ディレクトリがあるだけでは足りません。それでは、そのプロジェクトで後に開くすべてのセッションが判定の対象になってしまいます。provider が起動するすべての子プロセスには `DEV_ORCHESTRA_DELEGATED=1` が付き、フックはこれを見るとすぐに終わります。そのため、ユーザーのプラグインを読み込む implementer が、どの言語で答えるか指示されることはありません。

**返答をどう判定するか。** まず、ルール 11 が書かれたままにしてよいとしているものをすべて取り除きます。フェンスで囲んだブロック、HTML コメント、`>` の引用、インラインコード、リンク先、URL とメールアドレス、パス、コマンド行と `--flags`、ASCII のダブルクォートで囲んだ文、数字・`_`・`.`・`:`・`=`・`#`・`@` を含むトークン、大文字小文字の混ざった語とすべて大文字の語、表の区切り行です。残った文字を文字体系ごとに数え、その言語自身の文字体系をラテン文字と比べます。ラテン文字 1 文字は、かな・漢字・ハングル 1 文字の 3 分の 1、アルファベット系の文字 1 文字と同じ重みです。どの返答も、その言語以外の文字体系（ラテン文字は除く）の文字が 60 字以上あり、重みを付けてその言語の文字を 70% を超えて上回れば不合格です。`ja` での韓国語の返答や、`ko` での日本語の返答がこれにあたります。日本語では、漢字が 50 字以上あってかながまったくなければ不合格です（それは中国語です）。中国語では、かなが 20 字以上あり、かなと漢字のうち 15% 以上を占めれば不合格です（それは日本語です）。かなは中国語の文字として数えません。それ以外では、ラテン文字の単語が 20 語未満の返答は通します。それより長い返答は、残りのうちその言語の文字体系が 30% 未満のとき、またはラテン文字の単語が 40 語以上ある段落でそれが 10% 未満のときに不合格とします。

- **文字体系で判定する言語**: 日本語、中国語（`zh`、`zh-CN`、`zh-TW`、`zh-Hans`、`zh-Hant` など。簡体字も繁体字も同じに扱います）、韓国語、キリル文字の言語（ロシア語、ウクライナ語、ブルガリア語、セルビア語など）、ギリシャ語、アラビア文字の言語（アラビア語、ペルシャ語、ウルドゥー語）、ヘブライ語、タイ語、デーヴァナーガリー文字の言語（ヒンディー語、マラーティー語、ネパール語）。`sr-Latn` や `zh-Latn-TW` のような文字体系の副タグがあれば、中国語の地域を含め、その言語の通常の文字体系より優先します。
- **よく使う語で判定する言語**: 英語、スペイン語、フランス語、ドイツ語、ポルトガル語、イタリア語。ほかのラテン文字の言語と同じく、返答の大半が別の文字体系なら不合格です。そのうえで、残った語を小文字にしてアクセントは残したまま、言語ごとの短い一覧（`the`、`and`、`of`…、`el`、`los`、`que`…）と照らし合わせます。一覧にあるほかの言語それぞれについて、その言語の一覧にだけある語と、設定した言語の一覧にだけある語を数えます。両方の一覧にある語（`de`、`en`、`a`、`no` など）はどちらにも数えません。一覧の語が 20 語未満の返答は通します。それより長い返答は、ほかの言語にだけある語が 15 語以上で、かつ設定した言語にだけある語の 2 倍以上なら不合格です。したがって `es` で英語の返答、`en` でスペイン語の返答は不合格になり、識別子やコードの多いスペイン語の返答は `es` で通ります。
- **そのほかのラテン文字の言語**（`nl`、`sv`、`pl` など）は、返答の大半が別の文字体系のときだけ不合格になります。語の一覧がないので、ほかのラテン文字の言語とは区別しません。
- **知らないタグ**にはリマインダーだけを出し、判定はしません。

このうちどれに当たるかは `doctor` が示します。

**失敗したら何もしない。** フックがエラーになったとき、入力や設定を解析できないとき、トランスクリプトが見つからないとき、Python が見つからないときは、何も出力せずに 0 で終了します。1 つの返答をブロックするのは多くても 1 回です。書き直された返答は `stop_hook_active` 付きで届き、フックはそれを通します。理由の文面は判定が読み取る書き方（コードはバッククォート、引用した文は `>` の引用）を教えます。判定がユーザーの返答を誤判定するときの逃げ道が `language.rewrite: false` です。

**ホスト。** フックを実行するのは Claude Code だけです。Codex と Antigravity には、`doctor`（スキルはその *Reply language* の行を、ユーザーが頼んだ言語として読みます）とルール 11 で設定が伝わります。`.codex-plugin/plugin.json` はフックを指定していません。skills ディレクトリにコピーしたインストールにもフックはなく、`doctor` がそう伝えます。

**限界。** 判定するのはターンの最後のメッセージだけです。ターンの途中の英語の進捗行は、リマインダーで防ぐものです。`doctor` からはフック自身の `PATH` が見えないので、そこに Python がなければ、何も言わずに判定が止まります。しきい値、取り除くもの、理由の文面はフィクスチャで調整しており、マイナーリリースで変わることがあります。何を約束しているかは README.md の「互換性」の節にあります。

<a id="security"></a>

## セキュリティ

- **認証情報を要求も保存も出力もしません。** スキルはユーザーの環境を引き継ぎ、
  CLI 側の既存の認証に頼ります。
- `doctor` が報告するのは認証情報の *存在*（`present` / `unknown`）だけで、値は出しません。
- 取得した stdout/stderr は、`.ai/` に書かれたり表示されたりする前に、認証情報らしき
  文字列を取り除く redactor を通ります（`references/providers.md`）。
- architect とレビュアーは読み取り専用で動き、それはプロンプトではなく CLI が強制します。
  Claude は plan モード、`Read`・`Grep`・`Glob` のツールだけ、MCP サーバーなし、
  `--restricted` で動くので、シェルはなく、リポジトリの設定ファイルのフックも動かず、
  作業ディレクトリと `--add-dir` の外は読めません。Codex は `-s read-only` で書き込みが
  止まりますが、MCP サーバーは確認していません。`--restricted` のため、ユーザー自身の
  `permissions.deny` もこれらの Claude の実行には効きません。そうしたルールは
  managed settings に置くものです。
- 読み取り専用の実行は、それを緩めうる生の引数を拒否します。Claude が受け付けるのは
  global 設定か `--extra` からの `--add-dir <path>` だけで（project ファイルからは
  受け付けません）、Codex は何も受け付けません。拒否のメッセージは値を表示しません。
  [ロールのオプション](configuration.md#role-options) を参照してください。
- 成果物は `.ai/` に置かれ、`.ai/` は既定で自分自身を git の管理外にします。

セキュリティ上の問題は、`CONTRIBUTING.md` にあるとおり非公開で報告してください。

<a id="deliberate-non-goals"></a>

## 意図的に目標としないこと

- デーモンもサーバーも持たず、プロジェクトと設定ファイルの外に状態を持ちません。
- 自前のネットワークアクセスは行いません。ネットワーク通信は CLI がそれぞれ行います。
- リリースのたびに更新しなければならないモデルカタログを同梱しません。
- オーケストレーション用の DSL はありません。パイプラインは文章で説明できるほど短いからです。
