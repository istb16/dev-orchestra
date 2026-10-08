<!-- translated-from: references/cli.md sha256:d34aec7f617954b4c859b0aa259dcbee67b41f89c36569925de7460e5f1ad243 -->

> この文書は [references/cli.md](../../../references/cli.md) の日本語訳です。内容が食い違うときは英語版が正です。

<a id="cli-reference"></a>

# CLI リファレンス

<!-- contents: start -->

**目次**

- [config](#config)
- [model](#model)
- [reviewer](#reviewer)
- [doctor](#doctor)
- [hooks](#hooks)
- [run](#run)
  - [architect 自身のセッションで plan を改訂する（`--resume`）](#revising-the-plan-in-the-architects-own-session---resume)
- [review](#review)
- [design](#design)
- [status](#status)
- [jobs](#jobs)
- [budget](#budget)
- [tokens](#tokens)
- [optimization](#optimization)
  - [Architect revisions](#architect-revisions)
- [progress](#progress)
- [workflow](#workflow)
- [state / summary](#state--summary)
- [環境変数](#environment-variables)
- [トラブルシューティング](#troubleshooting)

<!-- contents: end -->

```
python scripts/dev_orchestra.py <command> [options]
bin/dev-orchestra <command> [options]           # POSIX wrapper
bin\dev-orchestra.ps1 <command> [options]       # Windows wrapper
```

インタプリタの名前が `python3` しかない環境では、`python` を `python3` と読み替えてください。
ラッパーはインタプリタを自分で探します。POSIX では `python3`、次に `python` の順、Windows では
`python`、`py`、`python3` の順です。Windows では PATH 上の `python3` はたいていインタプリタではなく
Microsoft Store のエイリアスだからです。

グローバルオプション: `--cwd <dir>`（そのディレクトリから実行したものとして動作します）、
`--workflow <id>`（これらの成果物が属するワークフロー。後述）、
`--version`。

終了コード: `0` 成功、`1` 操作は実行されたが結果が否定的（設定が無効、スナップショットが空、
すべてのレビュアーが失敗、ロールの実行が失敗）、`2` 使い方または設定のエラー、`3` 予算が尽きて
コマンドが実行を拒否した、`4` ジョブがまだ実行中のまま `jobs wait` が戻った、`5` plan が承認されて
おらず `design.require_approval` が有効、`130` 中断。

<a id="config"></a>

## config

| コマンド | 説明 |
| --- | --- |
| `config show [--scope effective\|global\|project] [--json]` | 設定を表示します。デフォルトは `effective`（マージ済み）です。スコープを指定すると、そのレイヤーをディスク上にあるとおりに表示し、たいていはずっと短くなります。effective の表示では `Source:` の後に有効なプリセット（`Preset: quality (global; fitted to claude, codex)`）を示し、フィットし直した点ごとに `note:` 行を出します。`--json` では `preset`（`name`、`source`、`notes`）に入ります。effective の `--json` には、`Source:` に並ぶファイルが `project` と `global` として入ります（ないときは `null`）。`Providers:` 行は、参照している各 provider がどこから来ているか（built-in、ユーザーモジュールのパス、または adapter なし）を示します。`--json` では同じ内容が `providers` の下に入ります。サマリーには、各席の高リスク用のモデル（`(opus when high-risk)`）と `relevance`、最適化レベルの下の `skip unneeded roles: on\|off  (optimization.skip_unneeded_roles)` 行、そして `Design reviews` ブロックが表示されます。このブロックは、フィットかファイルが design パネルを設定していればそれを、なければ `(the code panel; when conditions ignored)` を示します。design パネルがあるときは、effective の `--json` に `design_reviewer_origins`（フィットした席は `fit design`）が加わります。続いて `Reply language: <tag>  (language.reply)` の行が出ます。設定がなければ `not set`、Stop フックの判定を止めているときはタグの後に `(no rewrite: language.rewrite false)` が付きます。 |
| `config path` | 両方のレイヤーの場所を表示します。 |
| `config setup [--scope global\|project] [--preset quality\|standard\|fast \| --defaults] [--language TAG] [--no-hooks] [--force]` | セットアップウィザードです。グローバルファイルでは最初の質問がプリセットで、最後の質問（保存の直前）が返答の言語です（`ja` のようなタグ。タグか空欄になるまで聞き直し、空欄ならファイルの値を残し、`none` で消します）。`--language TAG` は `--preset` や `--defaults` と一緒に使うと `language.reply` を書き込み、ウィザードでは最初に示す答えになります。タグでない値なら、何も書かずに終了コード 2 で終わります。保存のあと、`config set language.reply` と同じように、このファイルの保存前と保存後の値をもとに Claude Code のユーザー設定に返答の言語のフックを入れたり外したりします。`--no-hooks` を付けるとそれをしません。`--preset` は何も尋ねません: `version` と `preset` を書き込み、プリセットが決めるキー以外にファイルが持っていた値は残し（ロールは `options` と `model_tiers` を残し、そのためフィットされません）、そのプリセットがこのマシンで解決される設定を note とともに表示します（`references/configuration.md` のプリセットを参照）。プリセットを指定できるのはグローバルファイルだけで、`--scope project` では拒否され（exit 2）、何も書き込みません。`--defaults` は何も上書きしないため、ファイルには `version: 1` だけが入り、プリセット `standard` で動きます。`--force` は TTY がなくてもプロンプトを表示します。 |
| `config reset [--scope …] [--delete]` | このレイヤーの上書きを消去し（ファイルは残り、`version` だけ、グローバルファイルなら既知の `preset` も入った状態になります。知らないプリセット名は `note:` を表示して消します）、残った設定を表示します。`--delete` を付けるとファイルを削除し、グローバルレイヤーは `standard` で動きます。リセットでこのファイルの `language.reply` が消え、グローバルのファイルにもこのプロジェクトのファイルにも設定がなくなれば、返答の言語のフックを外します。リセットでフックを入れることはありません。 |
| `config prune [--scope …] [--dry-run]` | レイヤーが持つ値のうち、継承される値と等しいものを削除します。すべてのデフォルトを保持している 0.6.0 より前に書かれたファイル向けです。値を削除するのは組み込みのデフォルトとプリセットのフィットがどちらもその値で一致するときだけなので、prune で有効な設定が変わることはありません。同じ理由で、`reviewers` を削除すると設計レビューがファイルのパネルのコピーからプリセットの設計パネルに移ってしまう場合は、`reviewers` を残し、その理由を 1 行で表示します。`--dry-run` は書き込まずに一覧表示します。 |
| `config set <path> <value> [--scope …] [--raw] [--no-hooks]` | 値を 1 つ設定します。タグのなかったファイルで `language.reply` をタグにすると、Claude Code のユーザー設定へ返答の言語のフックを入れます。入れるのは設定ディレクトリがあるときだけで、なければ `note:` で `hooks install` を案内します。どのタグを設定しても、入っているが古いフックは直します。すでにタグのあったファイルでタグを変えたとき、フックが入っていなければ入れず、`note:` で `hooks install` を案内します。プロジェクトのファイルなど、ほかのファイルだけが設定しているタグでフックを入れることはありません。ファイルから消して、グローバルのファイルにもこのプロジェクトのファイルにも設定がない状態にするとフックを外し、ほかのプロジェクトでファイルに設定していればそこでも判定が止まると伝えます。グローバルのタグの上にプロジェクトで `null` を設定しても外しません。`--no-hooks` を付けると値だけを保存し、Claude Code には触れません。委譲された実行の中でも触れません。編集できない設定ファイルは `warning:` になるだけで、値は保存され、終了コードは 0 のままです（[hooks](#hooks) を参照）。パスは `a.b.c` と `reviewers[0].role` をサポートします。インデックス付きの編集では、リストの残りを下のレイヤーからコピーします。末尾を超えたインデックスは終了コード 2 で終了します。`preset` は、プロジェクトファイルがあってもグローバルファイルに書き込みます。`--scope project` を付けると終了コード 2 で終了し、何も書き込みません。読み取り専用の席の provider（`orchestrator.provider`、`architect.provider`、`<role>.model_tiers.<tier>.provider`、`reviewers[<n>].provider`、`review.design.reviewers[<n>].provider`）を project ファイルで `agy` にすると、終了コード 2 で終了して何も書き込まず、代わりに `--scope global` のコマンドを示します。global ファイルでは書き込んだうえで警告します。`design.require_approval` と、リポジトリの外を指す `workspace.dir` は、`preset` と同じく `--scope` が無ければ global ファイルに書き込みます。どちらも `--scope project` を付けると終了コード 2 で終了し、何も書き込みません。 |
| `config suggest-roles [--write] [--json] [--provider P] [--model F]` | プロジェクトのファイル名とルートの `package.json` から、パスで絞り込む `database`、`frontend`、`backend` のレビュアーを提案します。モデルは呼びません。提案ごとに id、プロバイダー、family、`when.paths`、根拠、一覧のファイルのうち何件に一致するか（そのうち何件を `review.exclude` が withheld にするか）を表示し、続いて提案しなかったロールをすべて理由とともに表示します。git リポジトリの中では `git ls-files` だけを読み、一覧の取得に失敗したときや大きすぎるときは終了コード 2 です。`--write` は一覧のルートにあるプロジェクトファイルの `reviewers_extra` に追記し、有効なプロジェクトファイルが別の場所にあるときは拒否します（終了コード 2、何も書き込みません）。`--json` は 1 つのオブジェクト（`root`、`source`、`files`、`truncated`、`notes`、`suggestions`、`skipped`、`written`）を表示し、注記は stderr に出します。`references/configuration.md` の「パスで絞り込むレビュアーを提案させる」を参照。 |
| `config validate [--json]` | 有効な設定を、design パネルと各席の `high_risk_model` と `relevance` も含めて検証します。無効な場合は終了コード 1 です。`Warnings:` セクション（`--json` では `warnings`）には、読み取り専用のロールの実行が拒否することになる生引数 — project ファイルにある `options.args` のすべてと、アダプタの許可リストが受け付けないもの — 、project ファイルから来た `agy` の読み取り専用の席、agy の書き込みロールが project ファイルから受け取ることになる `options.skip_permissions` や `options.args`、global ファイルから来た agy の読み取り専用の席ごとに 1 行の `<seat>: read-only is NOT enforced by agy -- ...`、project ファイルが設定していて無視された `design.require_approval` やリポジトリの外の `workspace.dir`、そして project ファイルが、それが無い場合の設定よりゆるめているレビューの関門それぞれ（`references/configuration.md` の「プロジェクトファイルがゆるめられないもの」を参照）が一覧表示されますが、終了コードは変わりません。`config set` も、ゆるめた関門を除いて同じものを `warning:` 行として表示します。警告には、`reveiw` や `review.timout_seconds` のような綴り間違いなど、ファイルにあっても何も読まないキーも並びます（`<key> in the <global\|project> file: unknown key, ignored` と、似ている既知のキー）。プロバイダーの `options`、ティアの名前、`budgets` は独自のキーを取るので検査しません。`config set` は書いたキーがそれに当たるときだけ警告し、`doctor` はこれらをノートとして並べます。 |

```bash
dev-orchestra config set implementer.model.family opus
dev-orchestra config set review.max_review_iterations 3
dev-orchestra config set --scope project workspace.dir .agent-work
dev-orchestra config set --raw review.note "3 reviewers"
```

保存されたファイルには、そのファイルで設定されたものだけが入ります。それ以外はすべて下のレイヤーから
解決されるため、改善されたデフォルトがそのファイルにも反映されます。`config reset --delete` は、
削除したファイルが隠していた 2 つ目のプロジェクトファイル（`.dev-orchestra.yaml` の隣にある
`.dev-orchestra.yml`、または親ディレクトリにあるもの）を表に出すことがあります。`config reset`
はファイルをその場に残すので、引き続きそれを隠したままになります。

<a id="model"></a>

## model

| コマンド | 説明 |
| --- | --- |
| `model list [--provider <name>] [--json]` | インストールされている CLI が公開しているモデルを、それぞれの検出元（`cli-help`、`cli-catalog`、`cli-config`、`cli-default`、`builtin-fallback`）とともに表示します。例外を送出した adapter は報告され、残りは引き続き一覧表示されますが、終了ステータスは 1 になります。一覧に出るモデルが日付入りの id である CLI（agy）では、そのあとに `families to put in a config` の節が続き、このマシンでアダプタが解決できる family ごとに `family=<name> now <id>` の行が 1 つ出ます。設定に書くのはこの名前です。id は新しいモデルに追従しません。`--json` のすべてのエントリは同じキーを持ち、`origin`、`adapter_error`（正常に動作した場合は `null`）、`families`（ない場合は `[]`。各要素は `{"family", "resolves_to"}`）も含みます。 |

<a id="reviewer"></a>

## reviewer

| コマンド | 説明 |
| --- | --- |
| `reviewer list [--design] [--json]` | 設定されているパネルを一覧表示します。各席には、設定されていれば `(opus when high-risk)` と `(relevance: always)` が表示されます。`--design` は design パネルを一覧表示し、最初にその出どころを示します: `(design panel: the preset's fit)`、`(design panel: the code panel; when conditions ignored)`、`(design panel: global file)`、または `(design panel: project file)`。 |
| `reviewer add --provider <p> [--model <family>] [--role <r>] [--id <id>] [--pin <model-id>] [--when always\|high-risk \| --when-paths GLOB [GLOB ...]] [--high-risk-model FAMILY] [--relevance security\|test\|architecture\|always] [--design] [--scope …]` | レビュアーを追加します。id を省略すると生成されます（`codex-security`、`codex-security-2`、…）。`--when high-risk` にすると、高リスクと判定されたラウンドでだけコードレビューに加わります。`--when-paths "*migrate*/*" "*.sql"` にすると、変更されたパスがそれらのパターンのどれかに一致したときだけ加わり、`paths` を持つ `when:` のマッピングとしてブロック形式で書き込まれます（`references/configuration.md` を参照）。シェルに展開されないよう、各パターンは引用符で囲んでください。デフォルトの `always` ではキーを書きません。`--when` と `--when-paths` を同時に指定すると終了コード 2 になります。`--high-risk-model` は `high_risk_model`（`version: latest`）、つまり高リスクのラウンドで使うモデルを書き込みます。`--relevance` は、見るもののないラウンドからそのレビュアーを外しうるロールの規則を指定し（`security` の席が判定されるのは `--relevance security` のときだけです）、`always` なら規則なしです。`general` の席に規則を指定すると終了コード 2 になります。`--design` は design パネルに追加します。ファイルが `review.design.reviewers` を並べていればそこへ、なければ `review.design.reviewers_extra` へ書き込み、design パネルが引き続き何に従うかを示します。`--design` と `--when-paths` を同時に指定すると終了コード 2 になります。 |
| `reviewer remove <id\|role\|position> [--design] [--scope …]` | id、一意なロール、または 1 始まりの位置で削除します。条件付き（`when: high-risk` またはパスで絞り込んだもの）のレビュアーだけが残る場合は拒否されます（exit 2）。`--design` は design パネルから削除します。その席が継承されたものなら、まず有効な design パネルをファイルにコピーします（コードのパネルからなら `when` を外し、note を出します。プリセットのフィットからなら note を出し、global ファイルではコピーしたものを記録します）。project スコープでは、このコピーはコードのパネルと同じく agy の席を `not copied into` の note とともに外します。ただし、その席の出どころのパネルを global ファイルが並べている場合は外しません。 |
| `reviewer set <selector> [--provider] [--model] [--role] [--id] [--pin] [--when always\|high-risk \| --when-paths GLOB [GLOB ...]] [--high-risk-model FAMILY \| --clear-high-risk-model] [--relevance security\|test\|architecture\|always\|default] [--design] [--scope …]` | 既存のレビュアーを変更します。`--when always` は条件を外します。`--when high-risk` と `--when-paths` はどちらも条件を丸ごと置き換えるので、`--when-paths` はリストに追加するのではなく置き換えます。常に走る最後のレビュアーを条件付きにする変更は拒否され（exit 2）、`--when` と `--when-paths` の同時指定も同様です。`--clear-high-risk-model` は `high_risk_model` を外し、`--provider` で別の CLI にしたときも、`--model` の有無にかかわらず note を出して外します。一緒に `--high-risk-model` を指定すれば新しいものを書きます。`--relevance default` は `relevance` を外すので、その席は自分のロールの規則に従います。`--design` は、`remove --design` と同じように design パネルを編集します。 |

`--model` なしの `reviewer add` は CLI の既定の family（Claude では `opus`、agy では `default`、それ以外では
`recommended-coding`）を書き、`--model` も `--pin` もない `reviewer set --provider <other>` は family を新しい CLI の
既定値に戻して、元の値を `note:` で示します。`--provider agy` では、どちらも project ファイルに対しては
何も書かずに終了コード 2 で終了し、global ファイルに対しては書き込んだ後に `warning:` 行を表示します。
agy のレビュアーは読み取り専用に保たれないからです。provider にかかわらず、`--scope project` は project ファイルに
パネルがなければ global のパネルを agy のレビュアーごと project ファイルに写します。書き込んだ後、そうしたレビュアーには
それぞれ `warning: reviewer <id>: the reviewers list comes from the project config ...` の行が、`config set` と同じように
表示されます。以後それは拒否されるからです。

`add`・`remove`・`set` は、ファイル内のリスト全体を編集します。書き込むファイルもその下のファイル
（プロジェクトファイルに対するグローバルファイル）もまだ `reviewers` を並べていないときは、プリセットがこのマシンに与えるパネルから始め、それをファイルにコピーし、記録した
レビュアーを挙げた `note:` を1行表示します: それ以降、パネルはファイルのものになり、プリセットには
従いません。`config set reviewers[...]` も同じです。

```bash
dev-orchestra reviewer add --provider codex --role security
dev-orchestra reviewer add --provider claude --role database --id db-review
dev-orchestra reviewer set 2 --role performance
dev-orchestra reviewer remove db-review
```

<a id="doctor"></a>

## doctor

| コマンド | 説明 |
| --- | --- |
| `doctor [--json] [--fast] [--strict]` | CLI、認証情報の有無、設定、そして各ロールのモデルが解決できるかを診断します。`--fast` はモデルの検出と読み取り専用の強制のプローブを省略します。`--strict` は問題が見つかった場合に終了コード 1 で終了します。 |

認証情報の値は決して表示しません。認証情報が存在するように見えるかどうかだけを表示します。

project ファイルが設定した `design.require_approval` や、リポジトリの外を指す
`workspace.dir` は無視され、問題（problem）として報告されます。project ファイルが、
それが無い場合の設定よりゆるめているレビューの関門はメモ（note）で、`--json` では
`config.loosened` にも並びます（`references/configuration.md` の「プロジェクトファイルが
ゆるめられないもの」を参照）。

インストール済みの各 provider には `Read-only runs:` 行があり、その `plan` と `review` の実行がどう
読み取りに限定されているかを示します。`enforced by <flags>`（verified）、`enforced by <flags>; <対象外のもの>`
（partial。MCP サーバーを確認していない Codex）、`NOT ENFORCED (runs allowed, warned) -- <理由>`（読み取り専用の
モードがない agy）、`NOT ENFORCEABLE`（CLI がフラグを提示していない）、
`UNVERIFIED`（`--help` を読めなかった）、`not reported by this adapter`、`not checked (--fast)` の
いずれかです。agy の状態は定数なので、その行は `--fast` のときも CLI がインストールされていないときも表示されます。
`--json` では `providers.<name>.read_only_enforcement` で、`status` は `verified`、`partial`、`unenforced`、
`unsupported`、`unverified`、`unspecified`、`not-checked` のいずれかです。読み取り専用のロール
（orchestrator、architect、レビュアー）がその provider を使っている場合、`NOT ENFORCEABLE` と `UNVERIFIED`
は問題として扱われます。その実行は拒否されるからです。ロールの各 tier は実際に使う provider に対して確認され、
`<Role> (tier <name>)` として報告されます。global ファイルから来た agy の読み取り専用の席は、代わりに注記に
なります（`Reviewer agy-general: read-only runs are NOT enforced by agy (allowed, warned) -- <理由>`）。どちらの
モードでも、agy がインストールされていてもいなくても出て、`--json` のエントリには `read_only: "unenforced"` が
入ります。`--strict` は通ります。同じ席が project ファイルから来た場合は問題（`run` が返す拒否）になるので、
`--strict` は失敗します。`config validate` が表示する生引数の警告も
ここでは問題として扱われ、`--json` では `config.warnings` に入ります。`--fast` は `--help` を一切読まない
ことを約束するものではありません。`options.permission_mode` の検証では読みます。

「Pinned at a value the built-in default has moved off」セクションは、ファイルが固定している設定の
うち、その後推奨値が変わったものを一覧表示します。これは報告であって書き換えではありません。
意図的な選択と継承されたデフォルトは、ディスク上では見分けがつかないからです。`config prune` は、
求められれば、現在のデフォルトと等しいものを削除します。`reviewers` については何も言いません。
パネルは誰のデフォルトでもないからです。`optimization.extra_high_risk_paths` についても何も
言いません。どの版もこの設定をファイルに書き込んだことはないので、値があれば必ず誰かが足したものだからです。

Config ブロックの `Reply language:` の行は、`language.reply` と、ホストごとに何が返答をその言語に
保つかを示します。`not set (language.reply; replies follow the user's language)`、または
`<tag> (<layer>) -- Claude Code: <what>; Codex, Antigravity: rule 11 only` です。`<what>` は
`Stop-hook rewrite + reminder (hooks in ~/.claude/settings.json)`、`reminder only (hooks in …)`
（`language.rewrite: false` のとき、または判定が文字体系を知らないタグのとき）、
`hooks out of date (<reasons>) -- run: dev-orchestra hooks install`、または `rule 11 only (<why>)` です。
最後のものは、フックが入っていないとき、設定ファイルを解析できないとき、またはそれが `disableAllHooks` を
設定しているときに出ます。`--json` では `language` に入ります: `reply`（タグ。主タグは
小文字。なければ `null`）、`rewrite`、`layer`（`project`、`global`、`default`。`language.reply` を書いたファイルで、プロジェクトの `null` も数えます）、`check`（`script`、
`words`、`latin`、`none`）、`hosts`。`hosts` の `claude` は `hooks status --json` が出すオブジェクトそのもので、
`codex` と `agy` は `status: not-enforced` です。`check` が `words`（英語、スペイン語、フランス語、ドイツ語、ポルトガル語、
イタリア語）なら、これらの言語をよく使う語だけで見分け、短い返答は通すという注記が、`latin` なら、
ラテン文字の言語どうしは区別できないという注記が、`none` なら、どの返答も判定しないという注記が付きます。
ほかにも、問題ではなく注記として次のものが出ます。言語を設定しているのにフックが入っていないか古いとき
（理由と直すコマンド付き）、設定ファイルを解析できないとき（ファイルには触れていません）、`disableAllHooks`、
言語を設定していないのにフックが入っているとき（フックは黙ったままで、`hooks uninstall` で外せます）。
`references/architecture.md`（「返答の言語のフック」）を参照。

誤りではないが知っておくべきことがあると、問題の後に **Notes** ブロックが表示されます。注記は
`--strict` の判定に数えられず、`--json` では `notes` に入ります（ないときは `[]`）。1 種類は、上で述べた
agy の読み取り専用の席です。1 つは、このマシンでまだ live check していないインストール済み CLI のバージョンを挙げるものです（後述の
`Live check:` 行を参照）。もう 1 つは次のものです:
組み込みの `high_risk_paths` だけで判定される -- リポジトリ独自のリストも `extra_high_risk_paths` もない --
`when: high-risk` のレビュアーを、すべて 1 つの注記にまとめて名前を挙げます。デフォルトは一般的な名前に
合わせてあり、このリポジトリの機密性の高いパスを見逃して、そのレビュアーがほとんど走らないことがあるからです。
デフォルトに含まれるパターンだけでできた `high_risk_paths` は、順序を問わず、リポジトリ独自のリストとは
みなしません。0.6.0 より前に書かれた設定が持つもので、その後追加されたパターンを欠いた古いデフォルトの
リストかもしれないからです。パスで絞り込んだレビュアーがこの注記に挙がることはありません。そのパターンは
誰かが選んだものだからです。
条件付きのレビュアーの **Roles** の行は `(when: high-risk)` または `(when: paths *migrate*/*, *.sql)` で終わり、
`--json` のエントリには `always` 以外のとき `when`（`high-risk` または `paths`）と `condition`（行と同じ
ラベル）が入り、パスで絞り込んだレビュアーではさらにそのパターンの `paths` が入ります。
`high_risk_model` を持つレビュアーはそのモデルも解決されます -- 解決できない family は、席自身の
モデルと同じく問題です -- そしてそのエントリには `high_risk_model`（`family`、および席自身が解決できた
ときは `status` と `resolved`）が入ります。`relevance` を持つレビュアーにはそれが入ります。
`design_reviewers` と `design_panel_source`（`fit`、`code`、`global`、`project`）は design パネルを
表します。フィットやファイルが `review.design` の下に書いた各席はレビュアーと同じように
`Design reviewer <id>` として診断され、コードのパネルからコピーされた席には `id`、`role`、`origin` だけが
入ります。フィットかファイルが design パネルを設定していれば、テキストのレポートは Roles の下に
`Design:` 行を加えます（`Design: 3 (the preset's fit)`）。
`optimization.skip_unneeded_roles` が on のとき、`optimization.security_paths` や
`optimization.architecture_paths` が（その `extra_` のリストと合わせて）有効なパターンを 1 つも
残さず、しかもどちらかのパネルにその規則で判断される席があるなら問題です。そうなると、規則はその席を残す理由を何も見つけられないからです。

Antigravity の入れ先（`~/.gemini/config/plugins/dev-orchestra`、または `doctor` を実行したリポジトリの
`.agents/plugins/dev-orchestra`）がこのチェックアウトそのもの（リンクやジャンクション経由でも、チェックアウト
自体がそこに置かれていても）のとき、`doctor` はチェックアウト直下にある `hooks.json`、`mcp_config.json`、
`plugins.json`、`rules/`、`agents/*.md` を問題として報告します。Antigravity は次に起動したとき、これらを
スキルと一緒に読み込むからです。一覧を取得できない `agents/` も問題になります。コピーによるインストールには
これらは含まれません。`plugins.json` のエントリで登録したチェックアウトや、`agy plugin install` が置いた
コピーは `doctor` の確認対象外です（`references/workflow.md` の「Skill のチェックアウトからのインストール」を
参照）。`--json` では `antigravity` に `root`、`live`（このチェックアウトそのものである入れ先）、`autoload` が
入ります。

各 provider ブロックには `Source:` 行があり、`built-in` または `user module <path>` と表示されます。
**User providers** ブロックは常に表示されます。ユーザー adapter をインポートするディレクトリ（または
それが存在しないこと、あるいは `DEV_ORCHESTRA_NO_USER_PROVIDERS` で無効化されていること）、
インポートされたもの、そして読み込みに失敗したすべてのファイル（これも問題として扱われます）を
示します。診断中に例外を送出した adapter には、トレースバックの代わりに
`Installed: unknown (adapter failed)` と `Adapter error:` 行が表示され、それを使うロールには
`adapter-error` が表示されます。`--json` では `providers.<name>.origin`、
`providers.<name>.adapter_error` と、トップレベルの `user_providers` になります。
`references/providers.md` を参照してください。

続いて `Resume:` 行があり、その CLI で `run architect --resume` がセッションを継続できるかを示します。
`verified for claude <version> on <date> (built-in)` または `(record: <path>)`、
`trusted for claude <version> as newer than <version> (verified on <date>, built-in); not verified
itself -- run python scripts/smoke_live.py --provider claude`（または `record: <path>`）、
`UNVERIFIED -- <理由>; --resume runs fresh until then`、`NOT SUPPORTED -- <理由>`、
`not reported by this adapter`、`not checked (--fast)` のいずれかです。`--json` では
`providers.<name>.resume_support`（`status` は `verified`、`trusted`、`unverified`、`unsupported`、
`unspecified` のいずれか。ほかに `detail`、`version`、`source`、`record`、`verified_at`、`newer_than`、
`missing`）です。これは trusted の行も含めて決して問題として扱われません。継続できない実行は新規に走るだけだからです。

その隣に、オフラインの `mock` を除くインストール済みのすべての provider について `Live check:` 行があり、
このマシンで `scripts/smoke_live.py` がこの CLI バージョンを走らせたかを示します。実際の CLI を走らせるのは
このスクリプトだけで、CLI の更新は出力やフラグが adapter とずれうるときだからです。
`passed for <version> on <date>`（スキップしたチェックがあれば `, N skipped` が付きます）、
`FAILED for <version> on <date> (<チェック名>)`、
`not run for <version> (last passed: <古いバージョン> on <date>)`、`never run on this machine`、
または記録の問題（たとえばワークスペースの内側にあること）のいずれかです。記録は設定ディレクトリの
`verified/<provider>-smoke.json` で、スクリプトが書き込みます。読むのはファイルの読み込みだけなので、
`--fast` でもこの行は表示されます。まだチェックしていないバージョンには注記も付きます:
`<provider> <version> has not been live-checked on this machine (last passed: <version>|never); run python
<path>/smoke_live.py --provider <provider> -- it spends a few real tokens`（スクリプトの実際のパス付き）。
チェックに失敗したバージョンは行だけで、注記は付きません。実行した人がすでに失敗を見ているからです。
記録がワークスペースの内側にある場合も同様です。スクリプトはそこへの書き込みを拒むからです。
インストール済みでもバージョンを読み取れなかった CLI は `version unavailable` と表示され、注記は付きません。
照らし合わせるバージョンがないからです。
`--json` では `providers.<name>.live_check`（`status` は `passed`、`failed`、`absent`、`version-unavailable` のいずれか。
`entry`、`last_passed`、`problem`）です。これは決して問題として扱われません。

<a id="hooks"></a>

## hooks

Claude Code のユーザー設定（`$CLAUDE_CONFIG_DIR/settings.json`、なければ `~/.claude/settings.json`。
プロジェクトのものには入れません）にある、返答の言語のフックを扱います。どう書き込み、何を実行し、
いつ動くかは `references/architecture.md`（「返答の言語のフック」）にあります。

| コマンド | 説明 |
| --- | --- |
| `hooks install [--dry-run]` | 中継スクリプトとその記録を `<config dir>/hooks/` に書き、イベントごと（`UserPromptSubmit`、`compact\|resume` の `SessionStart`、`Stop`）にマッチャーのグループを 1 つ設定ファイルに加えます。ファイルがなければ作ります。設定ファイルのパス、加えたエントリ（`+ Stop: <python> -I <relay> stop`）と外したエントリ、元のファイルの控えを表示し、Claude Code はセッションの開始時にフックを読むと伝えます。`<python>` は dev-orchestra を動かしている Python で、仮想環境の中ではその環境の元になった Python です（そのときはそう表示します）。それが見つからなければ拒否します（終了コード 2）。`language.reply` を設定していなければ、設定するまでフックは黙ったままだと付け加えます。すでに入っていれば `already installed` と表示し、何も書きません。解析できない設定ファイルや形の違う設定ファイル、Microsoft Store 版の Python では Claude Code の読まない場所に書かれてしまう設定ファイルは拒否します（終了コード 2、何も書きません）。`--dry-run` は同じ内容を表示するだけで、設定ファイルも中継スクリプトも書きません。`hooks` のコマンドは、Claude Code の中で実行したほかのコマンドと違い、中継スクリプトの記録を実行中のチェックアウトに移しません（`references/architecture.md` の「返答の言語のフック」を参照）。`install` 自身はそれを記録します。 |
| `hooks uninstall [--dry-run]` | dev-orchestra のエントリを外し、それで空になったグループ、イベントの一覧、`hooks` オブジェクトも外して、中継スクリプトとその記録を消します。拒否したときは `install` と同じく終了コード 2 です。 |
| `hooks status [--json]` | フックが入っていて最新かどうかを示し、言語を設定しているのにフックが古いかないときは直すコマンドも示します。終了コードは 0 です。`--json`: `settings_path`、`status`（`installed`、`stale`、`not-installed`、`unreadable`）、`reasons`（`python-differs`、`python-missing`、`relay-missing`、`relay-outdated`、`plugin-root-differs`（記録が別のチェックアウトを指す）、`record-differs`（記録がないか、別の設定ディレクトリや設定ファイルを指す）、`events-missing`（あるイベントにこちらのフックがないか 2 つ以上ある）、`entries-differ`（`install` が書く形の専用グループに入っていない。マッチャー・タイムアウト・引数が違う、または別のイベントにある）のどれか）、`python`（エントリが実行するコマンド）、`relay`、`plugin_root`（記録から）、`events`、`hooks_disabled`、`error`。 |

このうち何を約束しているかは README の「互換性」にあります。

<a id="run"></a>

## run

| コマンド | 説明 |
| --- | --- |
| `run <role> [--prompt <text>\|--prompt-file <path>] [--tier <name>] [--mode plan\|implement\|review] [--output <path>] [--timeout <s>] [--idle-timeout <s>] [--resume --resume-prompt-file <path>] [--detach] [--force] [--json] [--print-command] [--extra …]` | 設定されたロールを 1 つ実行します。`<role>` は `orchestrator`、`architect`、`implementer`、`review_fixer`、またはレビュアーの id です。`--tier` はそのロールに設定された `model_tiers` の 1 つを選びます。未知のものはデフォルトのモデルで実行されるのではなく拒否されます。そのステージの予算から試行を 1 回消費し、予算が尽きていれば `--force` がない限り拒否します（終了コード 3）。`run implementer` は、plan が存在し承認されていない間は拒否します（終了コード 5）。これは親プロセスで行われ、detach されたワーカーでも再度行われます。ワーカーでの拒否はそのままジョブレコードに記録されます。`--force` は適用されません。 |

プロンプトは stdin からパイプで渡すこともできます（`--prompt-file -` は明示的に stdin を読みます）。
デフォルトのモード: architect/orchestrator は `plan`、implementer と review_fixer は `implement`、
レビュアーは `review` です。orchestrator、architect、レビュアーは読み取り専用のロールで、これらに
`--mode implement` を指定すると終了コード 2 になります。`--print-command` は、実行せずに正確な CLI の
呼び出しを表示します。空白を含む引数は引用符で囲みます。agy では、実行時にしか書かれないプロンプトのファイルが
`.ai/agy-prompt-<pid>-<random>.md` として表示されます。`--extra` は、`implement` の実行では残りのすべての引数をそのまま provider CLI に
渡します。`plan` と `review` では、通るのは `--add-dir <path>` だけ（Claude）で、Codex では何も通りません。
それ以外は試行を消費する前に終了コード 2 になり、読み取り専用のロールが project ファイルから受け取る
`options.args` も同様です。インストール済みの Claude CLI の `--help` に `--tools`、`--strict-mcp-config`、
`--restricted` が載っていない場合、または `--help` を読めない場合の `plan` や `review` の実行も同様です。
拒否メッセージはフラグ名、位置、出所を示し、値は決して表示しません。detach されたワーカーでの拒否は
ジョブレコードに書かれます。

読み取り専用のモードがない `agy` での `plan` や `review` の実行は、席が global 設定から来ていれば
実行の前に `warning: <role>: read-only is NOT enforced by agy -- ...` の行を出して走り、provider が
project ファイルから来ていれば拒否されます（終了コード 2。実行ログを開く前、プロンプトを読む前）。
実行時に問い合わせたインストール済みの CLI が `unenforced` を報告するアダプタも同じで、報告が静的でないため
設定コマンドでは拒否されないものも含みます。agy での `implement` の実行も、project ファイルがそのロールの
`options.skip_permissions` を挙げるか何らかの `options.args` を設定していれば、同じように拒否されます。
結果にかかわらず、adapter の実行の警告（agy が拒否した操作、JSON として読めなかった agy の stdout の行、
`agy: no answer: ...` の警告）は `warning: <role>: ...` の行として表示され、実行ログの終了イベントと
ジョブレコードに `warnings` として記録されます。強制の警告もそこに記録されますが、表示は実行の前の
1 回だけです。`agy: no answer:` の警告（agy の出力に結果がない、状態が `SUCCESS` でない、応答が空、
出力が読めない）は、agy の終了コードにかかわらず、その実行が失敗したことを意味します。`--output` は
そのまま残り、agy がストリームした途中までのテキストは `<output>.rejected` にだけ入ります。JSON でない
agy の stdout の行は、短く切って最大 20 行まで実行の stderr に写します。JSON の行は写しません。

空のプロンプトは、何かが委譲される前に拒否されます（終了コード 1）。そのため試行は消費されません。
対象は、存在しない `--prompt-file`、存在するが空のもの、明示的な `--prompt ""`、そして何も運ばなかった
パイプです。メッセージはそのどれだったかを示し、パスを書かれたとおりに示すとともに、どこを探したかも
示します。以前は読み込めない `--prompt-file` が空のプロンプトとして読まれ、それが委譲され、provider CLI
が自分の stdin について文句を言う応答が返っていました。

`--timeout` は全体の期限です。指定しなければ、ロールの実行は `run.timeout_seconds.<role>`
（implementer は 3600、ほかは 1800）を、`run <reviewer-id>` は `review run` と同じく
`review.timeout_seconds`（1800）を使います。期限で止められた実行は、どちらに当たったかと、
それがどこで設定されたかを伝えます。`config show` は、すべての期限を設定したファイルとともに並べます。
`--idle-timeout` は *出力がない* 状態の期限です（指定しなければ、どの実行でも
`review.idle_timeout_seconds`）。固まったエージェントは
静かになり、遅いだけのエージェントは出力を続けるので、これを使えば stall を全体の期限ではなく数分で
検出できます。これが適用されるのは Claude だけです。Codex と agy の adapter は進捗のストリームを
主張せず（agy はツールの動きを報告しますが、モデルが考えている間は黙ります。`references/providers.md` を
参照）、そこでは推測せずに無視されます。

`--output` は、実行が成功して何かを出力した場合にのみ、実行の stdout を書き込みます。stall した、
タイムアウトした、または失敗した実行では既存のファイルはまったくそのまま残り、その旨が stderr に
表示されます。書き込み先はたいてい、その実行が修正を求められたファイルなので、断片で上書きすると
入力が失われてしまいます。拒否された実行が出力したものは、ターゲットの隣に `<output>.rejected` として
保存され、同じメッセージでその名前が示されます。以前の試行が残したサイドカーファイルは、今回のものとして
読まれないよう、残さずに削除されます。書き込みが拒否された場合は、実行そのものが成功していても終了
コード 1 になります。`--output` は、指定したファイルにこの実行の結果が入っていることを約束するもの
なので、後続のコマンドが古いファイルを新しいものとして読んではならないからです。detach された実行では
同じことがジョブレコードに記録され、`jobs show` がそれを報告します。その stderr はどこにも出力されない
からです。`--output` がない場合は、結果にかかわらず stdout が表示され、実行が失敗すればどちらの場合でも
終了コードは 1 です。終了コード 0 で終わった実行が空白以外何も出力しなかった場合も 1 になります。これは
`--output` が書き込みを拒否するのと同じ判断です。呼び出し側がどちらを使ったかは、実行が応答したか
どうかとは関係がないからです。そのメッセージはロールを示し、生の stderr の冒頭を引用します。プロンプトを
拒否した CLI がその理由を述べるのはそこだからです。
実行の終了イベントは同じ判断を `answered` として記録します（`ok` の実行で何かを出力した場合にのみ
`true`）。これにより `status` は、修正や fix を、沈黙の上の終了コード 0 と区別できます。

`--detach` は実行を独自のプロセスで開始し、すぐにジョブ id を返すので、呼び出しがブロックすることは
ありません。後述の `jobs` を参照してください。

<a id="revising-the-plan-in-the-architects-own-session---resume"></a>

### architect 自身のセッションで plan を改訂する（`--resume`）

`--resume` は、新しいセッションを始める代わりに直前の architect run のセッションを継続して、この
ワークフローの plan を改訂します。これにより architect はコードを読み直して文脈を組み立て直さずに
済みます。プロンプトは 2 本取ります。`--prompt-file`（または `--prompt`、stdin）は新規に走る場合の全文
プロンプトで、`--resume-prompt-file` は継続したセッションに渡す短いプロンプトです。どちらも何かを消費する
前に読み込まれ、どちらを送るかはその後で決まります。

次のすべてを満たさない限り拒否されます（終了コード 2。何も消費する前に、引数の値を含まない固定の
メッセージで）。ロールが `architect` であること。実効の mode が `plan` であること（`--mode implement` は
これまでどおり拒否され、`--mode review --resume` も拒否されます）。`--resume-prompt-file` が指定されて
いること（`--resume` なしの指定も拒否されます）。`--resume-prompt-file` に `-` を指定するのは、
新規用のプロンプトを `--prompt` かファイルから渡すときだけであること（stdin は 1 回しか読めないため）。
`--output` がこのワークフローの plan（`.ai/plan.md`、
またはそれが解決される先のパス）であること。同じ検査が `--print-command`、`--detach` の親プロセス、
そしてワーカーでも改めて行われます。

継続するのは、このワークフローの直前の architect イベントが終わったセッションで、元のセッションを
そのまま残すよう fork されます。Claude では実行は読み取り専用のコマンドに `--resume=<id> --fork-session` を足す
だけで（Codex は `references/providers.md` のコマンドで fork します）、`--extra` や `options.args` に書いた生の `--resume` は他の生引数と同じく拒否されます。次のうち
最初に当てはまるものがあれば、代わりに全文プロンプトで新規に走り、その理由を stderr に出します
（`note: --resume requested, running fresh: <reason>`）。

| 理由（`resume.reason` として記録） | 条件 |
| --- | --- |
| `the provider cannot resume a session` | provider がセッションを継続しない（Codex、ユーザー adapter） |
| `the provider cannot resume a session (unsupported)` | `--help` が `--resume` と `--fork-session` を挙げていない（Codex: `codex exec fork --help` が fork に要るフラグをすべては挙げていない） |
| `the provider cannot resume a session (unverified)` | この CLI の版が、継続したセッションを読み取り専用のまま保つと確認されていない（後述） |
| `the provider cannot resume a session (unspecified)` | 継続はするが、それが読み取り専用のままかを示さない adapter |
| `no earlier architect run in this workflow` | 継続するものがない |
| `the last architect run is not resumable: it did not succeed` | 失敗した、stall した、または拒否された |
| `the last architect run is not resumable: it did not answer` | 沈黙のまま終了コード 0 で終わった |
| `the last architect run is not resumable: it has no session id` | この機能より前の run log |
| `the last architect run is not resumable: its session id is not a UUID` | 記録された id が UUID でない |
| `the last architect run is not resumable: its recorded mode is not plan` | 読み取り専用の plan の実行ではなかった |
| `the last architect run is not resumable: its output is not this workflow's plan` | 別のものに答えていた |
| `the last architect run is not resumable: its recorded provider differs` | その後 architect の provider が変わった |
| `the last architect run is older than design.resume.max_age_seconds` | デフォルトは 1 時間 |
| `the last architect run's context exceeds design.resume.max_context_tokens` | 上限を設定した場合のみ |
| `the last architect run's context is unknown and design.resume.max_context_tokens is set` | 上限を設定していて、その実行が照らし合わせる `context_tokens` を記録していない |
| `the CLI rejected the session it was asked to resume` | 後述の再実行 |

`(unverified)` の場合は 2 行目の note に adapter の詳細が出ます。CLI の版と
`python scripts/smoke_live.py --provider claude` です。CLI の版が継続を許されるのは、adapter に同梱された
表にあるか、このマシンでそのスクリプトが合格してその版を記録したときです（`references/providers.md`）。
そうして許された版より新しく、メジャー版が同じ版も、信頼に基づいて継続します。ただし、その版以下でこのマシンが不合格と
記録し、その後ここでの合格で覆っていないものがあれば別です。その場合は `note: resuming the last
architect session` の後に 2 行目の note として adapter の詳細が出ます。どの版より新しいとして信頼したか、
そしてその版自体は確認されていないことです。したがって CLI を更新した直後は、新しい版でスクリプトを
実行するまで、`--resume` は信頼に基づいて継続します。許されたどの版よりも古い版、新しいメジャー版、ここでの不合格の後の版は
新規に走り（`(unverified)`）、`doctor` の `Resume:` 行も同じことを示します。

セッションがもう存在しないために CLI が継続を拒んだ場合、その実行は失敗イベントとして記録され
（`resume.outcome: "rejected"`。stderr は写しません）、全文プロンプトで新規にもう一度だけ実行されます。
adapter が継続の実行を自分で始めないこともあり、同じように扱われます。Codex は、fork に渡すモデルが
ないとき、またはそのセッションがこのワークスペースで始まったものでないときに拒否します。拒否の理由は
stderr の `note: the CLI rejected the session it was asked to resume` の後に出て、これも記録されません。
CLI が拒んだ後の 2 回目は別の試行です。architect の予算に照らして確認され、試行を 1 回消費し、残っていなければ
実行されません（終了コード 3、`running fresh would spend an attempt`）。adapter が拒否した後は何も
走っていないので、新規の実行は継続の実行が確保した試行を使い、それが最後の 1 回でも実行されます。`--force` はユーザーが指定した
とおりに効き、detach されたワーカーでも同じです。継続した実行のそれ以外の失敗（stall、タイムアウト、
エラー）はこれまでどおり報告され、再試行されません。次の `--resume` は新規に走ります
（`it did not succeed`）。継続した Codex の実行で、fork の rollout が読み取り専用のファイルシステムの
サンドボックスを確かめられなかったものもこの失敗で、その回答は使われません。

すべての `run` の終了イベントは `session_id`、`context_tokens`（CLI が報告した場合、モデルが最後に見た
文脈の大きさ）、`cost_usd`、`cache_read_tokens` を記録するようになりました。`--resume` の実行は `resume`
（`requested`、`mode` は `resumed` または `fresh`、`resumed_from`、`reason`、`outcome`、そして信頼に
基づいて継続した実行では `resume.trust: "newer"`。版は決して記録しません）も記録し、
実行中のエントリにも入ります。継続した実行の使用量は `tokens show` で `architect:resumed` とラベル付け
されます（`--tier` のラベルが優先されます）。`--detach` では両方のプロンプトがジョブにコピーされるので、
コマンドが戻った後にどちらかのファイルを編集しても実行には届きません。ジョブレコードには `force`、
`resume_prompt_file`、`session_id`、`resume` が入ります。

```bash
dev-orchestra run architect --prompt-file .ai/execution/design-request.md --output .ai/plan.md
dev-orchestra run architect --resume \
  --prompt-file .ai/execution/design-revise-request.md \
  --resume-prompt-file .ai/execution/design-resume-request.md \
  --output .ai/plan.md
dev-orchestra run implementer --print-command
echo "explain the failure" | dev-orchestra run orchestrator
```

<a id="review"></a>

## review

| コマンド | 説明 |
| --- | --- |
| `review snapshot [--base <rev>] [--no-untracked] [--surrounding none\|enclosing] [--json]` | レビュー対象の変更を固定します。空の場合は終了コード 1 です。`review.context.max_chars` を超える変更には警告が出ますが、それでも書き込まれます。スナップショットを取ること自体は何も消費せず、拒否するのは消費するコマンドの役目だからです。`--json` は同じことを数値で示します: `change_chars`、`max_chars`、`over_context`。`review.context.surrounding: enclosing` のときは、各 hunk を囲むシンボルも、diff を取ったツリーから `review-surrounding.json` に固定し、固定したシンボル数と文字数、抽出しなかったファイルの数とその理由を示す `context:` 行を出力します。メタデータには `surrounding` ブロックが加わります。`--surrounding` はこのスナップショットに限って設定を上書きします: **`enclosing` は設定が `none` でもこのスナップショットについて候補を凍結します**。この凍結が無いと `review run --surrounding enclosing` は拒否されます。`none` は何も凍結せず、古い凍結ファイルを削除します。設定そのものは変わりません。`references/reviews.md` と [周辺コンテキストの効果を測る](limits.md#measuring-what-surrounding-context-does) を参照してください。 |
| `review run [--design] [--request <path>] [--iteration N] [--only <ids/roles>] [--sequential] [--context <text>] [--base <rev>] [--timeout <s>] [--idle-timeout <s>] [--force] [--surrounding none\|enclosing] [--high-risk] [--progress] [--json]` | スナップショットに対してすべてのレビュアーを実行し、レポートと統合結果を書き込みます。ディスクにスナップショットがなければ先に取ります。`--base` を付けるとそのリビジョンを基準に取り、ディスクのスナップショットが別の基準で取られていれば取り直して（`note:` を表示します）、`review snapshot --base` と同じくラウンドの数え直しになります。基準なしで取ったスナップショットは `HEAD` を基準にしたものとみなし、`--base` と同じコミットを指す名前で取ったものは取り直しません。`--base` がなければ、ディスクのスナップショットをそのままレビューします。終了コード 1 になるのは、`ok` で戻ったレビュアーが 1 人もいない場合だけです。すべてのレビュアーが失敗した場合や、変更本体が大きすぎてインライン化できずファイルとして渡されたラウンドがこれにあたり、後者はクリーンではなく `partial` として記録されます。ラウンドは `--iteration` が指定されない限りスナップショットから導出され、`review.max_review_iterations` を超えるラウンドは `--force` がない限り拒否されます（終了コード 3）。上限に達したラウンドでも fix と再テストは行われ、拒否されるのは再レビューだけです。最適化ゲートに拒否されたラウンド（テストが失敗として記録されている）も終了コード 3 で終了し、`optimization report` が数えられるよう `refused` として記録されます。`review.context.max_chars`（400,000）を超える変更本体も同様です。何もレビューされず、メッセージはサイズ、上限、上限内に収める方法を示し、ラウンドは `refused_by: "context"` として記録されます。`--force` を付けると構わず実行し、そのラウンドは報告されるすべての場所で `over_budget` として記録されます。本体をプロンプトに入れるかパスとして渡すかは `review.context.inline_chars`（400,000。デフォルトでは同じ数値）で決まり、各レビュアーのエントリには判断に使われた値が記録されます。`budgets.max_runtime_seconds` 分の委譲実行時間を使い切った場合も同様にラウンドは拒否され（パネルはその最大の消費者です）、メッセージはどの予算だったかを示します。`--only` は一部だけを実行しますが、統合はすべてのレビュアーの現在のレポートに対して行うので、何も失われません — ただし、このラウンドで外された条件付きのレビュアーのレポートは統合されません。条件付きのレビュアーはそれぞれ、理由（高リスクなパス、またはパスで絞り込んだレビュアーなら自分のパターンの 1 つ、`when: high-risk` のレビュアーなら `--high-risk`、差分ラウンドでの自身の未解決の accepted の指摘、`--only` での指名）を示す `note:` とともに加えられるか外され、その判断はイベントと `--json` の `optimization.conditional` に `optimization.declared` とともに記録されます。ラウンドに見るものがない `test`、`architecture` のレビュアー（および `relevance: security` でオプトインした `security` のレビュアー）も、どのレベルでも同じように外され、`note: <id> (when: relevance) left out: <reason>; --only <id> to include it` と `when: relevance` の記録が残ります（`references/reviews.md` の「ラウンドが必要としないロール」を参照）。`--high-risk` は変更を高リスクと宣言します。`when: high-risk` のレビュアーを加え、パネルを縮小させず、すべてのロールを残しますが、レベル・指摘の上限・ゲートは決して変えないので、red のツリーに対する宣言付きのラウンドは他と同じように拒否されます。`--design` と一緒に使うと、design パネルの `when: high-risk` の席を加え、すべてのロールを残します。高リスクへの一致があるか `--high-risk` のラウンドでは、`high_risk_model` を持つ席はそのモデルを使い、`note: high-risk round (<why>): <id> runs <model> instead of <model>` を出し、そのレビュアーのエントリに `model_slot: high-risk` が加わります。`review.context.surrounding: enclosing` のときは、固定されたシンボルを `review.context.surrounding_chars` と、diff が両方の上限の下に残す分の範囲で採用し、`Surrounding context:` 行が採用した数と除外した数とその理由を示し、`--json` にはラウンドの `surrounding` レコードが入ります。上限が計測するサイズは、diff に採用したコンテキストを足したものになります。`--surrounding none\|enclosing` は、1 つのスナップショットをコンテキストあり・なしでレビューするために、この run に限って `review.context.surrounding` を上書きします（[周辺コンテキストの効果を測る](limits.md#measuring-what-surrounding-context-does) を参照してください）。設定は変わらず、行は `(--surrounding enclosing for this run)` または `Surrounding context: none (--surrounding none for this run; review.context.surrounding unchanged)` となります。次の場合は何も課金される前に終了コード 2 で拒否されます: `--design` と併用したとき。incremental ラウンド（再レビューのプロンプトには実行時点の accepted findings が載るので、2 本の run はコンテキスト以外でも違ってしまう）。`enclosing` で何も採用されないとき（理由を問わない: `review snapshot --surrounding enclosing` で凍結していないスナップショット、候補なし、ファイル渡し、予算なし）。同じスナップショットに対する 2 本目の run（間に `budget reset` を挟んでも同じ）で、前回の run が組み立てた後に finding のトリアージまたはトリアージのメモが設定されたとき（前のラウンドから引き継がれたものは数えない）。同じスナップショットの再実行はラウンドを進めず、findings の署名を登録しないので、ペアが「何も変えなかった修正」に見えることはありません。1 本目は通常どおり登録します。lineage が変わった後の run や、`--iteration` で別のラウンドを指定した run は再実行ではなく、署名を登録します。run のイベントと `--json` には `measurement` ブロック（`surrounding`、完全な `snapshot` sha256、凍結した `tree`、`head`、`base`、`workflow` ディレクトリ、予算の `epoch`、`rerun`、そして `inputs`: `context_sha256`、`max_findings`、`inline_chars`、`max_chars`、`force`）が加わり、`consolidated.json` には `triage_at_build`（キーごとの各 finding の `triage` と `triage_note`）を持つ `measurement` が加わります。フラグが無ければ、これらは何も書かれません。`--progress` は、ラウンドの実行中に各レビュアーのツール使用を `[<reviewer> +mm:ss] <line>` の形で stderr に出します。行の中身と規則はジョブの activity と同じで（[jobs](#jobs) を参照）、レビュアーごとに 300 行までです。実行がどう終わっても最後に `[<reviewer> +mm:ss] done: <status>` を出します。ファイルには何も書かず、stdout（`--json` を含む）は変わりません。出力をリダイレクトしてバックグラウンドで実行するラウンド向けで、付けなければ stderr はこれまでどおりです。何よりも先に、project ファイルが設定していて無視されたもの（`design.require_approval`、リポジトリの外の `workspace.dir`）と、project ファイルが、それが無い場合の設定よりゆるめているレビューの関門それぞれを `warning:` 行で示します（`references/configuration.md` の「プロジェクトファイルがゆるめられないもの」を参照）。 |
| `review consolidate [--design] [--iteration N] [--json]` | 既存のレポートを再解析し、そのステージのパネル（`--design` なら design パネル）について統合結果を再構築します。現在のスナップショットまたは plan に対する直近のラウンドが外したレビュアー -- 条件付きのレビュアーや、見るもののなかったロール -- のレポートを除くので、そのラウンドが読んだレポートを読みます。design ラウンドが誰を外したかを記録するようになる前に記録された design ラウンドでは、誰も除きません。 |
| `review show [--design] [--accepted] [--json]` | 統合されたレビューを表示します。 |
| `review triage [--design] <ids…> --status <status> [--note <text>]` | トリアージの判断を記録します。判断のたびに、`needs-triage` も含めて指摘に `triage_set_at` を刻むので、指摘を戻したことと一度も判断していないことが区別できます。 |
| `review fix-brief [--design] [--output <path>]` | fixer 向けに、受け入れた指摘のブリーフを出力します。 |
| `review status [--design] [--json]` | 再レビューが必要かどうか、イテレーション予算、そしてラウンドの `coverage` を示します。`coverage` には `round`、`change`、ラウンドの計測に使われた `inline_chars` に加え、`unverified` を解消するための操作が含まれます。変更を絞るか `review.context.inline_chars` を引き上げ、その後スナップショットを取り直すことです。また、その上限がラウンドに記録されたサイズを超えて引き上げられた後は、同じスナップショットが今ならインライン化されるので、それに対して `review run` を実行すればよいだけだということも示します。`over_budget` は、そのラウンドが `--force` で `review.context.max_chars` を超えて送られたためにだけ実行されたことを示します。周辺コンテキストを運んだラウンドでは `surrounding context:` 行が加わり（レビュアーごとに渡されたものが違う場合はレビュアーごとに 1 行）、除外されたシンボルを最大 5 つまで名前で示します。`--json` にはレポートの `surrounding` ブロックが入ります。ラウンドの予算を使い切った後は、そのラウンドの最後のパスがどこまで進んでいるかも示します。これは台帳、実行ログ、承認状態から読み取られます（何も消去されません）。`final_fix` は `pending`（もう一度 fix する）、`retest`（fix 済み。再テストを記録する）、`done`、`blocked`（`review_fixer` の試行が残っていない）、`--design` の場合の `final_revision` は `pending`（もう一度修正する）、`done`、`blocked`（`architect` の試行が残っていない）、`approved`、`implemented` のいずれかです。どちらも上限に達する前は `null` で、`final_fix_pending` / `final_revision_pending` フラグを伴います。最後の行は次のステップを示し、前のラウンドの指摘を繰り返したラウンドについて注記します。`--design` を付けると最初の行は `design review: <label>` になります。ラベルは `on`、`off`、`auto -> run (<reason>)`、`auto -> skip (<reason>)` のいずれかで、`status` と同じ答えです。2 行目は `design panel: <ids> (<source>)` で、次のラウンドが外す席ごとに `; <id> left out (<reason>)` が続きます。`--json` には `enabled`（そのステージを実行するかどうか）、`mode`（`on`、`off`、`auto`）、`reason`（`on` と `off` では `null`）、`reviewers`（design パネルの id）、`panel_source`（`fit`、`code`、`global`、`project`）、`optimization`（次の design ラウンドが決めること）が加わります。`references/reviews.md` を参照してください。 |

`--design` を付けると、これらすべてが *design* レビューに切り替わります。実装前に `.ai/plan.md` を
design パネル（`review.design.reviewers`、なければプリセットがフィットした design パネル、ファイルが `reviewers` を並べていればコードのパネル）で評価するもので、独自のレポート、ラウンドカウンター、トリアージが `.ai/reviews/design/`
の下にあります。`review run --design` は diff ではなく plan そのものを固定し（`review snapshot --design`
はなく、git も必要ありません）、plan が応えるリクエスト（`--request <path>`、デフォルトは
`.ai/execution/design-request.md`。存在しない場合は注記されるだけで致命的ではありません）と一緒に
ハッシュします。plan がない場合は終了コード 2、`review.design.max_iterations` を超えるラウンドは
`--force` がない限り終了コード 3（上限に達したラウンドでも修正は行われ、拒否されるのは再レビュー
だけです）、すべてのレビュアーが失敗した場合は終了コード 1 です。`review.context.max_chars` は plan
*と* リクエストを合わせて計測されます。どちらもすべてのレビュアーのプロンプトに入るからです。また、
ラウンドは plan が固定される前に拒否されるので、前のラウンドのレポートとトリアージは報告のために
そのまま残ります。最適化ゲートとパネルの削減は適用されません。コードのパネルでは `when` にかかわらず
すべてのレビュアーが走り、独自の design パネルは plan に対して `when: high-risk` を尊重します。どちらでも、
plan に見るもののないロールは休み、`--high-risk` は `when: high-risk` の席を加え、すべてのロールを残し、
席を `high_risk_model` に切り替えます。各判断は `note:` になり、ラウンドのイベントには `optimization`
ブロック（`level`、`high_risk`、`declared`、`conditional`、`files`）が加わります。`--base` は無視されます。
`review.design.enabled` が false のとき、または `auto` でこの plan ならスキップされるときに実行すると、
注記（`note: review.design.enabled is auto and this plan would be skipped (<reason>); running
because you asked`）を表示したうえで続行します。この設定は orchestrator がそのステージを実行するか
どうかを示すものであり、あなたが実行してよいかどうかを示すものではないからです。こうして実行した
ラウンドも design ラウンドなので、それ以降 `auto` は実行（`a design round already ran`）と答え、
ループは `true` のときと同じように進みます。`references/reviews.md` を参照してください。

読み取り専用のモードがない `agy` のレビュアーは、パネルが global 設定から来ていれば、どちらの種類の
ラウンドでも実行の前に警告されます（`warning: reviewer <id>: read-only is NOT enforced by agy -- ...`）。
reviewers のリストが project ファイルから来ていれば、そのレビュアーは拒否を `error` としてラウンドで
失敗し、ほかのレビュアーは走ります。ラウンドの前に問い合わせたインストール済みの CLI が `unenforced` を
報告するアダプタの project のレビュアーも同じです。ラウンドの後、各レビュアーの実行の警告は、実行の前に
すでに表示した強制の警告を除いて `warning: reviewer <id>: ...` の行として表示され、すべてがそのエントリに
`warnings` として残ります。

`review status --json` は、予算をその取得元の設定の名前で報告します。`--design` なしでは
`max_review_iterations`、ありでは `max_iterations` です。ペイロードの残りはどちらでも同じです。

`consolidated.json` を書くたびに -- `review run`、`review consolidate`、`review triage` のいずれも、
`--design` の有無を問わず -- `reviews/rounds/<sha12>-<round_id>.json`（design レビューでは
`reviews/design/rounds/`）にもコピーを書きます。コピーを上書きするのは同じラウンドへの後の書き込み
だけなので、ラウンドの指摘とトリアージは次のラウンドの後も残ります。`optimization report` がこれを
読みます。背後にスナップショットの無いレポートにはコピーを作りません。現在の凍結の後に前のラウンドの
id のまま作られたレポート -- どのレビュアーもレビューを返さなかった design ラウンドや、ラウンドの
レビュアーが戻る前の `review consolidate` -- にも作りません。同じツリーや plan を凍結し直した場合、
それは前のラウンドのコピーだからです。

`review snapshot` は、同じツリーの 2 回目のスナップショットも含め、すべての code スナップショットに
`reviews/review-target.json` 内の新しい `round_id` を与え、`review run` がそれを統合レポートの
`snapshot` と run イベントに写します。`review run --design` は、同じ plan の再実行も含め、すべての
ラウンドに `reviews/design/review-target.json` 内の新しい `round_id` を与えます。
`review consolidate --design` と `review triage --design` はそれに手を付けません。統合レポートが
ラウンドを示すのは、`review run --design` でそのラウンドのすべてのレビュアーが戻った後だけです。
`review consolidate --design` は、より新しいラウンドが実行中であっても、前のレポートが示していた
ラウンドを保持します。承認は、その時点で最新だったラウンドに対して与えられます。

<a id="design"></a>

## design

| コマンド | 説明 |
| --- | --- |
| `design approve [--json]` | 現時点の `.ai/plan.md` に対するユーザーの承認を（plan 単体の sha256 で）、承認の対象となった design レビューのラウンドと一緒に記録します。その後の修正や、その後に実行された design レビューがあれば、再度承認が必要です。ラウンドは統合レポートを持つ最後のものであり、そこに示された指摘と合わせて読み取られます。`review run --design` のラウンドが開始されたもののまだレポートがない間（実行中、またはクラッシュした場合）は、何も記録せず終了コード 2 で終了します。どのレビュアーのレビューもなく最後まで実行されたラウンド（すべてのレビュアーが失敗した）の場合は、代わりにそのラウンドに対して承認し、plan に design レビューの指摘がまったくないことを注記します。これにより、失敗し続けるパネルのせいでユーザーが先に進めなくなることはありません。未解決の design の指摘（重大度にかかわらず、却下されておらず重複ともされていないものすべて）は表示されますが、拒否はされません。それらを承知で承認するかどうかはユーザーの判断です。plan の以前の版に対するレビューからの指摘（固定された `review-target.md` が plan と異なる）はその旨がラベル付けされます。同じ plan を同じラウンドに対して再度承認すると `already approved` と表示し、何も記録しません。plan がない場合は終了コード 2 です。ユーザーが会話の中で了承した後にだけ実行してください。 |

承認は `state.json` の `design_approval` の下に保存されます。書き込むのはこのコマンドだけで、隣に
`design_approval` / `approved` イベントが記録されます。`state record` で偽造することはできません。
`budget reset` は承認を保持し、`workflow remove` は削除します。ラウンドに id が付く前に書かれたラウンドは
ラウンドなしとして扱われます。それに対して承認しても何も記録されず、次の `review run --design` で承認は
古くなります。

<a id="status"></a>

## status

| コマンド | 説明 |
| --- | --- |
| `status [--json]` | *続行か停止か* に答える唯一のコマンドです。理由付きの `continue` / `stop-and-report` の判定、stall または放棄されたステージ、実行中のもの、残りの予算、未解決のレビュー指摘を報告します。 |

これを読み取ると、プロセスがなくなった実行中エントリも消去されます。そのため、結果を記録せずに終了した
ステージは、永遠に実行中に見えるのではなく `abandoned` として表示されます。

サイズを理由に拒否されたラウンドは、実際に変更をレビューするラウンド（コードでも design でも）がある
までは `stop-and-report` の理由になります。それ以外に解消する手段はありません。どちらの種類であれ後続の
拒否でも、`status` 自身がプロセスのなくなったステージを消去するときに書き込む `abandoned` エントリでも
解消されません。拒否は前のラウンドの統合結果をそのまま残すので、このルールがなければ、大きすぎる変更が
別のもののクリーンなレビューを根拠に `continue` と答え続けてしまいます。
理由にはサイズと上限が示されます。`--json` では `review.refused_for_size` と
`design_review.refused_for_size` が同じ 2 つの数値を持ちます。

`Optimization:` 行は次の `review run` が判断するであろう内容で、加える、または外すことになる
条件付きのレビュアーをそれぞれ理由とともに示します:
`; claude-security left out (no high-risk path matched)`、
`; claude-security added (has open accepted finding F3)`、または
`; codex-database left out (no path matches *migrate*/*, *.sql)`。`--json` では同じ内容を
`optimization.conditional` に持ち、ラウンドに見るもののないロールもそこに含まれます
（`; claude-test left out (docs-only change: ...)`）。これは予測にすぎません。`status` は設定を検証せずに読むので、
`review run` が拒否するようなパネルは、ここではなく `config validate` と `doctor` が報告します。

`Design review:` 行は、この plan でそのステージを実行するかどうかから始まります。`on`、`off`、
`auto -> run (<reason>)`、`auto -> skip (<reason>)` のいずれかで、たとえば
`Design review: auto -> skip (5 code files, none high-risk), round 0/2, 0 accepted, 0 blocking`
のようになります。`--json` では `design_review.enabled` がその答え（ステージを実行するかどうか）、
`design_review.mode` が `on`、`off`、`auto` のいずれか、`design_review.reason` が理由で、`on` と
`off` では `null` です。スキップは理由ではなく、判定も変えません。行の末尾には、次の design
ラウンドが外す席ごとに `; <id> left out (<reason>)` が付き、`design_review.optimization` にはその
ラウンドの判断が入ります。

`design_approval` と `Plan approval:` 行は、plan をまだユーザーに提示する必要があるかどうかを示します
（`references/workflow.md`）。これらは理由ではなく、判定も変えません。それを強制するのは
`run implementer` です。唯一の例外は逆方向に働きます。ユーザーが現在の plan を承認した後は、未解決の
指摘を残したまま使い切った design レビューの予算は理由ではなくなります。ユーザーはそれらを見せられた
うえで先に進むと決めたからです。

使い切ったラウンドの予算は、上限に達したラウンドが最後のパスを終えるまでは理由になりません。
Design（`design_review.final_revision`）: `pending`（指摘をもう一度 plan に反映する。再レビューは
しない）は `continue` です。`done`（plan が固定されたものと異なる、または architect がそのラウンドの後に
plan を指す `--output` 付きで応答した）は `stop-and-report` で、`; revised after the last round, not re-reviewed --
present the plan and ask` または `; the architect left the plan unchanged …` が付きます。
`blocked`（architect の試行が残っていない）はただちに停止します。`approved`（`design.require_approval`
が有効かどうかにかかわらず、この plan とラウンドに対する承認が記録されている）は理由を出しません。
`implemented`（implementer がすでにこの plan で実行された）と `unaccepted`（未解決の指摘のどれも
`accepted` ではなく、反映するものがない）は通常の理由のままです。Code（`review.final_fix`）: `pending`
（もう一度 fix する）と `retest`（fix 済み。fix の後に記録される `test` または `re-test` の結果を待っている）
は `continue` です。`done` は `stop-and-report` で、`; fixed and re-tested
after the last round, not re-reviewed -- report` が付きます。`blocked`（`review_fixer`
の試行が残っていない）はただちに停止します。`unaccepted`（fix すべき受け入れ済みのものがない）は通常の
理由のままです。plan が書かれている場合、`architect has no attempts left` が理由になるのは、予算内の
design ラウンドにまだ修正すべきブロッキングの指摘がある間だけです。`Review:` 行と `Design review:` 行は、
最後のパスがまだ残っているときにその旨を示します。

2 つの理由もそれを待ちます。`the last (design) review round found exactly
what the previous one found` は、修正、または fix とその再テストがまだ残っている間は保留されます。その間は
`review` / `design_review` の `identical_rounds` がカウントを持ち、人間向けの行には `identical to the
previous round` と表示されます。そして `<stage> has no attempts left` は、まだ必要なステージについてだけ
出されます。`architect` は plan がない間だけ（まだ残っている修正はそれ自身の理由でその旨を示し、保留中の
承認の行には変更のための試行が残っていないことが注記されます）、`review_fixer` は fix が行われていない
間だけです。

```bash
dev-orchestra status --json
```

```json
{
  "verdict": "stop-and-report",
  "reasons": ["review budget spent (2/2 rounds) with 1 finding(s) still open; fixed and re-tested after the last round, not re-reviewed -- report"],
  "stalls": [],
  "review": {"iteration": 2, "max_review_iterations": 2, "blocking": ["F1"], "accepted": 1, "refused_for_size": null, "identical_rounds": 1, "final_fix": "done", "final_fix_pending": false},
  "design_review": {"enabled": true, "mode": "auto", "reason": "touches db/migrate/ (Files to Modify)", "iteration": 0, "max_iterations": 2, "blocking": [], "accepted": 0, "identical_rounds": 0, "final_revision": null, "final_revision_pending": false},
  "budgets": {"implementer": {"used": 2, "limit": 5, "remaining": 3}}
}
```

<a id="jobs"></a>

## jobs

detach された実行です。期限はエージェントが異常な振る舞いを続けられる時間を制限しますが、実行中は
呼び出し側がその呼び出しの *中に* います。orchestrator 自身がエージェントである場合、長いブロックは
クラッシュと区別がつきません。detach するとそれがなくなります。作業は別の場所で実行され、待機には
自分で決めた期限を設けられます。

| コマンド | 説明 |
| --- | --- |
| `jobs list [--json]` | 記録されているすべてのジョブを新しい順に表示します。 |
| `jobs show <id> [--output] [--since <n>] [--activity <m>] [--json]` | 1 つのジョブを表示します。オプションでその出力も表示し、ジョブが何をしているかも示します（下記）。 |
| `jobs wait <id> [--timeout <s>] [--poll <s>] [--since <n>] [--activity <m>] [--json]` | 待機しますが、`--timeout`（デフォルト 60 秒）より長くは待ちません。待機が終わった時点でジョブがまだ実行中であれば終了コード 4 で終了します。これはエラーではなく通常の結果です。ジョブが `--output` の書き込みを拒否した場合は、フォアグラウンドの実行と同様に終了コード 1 で終了します。 |
| `jobs cancel <id>` | 実行中のジョブとそのプロセスツリーを停止します。 |

```bash
id=$(dev-orchestra run implementer --prompt-file plan.md --detach --json | jq -r .id)
dev-orchestra jobs wait "$id" --timeout 120   # exit 4 means "still going"
dev-orchestra jobs show "$id" --output
```

ジョブの一生は `.ai/jobs/` の下にある 1 つの JSON ファイルで表され、ワーカーが書き込みます。そのため、
親プロセスが終了しても進捗は失われません。結果を記録せずにワーカープロセスがなくなったジョブは、永遠に
実行中に見えるのではなく `abandoned` として報告されます。

ジョブが終わっていない間、`jobs show` と `jobs wait` は `elapsed:`（ワーカーがジョブを引き受けてからの
時間）を表示します。ワーカーは CLI が報告したツール使用を 1 回につき 1 行、`.ai/jobs/<id>.activity` にも
書き残します。このファイルができると、両コマンドは `activity:` 行を加えます。ツール使用の回数と、CLI が
報告する場合は **コンテキストトークン**（モデルが最後に見たコンテキストの大きさで、累計ではありません）
です。続いて最新の行を開始からの時間つきで並べ、ジョブが実行中なら、次の待機に渡す `--since` の
カーソルを示す `next:` 行を出します。`--since <n>`（0 から 1,000,000,000）は `n` 回目より後のツール使用だけを
並べ、`--activity <m>`（0 から 100、デフォルト 10）は最新から何行並べるかで、残りは
`(K earlier lines not shown)` として数だけ示します。`(some lines were dropped)` は番号の抜けがあるという
意味です。ファイルは 500 件を保持すると `<id>.activity.1` に移して書き直すので、長い実行では古い行から
なくなります。また Windows でリーダーがファイルを開いている間に書かれた行は捨てられます。どちらのフラグも
これ以外の値は使い方の誤り（終了コード 2）です。`--json` では、ファイルがある場合に限り、ジョブに
`activity` キー（`count`、`context_tokens`、`elapsed_seconds`、`lines`（`n`、`s`、`line`）、`dropped`、
`omitted`）が加わります。ジョブのレコード自体は変わりません。

1 行に書けることは許可リストで決まっており、モデルのテキストやツールの自由記述の引数は決して含みません。

| ツール | 行 |
| --- | --- |
| `Read`、`Edit`、`Write`、`NotebookEdit` | ツールとパス。パスはプロジェクトからの相対パス、外なら `<outside>/<ファイル名>` |
| `Glob` | パターン（相対で `..` を含まないときだけ）と、`Read` と同じ扱いの検索パス。`Glob src/**/*.py`、`Glob src/**/*.py in src`、または `Glob` だけ |
| `Grep` | 検索したパス。パターンは決して出しません |
| `Bash`、`PowerShell`、Codex のコマンド | `Bash: <プログラム>`（ディレクトリと `.exe` は除く）。`Bash: <プログラム> <サブコマンド>` になるのは `git`、`gh`、`npm`、`pnpm`、`yarn`、`npx`、`cargo`、`go`、`docker`、`kubectl`、`pip`、`uv`、`poetry`、`make`、`dotnet`、`terraform` だけです。先頭の変数代入（`FOO=1`、`$env:FOO='x';`）は飛ばします。引用符の中の `;` では代入は終わらず、バッククォート、`(`、`{` を含む `$` の文は `Bash` だけになります。プログラム名がただの単語でなければ `Bash` だけになります。Codex がシェルで包んだコマンド（`pwsh -Command '...'`、`bash -lc '...'`）は先に包みを外します |
| `WebFetch` | スキーム、ホスト、指定があればポート。多くの API がトークンを置くパスは出しません |
| MCP のツール | `<サーバー>.<ツール>` |
| Codex のファイル変更 | ファイルごとに 1 行の `Edit <パス>` |
| agy の `view_file`、`write_to_file` | `Read <パス>`、`Write <パス>`。パスは `Read` と同じ扱い |
| agy の `run_command` | `Bash` と同じ扱い |
| そのほかの agy のツール | 素直な名前（英字 1 文字のあとに、英数字、`_`、`.`、`-` が 63 文字まで）なら名前だけ、`mcp__<サーバー>__<ツール>` は `<サーバー>.<ツール>`、それ以外の名前は `tool` |
| それ以外（`Task` と Codex の `web_search` を含む） | 名前だけ |

どの行も最初の改行と 100 文字で切り、エスケープシーケンスと制御文字を除き、伏せ字にしてから書き込み、
読むときにも同じ処理をします。Claude はメッセージごとにコンテキストを報告します。agy はツールの使用を
ステップの始まりに、モデルの各ステップのコンテキストをその終わりに示します。Codex は使用量をターンの
終わりにしか報告しないので、Codex のジョブにはコンテキストトークンが出ません。また Codex がツール使用を
流すのは `--json` で実行する architect の実行だけなので、Codex の implementer や fixer のジョブには
`elapsed:` しか出ません。

<a id="budget"></a>

## budget

| コマンド | 説明 |
| --- | --- |
| `budget show [--json]` | ステージごとに使った試行回数、委譲実行の合計、使用した委譲実行時間を表示します。 |
| `budget consume <stage> [--force]` | orchestrator が自分で実行するステージ（特に `test`）の試行を 1 回確保します。予算が尽きていれば終了コード 3 で終了します。 |
| `budget reset` | ラウンドカウンターを含め、予算を最初からやり直します。トークンの集計は保持されます。これは作業にかかったコストの記録であって予算ではなく、どのリセットでも消去されません。台帳が `budgets.session_idle_reset_seconds` の間アイドル状態だった場合にも自動的に行われます。 |

`run` と `review run` は自分の予算を消費するので、`budget consume` が必要なのは orchestrator が直接行う
ステージだけです。

<a id="tokens"></a>

## tokens

| コマンド | 説明 |
| --- | --- |
| `tokens show [--json]` | ワークフローが消費した量を、ステージごと、レビュアーごとに表示します。 |

これは集計であって予算ではありません。ここにあるものが実行を拒否することはありません。強制される
`budget` とは意図的に分けてあります。一方を他方として読むことこそ、この分離が防ごうとしている誤りです。

集計は現在の予算ではなく **ワークフロー全体** を対象とします。`budget reset` とアイドルリセットは予算を
最初からやり直しますが、集計は引き継ぎます。そのため、ここでの合計は作業が経てきたすべてのリセットに
またがり、どのリセットでも消去されません。消去されるのはワークフローを削除したとき（`workflow remove`）
だけです。したがって、別の集計とは別のワークフローのことです。
`dev-orchestra --workflow <id>` はその作業に独自の `.ai/workflows/<id>/` を与え、それとともに独自の
台帳、予算、集計を与えます。

数値は委譲先の CLI から得られるので、その完全さは CLI がどれだけ情報を出すかに左右されます。
Claude Code は、`result` イベントで input、output、cache-read、cache-write の数と cost を報告します。
Codex は合計を 1 つだけ文章で出力し、言い回しが変わるとそれは誤った値ではなく未報告になります。そのため
すべての列は `meas.`（そのステージの実行のうち何かを報告したものの数）と対になっています。報告しなかった
ものがある場合、出力には合計が *下限* であると示されます。

`billed` は input + output + cache-write の合計です。キャッシュの読み取りは意図的に除外しています。
その費用は新規の input の約 10 分の 1 であり、含めると、キャッシュがよく効いたステージが高価なステージ
より上位に来てしまうからです。金額には `cost` を使ってください。

`prompt_chars` は dev-orchestra 自身が組み立てたプロンプトのサイズです。input のうちこのリポジトリが
短くできる唯一の部分であり、そのため合計とは別に数えています。

`tools` と `tool out` は、委譲先のエージェントがツールを使って行ったことです。何回ツールを呼び出したか、
そしてそのツールが何文字を返したかです。これらは Claude Code のイベントストリームから得られます。
**Codex はどちらも報告しません**。フッターには、これらの列がいくつの実行をカバーしているかが示されます。
この 2 つの列では、`-` は *実行が報告しなかった* ことを、`0` は *実行がツールを使わなかったと報告した*
ことを意味します。これはトークンの列にはない区別であり、何も開かなかったレビュアーを見えるようにする
区別です。

agy はストリームのツールのステップからツール呼び出しを数えますが、その出力は数えません。勘定には
合計しか残らないので、agy だけが走った行は `tool out 0` と表示されますが、これは測定値ではありません。
未報告として読んでください。agy と Claude が一緒に走った行は、Claude の出力だけを数えています。

**`tool out` は観測されたツールの出力であり、読んだソースではありません。** 200 行のファイルに `wc -l`
を実行したレビュアーは 3 文字と数えられ、同じファイルに `cat` を実行したレビュアーはその全体が数えられ
ます。CLI の外からはこの 2 つを区別できないので、**レビュアーがどれだけのソースを読んだかは、ここからは
まったく知りようがありません**。これらの数値は代理指標であり、同種のもの同士（同じレビュアー id の、
変更の前と後）を比較するには役立ちますが、何が読まれたかを述べるためのものではありません。

ツール呼び出しは名前にかかわらずすべて数えられ、内訳は `tool_uses_by_name`（`--json`、および各実行の
`usage`）に保持されます。`Read` だけを数えると過小に数えることになります。Claude の読み取り専用の
実行には `Bash` はありませんが、`Grep` と `Glob` もファイルを読みますし、implement の実行はすべての
ツールを持ちます。実際、これを設計している間に計測した実行は、読み取り専用の実行から `Bash` がなくなる
前のもので、`wc -l` でファイルを読み、`Read` を一度も呼び出しませんでした。

これらのカウントができる前に記録された実行は、ツールを使ったかどうかを示せないので、使わなかったものと
してではなく、そのように報告されます。そのような集計に書き込まれる最初の実行は（自分自身がツールを報告
するかどうかにかかわらず）`tool_reported_runs` を作成します。これにより、カウントより前の実行の数は、
存在しないキーから読み取られるのではなく `tool_unknown_runs` に保持され、その注意書きはその後に記録される
すべての実行を経ても残ります。

`options.output_format: json` を設定した Claude のロールも、ツールの活動を報告しません。この形式は
`result` オブジェクトを 1 つ出力するだけでツールのイベントを一切出さないので、その実行は計測された `0`
ではなく `-` になります。

```bash
dev-orchestra tokens show
dev-orchestra tokens show --json
```

<a id="optimization"></a>

## optimization

| コマンド | 説明 |
| --- | --- |
| `optimization report [--json]` | このプロジェクトが記録したすべてのレビューラウンドにわたって `optimization.level` が何を決めたか、どの高リスクパターンがそれをエスカレートさせたか、そしてレビュアーが何を課金されたかを表示します。 |

台帳ではなく実行ログから読み取ります。レベルの効果は *率*（どれだけの頻度でラウンドを拒否したか、
どれだけの頻度でパネルを削減したか）であり、率にはラウンドが必要です。`budget reset` は新しい台帳を
始めますが、イベントログは蓄積を続けます。

同じ理由で、現在のワークフローだけでなく `.ai/` 内の **すべてのワークフロー** を読み取ります。1 つの
ワークフローは数ラウンドにすぎず、それでは率になりません。`--workflow <id>` は 1 つに絞り込みます。
これは「このリポジトリで」ではなく「この作業で、レベルが何をしたか」に答えます。

```
Review rounds recorded: 14 (12 ran, 2 refused)
  levels in force        aggressive x14
  gate verdicts          allow x12, refuse x2
  panel reduced          5
  escalated (high risk)  3

Reviewer runs: 19 (19 reported usage), 823,104 billed
  68,592 billed per round that ran

Estimated saving from 2 refused round(s): ~137,184 billed tokens.
An estimate: what a round that did not happen would have cost is
unknowable, so this is the mean of the 12 that did.
```

条件付きのレビュアー（`when: high-risk` またはパスで絞り込んだもの）が設定されていると、`panel reduced` の後に `conditional reviewers` の行が
続きます -- `added x3, left out x7`、どこかのラウンドが宣言されていればさらに
`declared with --high-risk x2` -- 拒否されたラウンドも含め、すべてのコードラウンドについて数えます。
`--json` では `conditional`（`added`、`left_out`、`declared_rounds`）です。そうした判断が 1 つもない
ログでは、この行は表示されません。ラウンドに見るもののなかったロールはそこでは数えず、その隣の
`roles skipped` の行で数えます -- `left out x12 of 40 judged (balanced x7, quality
x5), est. 1,200,000 tokens` -- 対象は実行されたコードラウンドで、有効だったレベルごとに分け、推定削減量は
外された各席をレビュアー実行 1 回あたりの平均課金トークンで見積もります。design ラウンドにも同じ行があり、
費用のブロックで `design review` の下に字下げして表示されます。`--json` では `relevance` と
`design_relevance`（`judged`、`added`、`left_out`、`left_out_by_level`、`estimated_saving`）です。
そうした判断が 1 つもないログでは、この行は表示されません。`left_out_by_level` は、誤って外した場合の
損失が最も大きい `quality` のラウンドから、削減量のどれだけが来ているかを示します。宣言されたラウンドはエスカレーションではなく、
`escalated (high risk)` には決して現れません。`optimization.extra_high_risk_paths` のパターンへの一致は
エスカレーションであり、そのパターンも他と同じように一覧されます。すべてのラウンドがエスカレーション
された場合の助言は、`optimization.high_risk_paths` と `optimization.extra_high_risk_paths` の両方を
挙げます。

`Reviewer runs:` より上はすべてコードレビューだけのものです。`review.design` のラウンドは plan に対して
レビューされ、plan には計測する diff もゲートの判断に使うテスト結果もないので、それらについてはどの
レベルも何も決めていません。それらはコストには現れますが、どの率にも現れません。design レビューが有効
だと、消費量は次のように分かれます。

```
Review rounds recorded: 4 (4 ran, 0 refused)
  levels in force        balanced x4
  gate verdicts          allow x4
  panel reduced          0
  escalated (high risk)  0

Reviewer runs: 12 (12 reported usage), 909,313 billed
  code review            8 (8 reported usage), 558,884 billed over 4 round(s), 139,721 each
  design review          4 (4 reported usage), 350,429 billed over 2 round(s), 175,214 each
```

この 2 つが一緒に平均されることはありません。plan に対するラウンドと diff に対するラウンドは同じ単位の
作業ではないので、両方にまたがる数値はどちらも表しません。design レビューが無効な場合は design の行は
ありません。実行されなかったステージの `0` は、計測を装ったノイズだからです。`--json` では、
`reviewer_runs`、`measured_runs`、`billed_tokens`、`billed_per_round` はこれまでどおりコードレビューの
数値であり、design のものは `design_rounds`、`design_reviewer_runs`、`design_measured_runs`、
`design_billed_tokens`、`design_billed_per_round` です。`design_rounds` は *実行された* ラウンドを
数えます。失敗した、または kill の後に放棄されたラウンドは何も課金されていないので、ここではラウンドでも
なく、その除数でもありません。

レビュアーがツールで何をしたかを報告した場合は、次のブロックが続きます。

```
Tool activity, per run and only over the runs that reported it:
  code review            6.5 use(s)/run, 41,300.0 observed output chars/run (4 of 8 run(s) reported)
```

分母は `tool_reported_runs` であって決して `reviewer_runs` ではなく、そのために表示されています。Codex は
ツールの活動を報告しないので、パネル全体で割ると、パネルの構成以外に理由もなく数値が半分になってしまい
ます。そして、これらの数値が存在する目的である比較が、レビュアーが追加または削除されるたびに動いて
しまいます。何も報告されていなければ、ゼロの並んだ行ではなく行そのものがありません。
実行あたりの出力文字数は、出力を報告した実行の数 `tool_output_reported_runs` で割ります。agy は呼び出しを
数えますが、その出力は数えないからです。この数がツールを報告した実行の数と違うときは、行に
`, output chars over K run(s)` が加わります。出力を報告した実行がなければ、その数値は `-` と表示されます。

**観測された出力はツールが返したものであり、読んだソースではありません**。`wc -l` は 200 行のファイルに
対して 3 文字を返します。`--json` では `tool_reported_runs`、`tool_output_reported_runs`、`tool_uses`、
`tool_output_chars`、`tool_uses_per_run`、`tool_output_chars_per_run`、そしてその隣に `design_` を前に
付けた 6 つがあります。コンテキストのグループとペアの各側にも `tool_output_reported_runs` があり、その
1k あたりの出力の数値は、出力を報告した実行の数で重み付けされます。

コードラウンドが一度でも[周辺コンテキスト](reviews.md#surrounding-context)を運ぶと、実行されたコード
ラウンドが 2 つに分かれて表示されます。

```
Surrounding context (review.context.surrounding), code review rounds only:
  with context           3 round(s), 6 run(s), 412,300 billed, 137,433 per round; 4.0 use(s)/run, 12,400.0 observed output chars/run (3 of 6 run(s) reported); 114,360 context chars adopted, 9,812 left out
                         per run and 1k chars of change (3 sized round(s), 61,200 chars): 3,368.5 billed over 6 billed run(s), 607.8 observed output chars over 3 reporting run(s)
  without context        9 round(s), 18 run(s), 1,522,880 billed, 169,208 per round; 9.6 use(s)/run, 44,120.0 observed output chars/run (9 of 18 run(s) reported)
                         per run and 1k chars of change (7 sized round(s), 210,400 chars): 2,851.7 billed over 14 billed run(s), 1,425.9 observed output chars over 7 reporting run(s)
```

`with` になるのは、実際にどれかのレビュアーにコンテキストが示されたラウンドだけです。設定が on でも
何も採用しなかったラウンドは `without` です。**比べるのは各群の 2 行目で、1 行目ではありません。**
生の数値は変更ごとの大きさとパネルの大きさに連動して動きます — 小さな変更はレビュアー 1 人に削減される
のが普通です — そのため 2 行目は、変更のサイズを各数値を報告した実行数で重み付けしたもので割ります。
課金は `change_chars × 課金を報告した実行数` で、ツール出力は `change_chars × 出力を報告した実行数`
で割り、どちらもサイズを記録したラウンドだけを対象にします。コンテキストを運んだラウンドが存在する
までは何も表示されません。`--json` には `by_context.with` と `by_context.without` が常に含まれ、それぞれ
`rounds`、`reviewer_runs`、`measured_runs`、`billed_tokens`、`billed_per_round`、ツールの数値、
`sized_rounds`、`change_chars`、`sized_billed_tokens`、`billed_run_change_chars`、`sized_billed_runs`、
`sized_tool_output_chars`、`output_run_change_chars`、`sized_output_runs`、
`billed_per_run_per_1k_change_chars`、`tool_output_chars_per_run_per_1k_change_chars`、`adopted_chars`、
`trimmed_chars` を持ちます。`tokens show` は台帳の累計なのでラウンドを分けられず、変更はありません。
ラウンドごとの比較はこちらで行います。

この分割は異なる変更どうしを比べます。1 つのスナップショットを `review run --surrounding none` と
`--surrounding enclosing` の両方でレビューすると
（[周辺コンテキストの効果を測る](limits.md#measuring-what-surrounding-context-does) を参照してください）、
その 2 本の run を対にしたブロックが出ます:

```
Paired on one snapshot (--surrounding none vs enclosing: the same frozen diff, tree, panel and prompt inputs):
  3f2a9c1b7e04 in issue-54   change 12,400 chars; panel claude-general (claude, opus), codex-general (codex, gpt-5); 3,100 context chars adopted (3,521 as carried), 0 left out
      with:    2 run(s), 41,200 billed, 20,600.0 per run; 3.0 use(s)/run, 9,800.0 observed output chars/run (1 of 2 run(s) reported)
      without: 2 run(s), 45,900 billed, 22,950.0 per run; 6.0 use(s)/run, 22,100.0 observed output chars/run (1 of 2 run(s) reported)
      delta:   -2,350.0 billed/run, -3.0 use(s)/run, -12,300.0 observed output chars/run
  total, 1 pair(s) counted   with: 20,600.0 billed/run, 3.0 use(s)/run, 9,800.0 observed output chars/run; without: 22,950.0, 6.0, 22,100.0; delta: -2,350.0, -3.0, -12,300.0
```

その後に、ペアが何を揃え、何を揃えていないかの注記が続きます。対になるのは `--surrounding` 付きで
実行した run だけです。側はフラグの値で決まり、ペアの鍵は workflow ディレクトリ、完全なスナップショット
sha256、凍結したツリー、`head`、`base` で、予算の epoch は決して含みません。各側で最新の run が対に
なります。実行あたりの課金は usage を報告した run で、ツールの数値はツールを報告した run で割ります。
次の場合、ペアは一覧には載りますが合計からは外れ、その理由が 1 行目の末尾に付きます:

- パネルが違う（`panels differ (without: ...)`）: `(id, provider, model, role)` の集合が等しくない（role はプロンプトを変える）。
- どちらかの側のレビュアー run が届かなかった（`not delivered (with: codex-general failed)`）:
  `ok` 以外の status。
- 2 本の run のプロンプト入力が違った（`inputs differ (context, max_findings)`）。
- enclosing の run が何も採用しなかった（`nothing adopted`）。

`--json` には `paired` が常に含まれます: `pairs`（それぞれ `workflow`、両側の `epoch`、`snapshot`、
`tree`、`same_panel`、`delivered`、`same_inputs`、`nothing_adopted`、`counted`、`panel`、
`without_panel`、`undelivered`、`inputs_differ`、`change_chars`、`adopted_chars`、`context_chars`、
`trimmed_chars`、`with`、`without`、`delta` を持つ）、`pairs_total`（counted なペアの数）、
`pairs_listed`、counted なペアについての合計 `with`、`without`、`delta`、そして `note` です。各側は
`reviewer_runs`、`measured_runs`、`billed_tokens`、`billed_per_run`、`tool_reported_runs`、
`tool_uses`、`tool_output_chars`、`tool_uses_per_run`、`tool_output_chars_per_run` を持ちます。
`delta` は実行あたりの with − without で、どちらかの側に割る対象が無ければ `null` です。
`by_context` は変わりません。

ラウンドのレポートを 1 つでも読めると、stage ごとに採点表が続きます。各レビュアーが何を報告し、
オーナーがそれをどう判断し、その run にいくら掛かったかです。指摘とトリアージは全ラウンドの
アーカイブされたレポート（`reviews/rounds/`、[再レビュー](reviews.md#re-review) を参照）から、
run とコストは run ログから読み、両者をラウンドごとに突き合わせます:

```
Reviewer scorecard, code review: 18 of 27 recorded round(s) had a report to read.
  claude-general         49 reported: 43 accepted, 1 rejected, 4 duplicate, 1 open; 37 found alone (33 accepted)
                         18 run(s), 2,794,838 billed, $42.07 over 18 priced run(s); 2% rejected, 64,996 billed / $0.98 per accepted
    usual (sonnet)       30 reported: 27 accepted, 0 rejected, 2 duplicate, 1 open; 23 found alone (21 accepted)
                         12 run(s), 1,394,838 billed, 116,236 per run, $12.07 over 12 priced run(s), $1.01 per run; 0% rejected, 51,660 billed / $0.45 per accepted
    high-risk (opus)     19 reported: 16 accepted, 1 rejected, 2 duplicate, 0 open; 14 found alone (12 accepted)
                         6 run(s), 1,400,000 billed, 233,333 per run, $30.00 over 6 priced run(s), $5.00 per run; 5% rejected, 87,500 billed / $1.88 per accepted
  codex-general          25 reported: 15 accepted, 2 rejected, 6 duplicate, 2 open; 16 found alone (11 accepted)
                         17 run(s), 1,166,386 billed, no cost reported; 9% rejected, 77,759 billed per accepted, $ -
  localllm-qwen          22 reported: 1 accepted, 19 rejected, 2 duplicate, 0 open; 20 found alone (1 accepted)
                         9 run(s) (6 failed), nothing reported; 86% rejected, per accepted withheld under 10 accepted (when: high-risk; left out of 9 round(s))
  panel                  96 reported: 59 accepted, 22 rejected, 12 duplicate, 3 open
                         44 run(s), 3,961,224 billed, $42.07 over 18 of 44 run(s); 24% rejected, 67,139 billed / $0.71 per accepted

Review effort, code and design together: 128 accepted over 28 of 46 recorded round(s); 5,614,101 billed,
  $71.90 over 30 priced run(s); 43,860 billed / $0.56 per accepted
```

各ブロックの後には、その数字が何を主張できるかの注記が付きます。ラウンドの区別の仕方と、イベントが
自分のレポートを見つける方法:

- **ラウンドとは凍結 1 回** で、スナップショットの sha256 の先頭 12 文字と、与えられた `round_id` で
  名付けられます。同じツリーや plan を 2 回凍結すると sha は繰り返し、ラウンドは 2 つです。`--only` の
  再実行と `--surrounding` の対の 2 回の run は 1 ラウンドです。そのイベントはすべてコストを足し、
  指摘とトリアージは最後の run のものです。
- **`round_id` を持つイベント** は、ちょうどそのラウンドのレポートに一致します。イベントがそれを
  持つ前に記録されたものは、その sha のレポートがちょうど 1 つのときだけ一致します。同じ sha の
  レポートが 2 つあれば 2 ラウンドであり、新しい方を取ると古いラウンドのコストを新しいラウンドの
  指摘に付けてしまいます。レビュアーエントリにスナップショットの印が無いイベントは、iteration が
  等しく、より確かなものがまだ取っていなければ live のレポートに一致します。
- **読めるレポートの無いラウンドは、コストも含めてすべての数字から除かれます。** コストと指摘が
  同じラウンドを指すようにするためです。0.11.0 より前はワークフローの最後のラウンドしか残らず、
  失われたラウンドは指摘が直される前の変更をレビューしていました。残ったものに対する率は偏っており、
  偏りの向きは分かりません。見出しは、記録されたラウンドのうちいくつを読めたかを示します。どの
  レビュアーもレビューを返さなかったラウンドは穴ではなく、レポートがあっても読みません -- code の
  ラウンドは誰かが戻ったかどうかに関わらずレポートを書き、その中身はそのラウンドのレビュアーが
  書いたものではないからです。その run とコストは数えられ、見出しはこれを別に数えます。
- **指摘はワークフローと stage ごとに、`key` で 1 回だけ数えます。** 誰も直さなかった指摘は
  毎ラウンド戻ってくるからです。ラウンドはそのイベントが記録された順に取り、最後の *明示的な*
  トリアージが勝ちます -- `accepted`、`rejected`、
  `duplicate`、`needs-investigation`、または `triage_set_at` を持つもの。印の無い `needs-triage` は
  再構築されたレポートが指摘に与える既定値で、採用を取り消しません。印のあるものは
  `review triage --status needs-triage` で、取り消します。未判断は `open` です。
- **found alone は、そのレビュアーを外したときに失われ得る数の上限 (upper bound) です**: その
  レビュアーだけが報告し、どのラウンドでも、別レビュアーの指摘と重複候補として結ばれ、かつどちらかが
  `duplicate` とトリアージされた、ということが無いもの。候補リンクで結ばれなかった重複は、
  それでも数えられます。
- **率は判定済み（`accepted`、`rejected`、`duplicate`）10 件から、1 採用あたりの数字は採用 10 件から
  印字します**。それ未満では件数だけが出ます。1 採用あたりの数字は読めたラウンドだけに対する値で、
  読めなかったラウンドはそれをどちらにも動かし得ます。コスト合計は床 (floor) です: 何も報告しなかった
  run は何も足さず、トークンは報告したが価格を報告しなかった run はドルを足しません。したがって価格を
  報告しないレビュアーの 1 採用あたりコストは `$0.00` ではなく無しです。
- **code と design の数字は最後の行でだけ合算します。** その分母は採用された指摘、つまりどちらの
  stage が見つけたにせよオーナーが直すと決めた欠陥 1 件です。ラウンドあたりの数字はラウンドで割り、
  ラウンドは plan と diff で違う作業単位です。それでも stage の混合比はこの数字を動かすので、2 つの
  stage のブロックと並べて読みます。
- **条件付きレビュアーのコスト行の末尾には、その条件と、コストが入っているラウンドのうち外れた数が
  付きます。** 「N 回走り、M ラウンド外れた」と並ぶので、条件が役目を果たしているかを判断できます。
  1 ラウンドにイベントがいくつあっても外れたのは 1 回と数え、そのうちの 1 つ（`--only` の再実行など）
  がそのレビュアーを走らせていれば外れたとは数えません。すべてのラウンドで外れたレビュアーも、件数
  ゼロの行として出ます。外れたラウンドには run が無いので、1 採用あたりのコストはそれで薄まりません。
  ラウンドに見るもののなかったロールも同じように数え、`when` は `relevance` と示します。design
  ラウンドも、そのイベントが `optimization` ブロックを持つようになってからは数えます。条件付きレビュアー
  より前のイベントと、そのブロックより前の design のイベントは何も記録していません。
- **`high_risk_model` を走らせた席には、モデルの枠ごとの行が付きます。** 席自身の 2 行の下に
  `usual`、`high-risk` の順で、それぞれの run、コスト、run あたりのコスト、指摘、率を出し、閾値は
  枠ごとに単独で当てはめます。通常のモデルしか走らせたことのない席は行を足しません。run の
  枠はそのエントリが `model_slot` に記録したもので、それが無いエントリは、high-risk モデルより前の
  run もすべて含めて `usual` です。枠はその時点で席に設定されていた枠であってモデルでは
  ありません。ラベルはその枠が走らせたモデルを示し、複数あれば `usual (sonnet x6, opus x4)` の
  ようになります。プリセットが変わる前後で通常の枠がそうなったようにです。指摘は、それを最初に
  報告した run の枠に数え、統合された指摘は報告者ごとにその報告者の枠に数えます。その
  ラウンドに run の無い報告者は `usual` と数えます。1 ラウンドの 2 つのイベントが席を別々の枠で
  走らせた場合（`--only` の再実行に `--high-risk` を付けたとき）、各 run のコストはそれぞれの枠に、
  指摘はレビューを終えた後のほうの run の枠に入ります。失敗やタイムアウトに終わった run は指摘を
  返していないからです。列に収まらないラベルは、数字の上に 1 行で出します。

これはレビューが何を買ったかを言うものであって、レビューが悪くなったかどうかを言うものではありません。
1 採用あたりのコストが上がるのは、レビュー対象のコードが良くなったときの姿でもあり、レビュアーが欠陥を
見落とし始めたときの姿でもあり、価格が上がったときの姿でもあります。この数字は 3 つを区別できません。

読めるレポートの無い stage はブロックを出しません。`--json` には `scorecard` が常に含まれ、`code`、
`design`、`total` を持ちます。各 stage は `rounds_recorded`、`rounds_read`、`rounds_unreviewed`、
`rerun_rounds`（コストが入っているラウンドのうち `--surrounding` の対の数）、`workflows_read`、
`findings`、`reviewers`（id ごと）、`panel` を持ちます。レビュアーは `runs`、`failed_runs`、
`measured_runs`、`priced_runs`、`billed_tokens`、`cost_usd`、`billed_per_run`（計測された run
あたり）、`cost_per_run`（価格の付いた run あたり）、`reported`、`accepted`、`rejected`、
`duplicate`、`open`、`alone`、`alone_accepted`、`rejection_rate`、`billed_per_accepted`、
`cost_per_accepted` を持ち、run あたりの 2 つは割るものが無ければ `null`、最後の 3 つは閾値未満で
`null` です。数えたラウンドのどれかで条件付きと
記録されたレビュアーは、さらに `when`（`high-risk`、`paths`、`relevance` のいずれか。そう記録された最新のラウンドの値）と
`left_out_rounds` を持ちます。どのレビュアーも `models` を持ち、枠（`usual`、`high-risk`）を
キーとして、走ったか報告した枠だけを持ちます -- 一度も走らなかった席は `{}` です。各枠は
`when` と `left_out_rounds` を除いたレビュアーと同じ数字に加え、`by_model`（run を走らせたモデルごとに
数えたもの）を持ちます。`panel` と `total` は `alone` の 2 つと `models` を
除いた同じ列で、2 人のレビュアーが報告した指摘は 1 回と数えます。`total` は 4 つのラウンド数の和も
持ちます。

すべてのラウンドがエスカレートした場合、レポートはそれをはっきり述べます。設定されたレベルは一度も適用
されなかったということであり、その原因となったパターンが示されます。すべてのラウンドでエスカレートして
存在しないも同然になったダイヤルと、一度も作動しないダイヤルは、カウントの上では見分けがつかず、どちら
なのかを示すのはパターンだけです。インフラストラクチャのリポジトリでは `*.tf` が常にマッチしますが、
その絞り込みに使う設定が `optimization.high_risk_paths` です。

節約量は推定値であり、そう明記されています。拒否されたラウンドが *かかっていたであろう* コストは知り
ようがありません（実際には起きていないからです）。そのため、この数値は同じリポジトリで実際に実行された
ラウンドの平均であり、それが最も誠実な代替値です。

テスト結果なしで記録されたラウンドも報告されます。ゲートは `state record test ok|failed` が書き込んだ
ものを読むので、何も書き込まれなかったラウンドでは判断の材料がなく、ゲートが作動したはずがありません。
これは効果のなかったレベルとは別のことであり、合計だけからでは両者を取り違えやすいのです。

<a id="architect-revisions"></a>

### Architect revisions

plan の改訂にかかった費用を、その改訂が architect のセッションを継続したもの（`resumed`、
`run architect --resume` による）か新規に走ったものかで分けて示します。ワークフローごとに、`--output` が
そのワークフローの plan だった architect の実行だけを数えます。成功して応答した最初のものが初回の設計で、
それ以降のものはすべて改訂の試みです。各改訂は、そのワークフローの初回の実行に対する費用の比で測ります。
plan の大きさの違いは 2 種類の実行の違いよりはるかに大きいからです。0 より大きい費用を報告しなかった初回の
実行（mock は `0.0` を報告します）では比を出しません。群ごとの行には、完了した改訂の数、比の平均、そして
途中で失敗した試みの費用も含めた完了 1 件あたりの比が出ます。

```
Architect revisions (cost against each workflow's initial design run):
  resumed: 3 revisions (3 priced, 3 with ratio) cost ratio to initial 0.21 mean, 0.27 per completed incl. 1 stalled + 0 rejected + 0 failed attempts (0 priced)
  fresh: 1 revisions (1 priced, 0 with ratio) cost ratio to initial n/a (initial run has no usable cost); 0 stalled + 0 rejected + 0 failed attempts (0 priced)
  --resume ran fresh because:
    no earlier architect run in this workflow x2
```

理由は `run --resume` が記録する固定句のまま数えます。このブロックは試みが 1 つでもあればレビューの
ラウンドの有無にかかわらず表示され、なければ表示されません。`--json` では `architect_revisions` で、
`attempts`、`resumed` と `fresh`（それぞれ `runs`、`measured_runs`、`priced_runs`、`ratio_runs`、
`billed_tokens`、`cost_usd`、`cost_ratio_sum`、`duration_seconds`、`context_runs`、`context_tokens`、
平均の `billed_per_run`、`cost_per_run`、`cost_ratio_mean`、`duration_per_run`、`context_per_run`、
`failed_attempts`、`cost_per_completed_ratio`）、そして `fallbacks`（`total`、`reasons`）を持ちます。
コンテキストの数値は、CLI がコンテキストを報告したすべての実行を数えます。Claude と agy（最後のモデルの
ステップの `input_tokens + cache_read_tokens`）で、Codex は報告しません。

<a id="progress"></a>

## progress

| コマンド | 説明 |
| --- | --- |
| `progress record <stage> --signature <text> [--json]` | ステージが生み出したものを記録します。連続して同一のシグネチャは、最後の試行が何も変えなかったことを意味し、コマンドは停止するよう伝えます。 |

```bash
dev-orchestra progress record test --signature "3 failed: test_totals, test_discount, test_coupon"
```

レビューは、未解決の指摘の集合から自分のシグネチャを自動的に登録します。

<a id="workflow"></a>

## workflow

成果物はワークフローごとに 1 つのディレクトリ `.ai/workflows/<id>/` に置かれます。そのため、同じ
チェックアウト内の 2 つのセッションが、plan、レポート、予算、ラウンドカウンターを共有することはもう
ありません。id はコマンドごとに、`--workflow`、`DEV_ORCHESTRA_WORKFLOW`、ホストのセッション id
（12 文字にハッシュ化）、`current.json`、そして最後に新しい id の順で解決されます。どのルールで決まったか
は `workflow show` が示します。

コンテナに対して書かれたパスはワークフローの中で解決されます。`--output .ai/plan.md` は *この*
ワークフローの plan を意味します。`.ai/` の外のパスや、すでにワークフローを指定しているパスは、書かれた
とおりに使われます。

これが分離するのは成果物であって、作業ツリーではありません。1 つのチェックアウトにはファイルの組が 1 つ
しかなく、レビュアーはその `git diff` を読みます。本当に同時に実行される作業では、各ワークフローに独自の
worktree（`git worktree add ../x x`）を与えてください。それは別のルートであり、したがって別の `.ai/` に
なります。

ワークフローのディレクトリが自動で片付けられることはありません。新しいワークフローの最初のコマンドは、
`workspace.stale_notice_days` 日（デフォルト 30。`0` で止まります）以上アクティブでないほかのワークフローを、
stderr に一度だけ知らせます。実行中のステージがあるワークフローは挙げません。何も削除しません。ワークフロー
を削除するのは、これまでどおり `workflow remove` だけです。

| コマンド | 説明 |
| --- | --- |
| `workflow list [--json]` | ここにあるすべてのワークフローを、最近アクティブだった順に表示し、現在のものと実行中のステージに印を付けます。 |
| `workflow show [--json]` | このコマンドがどのワークフローにいるか、その成果物がどこにあるか、どのルールでそれが選ばれたかを表示します。 |
| `workflow use <id>` | このディレクトリ用に id を記憶します（`current.json`）。セッション id をエクスポートしないホスト向けです。セッション id をエクスポートするホストでは、引き続きそちらが優先されます。 |
| `workflow remove <id> --yes [--force]` | 1 つのワークフローの成果物を削除します。`--yes` がなければ拒否し、現在いるワークフローも拒否します。実行中のステージがあるワークフローや、`jobs/` に終わっていない detached のジョブがあるワークフローも拒否します（exit 2。ワーカーがもういないジョブは先に `abandoned` にされ、数えません）。削除のあとに終わったワーカーが、空になったワークフローへ状態を書き戻すためです。最近動いていたというだけでは拒否しません。1 分前に終わったワークフローも削除します。`--force` を付けると、それでも削除します。クラッシュしたステージが残した印を消すときに使います。Windows でほかのプロセスが開いているファイルなどのために削除が途中で止まったときは exit 1 で、残ったファイルの数を伝えます。 |

<a id="state--summary"></a>

## state / summary

| コマンド | 説明 |
| --- | --- |
| `state show [--json]` | このプロジェクトで記録されたステージのイベントを表示します。 |
| `state record <stage> <status> [--detail k=v …]` | ステージの結果を追記します（`run` を通して実行されないステージ用）。`state record test ok\|failed` はレビューゲートが読むものです。再テストも同じ方法で記録してください。`test` と `re-test` の status は `ok` か `failed` だけで、それ以外は拒否します（exit 2）。ゲートはほかの語を合格と読んでしまうためです。`--detail` でイベント自身の欄である `stage`、`status`、`at` は指定できません（exit 2）。 |
| `summary [--json]` | 実行終了時のステージとモデルのサマリーを表示します。plan が承認されていれば `design_approval` も含みます。`--json` は `stages`、`counts`、`tokens` に加えて、テキストに出る内容を持ちます。`design_counts`（`reviewers_ok`、`reviewers_total`。design のレポートがなければ `{}`）、`models`（ロールごとの `provider`、`family`、`version`）、`reviewers`（`id`、`provider`、テキストに出るとおりの `model`、`status`。並びはテキストと同じ）、`skipped`（`refused`、`refused_by`、`design_refused`、`panel_reduced`。名前は `optimization report --json` と同じ）です。 |

<a id="environment-variables"></a>

## 環境変数

| 変数 | 効果 |
| --- | --- |
| `DEV_ORCHESTRA_CONFIG` | このファイルをそのままグローバル設定レイヤーとして使います |
| `DEV_ORCHESTRA_HOME` | プラットフォームの設定ディレクトリの代わりにこのディレクトリを使います |
| `DEV_ORCHESTRA_WORKFLOW` | 使用するワークフロー。ホストのセッション id より優先されます |
| `DEV_ORCHESTRA_SESSION` | ワークフローを導出するためのセッション id。セッション id をエクスポートしないホスト向けです |
| `DEV_ORCHESTRA_MOCK_DIR` | mock provider 用の定型応答 |
| `DEV_ORCHESTRA_MOCK_RESPONSE` | mock provider 用のインラインの定型応答 |
| `DEV_ORCHESTRA_MOCK_FAIL` | mock の実行を失敗させます（`1` = すべて、それ以外はプロンプトの部分文字列） |
| `DEV_ORCHESTRA_MOCK_ACTIVITY` | mock の実行がジョブの activity や `review run --progress` に報告するツール行。`\|` 区切りで、`<部分文字列>=>行` はその部分文字列を含むプロンプトのときだけ |
| `CODEX_HOME` | Codex CLI の設定と認証情報を探すときに考慮されます |
| `CLAUDE_CONFIG_DIR` | Claude Code のユーザー設定の場所。`hooks install` と、フックを入れたり外したりするコマンドが使います |
| `DEV_ORCHESTRA_TEST_ASSUME_NO_CLI` | テスト専用で、今は不要です。テストは常に provider CLI を隠し、起動もしません。付けても受け付けますが、何も変わりません |

<a id="troubleshooting"></a>

## トラブルシューティング

まず `dev-orchestra doctor` を実行してください。`doctor --json` で同じ内容を機械可読な
形で得られます。

| 症状 | 原因と対処 |
| --- | --- |
| `Source: built-in defaults, fitted as preset standard` | 設定ファイルがまだなく、インストール済みの CLI に合わせた組み込みの `standard` プリセットが有効です。`dev-orchestra config setup`、または `config setup --preset <name>`。 |
| `note: no config file; running preset standard …` | 同じ状況を `run` と `review run` が示すものです。新しいワークフローでは、設定全体も一度表示されます。 |
| `codex: … does not vouch for …` | この Codex CLI が提供していない family です。`dev-orchestra model list` で確認し、`recommended-coding` を使うか、正確な id を pin してください。 |
| `claude: cannot resolve model family 'x'` | 提示されている alias ではありません。`dev-orchestra model list`。 |
| `Installed: no` | CLI が PATH にありません。自分でインストールしてください。スキルはインストールしません。 |
| パスの後ろに `(stored at …\Packages\…)` | Microsoft Store 版の Python です。この Python が AppData の下に書くファイルを、Windows はパッケージ専用のフォルダーに置きます。そこにあるファイルは、エクスプローラーやエディター、ほかの Python からは見えません。python.org 版の Python（`py`）を使うか、`DEV_ORCHESTRA_HOME` を `%USERPROFILE%\AppData` の外で、どのプロジェクトのチェックアウトの中でもない、自分で管理する信頼できるフォルダーに設定し、そこへ `config.yaml` だけをコピーしてください。詳しくは `doctor` が示し、残りは `references/configuration.md`（「置き場所」）にあります。 |
| 委譲先の CLI から `Failed to authenticate` | その CLI で直接ログインしてください（`claude`、`codex login`）。`doctor` が報告するのは認証情報の *存在* で、有効かどうかではありません。 |
| `review snapshot` が empty と言う | `HEAD` から何も変わっていません。`--base <rev>` を使うか、実装が動いたかを確認してください。 |
| `not a git repository` | スナップショットには git が必要です。`git init` するか、commit のあるリポジトリをレビューしてください。 |
| レビュアーが 1 人失敗した | 想定内で、処理は続きます。理由はワークフローの `reviews/consolidated.md` にあります。 |
| implementer がテストを実行できない | `acceptEdits` が自動承認するのは編集で、シェルコマンドではありません。プロジェクト自身の CLI 設定でそのコマンドを許可リストに入れるか、`implementer.options.permission_mode` を設定してください。 |
| 明らかに同じ finding が 2 件ある | 自動統合は意図的に保守的です。「Possible duplicates」の一覧を確認し、片方を `duplicate` としてトリアージしてください。 |
| レビューが終わらない | `review.timeout_seconds` を下げるか、`--sequential` でどのレビュアーが止まっているかを確かめてください。`review.timeout_seconds` が効くのはレビュアーだけです。ロールの `run` には `run.timeout_seconds.<role>` があります。 |
| `… hit its Ns deadline (run.timeout_seconds.<role>, …) and was killed` | 実行がロールの期限より長くかかりました。1 回だけなら `--timeout` で、ずっとなら `config set run.timeout_seconds.<role> N` で引き上げてください。 |
| 設定のパースエラー | 内蔵の YAML パーサは anchor、alias、ブロックスカラーを拒否します。簡素にするか、PyYAML を入れてください。 |
| `—` が `\u2014` と表示される | コンソールがその文字を表現できません（日本語 Windows の cp932 など）。落としたり止まったりせず、エスケープして表示します。`chcp 65001` か `PYTHONIOENCODING=utf-8` で正しく表示されます。 |
