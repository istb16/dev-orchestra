# AI Development Orchestrator

[English](README.md) | **日本語**

**複数のAIコーディングCLI** を組み合わせてソフトウェア開発フロー全体をオーケストレーションする、汎用の
[Agent Skill](https://code.claude.com/docs/en/skills) です。あるモデルで設計し、別のモデルで実装し、
完了とみなす前に複数のモデルが独立してレビューします。どんなコードベースでも動き、リクエストに必要な
工程だけを実行します。typo修正なら編集だけ、スキーマ変更ならフルパイプラインです。

## なぜ作るのか

**モデルは自分の成果物のレビューが苦手です。** バグを生んだ前提をそのまま共有しているからです。
互いの意見を知らない状態で同じdiffを見た2〜3の**異なる**モデルは、有用な形で食い違います。
その食い違いこそが本物のバグのありかです。

**生のレビュー出力は修正リストではありません。** 複数のレビュアーは互いに重複し、一部はfalse positiveで、
それを全部fixerに丸投げすると無駄な変更が量産されます。そこで findings は機械的に重複統合したうえで、
オーケストレーターが**トリアージ**し、accepted になったものだけが fixer に届きます。そしてモデル名は
決して推測しません。設定に書くのは *family* と `version: latest` で、検証できなければ adapter は
エラーで停止します。

## 誰が何を担当するか

| 工程 | 設定上のロール | 役割 |
| --- | --- | --- |
| 指揮 | `orchestrator` | 依頼に必要な工程を判断し、大量の入力を消化し、結果を統合する |
| 設計 | `architect` | 調査して計画を書く（読み取り専用）。承認するまで実装には進まない |
| 実装 | `implementer` | 計画からコードとテストを書く |
| レビュー | reviewers | 2つ以上のモデルが同じ凍結済みの差分を、読み取り専用で、互いを見ずに読む |
| 修正 | `review_fixer` | オーケストレーターが accepted にした findings だけを直す |

```mermaid
flowchart LR
    U[ユーザー] --> O[Orchestrator]
    O --> A[Architect<br/>read-only]
    A --> I[Implementer]
    I --> T[テスト]
    T --> S[[スナップショット凍結]]
    S --> R1[Reviewer 1]
    S --> R2[Reviewer 2]
    S --> R3[Reviewer N]
    R1 --> C[統合 + 重複排除]
    R2 --> C
    R3 --> C
    C --> TR[トリアージ]
    TR -->|accepted のみ| F[Review Fixer]
    F --> T2[再テスト] --> REP[最終報告]
```

重要なのは **設計と独立レビューを別ベンダーに割り当てる** という形です。同じ系列のモデル同士は、
バグを生んだ思い込みまで共有してしまいます。

## 必要なもの

- **Python 3.11以上** — 標準ライブラリのみ（PyYAML があれば使います）。`python3` という名前しか
  ない環境では `python3` で、または自動で選ぶ `bin/dev-orchestra[.ps1]` で実行してください。
- **git** — レビュースナップショットに必要です。
- **サポート対象CLIのいずれか**（認証済みであること）:
  - [Claude Code](https://claude.com/claude-code) (`claude`)
  - [Codex CLI](https://developers.openai.com/codex/cli) (`codex`)
  - Antigravity CLI (`agy`)。implementer と review fixer 向けです。読み取り専用のモードがないので、
    plan や review の席に置く場合は global 設定からだけ、警告付きで受け付けます
    （`docs/ja/references/providers.md` を参照）

Skill は Claude Code、Codex、Antigravity のいずれかの Plugin として動きます。上の CLI は
Skill が動かす相手で、Skill が動く場所ではありません。

このSkillは**既存のCLIログインをそのまま使います**。APIキーを要求せず、認証情報を保存せず、出力もしません。

## インストール

このリポジトリから Plugin として入れます（公式Marketplaceには公開していません）。Claude Code と
Codex は自分のキャッシュ（`~/.claude/plugins/cache/…`、`~/.codex/plugins/cache/…`）にある
コピーを実行し、次のセッションから使えます。Antigravity は自分の `plugins/` フォルダに置かれた
ディレクトリを読み込みます。

### Claude Code Plugin

```bash
claude plugin marketplace add istb16/dev-orchestra
claude plugin install dev-orchestra@dev-orchestra
```

Claude Code の中からは `/plugin marketplace add istb16/dev-orchestra`、続けて
`/plugin install dev-orchestra@dev-orchestra` です。

### Codex Plugin

```bash
codex plugin marketplace add istb16/dev-orchestra
codex plugin add dev-orchestra@dev-orchestra
```

`codex plugin list` で導入済みのものを確認できます。

### Antigravity Plugin

リポジトリをクローンし、Antigravity の plugins フォルダにリンクします。

```bash
git clone https://github.com/istb16/dev-orchestra.git
cd dev-orchestra
./install/install.sh --antigravity    # links into ~/.gemini/config/plugins/
```

```powershell
.\install\install.ps1 -Antigravity    # Windows
```

手でやる場合はリンク1つです: `ln -s "$PWD" ~/.gemini/config/plugins/dev-orchestra`。
`--project <path>` を付けると `<path>/.agents/plugins/` に入れます。入れたあとは Antigravity を
再起動してください。新しい Plugin のディレクトリは起動時にしか見つけられません。リンクで入れた
場合はチェックアウトで今のブランチがそのまま読み込まれるので、信頼できないブランチを見るときは
`--copy` か別の worktree を使ってください。Antigravity CLI の `agy plugin install <path>` でも
コピーを置けますが、これはチェックアウトをそのままコピーします。また `plugins.json` のエントリで
チェックアウトを含むフォルダを指せば、作業ツリーがそのまま読み込まれます。どちらも信頼できない
ブランチには同じ注意が必要で、インストーラの拒否も `doctor` の確認も働きません
（[詳細](docs/ja/references/workflow.md#installing-from-a-skill-checkout)）。Marketplace は
Google が選んで載せるもので、利用者が追加することはできません。

Plugin 以前のインストーラも使えます
（[Skill のチェックアウトからのインストール](docs/ja/references/workflow.md#installing-from-a-skill-checkout)）。
クローンから Plugin を動かす方法は `CONTRIBUTING.md`（英語）にあります。

## 初期セットアップ

初回実行時、設定が無いことを検知して今有効な設定を表示し、保存するプリセット
（`quality`・`standard`・`fast`）を尋ねます。プリセットはロール・レビュアー構成・設計レビュー・
最適化レベルをまとめて決め、そのマシンにインストールされている CLI に合わせます。Claude Code
だけのマシンなら、レビュアーも Claude だけになります。自分で選ぶ場合や、ウィザードの質問に
すべて答える場合:

```bash
dev-orchestra config setup --preset standard
dev-orchestra config setup
dev-orchestra model list        # the families your installed CLIs offer
```

ファイルを保存するまでは、インストール済みの CLI に合わせた `standard` が有効です。Claude Code と
Codex の両方があれば、それが推奨構成です: architect が Claude の `fable`、実装と修正が Claude の
`opus`、レビュアーが Claude と Codex の1人ずつ。ファイルに残るのは自分で決めた値だけです。
[プリセット](docs/ja/references/configuration.md#presets)、[ウィザード](docs/ja/references/configuration.md#the-wizard)
と [2社構成の設定例](docs/ja/references/configuration.md#worked-examples) を参照してください。

## 使い方

普段どおりエージェントに話しかけてください。「この issue を設定済みのワークフローで実装して」
「チェックアウトのタイムアウトを調査して直して」「このブランチを main と比較してマルチモデル
レビューして」「Codex の security reviewer を追加して」。1文で依頼すれば、必要な工程だけが
実行されます。1工程だけを自分で回したいときは、オーケストレーターが裏で実行している次の
コマンドを使います（各コマンドの説明は [ワークフロー](docs/ja/references/workflow.md)、[英語版](references/workflow.md)）。

```bash
# 先に設計の依頼を <Artifacts>/execution/request.md に書く（workflow show）
dev-orchestra run architect --prompt-file .ai/execution/request.md --output .ai/plan.md
dev-orchestra design approve            # after you have read the plan
dev-orchestra run implementer --prompt-file .ai/plan.md
# プロジェクトのテストを走らせ、結果を記録する
dev-orchestra state record test ok
dev-orchestra review snapshot --base main
dev-orchestra review run
dev-orchestra review triage F1 --status accepted --note "confirmed"
dev-orchestra review fix-brief --output .ai/fix-brief.md
dev-orchestra run review_fixer --prompt-file .ai/fix-brief.md
# 同じテストをもう一度走らせ、結果を記録する
dev-orchestra state record test ok
dev-orchestra review status             # 次の回に進むか、報告する
```

ログや長い仕様書は、先にまとめて消化させ、その要約から設計します
（[大量のテキストを渡す](docs/ja/references/limits.md#feeding-it-a-lot-of-text)）。

```bash
dev-orchestra run orchestrator --prompt-file .ai/execution/analysis-request.md --output .ai/analysis.md
```

## 設定

優先順位は **プロジェクト → グローバル → 内蔵デフォルト** です。`<repo>/.dev-orchestra.yaml`、
次に `~/.config/dev-orchestra/config.yaml`（Windows では `%APPDATA%\dev-orchestra\config.yaml`）。
`dev-orchestra config show` で結果を表示し、`config set` で値を1つ変更します。スキーマ、全フィールド、
設定例は [設定](docs/ja/references/configuration.md)（[英語版](references/configuration.md)）にあります。

`dev-orchestra config set language.reply ja`（`ko`、`zh-TW`、`es`、`fr` など、任意の言語タグ）で、
オーケストレーターが答える言語を固定できます。dev-orchestra は Claude Code のユーザー設定
（`~/.claude/settings.json`。書き換える前に元のファイルを控えます）に 3 つのフックを加え、
プロンプトのたびにその言語を思い出させ、明らかに別の言語で書かれた返答を一度だけ書き直させます。
ほかのホストでは `doctor` がこの設定をスキルに伝えます。`--no-hooks` を付けると設定だけを保存し、
`dev-orchestra hooks status` でフックの状態を確かめられます。設定を消すとフックも外れます。
プロジェクトの `.dev-orchestra.yaml` が言語を設定していても、それだけでフックが入ることはありません。
何を判定するか、フックをどう書き込むか、どこまでできるかは
[返答の言語のフック](docs/ja/references/architecture.md#reply-language-hooks) にあります。

## モデル選択

ロールには family と `version: latest`（または正確なIDを指定した `pinned`）を保存し、実行のたびに
インストール済みCLIに対して解決します（[モデルの family](docs/ja/references/configuration.md#model-families-and-version-policy)）。
`model_tiers` を使うと、1つのロールに安いモデルやセカンドオピニオン用のモデルを、呼び出し側の指定で
割り当てられます（[モデルの tier](docs/ja/references/configuration.md#model-tiers)）。

## レビュアー

0個以上（2個以上を推奨）。`dev-orchestra reviewer add --provider codex --role security` で追加します。
2巡目は修正だけをレビューし、ロックファイルやバンドルはレビュアーに送りません。ロール、スナップショット、
重複統合、トリアージは [レビュー](docs/ja/references/reviews.md)（[英語版](references/reviews.md)）にあります。

## ワークフロー例

Rails への機能追加、API 変更、typo 修正、レビューのみ、mock provider での試運転:
[ワークフローの例](docs/ja/references/workflow.md#example-workflows)。

## トラブルシューティング

`dev-orchestra doctor` が CLI、設定、モデルの family を確認します。`doctor --json` で同じ内容を
機械可読な形で得られます。症状と対処は [トラブルシューティング](docs/ja/references/cli.md#troubleshooting) にあります。

## セキュリティ

認証情報を要求も保存も出力もしません。architect と全レビュアーは読み取り専用で動き、それはプロンプト
ではなく CLI が強制します。Skill 自身はネットワークにアクセスしません。`--restricted` があなたの
`permissions.deny` に与える影響を含む詳細は [セキュリティ](docs/ja/references/architecture.md#security) にあります。

## 対応プラットフォーム

Linux、macOS、ネイティブの Windows（`bin\dev-orchestra.ps1`）を CI で検証しています。WSL は
Linux として動きますが、必須ではありません。中身は純粋な Python と `git` だけです。Windows での
Antigravity の導入はジャンクションを作るので、開発者モードは要りません。

## アップグレード

```bash
/plugin marketplace update                     # Claude Code, inside a session
codex plugin marketplace upgrade               # Codex: re-fetches the snapshot only,
codex plugin add dev-orchestra@dev-orchestra   # so add it again to install it
```

チェックアウト導入は `git pull` で更新します（[詳細](docs/ja/references/workflow.md#installing-from-a-skill-checkout)）。
Antigravity はチェックアウトで `git pull` し、Antigravity を再起動します。
設定、成果物、コマンドはメジャーバージョン内で互換を保ちます（[互換性](#互換性)）。対応が必要な変更は
`CHANGELOG.md` に明記します。

## アンインストール

```bash
dev-orchestra hooks uninstall   # the reply-language hooks in your Claude Code settings, if any
claude plugin uninstall dev-orchestra
codex plugin remove dev-orchestra@dev-orchestra
dev-orchestra config reset --scope global --delete   # optional: your configuration
```

返答の言語のフックを入れていれば、プラグインを外す前に `dev-orchestra hooks uninstall` で
Claude Code の設定から外してください。設定は消さない限り残ります。成果物も消す場合は、プロジェクトごとに `.ai/` を削除してください。
チェックアウト導入には `install/uninstall.sh`（Windows は `.ps1`）があります。
Antigravity は `./install/uninstall.sh --antigravity`（Windows は `-Antigravity`）のあと、再起動します。

## 互換性

1.0.0 から[セマンティックバージョニング](https://semver.org/lang/ja/)に従います。約束の対象に
挙げたものを壊す変更はメジャー、追加はマイナー、修正はパッチです。対応が必要な変更は `CHANGELOG.md`
（[Keep a Changelog](https://keepachangelog.com/ja/1.1.0/)）に明記します。1.0.0 より前はマイナー
リリースでもこれらを壊すことがあり、そのときは `CHANGELOG.md` の Changed に、何をすればよいかと
ともにそう書きます。`review.timeout_seconds` をレビュアーだけに狭めたリリースがそうしたようにです。

約束の対象:

- **設定スキーマ**（`version: 1`）: `config.yaml` と `.dev-orchestra.yaml` の、文書に書かれた
  すべてのキーと、それが受け付ける値。キーは追加されることがありますが、消したり、名前を変えたり、
  意味や型を変えたりはせず、検証を通るファイルは通り続けます。built-in の既定値はマイナー
  バージョンで変わることがあり、そのときは Changed に旧い値と新しい値を書きます。
- **`dev-orchestra` のコマンド、そのフラグと終了コード**（[cli](docs/ja/references/cli.md)）。
  コマンド、フラグ、終了コードは追加されることがありますが、消えたり意味が変わったりはしません。
- **環境変数** `DEV_ORCHESTRA_CONFIG`、`DEV_ORCHESTRA_HOME`、`DEV_ORCHESTRA_WORKFLOW`、
  `DEV_ORCHESTRA_SESSION`、`DEV_ORCHESTRA_NO_USER_PROVIDERS`。
- **すべてのコマンドの `--json` 出力**: キーは追加されることがありますが、消したり、名前を
  変えたり、意味や型を変えたりはしません。決まった値のどれかを取るフィールド（`status`、
  `coverage`、`resume.reason`）の、文書に書かれた値は保ちます。`notes` と `warnings` の中の
  文は文章であり、対象外です。
- **`.ai/` の成果物の形式**: dev-orchestra が `.ai/workflows/<id>/` と `.ai/current.json` に
  書くもの。[workflow](docs/ja/references/workflow.md#artifacts) の「形式の変え方」の決まりに
  従います。レビュアーや architect のレポートはモデル自身の文章なので、置き場所は対象、文面は
  対象外です。
- **built-in のアダプタがすること**（claude、codex、agy、mock）: モード、読み取り専用の強制、
  再開、実行が記録するもの。各 CLI に渡すフラグは対象外です。
- **返答の言語のフックがすること**（[返答の言語のフック](docs/ja/references/architecture.md#reply-language-hooks)）:
  dev-orchestra がこれを Claude Code のユーザー設定に加えるのは、`language.reply` を設定したときか
  `hooks install` を実行したときだけです。そこでは自分のエントリだけを変え、`hooks uninstall` か
  `language.reply` を消したときに外します。フックは `language.reply` が設定されているときだけ、
  dev-orchestra を使ったセッションでだけ動き、委譲した実行の中では動きません。1 つの返答を
  ブロックするのは多くても 1 回で、どんなエラーでも何も出力せずに終わります。対象外: しきい値、
  判定の前に取り除くもの、理由の文面、中継のスクリプトとその記録、エントリの `command`/`args` の中身。
- **必要な Python の最低バージョンと対応プラットフォーム**: 引き上げたり外したりするのは
  メジャーバージョンです。

対象外で、マイナーバージョンで変わることがあるもの:

- **ユーザーのアダプタが継承する `Provider` 基底クラス**とその周辺の型
  （[providers](docs/ja/references/providers.md#interface-stability)）。この変更は
  `CHANGELOG.md` で **User adapters** から始まる項目に書きます。
- **すべてのコマンドの人向けの出力**。プログラムから使うときは `--json` を読んでください。
- **`scripts/smoke_live.py` とその他の保守用スクリプト**（`stamp_translation.py`、
  `doc_contents.py`、`validate_skill.py`）、その `--json`、それらが設定ディレクトリに書く
  `verified/` の記録。記録はマシンごとのもので `schema` 番号を持ち、`doctor` が報告し、
  schema が変わったらスクリプトをもう一度実行して作り直します。
- **`scripts/orchestrator/` 以下のモジュール**の Python API としての使い方。
- **`skills/dev-orchestra/SKILL.md` の文面**とプロンプトのテンプレート。それらが実行する
  コマンドは上のとおり対象です。
- **`DEV_ORCHESTRA_MOCK_*`** と `DEV_ORCHESTRA_TEST_ASSUME_NO_CLI`。テスト用の仕組みです。
- **`DEV_ORCHESTRA_DELEGATED`**。provider が起動するプロセスに付けて、フックを委譲した
  実行から外すための内部の仕組みです。

## アーキテクチャ

判断はモデルに、手順はコードに置きます。`skills/dev-orchestra/SKILL.md` が何を実行するかを決め、
`scripts/dev_orchestra.py` が決定的な処理を行い、CLIのフラグとモデル名を知っているのは provider だけです。
自前の adapter は `<設定ディレクトリ>/providers/` に置けます（[providers](docs/ja/references/providers.md)）。
詳細版は [アーキテクチャ](docs/ja/references/architecture.md) にあります。

## リファレンス

この README の詳細です。英語版はオーケストレーターのモデルも読むドキュメントで、こちらが正です。日本語版は人が読むための訳です。

| 日本語版 | 英語版 | 答えている問い |
| --- | --- | --- |
| [workflow.md](docs/ja/references/workflow.md) | [references/workflow.md](references/workflow.md) | 各工程で何をするか、プロンプトのテンプレート、`.ai/` の成果物、ワークフローの例、チェックアウトからの導入 |
| [configuration.md](docs/ja/references/configuration.md) | [references/configuration.md](references/configuration.md) | スキーマ、階層、全フィールド、モデルの family と tier、ウィザード、設定例 |
| [providers.md](docs/ja/references/providers.md) | [references/providers.md](references/providers.md) | adapter のインターフェース、Claude と Codex と agy、CLI の追加方法 |
| [reviews.md](docs/ja/references/reviews.md) | [references/reviews.md](references/reviews.md) | スナップショット、送らないファイル、修正だけを見る2巡目、出力の形式、重複統合、トリアージ |
| [architecture.md](docs/ja/references/architecture.md) | [references/architecture.md](references/architecture.md) | 構成要素のつながり、その理由、セキュリティ |
| [limits.md](docs/ja/references/limits.md) | [references/limits.md](references/limits.md) | stall、タイムアウト、予算、大量の入力、実行コスト、最適化レベル |
| [cli.md](docs/ja/references/cli.md) | [references/cli.md](references/cli.md) | 全コマンドとフラグ、トラブルシューティング |

## コントリビュート

issue と pull request を歓迎します。詳細は `CONTRIBUTING.md`（英語）を参照してください。セキュリティ上の
問題の報告方法もそこにあります（公開issueは立てないでください）。とくに、**CLIがフラグやモデル名を
変更したときの adapter 修正**が最も価値の高い貢献です。

## ドキュメントの言語方針

Skill と `references/` は、AIモデルが最もよく読める英語で書きます。README は [English](README.md) と
[日本語](README.ja.md) の両方があります。`docs/ja/references/` の訳は人が読むためのもので、英語版が正です。
各訳は訳した英語版の sha256 を記しており、英語版が変わって訳が追いついていないとテストが失敗します。

## ライセンス

MIT — `LICENSE` を参照してください。
