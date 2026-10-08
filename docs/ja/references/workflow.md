<!-- translated-from: references/workflow.md sha256:d8ef0a34de971de98195409592360d9da48548d31b8a2ee5c7c083f88ad9f787 -->

> この文書は [references/workflow.md](../../../references/workflow.md) の日本語訳です。内容が食い違うときは英語版が正です。

<a id="workflow"></a>

# ワークフロー

<!-- contents: start -->

**目次**

- [ステージの選択](#stage-selection)
- [成果物](#artifacts)
- [Design](#design)
- [Design レビュー](#design-review)
- [承認](#approval)
- [実装](#implementation)
- [テスト](#test)
- [レビュー](#reviews)
- [修正](#fix)
- [再テストと再レビュー](#re-test-and-re-review)
- [記録と報告](#recording-and-reporting)
- [問題が起きたとき](#when-something-goes-wrong)
- [ワークフローの例](#example-workflows)
- [Skill のチェックアウトからのインストール](#installing-from-a-skill-checkout)

<!-- contents: end -->

各ステージの詳細です。スキル本体には短い版があります。要約だけでは足りず、
あるステージをより慎重に進める必要があるときにこちらを読んでください。

<a id="stage-selection"></a>

## ステージの選択

判断するのはオーケストレーターです。ほとんどの場合、次の 2 つの問いで決まります。

1. **その変更は機械的か？** 正しい編集内容がリクエストから明らかで、影響範囲が
   局所的であれば、design を省略します。
2. **リクエストで触れられていないものを壊す可能性があるか？** もしあるなら（スキーマ、
   API、共有モジュール、並行処理、認証など）、差分がどれほど小さくても design を実行します。

ステージを省略するのは、明言する価値のある判断です。「design を省略: doc コメント内の
1 行のタイポ」は役に立つ一文ですが、黙って省略するのはそうではありません。

<a id="artifacts"></a>

## 成果物

```
.ai/
├── .gitignore                      # `*`, written once, covers every workflow
├── current.json                    # the workflow this directory last resolved
└── workflows/
    └── <workflow-id>/
        ├── plan.md                 # Architect output
        ├── execution/
        │   ├── design-request.md   # prompt you wrote for the Architect
        │   ├── design-fix-brief.md # generated from accepted design findings
        │   ├── design-revise-request.md
        │   ├── design-resume-request.md
        │   ├── implement-request.md
        │   ├── fix-brief.md        # generated from accepted findings
        │   └── review-run.log      # only if you redirect `review run --progress` here
        ├── jobs/
        │   ├── <id>.json           # a detached run (`run --detach`)
        │   ├── <id>.out            # its output
        │   └── <id>.activity       # its tool uses, one line each; .activity.1 after 500
        ├── reviews/
        │   ├── review-target.diff  # frozen snapshot
        │   ├── review-target.json  # strategy, files, sha256, round_id
        │   ├── review-surrounding.json  # enclosing symbols, only with review.context.surrounding: enclosing
        │   ├── <reviewer-id>.md    # one per reviewer
        │   ├── consolidated.md
        │   ├── consolidated.json
        │   ├── rounds/             # consolidated.json of every round, <sha12>-<round_id>.json
        │   └── design/             # the design review, counted separately
        │       ├── review-target.md    # the frozen plan
        │       ├── review-target.json  # plan, request, sha256, round_id
        │       ├── <reviewer-id>.md
        │       ├── consolidated.md
        │       ├── consolidated.json
        │       └── rounds/
        └── state.json              # stage events, resolved model ids, plan approval
```

**ワークフローごとに 1 つのディレクトリ。** 以前は、同じチェックアウトで作業する
2 つのセッションが `plan.md`、レビューレポート、予算、ラウンドカウンターを共有しており、
しかもどちらも自分の存在を知らせませんでした。そのため、最初のセッションの plan は
上書きされ、その予算はもう一方に消費されていました。

```
$ dev-orchestra workflow show
Workflow: 5942d94f5248 (from: session)
Artifacts: /code/app/.ai/workflows/5942d94f5248

$ dev-orchestra workflow list
5942d94f5248  2026-09-15T09:12:04Z  runs=6  review/ok  [current]
9c1e07b3a880  2026-09-15T08:40:11Z  runs=2  implementer/ok
```

id はコマンドごとに、次の順で解決されます。

| 取得元 | |
| --- | --- |
| `--workflow <id>` | 明示的な名前（例 `--workflow auth-fix`） |
| `DEV_ORCHESTRA_WORKFLOW` | 同じものを環境変数から |
| ホストのセッション id | 12 文字にハッシュ化されるため、他ツールの内部識別子がこちらのパスに入り込みません。決定的なので、同じセッションのすべてのコマンドが、調整用のファイルなしで同じ値になります |
| `current.json` | このディレクトリが最後に解決した値。セッション id を持たないホスト向け |
| 新しい id | 初回 |

コマンドは成果物をコンテナ相対パスで指定するため、`--output
.ai/plan.md` は *このワークフローの plan* を意味し、そのディレクトリに保存されます。
`.ai/` の外のパスや、すでにワークフローを指定しているパスは、書かれたとおりに使われます。

こう読み替えるのはコマンドの引数だけです。自分で書くファイル（`--prompt-file` で渡す依頼）や
シェルのリダイレクトは、パスのとおりの場所に書かれます。そのため、`workflow show` が `Artifacts:`
として表示するディレクトリ（`--json` では `dir`）の下に書いてください。依頼は
`<Artifacts>/execution/design-request.md` に書き、`--prompt-file
.ai/execution/design-request.md` を渡せば、そのファイルに解決されます。バックグラウンドの
レビューのログは `review run --progress > <Artifacts>/execution/review-run.log 2>&1` です。
`.ai/execution/` そのものに書いた依頼は読まれません。`run` はファイルが無いと言い、解決した先の
パスを示し、書かれたとおりの場所にファイルがあることも伝えます。

**分離されるのは記録であって、作業ではありません。** implementer は作業ツリーを編集し、
レビュアーはその同じツリーの `git diff` を読みます。そしてそれはチェックアウトごとに
1 つしかありません。ここで同時に実行されている 2 つのワークフローは、依然として
互いの途中までの編集を目にします。本当に並行して進める作業では、ワークフローごとに
専用の worktree を用意してください。

```
git worktree add ../feature-x feature-x
```

これは別のリポジトリルートになるので、`.ai/` も別になります。
`workflow list` は、ここで稼働中と思われる他のワークフローを挙げ、各コマンドも、
ディレクトリが分かれていることで誤解を招かないよう、その旨を伝えます。

0.4.0 より前に書かれたフラットな `.ai/`（`.ai/` の直下にある `plan.md`、`state.json`、
`execution/`、`reviews/`、`jobs/`）は、もう取り込みません。`execution/` だけがある場合は
古い形とはみなしません。そこにあったのはオーケストレーターが書いたプロンプトとログだけで、今でも
`.ai/execution/` に依頼を書くとできるものだからです。ほかの項目と一緒にあれば、それらと並べて
挙げます。ワークフローを使うコマンドはすべて
実行を断り（exit 2）、見つかった項目を挙げて、どうすればよいかを伝えます。別の場所へ移すか
削除するか、先にそのチェックアウトで dev-orchestra 0.20.0 を一度実行してワークフローに
取り込ませてください。ただし、そのワークフローに同じ名前のファイルがすでにあれば 0.20.0 はそれを
元の場所に残すので、その残りは別の場所へ移すか削除するまで、また断られます。
`workflow list` はそれらがあることを知らせます。何も移動・削除しません。

**形式の変え方。** `.ai/` の成果物は公開された仕様の一部で、変更は追加だけで行います。新しい
バージョンはキーやファイルを足し、読む側は知らないキーを読み飛ばし、古いバージョンが書かなかった
キーは、その古いファイルが意味していたとおりに読みます。0 と読むと事実と違う主張になる場合、
たとえばトークン台帳の `priced_runs` では、0 ではなく「分からない」として扱います。キーを消したり、名前を変えたり、意味を変えたりはしません。追加で
済まない変更は互換性を壊す変更（メジャーリリース）として扱い、抜け道を付けて出します。古い形式を
新しい形式に移す処理か、0.4.0 の取り込みの廃止（CHANGELOG の Removed）のように、古い形式を
名指ししてどうすればよいかを伝える拒否です。そのため
ファイルには形式のバージョンを書いていません。読むものがなく、古いワークフローを読めるように
しているのは上の決まりだからです。コマンドの `--json` 出力も同じように変わります。キーは
追加されることがあります（たとえば `*_real` キーは、パスが別の場所に保存されているときにだけ
現れます）。キーを消したり、名前を変えたり、意味や型を変えたりはしません。互換性の約束の
全体は README の「互換性」にあります。

`.ai/` には初回使用時に `*` を含む `.gitignore` が作られるので、成果物はユーザーの
commit に入りません。成果物をレビュー可能にしたいチームはこのファイルを削除して
ディレクトリを commit できますし、決して含めたくないチームはリポジトリ自身の
`.gitignore` に `.ai/` を追加できます。変更した場合は、どちらにしたかを伝えてください。

`jobs/<id>.activity`（実行のツール使用が 500 回を超えると `.activity.1` も）は、detach した実行の
作業中に `jobs wait` が報告する内容です。`execution/review-run.log` は、バックグラウンドの
`review run --progress` の出力をそこへリダイレクトしたときにだけできます。どちらのツール行も同じ
許可リストと整形を通っており（モデルのテキストも自由記述の引数もありません）、ログの残りは
`review run` のいつもの出力です。どちらも自分自身を無視する `.ai/` の下にあります。`.ai/.gitignore` を
削除したチームは、自分の `.gitignore` でこれらを無視する必要があります。

`.ai/` 内のものも `.dev-orchestra.yaml` も、レビューのスナップショットに入ることは
ありません。スキル自身のファイルはレビュー対象の変更ではないからです。

<a id="design"></a>

## Design

design リクエストは自分で書いてください。Architect はこの会話のコンテキストを
一切持たずに開始します。読み取り専用で動き、Claude では `Read`・`Grep`・`Glob` だけを
使います。シェルも git もないので、重要な履歴はリクエストに書いてください。

```markdown
# Design request

## Goal
<what the user asked for, in your words>

## What I already know
- Entry point: app/controllers/orders_controller.rb:42
- Related: app/services/pricing.rb, spec/services/pricing_spec.rb
- The project uses <framework/conventions you observed>
- History that matters: <git log --oneline -- path, or blame of the lines in question -- the architect has no shell>

## Constraints
- Must stay backward compatible with the v1 API
- No new dependencies

## Out of scope
- <anything you have already ruled out, and why>

## Deliverable
Print the complete plan to stdout as Markdown, with these sections: Goal,
Current Behavior, Investigation, Root Cause, Proposed Change, Files to Modify,
Data/API Impact, Compatibility, Test Strategy, Risks, Implementation Steps.
The caller captures stdout. Do not write it to a file: this role runs in plan
mode. Do not modify any file.
```

plan は stdout に出力するよう求め、それ以外の場所には求めないでください。`--output` は
ロールが出力した内容を保存します。そしてこのロールは plan モードで動作し、自身の plans
ディレクトリの外には書き込めません。ファイルに書くよう指示されると、あなたが決して
目にしないファイルに書き込み、そうしたと報告します。そしてその *報告* こそが
`.ai/plan.md` に保存され、それがもっともらしいために、design レビューがそのまま
それをレビューしてしまいます。

```bash
dev-orchestra run architect \
  --prompt-file .ai/execution/design-request.md \
  --output .ai/plan.md
```

plan は先に渡す前に読んでください。曖昧である、コードベースと矛盾している、あるいは
リスクの高い部分を飛ばしている場合は、一度だけ差し戻します。2 回目もまだ不十分なら、
黙って即興で補うのではなく、レポートでその旨を伝えてください。

<a id="design-review"></a>

## Design レビュー

`review.design.enabled` が `true` のとき、または `auto`（デフォルト）で計画書にリスクがあるか
規模が大きいか、すでにラウンドが走っているときに実行します。どちらなのかとその理由は `status` が
表示します。design パネル（いずれかのファイルが `review.design.reviewers` を設定していればそれ、
なければ `when` を無視したコードパネル）が、コードが書かれる前に `.ai/plan.md` をコードベースに
照らして評価します。計画書に見るものがない専門ロールは、理由を示す note とともにそのラウンドを
休みます（コードのラウンドと同じく、それを報告してください）。
設計の誤りは、ここで見つけなければ実装とレビューを 1 回ずつ費やして見つけることになるので、
ここが最も安く見つけられる場所です。ただし 1 ラウンドにつきパネルの人数分のレビュアー実行が
かかるため、`auto` は計画書が必要とする場合にだけそのコストを使います。

レポート、ラウンドカウンター、トリアージは `reviews/design/` に独立して置かれるので、
design のラウンドがコードレビューのラウンド数を進めたり、その上限で拒否されたりする
ことはありません。design ステージを省略すると、design レビューも一緒に省略されます。

```bash
dev-orchestra review run --design
dev-orchestra review show --design
dev-orchestra review triage --design F1 --status accepted --note "confirmed"
dev-orchestra review fix-brief --design --output .ai/execution/design-fix-brief.md
dev-orchestra review status --design
```

トリアージはコードレビューとまったく同じように行います。plan が何を主張しているかを読み、
コードと照合し、判断します。その後、修正リクエストを自分で 2 つのファイルとして書いてください。
architect は、できるときは plan を設計したセッションを継続し、できないときはコンテキストなしで
改めて開始します。どちらになるかは `run` が知っていて対応する方を送るので、両方を書きます。
どちらも architect に、最小の変更で答えることと、追加したものを `## Added in this revision` に
並べることを求めます。記録されたラウンドでは、再レビューで新たに出た high の指摘の多くが修正版
自身の追加したものから出ていたので、再レビューはその一覧に向けられます。

テンプレートの `<Artifacts>` は `workflow show` が表示するディレクトリです。architect は
ファイルを置かれた場所のまま読み、`.ai/plan.md` が読み替えられるのはコマンドの引数のときだけです。

`design-revise-request.md` は新規に走る場合のものです（brief だけではプロンプトになりません）。

```markdown
# Revise the plan

<the original design request, unchanged>

Read <Artifacts>/plan.md and revise it. Keep every section it already has.

<paste <Artifacts>/execution/design-fix-brief.md here>

For each finding: say whether you addressed it and how, or why it is not a
problem. Do not widen the scope beyond the original request.

Answer each with the smallest change that does it. Add a new mechanism -- a
record, a flag, a rule, a state, a code path -- only when nothing smaller will
do. End the plan with a section `## Added in this revision` listing each one
you added: what it is, which finding it answers, and why a smaller change was
not enough. Write `None.` when you added none. Replace that section on every
revision; do not keep an earlier revision's list.

Print the complete revised plan to stdout as Markdown. The caller captures
stdout. Do not write it to a file: this role runs in plan mode.
```

`design-resume-request.md` は継続したセッション向けです。セッションがすでに持っている plan は
再掲しませんが、一度は読ませます。architect が出力したものをあなたが削っているかもしれず、指摘は
そのファイルを指しているからです。

```markdown
# Revise the plan

You are continuing the session in which you designed this plan. Read
<Artifacts>/plan.md once before changing anything: it is the plan you printed, as
saved by the orchestrator, and the findings below refer to its sections.
Do not re-read code you already read unless a finding contradicts what you
remember.

<paste <Artifacts>/execution/design-fix-brief.md here>

For each finding: say whether you addressed it and how, or why it is not a
problem. Do not widen the scope beyond the original request.

Answer each with the smallest change that does it. Add a new mechanism -- a
record, a flag, a rule, a state, a code path -- only when nothing smaller will
do. End the plan with a section `## Added in this revision` listing each one
you added: what it is, which finding it answers, and why a smaller change was
not enough. Write `None.` when you added none. Replace that section on every
revision; do not keep an earlier revision's list.

Print the complete revised plan to stdout as Markdown. The caller captures
stdout. Do not write it to a file: this role runs in plan mode.
```

`review status --design` が `final_revision: pending` と言うとき（どのレビューも見ない改訂）は、両方にこれを足します。その指摘は
たいてい前の改訂で足したものの穴で、穴を 1 つずつふさぐと次の穴が出ます。収まった最後の改訂は、
仕組みを減らすことで収めていました。

```markdown
This is the last revision; no review will see it. Where a finding is a hole
in something an earlier revision added, remove or simplify that mechanism
rather than patching it, and where a rule is left uncertain, make it fail
toward the safe side. Under `## Added in this revision`, also list each
removal as `Removed:` with the finding it answers; write `None.` only when
you neither added nor removed anything.
```

```bash
dev-orchestra run architect --resume \
  --prompt-file .ai/execution/design-revise-request.md \
  --resume-prompt-file .ai/execution/design-resume-request.md \
  --output .ai/plan.md
```

`--resume` は、直前の architect の実行が書いた plan を改訂するときにだけ付けてください。同じ
ワークフローで新しい design リクエストを出すときは付けません。継続したか新規に走ったか、そしてその
理由は、stderr の note と run log に残ります（`references/cli.md`）。note が
`running fresh: the provider cannot resume a session (unverified)` なら、その版の CLI は継続した
セッションを読み取り専用のまま保つと確認されていません。ユーザーに伝えてください。確認するには
ユーザーが `python scripts/smoke_live.py --provider claude` を実行できます。自分では実行しないで
ください。実際の CLI で実際のトークンを使うからです。`resuming the last architect session` の後の
2 行目の note が、その版は別の版より新しいとして `is trusted to resume as newer than` と言っていれば、
信頼に基づいて継続しています。同じコマンドを添えて、ユーザーに一度伝えてください。

修正の実行が stall した場合、タイムアウトした場合、または失敗した場合、`.ai/plan.md` は
元のまま残ります。この実行は自身の入力を書き換えるよう求められているので、失敗した実行が
その入力を消費してはならないからです。実行が出力した内容は `.ai/plan.md.rejected` に
保存されます。次の試行を使う前にそれを読んでください。それは今回の試行のもので、以前の
試行のものは削除されています。コマンドは書き込みを拒否した場合には必ず非ゼロで終了するので、
連鎖させた `review run --design` が古い plan を新しい plan としてレビューすることはありません。

これは `budgets.architect` から試行を 1 回消費します。design レビューに独自の予算キーが
ないのはこのためです。`review run --design` による再レビューは、`review status --design`
がそう示したときだけ行ってください。書き換えられた plan はハッシュが変わるので、次の
ラウンドは導出されるものであり、渡すものではありません。

`review.design.max_iterations` に達したラウンドでも、修正は行われます。
この上限が数えるのはレビューであり、拒否するのはその修正の再レビューであって、
修正そのものではありません。修正が行われるまで、`status` は `continue` を示し、
`review status --design` は accepted の指摘をもう一度取り込むよう示します
（`final_revision: pending`）。plan が凍結された `review-target.md` と異なるものになるか、
そのラウンドの後に architect が plan を `--output` として応答すると、`status` は
`stop-and-report` を示します（`final_revision: done`）。レビューされていない修正版を、
未解決の指摘とともに提示して尋ねてください。既知の問題に答えが出ていない plan を実装する
ことこそ、このステージが防ぐために存在する誤りだからです。修正に使える `budgets.architect`
の試行が残っていない場合は、ただちに停止となり（`blocked`）、問うべきは、指摘を残したまま
承認するか、試行を空けるかです。すでに承認済みの plan（`approved`。その後
`design.require_approval` が無効にされていても同様）や、すでに実装済みの plan
（`implemented`）には変更を求めません。取り込まれるのは accepted の指摘だけです。
`accepted` のものが 1 つもない場合（未トリアージ、`needs-triage`、
`needs-investigation`）、予算を使い切った時点でただちに停止します（`unaccepted`）。前の
ラウンドとまったく同じ指摘を見つけたラウンドでも、修正は省略されません。繰り返しである
ことはその横に示され、修正が行われた時点で停止の理由になります。

残るのは直前の plan だけで、`reviews/design/review-target.md` に凍結されます。
書き換えるとそれより古いものはすべて上書きされます。

<a id="approval"></a>

## 承認

`design.require_approval` が有効な場合（デフォルト）、ユーザーが plan を承認するまで
何も実装されません。ユーザーに提示するのは次のものです。

- plan の **Goal**、**Proposed Change**、**Files to Modify**、**Risks**
- 未解決の design 指摘と、それがこの plan のレビューから出たものか、以前の修正版の
  レビューから出たものか（`design approve` がどちらかを示します）

そのうえで尋ねてください。記録するのはユーザーの明示的な yes だけで、`dev-orchestra design
approve` で記録します。自分の判断で記録したり、拒否を回避するために記録したりしては
いけません。変更を求められた場合は、plan を修正し（design レビューが有効なら再レビューも
行い）、改めて尋ねてください。修正版はハッシュが変わるので、以前の承認はもう有効では
ありません。

ユーザーが求めた修正には fix brief がないので、2 つのファイルには代わりにユーザーの言葉を入れます。
`design-change-request.md` は新規に走る場合のものです。

```markdown
# Revise the plan

<the original design request, unchanged>

Read <Artifacts>/plan.md and revise it. Keep every section it already has.

The owner reviewed the plan and asked for these changes, in their words:

<the changes the owner asked for, unchanged>

For each change: say how you made it, or why it conflicts with the original
request. Do not widen the scope beyond the original request.

Answer each with the smallest change that does it. Add a new mechanism -- a
record, a flag, a rule, a state, a code path -- only when nothing smaller will
do. End the plan with a section `## Added in this revision` listing each one
you added: what it is, which change it answers, and why a smaller change was
not enough. Write `None.` when you added none. Replace that section on every
revision; do not keep an earlier revision's list.

Print the complete revised plan to stdout as Markdown. The caller captures
stdout. Do not write it to a file: this role runs in plan mode.
```

`design-change-resume-request.md` は継続したセッション向けです。

```markdown
# Revise the plan

You are continuing the session in which you designed this plan. Read
<Artifacts>/plan.md once before changing anything: it is the plan you printed, as
saved by the orchestrator. Do not re-read code you already read unless a
change below contradicts what you remember.

The owner reviewed the plan and asked for these changes, in their words:

<the changes the owner asked for, unchanged>

For each change: say how you made it, or why it conflicts with the original
request. Do not widen the scope beyond the original request.

Answer each with the smallest change that does it. Add a new mechanism -- a
record, a flag, a rule, a state, a code path -- only when nothing smaller will
do. End the plan with a section `## Added in this revision` listing each one
you added: what it is, which change it answers, and why a smaller change was
not enough. Write `None.` when you added none. Replace that section on every
revision; do not keep an earlier revision's list.

Print the complete revised plan to stdout as Markdown. The caller captures
stdout. Do not write it to a file: this role runs in plan mode.
```

```bash
dev-orchestra run architect --resume \
  --prompt-file .ai/execution/design-change-request.md \
  --resume-prompt-file .ai/execution/design-change-resume-request.md \
  --output .ai/plan.md
```

未解決の指摘を残したまま design レビューの予算を使い切ると、最後のラウンドの修正が
行われた時点で `stop-and-report` となり、そのレポートはこの問いで締めくくられます。
修正された plan を提示し、以前の修正版のレビューから出た未解決の指摘を挙げ、尋ねて
ください。architect が plan を変更しなかった場合は、指摘を残したまま承認するか、指摘を
トリアージし直すかを尋ねます。修正に使える architect の試行が残っていなかった場合は、
指摘を残したまま承認するか、試行を空けて（`budget reset`）修正するかを尋ねます。
ユーザーが承認すれば停止には答えが出たことになり、`status` は承認された plan について
その理由を取り下げます。

plan が承認されていない間、`run implementer` は拒否します（exit 5）。これは予算を消費する
前、かつ `--detach` がワーカーを起動する前に行われます。ワーカーも改めて確認し、拒否の
内容全体をそのジョブに書き込みます。`--force` は適用されません。plan がない（design
ステージがない）場合、ゲートはありません。

承認の対象は plan のテキストそのもの（sha256）と、承認が与えられた時点の design レビューの
ラウンドです。plan が変更された場合（`plan-changed`）や、その後に `review run --design` が
再度実行された場合（同じ plan に対してであっても）（`reviewed-since`）、承認は stale に
なります。再集約やトリアージではそうなりません。

`status --json` はこれを `design_approval` の下に報告します。

| フィールド | 意味 |
| --- | --- |
| `required` | `design.require_approval` |
| `state` | `pending`、`stale`、`approved`、`no-plan`、`not-required`、または `implemented-unapproved` |
| `pending` | ユーザーに尋ねる必要があるとき（`pending` または `stale`）に `true` |
| `stale_reason` | `plan-changed`、`reviewed-since`、または `null` |
| `plan_sha256`, `approved_sha256`, `approved_at` | 現在の plan と、承認された plan |
| `matches_current_plan` | 承認された sha が現在の plan のものかどうか。両方がそろっていなければ `null` |
| `reviewed_since_approval` | 承認後に design レビューのラウンドが実行されたかどうか。承認がなければ `null` |
| `open_findings`, `open_findings_of_current_plan` | ブロッキングな design 指摘と、そのラウンドがこの plan テキストをレビューしたかどうか |
| `design_review_exhausted` | 指摘が未解決のまま design レビューの予算を使い切った |

その横で、`design_review.final_revision` は最後のラウンドの修正の状況を示し
（`pending`、`done`、`blocked`、`approved`、`implemented`、`unaccepted`、または上限到達前は
`null`）、`final_revision_pending` は修正がまだ残っている間 `true` になり、
`identical_rounds` は同じ指摘を見つけたラウンドが何回連続したかを示します。

`implemented-unapproved` は、plan ファイルが最後に書き込まれた後に implementer が最後に
`ok` で終了し、承認の記録がないワークフローです。つまり、ゲートが導入された時点ですでに
実装済みだったワークフローです。`status` は尋ねるべきことはないと示すので、以降の
ステージのたびに問題として挙がることはありません。これは免除ではなく、このワークフローで
implementer を再度実行すると `pending` と同様に拒否されます。失敗した implementer の実行は
決して数えられず、その後に書き込まれた plan については尋ねることになります。

<a id="implementation"></a>

## 実装

```markdown
# Implementation request

Follow the plan in .ai/plan.md.

Rules:
- Match the conventions already in this codebase; do not introduce new ones.
- Make the minimal change that satisfies the plan.
- No unrelated refactoring, renaming, or formatting.
- Add or update the tests the plan's Test Strategy calls for.
- Run those tests and report the result.
- If the plan is wrong or impossible, STOP and explain why. Do not redesign.

Report: files changed, tests added/updated, test output, anything you could not do.
```

```bash
dev-orchestra run implementer --prompt-file .ai/execution/implement-request.md
```

implementer の失敗は致命的です。停止し、何が起きたかを報告し、ユーザーが調べられる状態で
ツリーを残してください。

<a id="test"></a>

## テスト

リポジトリから見つけたプロジェクト自身のコマンドを使います（`package.json`、
`Makefile`、`Rakefile`、`pyproject.toml`、CI 設定、CONTRIBUTING）。変更をカバーする
最も狭いコマンドを優先し、時間が許せば範囲を広げます。テストコマンドを決して
でっち上げないでください。見つからなければ、その旨を伝えてください。

テストスイートが失敗したらパイプラインは停止します。修正するか報告してください。
壊れたツリーをレビューしてはいけません。

<a id="reviews"></a>

## レビュー

```bash
dev-orchestra review snapshot   # freeze it
dev-orchestra review run        # fan out
```

`review snapshot` はデフォルトで作業ツリーと `HEAD` の差分を取り、未追跡ファイルも
取り込むので、新規作成したモジュールもレビューされます。ブランチの分岐点以降のすべてを
レビューするには `--base <rev>` を使います。

```bash
dev-orchestra review snapshot --base main
```

出力スキーマ、重複排除、トリアージについては `references/reviews.md` を参照してください。

<a id="fix"></a>

## 修正

```bash
dev-orchestra review fix-brief --output .ai/execution/fix-brief.md
dev-orchestra run review_fixer --prompt-file .ai/execution/fix-brief.md
```

生成された brief の先頭に、自分の指示を追加してください。

```markdown
For each finding below:
1. Read the cited code as it is now.
2. Verify the finding is still true. If it is not, say so and change nothing.
3. Fix valid findings with the smallest correct change.
4. Where a test can show the defect, add one that fails on the code as it
   was and passes after the fix; where none can (wording, documentation,
   a design choice), say why.
5. Run the relevant tests plus lint/type checks, and report the output.

Do not fix anything that is not listed here.
```

<a id="re-test-and-re-review"></a>

## 再テストと再レビュー

fixer が足したテストは、修正がないと失敗するときだけ証拠になります。どちらでも通るテストは、
すべて通った結果の中では区別がつきません。そこで `run review_fixer` の前に、その時点の作業ツリーを
記録しておきます。レビュー中の変更はたいていコミットしておらず、`git add` していないファイルも
あるので、`HEAD` も `git stash` も「修正の前」にはなりません。使い捨てのインデックスに、スナップ
ショットと同じく `git add -A` で全部を取り込めば、本来のインデックスにもファイルにも触れずに済み
ます。コマンドは別々に実行されるので、値はシェル変数ではなくファイルに残します。

```bash
GIT_INDEX_FILE=.ai/pre-fix.index git add -A
GIT_INDEX_FILE=.ai/pre-fix.index git write-tree > .ai/pre-fix.tree
```

修正のあと、再テストを記録する前に、fixer が変えた分だけを取り除いて新しいテストを流し、修正を戻します。

```bash
GIT_INDEX_FILE=.ai/post-fix.index git add -A
GIT_INDEX_FILE=.ai/post-fix.index git diff --cached "$(cat .ai/pre-fix.tree)" -- <files the fix changed, not the test> > .ai/fix.patch
git apply -R .ai/fix.patch
<the project's test command> <the new test>    # must fail, on its assertion
git apply .ai/fix.patch
rm .ai/pre-fix.index .ai/post-fix.index .ai/pre-fix.tree .ai/fix.patch
```

`fix.patch` が空なら、指定したファイルが修正で変わったものではありません。テストは、指摘が述べる
理由で、アサーションで失敗しなければなりません。そこで通ってしまうテストは何も再現していません。
修正で足したものを呼んでいて読み込みや収集の段階で失敗するテストも、何も示していません。どちらも
その結果を添えて fixer に戻します。指摘の重大度に関係なく行います。fixer は自分ではコマンドを実行
できないことが多いので、この確認はオーケストレーターが行います。

```bash
dev-orchestra review status
```

```json
{
  "iteration": 1,
  "max_review_iterations": 2,
  "blocking": ["F1"],
  "re_review_recommended": true,
  "iteration_budget_exhausted": false,
  "final_fix": null,
  "final_fix_pending": false
}
```

ラウンドは自動的に進みます。`review run` はラウンドをスナップショットから導出するので、
新しいスナップショットは新しいラウンドになり、同じスナップショットを再実行した場合
（たとえばレビュアーが失敗した後など）は現在のラウンドのままです。`--iteration` は、
意図的にそれを上書きする場合にだけ渡してください。

再レビューは `re_review_recommended` が true の場合にだけ、しかも新たに
`review snapshot` を取った後にだけ行ってください。予算を使い切ったら、もう一度修正し、
再テストしてそれを記録し、報告します。再レビューはしません。上限に達したラウンドでも
修正（`final_fix: pending`、`status` は `continue` を示します）と再テスト
（`retest`、修正が最後の `review_fixer` の試行を使った場合でも引き続き `continue`）は
行われます。修正の後に `test` の結果が記録されると（`done`）、`status` は
`stop-and-report` を示します。残っている指摘を重大度と場所とともに報告し、ユーザーに
判断を委ねてください。前のラウンドの指摘を繰り返した最終ラウンドでも修正は行われ、
繰り返しであることはその後で停止の理由になります。修正に使える `review_fixer` の試行が
残っていない場合はただちに停止となり（`blocked`）、修正すべき `accepted` の指摘が
1 つもない場合も同様です（`unaccepted`）。
予算を超えてループし続けると、実行は高くつくだけで何も生まないものになってしまいます。

<a id="recording-and-reporting"></a>

## 記録と報告

`run` を通じて委譲されたステージは自動的に記録されます。サマリーが完全になるよう、
それ以外は自分で記録してください。

```bash
dev-orchestra state record test ok --detail command="pytest -q" passed=128
dev-orchestra state record test ok --detail phase=re-test
dev-orchestra summary
```

再テストも `test` として記録してください。レビューゲートと `status` の再テスト確認は、
ステージ `test` を読みます。`re-test` ステージは `status` には再テストとして受け付け
られますが、ゲートからは決して見えません。

最終レポートには次のものを挙げます。実行したステージ、省略したステージとその理由、
テスト結果、失敗を含むレビューの結果、トリアージの件数、変更したファイル、そして
未解決のまま残っているもの。

<a id="when-something-goes-wrong"></a>

## 問題が起きたとき

| 状況 | 対応 |
| --- | --- |
| 必要な CLI がない | 報告し、まだ動くステージを実行します。自分でインストールしてはいけません |
| モデルが解決できない | 設定を修正するかユーザーに尋ねます。推測で代用してはいけません |
| レビュアーが 1 つ失敗した | 続行し、`N successful, M failed` と報告します |
| すべてのレビュアーが失敗した | レビューステージを失敗として扱います。変更がレビュー済みだと主張してはいけません |
| ラウンドが `partial` で返ってきた | 変更本体がファイルとして渡されたということです。指摘は本物ですが、レビューはクリーンではありません。指摘をトリアージし、そのラウンドは完全にはレビューされていないと報告します。解消方法は `review status` が示します |
| implementer または fixer が失敗した | パイプラインを停止して報告します |
| スナップショットが空 | レビューするものがありません。実装が実際に何かを書き込んだか確認してください |
| `review run --design` が plan がないと言う | design ステージを省略したのであれば、design レビューも一緒に省略します。そうでなければ先に Architect を実行します |
| 修正後にテストが失敗した | 出力とともに失敗を報告します。やみくもに修正を続けてはいけません |
| 実行が `stalled` で返ってきた | kill されるまで何も出力しなかったということです。失敗として報告し、警告された場合は孤児プロセスがないか確認します |
| コマンドが exit 3 で終了した | 予算を使い切っています。未解決のものを報告します。再試行せず、`--force` にも手を出さないでください |
| `run implementer` が exit 5 で終了した | plan が承認されていないか、承認後に変更された／レビューされたということです。plan を提示してユーザーに尋ね、yes なら `design approve` で記録します。再試行せず、自分で承認してはいけません |
| デタッチされた implementer ジョブが "not approved" で `failed` になった | 原因は同じで、ワーカーが検出したものです。ユーザーが承認したら、もう一度開始します |
| `status` が `stop-and-report` を示す | 停止します。予算、stall、未解決の指摘はすでに考慮済みです |

<a id="example-workflows"></a>

## ワークフローの例

**既存の Rails アプリケーションへの機能追加**

> 「チェックアウトに顧客ごとの上限金額を追加して」

```
Codex gpt-5.6-sol      reads the existing checkout code and recent logs,
                       breaks the request into stages
        |
Claude fable           designs: where the cap lives, what it touches, migrations
        |
Claude opus            implements the change and its tests
        |
Claude opus            reviews the frozen diff
Codex gpt-5.6-terra    reviews the same diff, independently
        |
Codex gpt-5.6-sol      consolidates both reports, drops duplicates, triages
        |
Claude opus            fixes the accepted findings only
        |
                       tests re-run, report
```

ユーザー側の操作はエージェントへの 1 文だけです。成果物は `.ai/` に残ります。plan、
各レビュー、統合済みの finding 一覧、トリアージの判断です。

**API 変更を伴う機能追加**

> 「orders エンドポイントにページネーションを追加して」

設計 (Fable) → 実装 (Opus) → テスト → 独立レビュー 2 件 →
トリアージ（accepted 2 件、rejected 1 件）→ 修正 (Opus) → 再テスト → 報告。

**タイポ**

> 「README の見出しのタイポを直して」

編集 1 回だけで、design もレビューもしません。オーケストレーターはそのことを報告に書きます。

**レビューのみ**

> 「このブランチの main 以降の変更を 3 人のレビュアー全員でレビューして」

```bash
dev-orchestra review snapshot --base main
dev-orchestra review run
dev-orchestra review show
```

**低コストの試運転** — レビュアーをオフラインの mock provider に差し替えます。

```bash
dev-orchestra reviewer add --provider mock --id dry --role general
DEV_ORCHESTRA_MOCK_RESPONSE=NO_FINDINGS dev-orchestra review run --only dry
```

<a id="installing-from-a-skill-checkout"></a>

## Skill のチェックアウトからのインストール

Plugin 以前のインストーラも、変更なしでそのまま使えます。手順が短いのは Plugin の
ほうですが、こちらは `git pull` で更新できるチェックアウトを手元に残します。

```bash
git clone https://github.com/istb16/dev-orchestra.git
cd dev-orchestra
```

**Claude Code:**

```bash
./install/install.sh              # symlinks into ~/.claude/skills/
```

```powershell
.\install\install.ps1             # Windows
```

インストーラはこのリポジトリを skills ディレクトリにリンク（`--copy` ならコピー）する
ので、`git pull` だけでその場で更新されます。`--project <path>` を付けると、全体ではなく
特定のリポジトリの `.claude/skills/` に入れます。そのパスは対象リポジトリの
`.git/info/exclude` にも追加されるので、入れ子のチェックアウトが *相手の* `git status` に
出ることも commit されることもありません（これがないと、そこでの `git add -A` が
"does not have a commit checked out" で失敗します）。

再実行とアンインストーラが置き換えるのは、インストーラが作ったものだけです。つまり、この
チェックアウトを指すリンクか、`.dev-orchestra-install` というファイルの入ったコピーです。
このファイルより前のインストーラが作ったコピーは、コピーに入るもの以外を含まないことで
見分けます。別の場所を指すリンク、自分で置いたディレクトリ、クローンはそのまま残し、
手で消す方法を表示します。skills ディレクトリに直接クローンしたものは、そのままで導入済み
です。そこからインストーラを実行しても、クローンを消さずに止まります。

Windows では、Git Bash で `install.sh` を動かすより `install.ps1` を使ってください。
Git Bash は MSYS 形式のパス（`/c/...`）を書き込み、ネイティブの Python はそれを開けません。
シンボリックリンクには開発者モードか管理者権限が必要で、リンクできないときは
インストーラが自動でコピーに切り替えます。Git Bash の `ln -s` は、リンクを作らずに
`.git` まで含めてチェックアウト全体をコピーし、成功を返します。`install.sh` はリンクに
ならなかったことに気づき、そのコピーを、コピーに入るものだけのコピーに置き換えて、
そう伝えます。以前の `install.sh` がそこに残した全体のコピーはクローンと見分けがつかない
ので、そのまま残します。断るときのメッセージに、自分のものが入っていないことを確かめて
から消す方法を表示します。

**Codex CLI:** Plugin を使わない場合、インストーラは `AGENTS.md` に、このチェックアウトを
指す短いマーカー付きのブロックを追記します。

```bash
./install/install.sh --codex                    # ~/.codex/AGENTS.md
./install/install.sh --codex --project /path    # <project>/AGENTS.md
```

`skills/dev-orchestra/SKILL.md` が唯一の情報源であり続けます。ブロックはそれを参照する
だけで、内容を複製しません。

**Antigravity:** インストーラはこのチェックアウトを Antigravity の plugins フォルダに
リンクします。ディレクトリを Plugin にするのはルートの `plugin.json` で、その下の
`skills/` は Antigravity が自分で見つけます。

```bash
./install/install.sh --antigravity                    # ~/.gemini/config/plugins/dev-orchestra
./install/install.sh --antigravity --project /path    # <project>/.agents/plugins/dev-orchestra
```

```powershell
.\install\install.ps1 -Antigravity                    # Windows
```

`--gemini`（`-Gemini`）も同じ指定です。Windows ではまずジャンクション（開発者モード不要）を
作り、だめならシンボリックリンク、それもだめならコピーにします。それ以外の環境では
シンボリックリンク、だめならコピーです。どれを使ったかは表示され、コピーの場合は
`git pull` のあとにもう一度実行する必要があります。`--copy` は常にコピーで、コピーには
`.dev-orchestra-install` というファイルが入り、次の実行が自分で作ったものだと分かります。
`--project` を付けると、そのリポジトリの `.git/info/exclude` にマーカーのコメント付きで
エントリを追加し、アンインストーラはマーカーがあるときだけそれを取り除きます。
インストーラは、別の場所を指すリンク、指す先がないリンク、自分が書いていない
ディレクトリ（クローンを含む）を置き換えません。その場で止まり、手で消す方法を表示します。
また、チェックアウトのルートに `hooks.json`、`mcp_config.json`、`plugins.json`、`rules/`、
`agents/*.md` があると、Antigravity がそれも読み込むため、リンクを作りません。コピーには
`agents/*.md` を入れません。この確認は最初に行うので、断ったときは入っていたものがそのまま残ります。
plugins フォルダに直接クローンしたものは、そのままで導入済みなので、インストーラは不要です。
`./install/uninstall.sh --antigravity`（`--project` も同じ指定）で取り除きます。
インストール、アップグレード、アンインストールのあとは Antigravity を再起動してください。
Plugin のディレクトリは起動時にしか見つけられません。リンクで入れた場合はチェックアウトで
今のブランチがそのまま読み込まれるので、信頼できないブランチを見るときは `--copy` か
別の worktree を使ってください。リンクしたチェックアウトや plugins フォルダに置いたクローンに、
あとのチェックアウトで上のどれかが加わったときは、`dev-orchestra doctor` が報告します。

Plugin が Antigravity に入る道は、ほかに三つあります。Marketplace は Google が選んで
載せるもので、利用者が追加するマーケットプレイスはなく、掲載は申込フォームを通します。
Antigravity CLI は `agy plugin install /path/to/dev-orchestra`（セッション内では
`/plugin install <local-path>`）でローカルの Plugin を入れ、そのコピーを
`~/.gemini/antigravity-cli/plugins/dev-orchestra/` に置きます。`agy plugin uninstall
dev-orchestra` で取り除けます。渡したディレクトリを丸ごとそのままコピーするので、
インストーラのペイロード一覧は使われず、`agents/*.md` も残ります。実行するのは、スキル以外に
Antigravity が読み込むもの（`hooks.json`、`mcp_config.json`、`plugins.json`、`rules/`、
`agents/*.md`。`python scripts/validate_skill.py` が報告します）がなく、信頼できない
ブランチもチェックアウトしていない、きれいなチェックアウトだけにしてください。コピーは
`git pull` に追従しないので、pull のあと、きれいな状態に戻したチェックアウトでもう一度
実行します。最後に、カスタマイズのルート（`~/.gemini/config/plugins.json`、プロジェクトでは
`.agents/plugins.json`）に置いた `plugins.json` で、別の場所にあるチェックアウトを
Antigravity に読ませることもできます。エントリに書くのは Plugin のディレクトリそのものではなく、
それを含む親ディレクトリです。
`{"entries":[{"path":"C:/Projects","include_only":["dev-orchestra"]}]}` は
`C:/Projects/dev-orchestra` にあるチェックアウトを読み込み、`path` にチェックアウトそのものを
書いたエントリは何も読み込みません。Windows では `C:/` の形のパスが使えます。これは
リンクと同じく作業ツリーをそのまま読み込むので、あとでチェックアウトしたブランチが次の
再起動で有効になります。リンクで入れた場合と同じく信頼できないブランチには注意が必要で、
しかも何も守ってくれません。インストーラの拒否はインストーラがリンクするときにしか働かず、
`doctor` が確認するのはインストーラの二つの入れ先だけで、`plugins.json` のエントリや
`agy plugin install` が置いたコピーは見ません。勧める方法はインストーラのままです。
そのリンクはアプリ、IDE、CLI のどれでも使え、`git pull` に追従し、スキル以外に
読み込まれるものがあるチェックアウトでは拒否され、そのあとは `doctor` が見張ります。
ほかの道にはそのどれもありません。

必要なら CLI を PATH に通し、動作を確認します。

```bash
export PATH="$PWD/bin:$PATH"      # then `dev-orchestra doctor` works anywhere
./bin/dev-orchestra doctor
```

チェックアウトでの導入の **アップグレード**:

```bash
cd /path/to/dev-orchestra
git pull
./bin/dev-orchestra doctor
```

シンボリックリンクで入れた場合はすぐに新しいバージョンになります。`--copy` の場合は
インストーラをもう一度実行してください。

**アンインストール** は skill のリンクと `AGENTS.md` のブロックを取り除き、設定には
手を付けません。

```bash
./install/uninstall.sh
```

```powershell
.\install\uninstall.ps1
```
