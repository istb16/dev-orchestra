<!-- translated-from: references/configuration.md sha256:21569d260122292d480c3be1c4a50d6b44e600b6aea2359c3f40118907aa17b7 -->

> この文書は [references/configuration.md](../../../references/configuration.md) の日本語訳です。内容が食い違うときは英語版が正です。

<a id="configuration"></a>

# 設定

<a id="where-it-lives"></a>

## 設定の置き場所

| レイヤー | パス | 用途 |
| --- | --- | --- |
| プロジェクト | `<repo>/.dev-orchestra.yaml` | リポジトリごとの上書き。commit するかどうかはお好みで |
| グローバル | 下記参照 | すべてのプロジェクトに対するあなた個人のデフォルト |
| 組み込み | `scripts/orchestrator/config.py` | 推奨デフォルト。ファイルが存在しないときに使われます |

設定ディレクトリ内の `providers/` ディレクトリには、あなた自身の provider
adapter を置きます（Windows では `%APPDATA%\dev-orchestra\providers\`、
それ以外では `~/.config/dev-orchestra/providers/`、それが設定されている場合は
`$DEV_ORCHESTRA_HOME/providers/`。`DEV_ORCHESTRA_CONFIG`
ではこの場所は変わりません）。`references/providers.md` を参照してください。

プラットフォーム別のグローバル設定のパス:

| プラットフォーム | パス |
| --- | --- |
| Linux / BSD | `$XDG_CONFIG_HOME/dev-orchestra/config.yaml`、なければ `~/.config/dev-orchestra/config.yaml` |
| macOS | `~/.config/dev-orchestra/config.yaml` |
| Windows | `%APPDATA%\dev-orchestra\config.yaml` |

環境変数による上書き:

- `DEV_ORCHESTRA_CONFIG` — このファイルをそのままグローバルレイヤーとして使います。
- `DEV_ORCHESTRA_HOME` — プラットフォームのデフォルトの代わりにこのディレクトリを使います。
- `DEV_ORCHESTRA_NO_USER_PROVIDERS` — 空または `0` 以外の値を設定すると、
  ユーザー adapter ディレクトリ（`<config dir>/providers/`）を完全にスキップします。

`dev-orchestra config path` は、解決された両方の場所を表示します。

プロジェクトファイルは、カレントディレクトリから上にたどり、git のルートで
止まって探索されます。そのため、サブディレクトリから CLI を実行しても見つかります。
受け付けるファイル名は次の順です: `.dev-orchestra.yaml`、`.dev-orchestra.yml`、
`.dev-orchestra.json`。

<a id="precedence"></a>

## 優先順位

```
project config  →  global config  →  built-in defaults
```

マッピングはキーごとにマージされるので、`implementer` だけを設定したプロジェクト
ファイルでも、グローバルの architect はそのまま維持されます。**リストは丸ごと置き換わります**:
`reviewers` を定義したプロジェクトファイルは、そのプロジェクトのパネル全体を定義します。
これは意図的です — 「このリポジトリは security + database だけでレビューする」を
表現できなければならないからです。

<a id="schema-version-1"></a>

## スキーマ（version 1）

```yaml
version: 1

orchestrator:                 # decides stages, delegates, writes the report
  provider: claude
  model:
    family: sonnet
    version: latest

architect:                    # investigation + design, read-only
  provider: claude
  model:
    family: fable
    version: latest

implementer:                  # writes code and tests
  provider: claude
  model:
    family: opus
    version: latest

review_fixer:                 # fixes accepted findings
  provider: claude
  model:
    family: opus
    version: latest

reviewers:                    # 0..n independent reviewers
  - id: claude-general
    provider: claude
    model:
      family: opus
      version: latest
    role: general
  - id: codex-general
    provider: codex
    model:
      family: recommended-coding
      version: latest
    role: general

review:
  max_review_iterations: 2            # hard stop on review→fix→re-review loops
  parallel: true                      # run reviewers concurrently
  re_review_severities: [critical, high]
  timeout_seconds: 1800               # per delegated CLI run
  design:
    enabled: false                    # review .ai/plan.md before implementing
    max_iterations: 2                 # design review -> revise -> re-review

design:
  require_approval: true              # implementer waits for the user's yes (design approve)

workspace:
  dir: .ai                            # relative to the repo root, or absolute
```

<a id="field-reference"></a>

### フィールドリファレンス

| Field | 型 | 備考 |
| --- | --- | --- |
| `version` | int | `1` でなければなりません。 |
| `<role>.provider` | string | 登録済みの adapter: `claude`、`codex`、`mock`、またはユーザー adapter（`references/providers.md` を参照）。 |
| `<role>.model.family` | string | provider が解決できる family/エイリアス（`opus`、`sonnet`、`fable`、`recommended-coding`）。省略するか `default` を使うと CLI に選ばせます。 |
| `<role>.model.version` | `latest` \| `pinned` | `latest` は実行のたびに解決し直します。`pinned` には `model.id` が必要です。 |
| `<role>.model.id` | string | 正確なモデル id。`version: pinned` のときのみ。 |
| `reviewers[].id` | string | 一意で、`[a-z0-9][a-z0-9._-]*` に一致すること。レポートファイルの名前になります。 |
| `reviewers[].role` | string | 組み込みのもの、または独自のもの。`references/reviews.md` を参照。 |
| `review.max_review_iterations` | int ≥ 0 | プロジェクト単位ではなくレビュー単位のラウンド数です。新しいブランチ、新しい `--base`、または `budget reset` でカウントはリセットされます。`0` で再レビューを完全に無効にします。 |
| `review.parallel` | bool | `false` にするとレビュアーを 1 つずつ実行します（デバッグしやすくなります）。 |
| `review.re_review_severities` | list | ブロッキングとみなす severity。 |
| `review.timeout_seconds` | int > 0 | 実行ごとのタイムアウト。タイムアウトは報告されるだけで、例外にはなりません。 |
| `review.exclude` | list | diff 本文をレビュアーに渡さない glob パターン。デフォルトのリストを丸ごと置き換えます。`[]` ですべてをレビューします。 |
| `review.incremental_rounds` | bool | `true`（デフォルト）にすると、2 回目のラウンドは 1 回目のラウンドがレビューした内容に対する diff になり、修正が対処しようとした指摘を引き継ぎます。`false` にすると毎ラウンド変更全体の diff を取り直します。 |
| `review.max_findings` | int \| null | 各レビュアーに求める指摘の数。`null`（デフォルト）は `optimization.level` に任せ、`0` は上限を外します。上限を超えて返ってきた指摘は保持され、切り捨てられることはありません。 |
| `review.context.max_chars` | int ≥ 1 \| null | レビューラウンドがそもそも送信する変更本文の最大サイズです。対象は diff、または plan とそれが答える依頼です（デフォルト 400,000 ≈ 100k トークン。ここで記録された最大のプロンプトの 4 倍で、これまで誰かが実行したものは何も拒否しません）。これを超えると、`review run` は何もレビューせずに exit 3 で終了します。上限内に収める方法は、`--base`、`review.exclude`、変更の分割、または plan を短くすることです。`--force` は人間による上書きで、そのラウンドを予算超過として記録します。`null` は `review.max_findings` と同様にデフォルトを意味します。この上限を無効にする値はありません。`references/limits.md` を参照。 |
| `review.context.inline_chars` | int ≥ 1 \| null | 変更本文のうちどれだけをレビュアーのプロンプトに含めるか（デフォルト 400,000、`max_chars` と同じ数値）。これ以下なら本文はインラインで渡され、ラウンドは clean になり得ます。これを超えると、レビュアーには凍結されたスナップショットのパスが渡され、何が返ってきてもラウンドは `partial` — カバレッジ未検証 — として記録されます。**`max_chars` より小さく設定すると、両者の間に、ラウンドは実行されるものの `partial` として記録される帯域が生まれます**: これは非常に大きなプロンプトに費用をかけたくない人が明示的に選ぶもので、`partial` はその代償です。`max_chars` *より大きく*設定することも許されており、誤りではありません — その場合、本文がファイルとして渡されるのは人間が強制したラウンドだけになります。`null` はデフォルトを意味します。各ラウンドは比較に使った数値を記録するので、`partial` のラウンドはどの上限によってそうなったのかがわかります。`references/limits.md` を参照。 |
| `review.context.surrounding` | `none` \| `enclosing` | `enclosing` にすると、各 hunk を囲む Python の関数・メソッド・クラスも、すべてのコードレビュアーに渡します。スナップショットの取得時に、その git ツリーから抽出します（デフォルト `none`: diff だけ）。効果が計測されるまでは off です — `optimization report` が、これを使ったラウンドと使わなかったラウンドを比較します。`false` と `null` は `none` を意味します（`off` は `false` として読まれます）。`true` は拒否されます。`references/reviews.md` を参照。 |
| `review.context.surrounding_chars` | int ≥ 1 \| null | 1 つのラウンドが追加できる周辺コンテキストの最大量（デフォルト 60,000: 記録済みのどのワークフローも切り詰めなしで収まり、最大でも 49,371 でした）。さらに、diff が `max_chars` と `inline_chars` の下に残す分で上限がかかるので、コンテキストがラウンドを拒否させたり、diff をファイル渡しにしたりすることはありません。収まらなかったものは、プロンプトとすべてのレポートで名前を挙げて除外されます。`null` はデフォルトを意味します。`references/limits.md` を参照。 |
| `review.design.enabled` | bool | `false`（デフォルト）は設計レビューを完全にスキップします。`true` にすると、実装の前に `.ai/plan.md` を同じパネルにかけます。このステージはラウンドごとにパネルのメンバー 1 人につきレビュアー実行 1 回分のコストがかかるため、オプトインになっています。 |
| `review.design.max_iterations` | int ≥ 0 | 設計レビューのラウンド数（レビュー → トリアージ → 修正）。`max_review_iterations` とは別にカウントされます（デフォルト 2）。上限に達したラウンドでも修正は行われます。上限が拒否するのはその後の再レビューだけです。`1`: 1 ラウンド、1 回の修正、その後ユーザーに確認。`0`: 設計レビューなし。`budgets.architect`（デフォルト 3）は、デフォルトでは設計とラウンドごとに 1 回の修正をまかないます。`max_iterations` に合わせて引き上げ、承認時に変更を求められることが予想される場合はさらに 1 つ増やしてください。 |
| `design.require_approval` | bool | `true`（デフォルト）にすると、`.ai/plan.md` が存在し、現時点の plan が `design approve` で承認されていない間は -- ユーザーが了承した後に承認するものです -- `run implementer` が拒否します（exit 5）。`false` は誰も見ていない実行（CI、バッチ）向けで、このゲートが導入される前の挙動に戻します。`review.design` の下ではなくトップレベルにあるのは、パネルが plan をレビューしたかどうかにかかわらず承認が重要だからです。`--force` ではバイパスできず、この設定だけがバイパスできます。 |
| `optimization.level` | `aggressive` \| `balanced` \| `quality` | どれだけ安く済ませようとするか。デフォルトは `balanced`。下記を参照。 |
| `optimization.high_risk_paths` | list | それに触れる変更に対して `quality` を強制する glob。デフォルトのリストを丸ごと置き換えます。 |
| `optimization.low_risk_max_files` | int | `quality` 未満のレベルで、小さな変更とみなすファイル数の上限（デフォルト 5）。 |
| `optimization.low_risk_max_lines` | int | さらに、変更行数の上限（デフォルト 150）。 |
| `workspace.dir` | string | `.ai/` の成果物を置く場所。 |
| `<role>.options` | mapping | provider 固有の設定項目。下記を参照。 |
| `<role>.model_tiers` | mapping | このロールのモデルに対する名前付きの代替。下記を参照。省略可。 |

<a id="role-options"></a>

### ロールのオプション

`options` は意図的に provider 固有になっています -- Claude の permission mode を
Codex の sandbox ポリシーに正直に対応付ける方法はないので、CLI を受け持つ adapter が
自分のキーも受け持ちます。キーは adapter が検証するので、typo は実行時ではなく
`config validate` で検出されます。

| provider | キー | 値 |
| --- | --- | --- |
| any | `args` | 追加の CLI 引数のリスト。そのまま末尾に追加されます |
| `claude` | `output_format` | `stream-json`（デフォルト）、`text`、`json`。`text` にすると stall 検出が無効になります |
| `claude` | `permission_mode` | インストールされている CLI が `--permission-mode` に対して提示するもの（`dev-orchestra model list` とは別に、`claude --help` を実行して確認してください） |
| `codex` | `sandbox` | `read-only`、`workspace-write`、`danger-full-access` |
| `codex` | `approve` | `true`（デフォルト）は `--approve-for-me` を渡し、`false` は省略します |
| any | `idle_timeout` | このロールの無出力期限を上書きします |

```yaml
implementer:
  provider: claude
  model:
    family: opus
    version: latest
  options:
    # The default, acceptEdits, auto-approves file edits but not shell commands,
    # so an Implementer told to "run the tests" may be unable to. Loosen it here
    # if your environment makes that appropriate.
    permission_mode: bypassPermissions
    args: ["--add-dir", "../shared-lib"]
```

**読み取り専用ステージを緩めるようなオプションは無視されます。** architect と
すべてのレビュアーは、`permission_mode` や `sandbox` が何と言っていても常に読み取り専用で
実行されます -- この不変条件こそが、独立したレビューに価値を与えるものです。
`dev-orchestra doctor` は、その理由で無視しているオプションを黙って捨てるのではなく
一覧表示します。

permission mode を緩める代わりの方法は、CLI 自身の設定で特定のコマンドを
許可リストに入れることです（Claude Code なら、`.claude/settings.json` の
`Bash(pytest:*)` のような `permissions.allow` エントリ）。こちらのほうが範囲が狭く、
このスキルではなくプロジェクトの側に置かれます。

`mock` は実在する登録済みの provider です。テストで使われるオフラインの adapter で、
トークンを消費せずにパイプラインを試運転するのにも便利です。セットアップウィザードには
表示されません。

<a id="optimization-level"></a>

## 最適化レベル

3 つの節約を 1 つのダイヤルで調整します。デフォルトは `balanced` です。

| | `aggressive` | `balanced` | `quality` |
| --- | --- | --- | --- |
| テストが失敗と記録されている | 拒否 | 拒否 | それでもレビュー |
| テスト結果が記録されていない | 警告してレビュー | 警告してレビュー | レビュー |
| 小さく低リスクな変更 | レビュアー 1 人 | レビュアー 1 人 | パネル全体 |
| 求める指摘の数 | 4 | 6 | 10 |

`--force` で拒否を越えられます。`--only` は縮小されたパネルを上書きします。
このフラグは誰かがレビュアーを手で指名しているということだからです。

**ゲートは記録された結果を読むだけで、何も実行しません。** このツールには
プロジェクトのテストコマンドを知る手段がありません -- オーケストレーターがリポジトリから
それを見つけ出して直接実行します -- そのため、ゲートは直近の
`dev-orchestra state record test ok|failed` が書き込んだものを読みます。状態は 2 つではなく
3 つです: 成功、失敗、そして未記録。拒否するのは記録された失敗だけです。未記録の場合は
警告して続行するので、`state record` を採用していないワークフローはこれまでとまったく
同じように動き続けます。

**高リスクな変更は、設定にかかわらず `quality` に引き上げられます。** 認証、
シークレット、決済、マイグレーション、SQL、暗号、またはデプロイ設定に触れる変更は、
パネル全体と指摘の予算の全量を得ます。パターンは設定可能です
（`optimization.high_risk_paths`、`[]` でクリア）。ただし、一致したら引き上げられる
という事実は変えられません。引き上げは原因となったファイルとパターンとともに表示されるので、
ただ従うだけでなく確認することもできます。

パターンは意図的に広めに一致します。`authors_controller.rb` は `*auth*` に一致し、
レビュアーが 1 人余分にかかります。`auth_controller.rb` を見逃すと、認可のバグという
代償を払うことになります。

**0.4.2 以降、`balanced` でもパネルが縮小されます。** これを `aggressive` に
限定していたため、最も必要としているリポジトリでは到達不能になっていました: 高リスクの
一致は `quality` に引き上げられ、`quality` は `aggressive` ではないので、`*.tf` がほとんどの
ラウンドで一致するインフラのリポジトリでは、このダイヤルがまったく作動できなかったのです。
`balanced` での実際の 11 ラウンドで計測したところ、パネルが縮小されたのは 0 回で、旧来の
2 ファイル / 50 行のしきい値に近づいたラウンドもありませんでした。両方が同時に引き上げられました。

いまでは、常にパネル全体の費用を払うレベルは `quality` だけであり、それがこのレベルの
意味するところです。

**サイズだけが判定基準になることはありません。** 縮小されたパネルになるには、変更が
両方のしきい値を下回り、*かつ*高リスクなものに何も触れていない必要があります。auth ファイルの
1 行こそ、認可のバグがまさに入り込む形だからです。

```yaml
optimization:
  level: aggressive
  low_risk_max_files: 3
  low_risk_max_lines: 80
```

<a id="model-tiers"></a>

## モデルティア

同じロール、同じプロンプトで、背後のモデルだけを変えます。1 行の修正は安いものに、
厄介な設計は高価なものに、セカンドオピニオンは別のベンダーに渡す、といった使い方です:

```yaml
implementer:
  provider: claude
  model:
    family: opus
    version: latest
  model_tiers:
    light:
      model:
        family: sonnet
        version: latest
    second-opinion:
      provider: codex
```

```bash
dev-orchestra run implementer --tier light --prompt-file .ai/execution/fix.md
```

ティアは 2 つ目のロールではなく、ロールごとの上書きです。implementer とは*何か*の定義を
2 つ持つことこそが高くつくからです。上書きできるのは `provider`、`model`、`options` だけで、
それ以外は黙って無視されるキーではなく設定エラーになります。

**選ぶのは呼び出し側です。** diff のサイズからティアを推測するものは何もありません。
タスクの難しさを知っているのはオーケストレーターだけで、推測を誤ると、ティアが制御する
ために存在するまさにそのコストを費やしてしまいます。

**未知のティアはエラーであり、決してフォールバックしません。** 黙って無視されたティアは、
何も言わずにデフォルトのモデルで作業を実行します -- 安いものを求めたのに高価なもので、
あるいは慎重さを求めたのに安いもので。

**各キーはマージされず、丸ごと置き換えられます。** そうしないと、id に pin された
ベースの上で family を指定したティアが pin を引き継ぎ、誰も求めていないモデルを実行して
しまいます。`provider` を変更すると、前の provider の `model` と `options` も一緒に
破棄されます: `opus` は Codex にとって意味がなく、`permission_mode` は Codex には存在しません。
新しい provider で特定の設定をしたいティアは、それを明示します。

ティアは `config show` で表示され、ティアを使った実行は `tokens show` でそれ専用の行に
記録されます。そのため、ティアが投げかける疑問 -- 安いほうは本当にコストが少なかったのか --
には答えが出ます。

レビュアーにはティアがありません。パネルはすでにレビュアーごとに 1 つのモデルであり、
それは別の名前で呼ばれる同じルーティングです。

<a id="model-families-and-version-policy"></a>

## モデル family とバージョンポリシー

設定に保存するのは**どんな種類のモデルが欲しいか**であり、**どのスナップショットを
得たか**ではありません。`family: opus` + `version: latest` は「インストールされている CLI が
提供する最新の Opus」を意味するので、モデルがリリースされた後もセットアップはそのまま動き続けます。

```yaml
implementer:            # tracks the latest Opus, whatever that is today
  model:
    family: opus
    version: latest

architect:              # frozen to one snapshot - only do this deliberately
  model:
    family: opus
    version: pinned
    id: claude-opus-5

review_fixer:           # let the CLI pick entirely
  model:
    family: default
    version: latest
```

解決は実行時に provider adapter の中で行われます。family を検証できない adapter は、
推測した名前を CLI に送るのではなく `ModelResolutionError` を送出します。*解決された* id は
追跡可能性のために `.ai/state.json` に記録され、設定ファイルには family が残ります。

<a id="what-the-user-asks-for-and-what-to-run"></a>

## ユーザーの依頼と実行するコマンド

コマンドの文法はスキルが持っています。こちらはその表現集です。

| ユーザーの発言 | 実行するもの |
| --- | --- |
| 「設定を見せて」 | `config show` |
| 「セットアップして」/「セットアップをやり直して」 | `config setup`、または `config setup --defaults` |
| 「どのモデルが使える？」 | `model list` |
| 「実装には Claude Opus を使って」 | `config set implementer.model.family opus` |
| 「architect に Codex を使わせて」 | `config set architect.provider codex` **と**、Codex が受け付ける family |
| 「implementer がテストを実行できない」 | `config set implementer.options.permission_mode bypassPermissions`、またはその CLI 自身の設定でコマンドを許可リストに入れる |
| 「設計もレビューして」 | `config set review.design.enabled true` |
| 「plan の承認を求めないで」/ CI で実行する | `config set design.require_approval false` |
| 「Codex のセキュリティレビュアーを追加して」 | `reviewer add --provider codex --role security` |
| 「レビュアーを 3 人にして」 | もう一度 `reviewer add …`、その後 `reviewer list` |
| 「パフォーマンスのレビュアーを外して」 | `reviewer remove performance` |
| 「2 番目のレビュアーを変えて」 | `reviewer set 2 --provider … --role …` |
| 「このプロジェクトだけ」 | 書き込み系のコマンドに `--scope project` を付ける |
| 「自分の設定を元に戻して」 | `config reset --scope global`（あなたの上書きをクリアし、組み込みのデフォルトだけが残ります） |
| 「このプロジェクトの上書きを捨てて」 | `config reset --scope project`（以後そのプロジェクトはグローバルレイヤーに従います） |
| 「設定が古いバージョンのものだ」 | `config prune --dry-run`、その後 `config prune` |
| 「環境をチェックして」 | `doctor` |

書き込みの後は必ず結果の設定を表示し、ユーザーが確認できるようにしてください。

<a id="editing"></a>

## 編集

```bash
dev-orchestra config show                    # effective configuration
dev-orchestra config show --scope project    # just the project layer
dev-orchestra config setup                   # interactive wizard
dev-orchestra config setup --defaults        # non-interactive, recommended values
dev-orchestra config set implementer.model.family sonnet
dev-orchestra config set --scope project architect.provider codex
dev-orchestra config set reviewers[1].role security
dev-orchestra config reset                   # clear this layer's overrides
dev-orchestra config reset --delete          # remove the file entirely
dev-orchestra config prune                   # drop values equal to what is inherited
dev-orchestra config validate
```

**保存された設定には、あなたが設定したものだけが入ります。** それ以外はすべて、設定の
読み込み時に下のレイヤーから解決されます。そのため、後のリリースで改善されたデフォルトは、
セットアップを実行した日の時点のコピーに隠されることなく、あなたの環境に届きます。したがって
`config setup --defaults` は `version: 1` だけを書き込みます: 推奨設定を選ぶということは、
何も上書きしないことを選ぶということです。`config show --scope global|project` はそのレイヤーを
ディスク上にあるとおりに表示し、`config show` は解決された結果の設定を表示します。

**0.6.0 より前に書き込まれたファイルには、すべてのデフォルトがそのまま入っています**。
0.4.2 で引き上げられた低リスクのしきい値が、それ以前にセットアップを実行した人に届かなかったのは
このためです。`doctor` は、組み込みのデフォルトが変わって値がずれた設定を、両方の数値とともに
一覧表示します。`config prune` は、そのレイヤーが継承する値と等しい値を、要求されたときにだけ
削除します -- 意図的な選択と継承されたデフォルトはディスク上では見分けがつかないので、この
コマンドは等しいことを根拠とみなし、そう明示します:

```bash
dev-orchestra config prune --dry-run         # list what would be dropped
dev-orchestra config prune --scope project
```

プロジェクトファイルは、組み込みのデフォルトではなく*あなたの*グローバルレイヤーと比較して
prune されます。そのため、グローバルの値を打ち消すためにそこに置かれた値は残ります。その裏返しとして、
チームで共有している `.dev-orchestra.yaml` を prune するのは、あなた自身のグローバルレイヤーが
何も上書きしていないときだけにしてください。そうでないと、結果があなたのマシンに左右されてしまいます。

`config set` は値を型変換します: `3` は int に、`true` は bool に、`[a, b]` は list に、
それ以外は string になります。string を強制するには `--raw` を使います。

書き込み先は、プロジェクトレイヤーが存在すればそこ、なければグローバルレイヤーです。
`--scope global|project` で明示的に指定できます。**リストの 1 エントリを編集すると
リスト全体が書き込まれます**。リストは下のリストを丸ごと置き換えるからです:
`reviewers[1].role`、`review.exclude[0]`、`optimization.high_risk_paths[2]` はいずれも、
まず下のレイヤーからリストの残りをコピーします -- グローバルレイヤーなら組み込みのデフォルトから、
プロジェクトレイヤーならグローバルレイヤーから。そのため、プロジェクトのパネルがあなたの
グローバルファイルに入り込むことはありません。リストの末尾を超えるインデックスは新しいエントリには
ならず、エラー（exit 2）になります。

<a id="the-wizard"></a>

### ウィザード

`config setup` は、あなたが答えた内容だけを保存します。推奨される回答と、あなたが承認する
サマリーは、編集中のレイヤーが継承するものから導かれます: sonnet を選んだグローバルレイヤーの上で
プロジェクトレイヤーをセットアップすると sonnet が提示され、グローバルレイヤーで設計レビューを
有効にしていればサマリーでも有効と表示されます。そのため、保存前に目にするものが、その後
`config show` が報告するものになります。

Enter を押して受け入れた回答もやはり回答であり、保存されます。4 つのロールについては、これは
後から見てわかります -- `doctor` は、組み込みのデフォルトが変わって family がずれたロールを
報告します。**`reviewers` についてはわかりません**: `doctor` はパネルをデフォルトのパネルと
比較することはありません。比較すると、レビュアーを追加したすべての環境が報告されてしまうからです。
そのため、ウィザードが書き込んだパネルは、その日のまま黙って残り続けます。元に戻す方法は 2 つで、
レイヤーに置かれているパネルを表示する `config show --scope global|project` と、現在の
デフォルトのパネルと等しいままならそれを削除する `config prune` です。何も上書きしたくない
のであれば、`config setup --defaults` を使ってください。

<a id="worked-examples"></a>

## 実例

**チーム全体で同じパネルを使うべきリポジトリ** — `.dev-orchestra.yaml` を
commit します:

```yaml
version: 1
reviewers:
  - id: claude-general
    provider: claude
    model:
      family: opus
      version: latest
    role: general
  - id: codex-security
    provider: codex
    model:
      family: recommended-coding
      version: latest
    role: security
  - id: codex-database
    provider: codex
    model:
      family: recommended-coding
      version: latest
    role: database
```

**CLI が 1 つしかインストールされていない** — もう一方の provider をすべての箇所から外します:

```bash
dev-orchestra config set architect.provider claude
dev-orchestra reviewer remove codex-general
dev-orchestra reviewer add --provider claude --role security
```

**小さなリポジトリ向けの安くて速いループ**:

```bash
dev-orchestra config set implementer.model.family sonnet
dev-orchestra config set review.max_review_iterations 1
dev-orchestra reviewer remove 2
```

**レビューをまったく行わない**（有効な設定で、`doctor` がそれを指摘します）:

```bash
dev-orchestra reviewer remove 1
dev-orchestra reviewer remove 1
```

<a id="yaml-dialect"></a>

## YAML の方言

設定ファイルは、PyYAML がインストールされていれば PyYAML で、そうでなければ組み込みの
パーサーで解析されます。組み込みパーサーは、ブロックマッピング、ブロックシーケンス、インラインの
空コレクション、インラインのスカラーリスト、コメント、クォートされた文字列に対応しています。
アンカー、エイリアス、複数ドキュメントのストリーム、ブロックスカラー（`|`、`>`）は、わかりやすい
エラーとともに拒否されます。JSON は常に受け付けます。
