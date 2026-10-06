<!-- translated-from: references/configuration.md sha256:6c064557f47ff42b00d0654b4f2a6708fe0ad0591c89a889a7786b1d7023fa1b -->

> この文書は [references/configuration.md](../../../references/configuration.md) の日本語訳です。内容が食い違うときは英語版が正です。

<a id="configuration"></a>

# 設定

<!-- contents: start -->

**目次**

- [設定の置き場所](#where-it-lives)
- [優先順位](#precedence)
- [プリセット](#presets)
  - [パネルの横にレビュアーを足す（`reviewers_extra`）](#adding-reviewers-beside-the-panel-reviewers_extra)
  - [独自の設計パネル（`review.design.reviewers`）](#a-design-panel-of-its-own-reviewdesignreviewers)
- [スキーマ（version 1）](#schema-version-1)
  - [フィールドリファレンス](#field-reference)
  - [ロールのオプション](#role-options)
- [最適化レベル](#optimization-level)
  - [高リスクな変更でだけ走るレビュアー](#reviewers-that-run-only-on-high-risk-changes)
- [モデルティア](#model-tiers)
- [モデル family とバージョンポリシー](#model-families-and-version-policy)
- [ユーザーの依頼と実行するコマンド](#what-the-user-asks-for-and-what-to-run)
- [編集](#editing)
  - [ウィザード](#the-wizard)
- [実例](#worked-examples)
- [YAML の方言](#yaml-dialect)

<!-- contents: end -->

<a id="where-it-lives"></a>

## 設定の置き場所

| レイヤー | パス | 用途 |
| --- | --- | --- |
| プロジェクト | `<repo>/.dev-orchestra.yaml` | リポジトリごとの上書き。commit するかどうかはお好みで。読み取り専用のロールはここから `options.args` を受け取りません（[下記](#role-options)） |
| グローバル | 下記参照 | すべてのプロジェクトに対するあなた個人のデフォルト |
| 組み込み | `scripts/orchestrator/config.py` | 推奨デフォルト。ファイルが存在しないときに使われます |

設定ディレクトリ内の `providers/` ディレクトリには、あなた自身の provider
adapter を置きます（Windows では `%APPDATA%\dev-orchestra\providers\`、
それ以外では `~/.config/dev-orchestra/providers/`、それが設定されている場合は
`$DEV_ORCHESTRA_HOME/providers/`。`DEV_ORCHESTRA_CONFIG`
ではこの場所は変わりません）。`references/providers.md` を参照してください。

その隣の `verified/<provider>-resume.json` には、セッションの継続（`run architect --resume`）について
`python scripts/smoke_live.py` がこのマシンで確認した CLI の版が記録されます。書くのはこのスクリプト
だけです。読まれるのは実パスがワークスペースの外にあるときだけで、`DEV_ORCHESTRA_HOME` をチェック
アウトの中に向けると記録は読まれず書かれもせず、`doctor` と `--resume` の note がその旨を示します。
ここか adapter の表で合格した版より新しく、メジャー版が同じ版は、ここに記録された不合格が間にない限り信頼に基づいて
継続します。そのため、継続したセッションが制限を失った新しい CLI も、その版でスクリプトを実行するまでは
継続します。`design.resume.max_age_seconds: 0` で継続を止められます。

プラットフォーム別のグローバル設定のパス:

| プラットフォーム | パス |
| --- | --- |
| Linux / BSD | `$XDG_CONFIG_HOME/dev-orchestra/config.yaml`、なければ `~/.config/dev-orchestra/config.yaml` |
| macOS | `~/.config/dev-orchestra/config.yaml` |
| Windows | `%APPDATA%\dev-orchestra\config.yaml` |

**Microsoft Store 版の Python。** Microsoft Store 版の Python では、この Python が
AppData の下に書くファイルを、Windows がパッケージ専用のフォルダー
（`%LOCALAPPDATA%\Packages\PythonSoftwareFoundation.Python.<version>_…\LocalCache\Roaming\dev-orchestra\`）
に置きます。dev-orchestra が読み書きするファイルは変わりませんが、エクスプローラーやエディター、
ほかの Python からはそのファイルが見えません。表示したパスとは別の場所に保存されているパスには、
後ろに `(stored at <実際のパス>)` が付きます。対象は `config path`、`config show --scope global`、
グローバルファイルに書き込むコマンドのメッセージ、そして `doctor` です。`doctor` は Store 版の
Python を使っていることも示し、直し方を note に書きます（`--json` には、そのときだけ、該当する
パスの隣に `*_real` キーが加わります）。パッケージのフォルダーにある adapter と記録は、実際の
`%APPDATA%` にある同じ名前のファイルより優先されます。直すには、python.org 版の Python（`py`）を
使うか、`DEV_ORCHESTRA_HOME` を `%USERPROFILE%\AppData` の外で、どのプロジェクトのチェックアウトの
中でもない、自分で管理する信頼できるフォルダーに設定してください。そのうえで、パッケージの
フォルダーから `config.yaml` だけをコピーします。`providers\*.py` は起動時に import されるコードなので、
移す前に中身を確認してください。`verified\` の記録はコピーせず、
`python scripts/smoke_live.py` に作り直させてください。

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
`.json` のファイルに書き込むコマンド（`config set`、`reviewer add` などの書き込み）は、
JSON のまま書き戻し、YAML のファイルに付ける先頭のコメントは付けません。

<a id="precedence"></a>

## 優先順位

```
project config  →  global config  →  the global file's preset, fitted to the installed CLIs  →  built-in defaults
```

マッピングはキーごとにマージされるので、`implementer` だけを設定したプロジェクト
ファイルでも、グローバルの architect はそのまま維持されます。**リストは丸ごと置き換わります**:
`reviewers` を定義したプロジェクトファイルは、そのプロジェクトのパネル全体を定義します。
これは意図的です — 「このリポジトリは security + database だけでレビューする」を
表現できなければならないからです。

プリセットのレイヤーが届くのは、どのファイルも設定していないロールとレビュアーパネルだけです
（[プリセット](#presets)）。Claude Code と Codex が両方インストールされているとき、またはフィットの
対象の CLI がどれも無いとき、デフォルトのプリセットは組み込みデフォルトとまったく同じで、それに
設計パネルが加わります。

<a id="presets"></a>

## プリセット

プリセットは、費用の軸での1つの選択です: `quality`・`standard`・`fast`。グローバルファイルに
キー1つとして保存され、設定を読み込むたびに、このマシンの PATH にある CLI に合わせて展開されます。
そのため、Codex の無いマシンが、毎回失敗する Codex のレビュアーを抱えることはありません。

```bash
dev-orchestra config setup --preset quality   # save it; nothing is asked
dev-orchestra config set preset fast          # switch, keeping your other settings (always the global file)
```

```yaml
version: 1
preset: quality
```

プリセットを指定していないグローバルファイルも、ファイルが1つも無い場合も、`standard` で動きます。
「なし」はありません: 新しいマシンで最初に `config set` を実行しても、フィットが止まることはありません。

それぞれが設定する値（family のみ、すべて `version: latest`。Codex はどの枠でも
`recommended-coding` です。CLI を起動せずに adapter が保証できる family はこれだけだからです）:

| キー | `quality` | `standard` | `fast` |
| --- | --- | --- | --- |
| `orchestrator` | opus | sonnet | sonnet |
| `architect` | fable | fable | opus |
| `implementer` | fable | opus | sonnet |
| `review_fixer` | fable | opus | sonnet |
| レビュアーの枠（role / Claude の family） | general / fable [Claude]、general [Codex]、security / opus [Claude]、security [Codex]、architecture / opus [Claude]、test / opus [Claude] | general / opus、general / sonnet、security / sonnet→opus [Claude]、test / sonnet | general / opus; security / sonnet、`when: high-risk` |
| 設計レビュアーの枠（`review.design.reviewers`） | general / sonnet→opus [Claude]、general [Codex]、security / opus [Claude]、test / sonnet [Claude]、architecture / opus [Claude] | general / sonnet→opus [Claude]、security / sonnet [Claude]、test / sonnet [Claude] | general / sonnet [Claude] |
| `review.design.enabled` | `auto` | 設定しない（`auto`） | `false` |
| `optimization.level` | `quality` | 設定しない（`balanced`） | `aggressive` |

`→opus` は `high_risk_model: {family: opus}` のことです: その枠は high-risk のラウンドでは opus で、
それ以外では自分の family で動きます。`[Claude]` と `[Codex]` は、配るのではなくそのベンダーに置く
枠を表します（下の手順 2）。

プリセットが決めるのはこの8つのキーです。それ以外 — budgets、タイムアウト、
`review.max_review_iterations`、`design.require_approval` — は、ファイルが設定しない限り
組み込みデフォルトのままです。`standard` のコードパネルは組み込みデフォルトから読み取って作るので、
両者がずれることはありません。設計パネルはデフォルトに無いので、プリセット自身が持っています。

どのプリセットも `relevance` を設定しないので、プリセットの security の枠は、設計の枠も含めて、
入っているすべてのラウンドで動きます。`quality` の opus の設計 security の枠は、`auto` が通す設計
ラウンドのたびに動きます。security に関係するトークンの無い計画で外したいときは、オプトインします:
`reviewer set claude-security --design --relevance security`（設計パネルをファイルにコピーします。
下記）。

**フィット。** 対象は `claude`、`codex`、`agy` と、`preset_family` を宣言したユーザー adapter です
（[Taking part in preset fitting](providers.md#taking-part-in-preset-fitting) を参照）。それ以外の
ユーザー adapter は、これまでどおりファイルが名前を挙げたときにだけ動きます。検出は PATH の探索だけで、
CLI は起動しません:

1. orchestrator と architect は `claude`、`codex` の順で最初にインストールされている CLI に、
   どちらも無ければ席に就けるユーザー adapter のうち名前順で最初のものに、それも無ければ agy に
   割り当てます。implementer と review fixer は `claude`、`codex`、`agy` の順で最初のものに、
   3つとも無ければオプトインしたユーザー adapter のうち名前順で最初のものに割り当てます。
   Claude にはプリセットの family、Codex には `recommended-coding`、agy には `default`、ユーザー
   adapter には宣言した family が付きます。agy は読み取り専用に保てないので、読み取り専用のロールや
   レビュアーの枠が agy に割り当てられるのは、Claude も Codex も席に就けるユーザー adapter も無い
   マシンだけです。そこではその席は、global ファイルの agy の席と同じ場所すべてで警告され、その note は
   外す方法で終わります（`; agy cannot be held to reading -- set architect in the global file to
   keep it off agy`）。
2. レビュアーの枠は、orchestrator と同じ CLI（`claude`、`codex`、無ければ席に就けるユーザー
   adapter、それも無ければ agy）に implementer の CLI（ファイルが implementer を
   設定していれば、ファイルが指定した provider）から順に配ります。そのため
   2社あれば、どのパネルにも両方が入ります。コードパネルと設計パネルは、それぞれ同じやり方で別々に
   フィットします。ここで配って数えるのはベンダーの付いていない通常の枠だけで、ベンダーの枠は置き、
   安い枠は手順 4 で扱います。配る枠のうち常に走るレビュアーが1人だけのパネル（`fast` のコード
   パネル）は、もう一方のベンダーから始めます: implementer と同じベンダーの常時レビュアー1人では、
   独立したレビューにならないからです。枠の `when` は、どこに配られても維持されます。席に就ける
   ユーザー adapter が2つあるときも同じように配るので、project ファイルが `implementer` を2つ目に
   すると、`standard` では2つ目が最初の枠を取り、`fast` の常に走る1つの枠は1つ目が取ります。

   ベンダーの枠は配らずに置くので、implementer によって動くことはありません。Claude の枠は Claude が
   インストールされていれば必ず Claude に置き、無ければベンダーの無い枠と一緒に配ります。Codex の枠は
   Codex に、無ければ席のプールのうち Claude 以外で最初の CLI（席に就けるユーザー adapter か agy）に
   置き、それも無ければ追加しません: `codex not found on PATH: reviewer seat 2 (general) was not
   added; it is a second vendor's opinion and nothing installed stands in for one`。
   `high_risk_model` を持つ枠がそれを保つのは Claude の上だけです。2つ目のモデルをオフラインで
   名前を挙げられる CLI は Claude だけだからです。ほかに配られた枠はそれを失い、note の末尾は
   `; no high-risk model on codex` になります。`standard` の security の枠はこれを持つので、
   Claude の枠です。
3. 前の枠とまったく同じになる枠（provider、family、high-risk の family、role、条件）は追加しません。
   id は provider と role から作るので、同じ role の2人目の Claude の枠は `claude-general-2` に
   なります。
4. 安い枠（`high_risk_model` の無い Claude の sonnet の枠。ここでは `test` の枠）は Claude にだけ
   配ります。オフラインで名前を挙げられる安いモデルを持つ CLI は Claude だけなので、Claude が無い
   ところには追加しません（`claude not found on PATH: reviewer seat 4 (test) was not added; codex
   has no cheap model named offline`）。そのため Codex とユーザー adapter は安い枠を受け取りません。
   `standard` の security の枠は通常の枠なので、Codex だけでも残り、そこでは毎回のラウンドで動きます。
   `standard` の general 以外のすべての枠、`quality` のコードパネルの `test`、設計パネルの general
   以外のすべての枠は、読み取り専用に保てる相手にだけ置く枠で、agy には置きません（`...; agy
   cannot be held to reading`）。そのため agy だけのときの `standard` は `agy-general` のまま、
   `quality` のコードパネルは3つの枠のまま、どの設計パネルも `agy-general` だけです。追加しなかった
   枠が、ほかの枠を動かしたり id を変えたりすることはありません。設計の枠の note は `design reviewer
   seat` と書き、agy では `-- list review.design.reviewers in the global file to keep them off agy`
   で終わります。ファイルが危険なパスのパターンを1つも残していないとき
   （`optimization.high_risk_paths: []` で `extra_high_risk_paths` もない）も、組み替えで入る
   `when: high-risk` の枠は残り、`review run --high-risk` で宣言したラウンドでだけ動きます。
   ファイルが `reviewers` や `reviewers_extra` に書いた `when: high-risk` のレビュアーは、
   これまでどおり断ります。ウィザードも、ファイルが持つ high-risk の枠は勧めません。
   `when: high-risk` の枠をフィットするのは `fast` だけです。
5. Claude も Codex もオプトインしたユーザー adapter も agy も無いときは、すべてのロールとパネルを
   書かれたとおりに展開します。CLI が無いことは `doctor` が報告します。agy だけのときは、すべての
   ロールと枠を agy に割り当て、それぞれ note を出します（`claude, codex not found on PATH:
   implementer went to agy (default)`）。

コードパネル:

| プリセット | Claude + Codex | Claude のみ | Codex のみ | agy のみ |
| --- | --- | --- | --- | --- |
| `quality` | claude-general fable、codex-general、claude-security opus、codex-security、claude-architecture opus、claude-test opus | claude-general fable、claude-security opus、claude-architecture opus、claude-test opus | codex-general、codex-security、codex-architecture、codex-test | agy-general、agy-security、agy-architecture |
| `standard` | claude-general opus、codex-general、claude-security sonnet→opus、claude-test sonnet（組み込みデフォルト） | claude-general opus、claude-general-2 sonnet、claude-security sonnet→opus、claude-test sonnet | codex-general、codex-security | agy-general |
| `fast` | codex-general; claude-security sonnet（high-risk） | claude-general opus; claude-security sonnet（high-risk） | codex-general; codex-security（high-risk） | agy-general; agy-security（high-risk） |

設計パネル:

| プリセット | Claude + Codex | Claude のみ | Codex のみ | agy のみ |
| --- | --- | --- | --- | --- |
| `quality` | claude-general sonnet→opus、codex-general、claude-security opus、claude-test sonnet、claude-architecture opus | Claude + Codex から codex-general を除いたもの | codex-general、codex-security、codex-architecture | agy-general |
| `standard` | claude-general sonnet→opus、claude-security sonnet、claude-test sonnet | 同じ | codex-general、codex-security | agy-general |
| `fast`（`enabled: false`） | claude-general sonnet | 同じ | codex-general | agy-general |

Claude と Codex があるとき、project ファイルが implementer を Codex にすると、`standard` のコード
パネルは codex-general、claude-general sonnet、claude-security sonnet→opus、claude-test sonnet と
配られます。`quality` のベンダーの枠と設計パネルはそのままです。

Claude や Codex と並んで agy があっても何も変わりません。Claude、Codex、agy がそろっていれば、
どのプリセットも Claude と Codex のときと同じで、組み込みデフォルトもそのままです。Claude + agy は
Claude のみと、Codex + agy は Codex のみと同じです。agy だけのマシンで読み取り専用の席から agy を
外すには、global ファイルで orchestrator と architect を設定するか、`reviewers` を並べます。
ファイルが設定したロールと、ファイルが並べたパネルはフィットされません（下記）。並べた `reviewers`
はフィットした設計パネルも一緒に外します。`review.design.reviewers` だけなら、外れるのは設計
ラウンドだけです。

フィットは読み込むたびにやり直されます: 後から Codex を入れれば、次のコマンドのパネルに
入ります。そのため、レビュアーの id はチームメンバーのマシンごとに違うことがあり、`--only` で
`codex-security` を名指しするスクリプトはそれを考慮する必要があります。何がなぜフィットし直されたかは、
`config show` の `Preset:` の下、`doctor` の Notes、`config setup` が表示するサマリーの下に1行ずつ
出ます: `codex not found on PATH: reviewer seat 2 (general) went to claude as
claude-general-2 (sonnet)`。

**フィットされるのは、どのファイルも設定していないものだけです。** どれかのファイルが
いずれかのフィールド — `provider`、`model`、`options`、`model_tiers` — を設定したロールは、
プリセットから何も受け取りません: プリセットが無かったころとまったく同じく、組み込みデフォルトと
ファイルから、ファイルが指定した provider で解決されます。ロールを設定するなら丸ごと設定して
ください。`implementer.options` だけを設定した `quality` のユーザーの implementer はデフォルトの
`opus` になり、`config show` はそのロールがフィットされなかったと表示します。その provider の CLI
が無いとき、`doctor` は `<role>.provider` をインストール済みの CLI にするか、ファイルからそのロールを
削除するよう示します。ロールをフィットから外し、その結果 provider が変わる最初の `config set` は、
その旨を表示します。

パネルも同じ規則です: どれかのファイルの `reviewers` リストはフィットしたパネルを丸ごと置き換え、
自分で並べたレビュアーの CLI が無ければ、並べたのは自分なので、毎回のラウンドで失敗し続けます。
`reviewers` を並べたファイルは、フィットした設計パネルとそのフィットの note も外します: その設計
ラウンドは、プリセットが設計パネルを持つ前と同じく、そのリストを `when` 抜きで動かします。どれかの
ファイルの `review.design.reviewers` リストはフィットした設計パネルを丸ごと置き換え、
`review.design.reviewers_extra` だけならそれに加わります
（[独自の設計パネル](#a-design-panel-of-its-own-reviewdesignreviewers)）。

**`reviewer add` はパネルの横に足し、ほかの書き込みコマンドはパネルを記録します。**
`reviewers` を並べていないファイルでは、`reviewer add` は新しいレビュアーをそのファイルの
`reviewers_extra` に書き、何もコピーしません。そのためパネルはフィット（project では
グローバルファイルのリスト）に従い続け、コマンドはその旨を表示します: `Added reviewer
claude-security-2 (claude / opus / security) to <path> as an extra; the panel still follows preset
standard's fit`。`reviewers` を並べたファイルでは、これまでどおりそこへ追加します
（[下記](#adding-reviewers-beside-the-panel-reviewers_extra)）。`reviewer remove` と
`reviewer set` は、有効なパネル（`reviewer list` が示す id と位置）から対象を探し、ファイル自身の
extra ならその場で編集します。それ以外の枠（フィットしたもの、並べたもの、project から見た
グローバルの extra）を指すときは、`config set reviewers[...]` と同じく、このマシンでフィットした
パネルから始め、それをファイルにコピーしてから編集します。これまで継承したリストから始めていたのと
同じです。それ以降はファイルのリストがパネルになり、このマシンでも、そのファイルを読む他のマシンでも
同じです。コマンドは一度だけその旨を表示します: `note: <path> now lists the reviewers; the panel no
longer follows preset standard's fit (recorded claude-general opus, claude-general-2 sonnet,
claude-security sonnet, claude-test sonnet)`。
ロール、設計レビュー、最適化レベルは引き続きプリセットに従います。フィットした設計パネルは、
ファイルが `reviewers` を並べたので従わなくなります。`review.design.reviewers` を並べたファイルも
ないときは、設計ラウンドもこのリストに移り、note の末尾に `; design rounds now run this list
without when, not preset standard's design panel` が付きます。同じ理由で `config prune` は、
フィットと等しい `reviewers` のリストでも、それが設計ラウンドをプリセットの設計パネルから外して
いる唯一のものであれば残し、`Kept reviewers in <file>: dropping it would move design reviews to
the preset's design panel.` と表示します。project スコープで、グローバル
ファイルがレビュアーを並べていない（つまりコピーするのがフィットしたパネルとグローバルの extra
である）ときは、agy の枠はコピーから外します。project ファイルはその枠を持てないからです。note の
末尾は `; not copied into .dev-orchestra.yaml: agy-general -- a reviewer on agy is taken only from
the global config` になります。外した枠を指す編集（`reviewer set agy-general` や、コピーした
リストの長さを超える index）は失敗し、エラーの横に同じ `note: not copied into ...` を出します。
グローバルファイルが並べたパネルは丸ごとコピーします（[下記](#role-options)）。これらの
書き込みコマンドはどれも、保存後の設定を両方のファイルを含めて組み立て、書き込みによって新たに
生じる `reviewers` の問題（たとえば常に走るレビュアーがいなくなること）があれば、何も書かずに
拒否します（exit 2）。ファイルにもともとあった問題は書き込みを止めません。グローバルファイルの
`config reset` は `preset` を残してリストと extra を消すので、フィットが戻ります。知らない
プリセット名はほかの上書きと一緒に消して `note:` を表示し、そのファイルは `standard` で動くように
なります。

`--model` なしで追加したレビュアーには、その CLI の既定の family が付きます: Claude では `opus`、
agy では `default`、Codex とそれ以外の adapter では `recommended-coding` です。`--model` も `--pin`
も付けない `reviewer set --provider <other>` も、同じように新しい CLI の既定の family を書きます。
family は古い CLI にとってのモデルの名前で、新しい CLI では解決できないからです。`note:` の行が
元の値を伝えます（`note: model family reset from 'opus' to 'default' for provider agy (--model
picks another)`）。

**プリセットを指定できるのは、今のところグローバルファイルだけです。** プロジェクトファイルの
`preset:` は `preset: only the global file can name a preset for now` で検証に失敗し、削除するまで
ワークフローのコマンドは exit 2 で終了します。そのため `config set preset <name>` は、独自の
プロジェクトファイルがあるプロジェクトの中でもグローバルファイルに書き込み、`--scope project` を
付けると何も書き込まずに拒否します（exit 2）。

**セキュリティ重視や無人実行は、プリセットではなく追加の設定です。** 危険な変更に security
レビュアーを付けるなら、`reviewer add --provider claude --role security --when high-risk` に加えて、
このリポジトリ独自の機密パスを `optimization.extra_high_risk_paths` に。これは上のとおりパネルの
横に足します。誰も見ていない実行なら `config set design.require_approval false`。`preset: quality` と
組み合わせれば「無人の quality」になります。

古い dev-orchestra が `preset:` のあるグローバルファイルを読むと、このキーを無視して組み込み
デフォルトで動きます。その `config validate` もこのキーを報告しません。

<a id="adding-reviewers-beside-the-panel-reviewers_extra"></a>

### パネルの横にレビュアーを足す（`reviewers_extra`）

ファイルは、継承するパネルをコピーする代わりに、そのパネルにレビュアーを足せます。パネルは
プリセットのフィットやグローバルファイルのリストに従い続け、足したものは残ります:

```yaml
version: 1
reviewers_extra:
  - id: claude-security
    provider: claude
    model:
      family: opus
      version: latest
    role: security
    when: high-risk
```

`reviewer add` は、ファイルが `reviewers` を並べていなければこのキーに書きます。各エントリは
`reviewers` のエントリと同じスキーマで、`null` と `[]` は何も足しません。どちらのファイルにも
置けて、extra はパネルの後ろに加わります:

| パネル | 条件 |
| --- | --- |
| project ファイルの `reviewers`、次に project ファイルの extra | project ファイルが `reviewers` を並べている |
| グローバルファイルの `reviewers`、次にグローバルの extra、次に project の extra | それ以外で、グローバルファイルが `reviewers` を並べている |
| プリセットのフィット、次にグローバルの extra、次に project の extra | それ以外 |

`reviewers: []` もリストとして数えます。

**id。** extra が加わる先のパネルは、すべての id をそのまま保ちます。id がすでに使われている
（フィット、並べたレビュアー、それより前の extra のいずれかで）extra は新しい id で走り、
`config show` の `Preset:` の下と `doctor` の Notes に note が出ます: `reviewers_extra[0] in the
project file: id codex-general is taken by the fitted panel; it runs as codex-general-2 (reviewer
set codex-general-2 --id <name> keeps a name)`。そのため `--only <フィットした id>` は常に
フィットした枠を選びます。フィットはインストールされているものに従うので、Codex を入れると
extra の id が変わることがあります。extra がそれ以外の理由で外されることはないので、後で
フィットが同じ枠を持つようになった extra は、削除するまで 2 回走ります。

**何を検査するか。** 各ファイルの extra は、パネルに加わるかどうかにかかわらず、エントリごとに
検査されます（id、role、provider と model、`when`、そのファイル内での id の一意性）。問題は
`reviewers_extra[0] in the global file: ...` のように表示されます。パネル全体の規則（常に走る
レビュアーが少なくとも 1 人いることなど）は、extra を含めたパネルに適用されます。
`reviewer remove`、`reviewer set`、`config set reviewers_extra[<n>].<key>` は、ファイル自身の
extra をその場で編集し（新しい id で走っている extra もその id で見つかります）、何もコピーしません。
新しい id で走っている extra を元の id で指すと、その id は今は別の枠を選ぶので、`reviewer remove` と
`reviewer set` は何も書かずに拒否し（exit 2）、メッセージは extra が走っている id を示します:
`reviewer claude-security: reviewers_extra[0] in the project file runs as claude-security-2, since
claude-security is taken; use claude-security-2 (select the other seat by its position in reviewer
list)`。

**それぞれの出どころ。** project の extra は、project の `reviewers` リストが受けるのと同じ拒否を
すべて受けます。agy のものや `options.args` を持つものは拒否されます（[下記](#role-options)）。
グローバルの extra が agy なら、警告付きで実行されます。`config show` は各 extra に印を付け
（`(extra, project file)`）、その JSON には `config.reviewers` と並行する `reviewer_origins` が
加わります。`reviewer list` は `(extra: project)` の印を付け、その JSON と `doctor` の JSON は
各レビュアーに `origin`（`fit`、`global`、`project`、`global extra`、`project extra`）を付けます。
`config prune` は extra を残し、`config reset` はほかの上書きと一緒に消して、いくつ消したかを
表示します。小さな変更でレビュアーを 1 人だけ走らせるときは、パネルの順で最初の `general` の
レビュアーなので、extra が選ばれるのは、加わる先のパネルに `general` の枠がないときだけです。

**継承するパネルにさらに足したものを持つリスト**（このキーができる前の `reviewer add` が残した
形）には、`doctor` が note を出します: `reviewers in <file>: holds the inherited panel plus
<ids>; move <ids> to reviewers_extra and remove reviewers to keep following it`。`reviewers` を
並べていないファイルで、継承する設計パネルにさらに足したものを持つ設計用のリストにも、
`review.design.reviewers` について同じ note が出ます（`holds the inherited design panel plus
<ids>; move <ids> to review.design.reviewers_extra ...`）。継承する設計パネルは、そのファイルの
下にある設計パネルです。グローバルファイルが `reviewers` を並べている下の project ファイルでは、
そのリストから `when` を除いたものです。ファイルを書き換えることはありません。

**コードの extra とプリセットの設計パネル。** 設計ラウンドがプリセットの設計パネルで走る間、
`reviewers_extra`（`reviewer add` や `suggest-roles --write` が書いたもの）はコードだけを
レビューします。以前は設計ラウンドがコードのパネルのコピーで走り、extra も一緒に加わっていました。
`doctor` はそれぞれに note を出します: `reviewers_extra in <file>: <ids> review code only; design
rounds run preset standard's design panel (add them to review.design.reviewers_extra as well to
review designs too)`。

**`reviewers` を並べたファイルは、並べた枠だけを持ち続けます。** プリセットの枠が増えても、
並べたパネル（書き込みコマンドが以前のフィットからコピーしたものも含む）には1つも加わりません。
有効なパネルは `config show` が表示します。

古い dev-orchestra は `reviewers_extra` を無視します。パネルは extra なしで走り、その
`config validate` もこのキーを報告しません。

<a id="a-design-panel-of-its-own-reviewdesignreviewers"></a>

### 独自の設計パネル（`review.design.reviewers`）

設計レビューは、いずれかのファイルが独自のパネルを与えるまで、プリセットがフィットした設計パネル
（[プリセット](#presets)）で走ります。コードのパネル（`reviewers`）を並べたファイルでは、代わりに
そのパネルが、すべての `when` を無視して走ります。`review.design.reviewers` は設計ラウンドに
限って継承される設計パネルを置き換え、`review.design.reviewers_extra` は、`reviewers_extra` が
コードのパネルに対してするのと同じように、継承される設計パネルがどれであってもそれに足されます:

```yaml
review:
  design:
    reviewers:
      - id: claude-general
        provider: claude
        model:
          family: sonnet
          version: latest
        high_risk_model:           # opus on a high-risk plan
          family: opus
          version: latest
        role: general
      - id: claude-security
        provider: claude
        model:
          family: sonnet
          version: latest
        role: security
        relevance: always          # never judged by the role rules (a security seat's default)
    reviewers_extra: []
```

| 設計パネル | 条件 |
| --- | --- |
| なし: 設計ラウンドはコードのパネルで走り、`when` は無視される | どのファイルも `review.design.reviewers` も `review.design.reviewers_extra` も設定しておらず、ファイルが `reviewers` を並べているか、グローバルファイルが知らないプリセットを指定している |
| project ファイルの設計用のリスト、次に project ファイルの設計用の extra | project ファイルが `review.design.reviewers` を並べている |
| グローバルファイルの設計用のリスト、次にグローバルの設計用の extra、次に project の設計用の extra | それ以外で、グローバルファイルがそれを並べている |
| プリセットがフィットした設計パネル、次にグローバルの設計用の extra、次に project の設計用の extra | それ以外で、どのファイルも `reviewers` を並べていない |
| 有効なコードのパネルからすべての `when` を外したもの、次にグローバルの設計用の extra、次に project の設計用の extra | それ以外（ファイルが `reviewers` を並べ、ファイルが設計用の extra だけを設定している） |

同じ id が両方のパネルの席を指してもかまいません（`claude-general`）。ラウンド、レポート、
使用量のラベルはステージごとに分けて保たれるので、その履歴は途切れません。id が使われている
extra は、コードのパネルと同じく note を出して別の id になります。

**設計パネルの席になれるもの。** `reviewers` のエントリと同じスキーマで、同じように検査されます
（`review.design.reviewers[0]: ...`）。違いは 2 つです。`when: paths` は拒否されます。plan には
変更されたパスがないからです。そして `when: high-risk` は尊重されます -- その席は、高リスクに
一致した plan や、`review run --design --high-risk` で宣言されたラウンドに加わります -- ので、
やはり 1 つの席は常に走らなければなりません。project ファイルの設計用の席が agy であるか、
`options.args` を持つ場合は、project の `reviewers` のエントリと同じく拒否されます。global の
コードの席と id・provider・options が同じ席でも同じです。ファイルが `review.design` の下に書いた
席は、常にそれ自身として判定されます。そのコードの席の規則に従うのは、コードのパネルから
コピーされた席だけです。

**編集する。** `reviewer list|add|remove|set --design` は設計パネルに対して働きます。
`reviewer add --design` は、ファイルが `review.design.reviewers` を並べていればそこへ、なければ
`review.design.reviewers_extra` へ書き込み、設計パネルが引き続き何に従うか（`preset standard's
fit`、`the code panel` または `the global file's design reviewers`）を示します。継承された席に対する
`reviewer set|remove --design` は、まず有効な設計パネルをファイルにコピーします。コードのパネルから
コピーした席は `when` を失い、note がそう伝えます。フィットからコピーしたときは、note が `copied from
preset standard's fit` と書き、どちらのファイルでも `; the design panel no longer follows preset
standard's fit (recorded claude-general sonnet, claude-security sonnet, claude-test sonnet)` を
足します。project スコープでは、このコピーはコードのパネルと同じ規則に
従います。agy の席は同じ `not copied into` の note とともにコピーから外されますが、その席の
出どころのパネルを global ファイルが並べている場合 -- 設計用の席なら設計のリスト、コードのパネルの
席なら `reviewers` -- は、そのままコピーされます。`--design` と `--when-paths` を一緒に使うと exit 2 で
終わります。`reviewer list --design` は最初の行で出どころを示します:
`(design panel: the preset's fit)`、`(design panel: the code panel; when conditions ignored)`、
`(design panel: global file)`、または `(design panel: project file)`。`review status --design` も
同じように示します。

**どこに現れるか。** `config show` には `Design reviews` ブロックがあり、設計パネルがあるときは
その JSON に `design_reviewer_origins` が加わります（フィットした席は `fit design`）。`doctor` は
`design_reviewers` と `design_panel_source`（`fit`、`global`、`project`、`code`）を報告し、設計
パネルがコードのパネルのコピーだけでなければ `Design: 3 (the preset's fit)` を表示し、フィットや
ファイルが書いた設計用の各席を、コードの席と同じように診断します。そのため agy だけのときは
`agy-general` が、パネルごとに1回ずつ、2回警告されます。
`review status --design` は設計パネルと、次のラウンドが外す席を挙げます。`review run --design
--only <id>` は設計パネルから選びます。`run <reviewer id>` は常にコードのパネルの席を実行します。

**高リスクのラウンドでのモデル。** どちらのパネルのどの席も `high_risk_model` を設定できます。
これはモデルのブロック（`family` と `version: latest`、または `id` で固定）で、高リスクに一致した
ラウンドや `--high-risk` のラウンドでは `model` の代わりにそれを使います。provider と `options` は
席自身のもののままなので、その中の `provider` や `options` は拒否されます。`when: high-risk` は
リスクに応じて席を加え、`high_risk_model` は席のモデルを変えます。この 2 つは併用できます。
切り替えた席ごとに `note: high-risk round (<path> matches <pattern>): <id>
runs opus instead of sonnet` が出力され、その実行記録に `model_slot:
high-risk` が加わり（これのない記録は通常の枠です）、`optimization report` は 2 つの枠を
分けて採点します。`reviewer add|set
--high-risk-model FAMILY` で設定し、`reviewer set --clear-high-risk-model` で外します。
`reviewer set --provider` で別の CLI にすると、`--model` の有無にかかわらず、note を出して
外されます。一緒に `--high-risk-model` を指定すれば、新しい CLI のものが書かれます。

古い dev-orchestra は `review.design.reviewers`、その下の `reviewers_extra`、`high_risk_model`、
`relevance` を無視します。設計ラウンドはコードのパネルで走り、どの席も普段のモデルを使います。
フィットした設計パネルもありません。

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

reviewers:                    # 0..n independent reviewers; two or more recommended; listed, design rounds run it too
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
  - id: claude-security        # security and test on sonnet, Claude's cheap model
    provider: claude
    model:
      family: sonnet
      version: latest
    high_risk_model:           # the full model on a high-risk change
      family: opus
      version: latest
    role: security
  - id: claude-test
    provider: claude
    model:
      family: sonnet
      version: latest
    role: test

run:
  timeout_seconds:                    # total deadline of one `run`, per role
    orchestrator: 1800
    architect: 1800
    implementer: 3600                 # implementers measured past 30 min
    review_fixer: 1800

review:
  max_review_iterations: 2            # hard stop on review→fix→re-review loops
  parallel: true                      # run reviewers concurrently
  re_review_severities: [critical, high]
  timeout_seconds: 1800               # reviewers only: `review run` and `run <reviewer-id>`
  idle_timeout_seconds: 300           # no output for this long: wedged; `run` too
  design:
    enabled: auto                     # review .ai/plan.md before implementing (true, false or auto)
    max_iterations: 2                 # design review -> revise -> re-review
    # reviewers: [...]                # a design panel of its own; unset, the preset's fitted one

optimization:
  skip_unneeded_roles: true           # a test/architecture seat sits out a round with nothing for it

design:
  require_approval: true              # implementer waits for the user's yes (design approve)
  resume:                             # run architect --resume
    max_age_seconds: 3600             # older than this, the revision runs fresh
    max_context_tokens: null          # no cap on the context a resumed session carries

workspace:
  dir: .ai                            # relative to the repo root, or absolute

language:
  reply: null                         # a language tag (ja, zh-TW, ko, en): the language to answer in
  rewrite: true                       # false: Claude Code reminds, but never asks for a rewrite
```

<a id="field-reference"></a>

### フィールドリファレンス

| Field | 型 | 備考 |
| --- | --- | --- |
| `version` | int | `1` でなければなりません。 |
| `<role>.provider` | string | 登録済みの adapter: `agy`、`claude`、`codex`、`mock`、またはユーザー adapter（`references/providers.md` を参照）。`agy` の読み取り専用のロールやレビュアーは、global 設定から、またはほかのインストール済みの CLI がその席に就けないときの global のプリセットのフィットからだけ受け付けます（[下記](#role-options)）。 |
| `<role>.model.family` | string | provider が解決できる family/エイリアス（`opus`、`sonnet`、`fable`、`recommended-coding`）。省略するか `default` を使うと CLI に選ばせます。 |
| `<role>.model.version` | `latest` \| `pinned` | `latest` は実行のたびに解決し直します。`pinned` には `model.id` が必要です。 |
| `<role>.model.id` | string | 正確なモデル id。`version: pinned` のときのみ。 |
| `reviewers[].id` | string | 一意で、`[a-z0-9][a-z0-9._-]*` に一致すること。レポートファイルの名前になります。 |
| `reviewers[].role` | string | 組み込みのもの、または独自のもの。`references/reviews.md` を参照。 |
| `reviewers[].when` | `always` \| `high-risk` \| `paths` を持つマッピング | **コード**レビューのラウンドでそのレビュアーがいつ走るか（デフォルト `always`）。`high-risk` は高リスクと判定されたラウンドにだけ加わります。`paths` を持つマッピングは、変更が自分のパターンのどれかに一致したラウンドにだけ加わります。コードのパネルで走る設計ラウンドはどちらも無視し、すべてのレビュアーを走らせます。独自の設計パネルは `high-risk` を尊重し、`paths` を拒否します。少なくとも 1 人は `always` のままでなければなりません。[高リスクな変更でだけ走るレビュアー](#reviewers-that-run-only-on-high-risk-changes)と[パスで絞り込むレビュアー](#reviewers-scoped-to-paths)を参照。 |
| `reviewers[].high_risk_model` | mapping | 高リスクのラウンド（高リスクへの一致か `--high-risk`）で、その席が `model` の代わりに使うモデルのブロック。どちらのパネルでも使えます。`family` と `version`、または `version: pinned` と `id`。`provider` や `options` は持てません。それらは席のもののままです。[独自の設計パネル](#a-design-panel-of-its-own-reviewdesignreviewers)を参照。 |
| `reviewers[].relevance` | `security` \| `test` \| `architecture` \| `always` | 見るもののないラウンドからその席を外しうるロールの規則。未設定なら、`test`、`architecture` の席は自分のロールの規則で判定され、それ以外のロールは判定されません。`security` もこれに含まれます。その規則はパスの名前しか読まないからです。`security` を設定すると security の席がオプトインします。`always` はその席を判定の対象から外します。`general` の席に規則を設定すると拒否されます。`general` が外されることはありません。`references/reviews.md`（「ラウンドが必要としないロール」）を参照。 |
| `reviewers_extra` | list \| null | ファイルが継承するパネルの横に足すレビュアー。`reviewers` のエントリと同じスキーマで、どちらのファイルにも置けます。id が使われていれば別の id になり、外されることはありません。[パネルの横にレビュアーを足す](#adding-reviewers-beside-the-panel-reviewers_extra)を参照。 |
| `review.design.reviewers` | list \| null | 設計レビュー独自のパネル。`reviewers` のエントリと同じスキーマです（`when: paths` は不可）。どのファイルでも未設定なら、設計ラウンドはプリセットがフィットした設計パネルで、ファイルが `reviewers` を並べていれば `when` を無視したコードのパネルで走ります。プリセットが決めるキーなので、`config setup --preset` はこれを置き換えます。[独自の設計パネル](#a-design-panel-of-its-own-reviewdesignreviewers)を参照。 |
| `review.design.reviewers_extra` | list \| null | ファイルが継承する設計パネルの横に足す設計レビュアー。どのファイルも設計パネルを並べていなければ、継承するのはフィットした設計パネルで、ファイルが `reviewers` を並べていれば `when` を外したコードのパネルです。 |
| `review.max_review_iterations` | int ≥ 0 | プロジェクト単位ではなくレビュー単位のラウンド数です。新しいブランチ、新しい `--base`、または `budget reset` でカウントはリセットされます。`0` で再レビューを完全に無効にします。 |
| `review.parallel` | bool | `false` にするとレビュアーを 1 つずつ実行します（デバッグしやすくなります）。 |
| `review.re_review_severities` | list | ブロッキングとみなす severity。`critical`、`high`、`medium`、`low` から選んだ空でないリストで、大文字小文字は問いません（デフォルトは `[critical, high]`）。リストにしない 1 つの名前、知らない名前、`[]` は断られます。検査せずにファイルを読むコマンドは、代わりにデフォルトでブロックします。 |
| `run.timeout_seconds.<role>` | int > 0 | `orchestrator`、`architect`、`implementer`、`review_fixer` の `run` 1 回の合計の締め切り（デフォルトは implementer が 3600、ほかは 1800）。`--timeout` で 1 回だけ上書きできます。ほかのキーは拒否されます。ロールのブロックの外にあるので、設定してもそのロールはプリセットのフィットから外れず、`config setup --preset` もこれを残します。この締め切りで止められた実行は、キーの名前を挙げてそう伝えます。 |
| `review.timeout_seconds` | int > 0 | 各レビュアーの合計の締め切り。`review run` のラウンドのレビュアーと `run <reviewer-id>` に効きます（デフォルト 1800）。ロールの `run` にはもう効きません。そちらは `run.timeout_seconds.<role>` です。タイムアウトは報告されるだけで、例外にはなりません。 |
| `review.idle_timeout_seconds` | int > 0 \| null | この時間出力がなければ、実行は固まったものとして扱われます（デフォルト 300。ストリーミングする provider のみ）。レビュアーとすべてのロールの `run` で共通です。沈黙の長さはタスクの大きさでは伸びないからです。ロールごとには `options.idle_timeout` で上書きできます。 |
| `review.exclude` | list | diff 本文をレビュアーに渡さない glob パターン。デフォルトのリストを丸ごと置き換えます。`[]` ですべてをレビューします。 |
| `review.incremental_rounds` | bool | `true`（デフォルト）にすると、2 回目のラウンドは 1 回目のラウンドがレビューした内容に対する diff になり、修正が対処しようとした指摘を引き継ぎます。`false` にすると毎ラウンド変更全体の diff を取り直します。 |
| `review.max_findings` | int \| null | 各レビュアーに求める指摘の数。`null`（デフォルト）は `optimization.level` に任せ、`0` は上限を外します。上限を超えて返ってきた指摘は保持され、切り捨てられることはありません。 |
| `review.context.max_chars` | int ≥ 1 \| null | レビューラウンドがそもそも送信する変更本文の最大サイズです。対象は diff、または plan とそれが答える依頼です（デフォルト 400,000 ≈ 100k トークン。ここで記録された最大のプロンプトの 4 倍で、これまで誰かが実行したものは何も拒否しません）。これを超えると、`review run` は何もレビューせずに exit 3 で終了します。上限内に収める方法は、`--base`、`review.exclude`、変更の分割、または plan を短くすることです。`--force` は人間による上書きで、そのラウンドを予算超過として記録します。`null` は `review.max_findings` と同様にデフォルトを意味します。この上限を無効にする値はありません。`references/limits.md` を参照。 |
| `review.context.inline_chars` | int ≥ 1 \| null | 変更本文のうちどれだけをレビュアーのプロンプトに含めるか（デフォルト 400,000、`max_chars` と同じ数値）。これ以下なら本文はインラインで渡され、ラウンドは clean になり得ます。これを超えると、レビュアーには凍結されたスナップショットのパスが渡され、何が返ってきてもラウンドは `partial` — カバレッジ未検証 — として記録されます。**`max_chars` より小さく設定すると、両者の間に、ラウンドは実行されるものの `partial` として記録される帯域が生まれます**: これは非常に大きなプロンプトに費用をかけたくない人が明示的に選ぶもので、`partial` はその代償です。`max_chars` *より大きく*設定することも許されており、誤りではありません — その場合、本文がファイルとして渡されるのは人間が強制したラウンドだけになります。`null` はデフォルトを意味します。各ラウンドは比較に使った数値を記録するので、`partial` のラウンドはどの上限によってそうなったのかがわかります。`references/limits.md` を参照。 |
| `review.context.surrounding` | `none` \| `enclosing` | `enclosing` にすると、各 hunk を囲む Python の関数・メソッド・クラスも、すべてのコードレビュアーに渡します。スナップショットの取得時に、その git ツリーから抽出します（デフォルト `none`: diff だけ）。1 つのスナップショットで計測したところレビューが安くならなかったため、off です — `optimization report` が、これを使ったラウンドと使わなかったラウンドを比較します。`false` と `null` は `none` を意味します（`off` は `false` として読まれます）。`true` は拒否されます。`references/reviews.md` を参照。 |
| `review.context.surrounding_chars` | int ≥ 1 \| null | 1 つのラウンドが追加できる周辺コンテキストの最大量（デフォルト 15,000: 1 つのスナップショットで計測したところ、実行あたりの費用は変わらず、60,000 では渡した分がそのまま上乗せされました）。さらに、diff が `max_chars` と `inline_chars` の下に残す分で上限がかかるので、コンテキストがラウンドを拒否させたり、diff をファイル渡しにしたりすることはありません。収まらなかったものは、プロンプトとすべてのレポートで名前を挙げて除外されます。`null` はデフォルトを意味します。`references/limits.md` を参照。 |
| `review.design.enabled` | bool \| `auto` \| null | 実装の前に `.ai/plan.md` を設計パネル（`review.design.reviewers` を参照）にかけるかどうか。このステージはラウンドごとにパネルのメンバー 1 人につきレビュアー実行 1 回分のコストがかかります。`true` は常に実行、`false` は実行しません。`auto`（デフォルト）は計画書から判断し、その答えと理由を `status` が表示します。計画書のどこにあってもバッククォートで囲まれたトークンと、計画書の `Files to Modify` 見出しの下にあるパスらしい語（バッククォートの有無を問わず、そこにあるフェンスブロックも含む）はすべて、大文字小文字を区別せずに高リスクパターン（`optimization.high_risk_paths` と `extra_high_risk_paths`）と照合され、一致すれば実行します。`db/migrate` のように `/` を含み拡張子のない名前はディレクトリとしても照合します。規模は、`Files to Modify` の下にあるファイル名らしいトークン（`/` かファイル拡張子を含むもの）のうち、フェンスブロックの中、`docs/`・`references/`・`tests/` と `.md`・`.rst`・`.txt` ファイルを除いたものを、ディスクを見ずに数え、6 個以上なら実行します。そこにグロブ、ディレクトリ、`/` を含み拡張子のない名前、`..` を通るパスがあれば実行します（`payload["mode"]` のようなコードはグロブとして読みません）。計画書が読めない、`Files to Modify` セクションがない、あってもファイルを 1 つも挙げていない場合も実行します。計画書がまだないときの答えは `auto -> run (once a plan is written)` です。そしてワークフローで設計レビューのラウンドが一度でも走ったら答えは実行のままなので、改訂によってループが途中で止まることはありません。`null` はデフォルトの `auto` を意味します。 |
| `review.design.max_iterations` | int ≥ 0 | 設計レビューのラウンド数（レビュー → トリアージ → 修正）。`max_review_iterations` とは別にカウントされます（デフォルト 2）。上限に達したラウンドでも修正は行われます。上限が拒否するのはその後の再レビューだけです。`1`: 1 ラウンド、1 回の修正、その後ユーザーに確認。`0`: 設計レビューなし。`budgets.architect`（デフォルト 3）は、デフォルトでは設計とラウンドごとに 1 回の修正をまかないます。`max_iterations` に合わせて引き上げ、承認時に変更を求められることが予想される場合はさらに 1 つ増やしてください。 |
| `design.require_approval` | bool | `true`（デフォルト）にすると、`.ai/plan.md` が存在し、現時点の plan が `design approve` で承認されていない間は -- ユーザーが了承した後に承認するものです -- `run implementer` が拒否します（exit 5）。`false` は誰も見ていない実行（CI、バッチ）向けで、このゲートが導入される前の挙動に戻します。`review.design` の下ではなくトップレベルにあるのは、パネルが plan をレビューしたかどうかにかかわらず承認が重要だからです。`--force` ではバイパスできず、この設定だけがバイパスできます。 |
| `design.resume.max_age_seconds` | int ≥ 0 \| null | `run architect --resume` がセッションを継続できる、直前の architect の実行の古さの上限です（デフォルト 3600。測定時に CLI がプロンプトキャッシュを保持していた時間）。これより古ければ、改訂は全文プロンプトで新規に走ります。`0` は常に新規、`null` はデフォルトの意味です。 |
| `design.resume.max_context_tokens` | int > 0 \| null | 継続を許す、セッション終了時の文脈の大きさ（トークン）の上限です（デフォルト `null`: 上限なし）。各実行は `context_tokens` を記録するので、測った値から上限を決められます。一方だけを設定しても、もう一方はデフォルトのままです。 |
| `optimization.level` | `aggressive` \| `balanced` \| `quality` | どれだけ安く済ませようとするか。デフォルトは `balanced`。下記を参照。 |
| `optimization.high_risk_paths` | list | それに触れる変更に対して `quality` を強制する glob。デフォルトのリストを丸ごと置き換えます。 |
| `optimization.extra_high_risk_paths` | list | `high_risk_paths` を置き換えずに、それに追加する glob（デフォルト `[]`）。一致すると `high_risk_paths` の一致とまったく同じように引き上げられます。他のリストと同様、project の値は global の値を置き換えます。 |
| `optimization.low_risk_max_files` | int | `quality` 未満のレベルで、小さな変更とみなすファイル数の上限（デフォルト 5）。 |
| `optimization.low_risk_max_lines` | int | さらに、変更行数の上限（デフォルト 150）。 |
| `optimization.skip_unneeded_roles` | bool | `true`（デフォルト）: 常に走る `test`、`architecture` の席は、見るもののないラウンドを休みます。コードレビューでも設計レビューでも、`quality` を含むどのレベルでもそうです。`relevance: security` でオプトインした `security` の席も同じで、オプトインしていない席は常に走ります。`false` にすると、そうした席を従来どおり毎ラウンドすべて走らせます。`references/reviews.md`（「ラウンドが必要としないロール」）を参照。 |
| `optimization.security_paths` | list | `relevance: security` の席について、`security` の規則が高リスクパターンのほかに探すもの: リクエスト処理と入力、ファイル・URL・クライアント・クエリ・データベースへのアクセス、実行とデシリアライゼーション、設定と依存関係のマニフェスト、そしてデフォルトの高リスクパターンすべて。デフォルトのリストを丸ごと置き換えます。`[]` にすると高リスクパターンだけが残ります（`doctor` がそれを報告します）。 |
| `optimization.extra_security_paths` | list | `security_paths` に追加する glob（デフォルト `[]`）。 |
| `optimization.architecture_paths` | list | `architecture` の規則が探すもの: 契約とスキーマ、モジュールの表面、設定とレコードの形式、CLI、ビルド。デフォルトのリストを丸ごと置き換えます。`[]` にすると規模とディレクトリの判定だけが残ります（`doctor` がそれを報告します）。 |
| `optimization.extra_architecture_paths` | list | `architecture_paths` に追加する glob（デフォルト `[]`）。 |
| `workspace.dir` | string | `.ai/` の成果物を置く場所。 |
| `workspace.stale_notice_days` | int 0–36500 | 新しいワークフローが始まったとき、その最初のコマンドが、最後の活動（`state.json` の `updated_at`、なければ `started_at`）からこの日数以上たったほかのワークフローを、stderr に一度だけ知らせます（デフォルト 30）。現在のワークフローは含めず、実行中のステージがあるワークフローも含めません。その印はそのワークフロー自身で `status` を実行したときにしか消えないため、ステージの途中で放置されたワークフローがここで名前を挙げられることはありません。`workflow list` では `in flight` と表示されます。使えるタイムスタンプがないワークフローや、`state.json` が読めないワークフローは数えません。何も削除しません。ワークフローを削除するのは、これまでどおり `workflow remove <id> --yes` だけです。`0` でこの通知を止め、`null` はデフォルトを意味します。通知がコマンドの動作を変えることはありません。 |
| `language.reply` | string \| null | オーケストレーターがユーザーに答える言語を、言語タグで指定します: `ja`、`zh-TW`、`ko`、`ru`、`en`、`es`、`fr` など（2〜3 文字の主タグに、任意の数の副タグが続く形。主タグは小文字として読みます）。`null`（デフォルト）は SKILL.md のルール 11 に任せます。ユーザーが頼んだ言語、なければユーザーが書いている言語です。ほかのキーと違い、プロジェクトのファイルの `null` は下の層を引き継がず、グローバルのファイルで設定したタグをそのプロジェクトでだけ取り消します。設定すると、どのホストでも `doctor` が *Reply language* として表示します。Claude Code では 3 つのフックが、プロンプトのたびにその言語を思い出させ、判定できる言語（文字体系で判定する言語と、よく使う語で判定する英語・スペイン語・フランス語・ドイツ語・ポルトガル語・イタリア語）なら、明らかに別の言語で書かれた返答を一度だけ書き直させます。タグのなかったファイルに `config set` や `config setup --language` でこれを設定すると、Claude Code のユーザー設定（`~/.claude/settings.json`、または `$CLAUDE_CONFIG_DIR` の下）のディレクトリがあればそこにフックも加えます。プロジェクトのファイルだけにある値で加えることはありません。グローバルのファイルにもプロジェクトのファイルにも設定がない状態にすると外します。`--no-hooks` を付けるとユーザー設定には触れず、`hooks install` / `hooks uninstall` で手で入れたり外したりできます。ホストごとの扱い、フックの書き込み方、判定で区別できること・できないことは `references/architecture.md`（「返答の言語のフック」）を参照。 |
| `language.rewrite` | bool \| null | `true`（デフォルト）: Claude Code の Stop フックによる判定が有効です。`false` にすると、リマインダーは残したまま判定を止めます。判定があなたの返答を誤判定するときのためのものです。`null` はデフォルトを意味します。 |
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
| `claude` | `permission_mode` | インストールされている CLI が `--permission-mode` に対して提示するもの（`dev-orchestra model list` とは別に、`claude --help` を実行して確認してください）。書き込みロールでは global 設定（または `--extra`）からだけ受け付けます |
| `codex` | `sandbox` | `read-only`、`workspace-write`、`danger-full-access`。書き込みロールでは global 設定（または `--extra`）からだけ受け付けます |
| `codex` | `approve` | `true`（デフォルト）は `--approve-for-me` を渡し、`false` は省略します。書き込みロールでは global 設定（または `--extra`）からだけ受け付けます |
| `agy` | `skip_permissions` | `true` にすると `implement` の実行で `--dangerously-skip-permissions` を渡し、implementer がコマンドを実行できるようになります。デフォルトは `false`。global 設定（または `--extra --dangerously-skip-permissions`）からだけ受け付けます |
| any | `idle_timeout` | このロールの無出力期限を上書きします |

```yaml
# In the global config: the project file's permission_mode and args are refused.
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

読み取り専用のロール（orchestrator、architect、それらの tier、すべてのレビュアー）では、
Claude が受け付ける生引数は `--add-dir <path>` だけで、Codex は何も受け付けません。
それも global 設定か `--extra` からだけ受け付けます。同じ `args` を project ファイルに
書くと、中身を問わずそのロールの実行は拒否されます。project ファイルはレビュー対象の
ブランチと一緒に持ち込まれうるもので、自分のレビュアーのディレクトリを指定できる
ブランチは、レビュアーが読める範囲を広げられてしまうからです。`claude`・`codex`・`agy`
以外の CLI では、implementer と review fixer はどちらのファイルからも `args` を受け取ります。
この 3 つでは、書き込みロールはパーミッションのオプション -- Claude の `permission_mode`、
Codex の `sandbox` と `approve`、agy の `skip_permissions` -- も `options.args` も
project ファイルからは受け取りません。project ファイルが implementer、review fixer、
またはそのいずれかの tier でそのどれかを挙げると、値を問わずそのロールの `implement` の
実行は拒否され、`config validate` と `doctor` がそう伝えます。これらは global 設定から、
1 回の実行だけなら `--extra` で指定します（`--extra --permission-mode bypassPermissions`、
`--extra --dangerously-skip-permissions`）。

**agy の読み取り専用の席は global 設定からだけ受け付けます。** agy には読み取り専用の
モードがないので、agy での plan や review の実行は、作業ツリー、`.ai/`（承認記録を含む）、
`.git/`、リポジトリの外のファイルを変更でき、後から確かめるものは何もありません。global
ファイルで設定した（または、Claude も Codex も席に就けるユーザー adapter も無いマシンで global の
プリセットのフィットが置いた。[プリセット](#presets)を参照）そうした席（orchestrator、architect、
そのいずれかの tier、レビュアー）は実行され、`config set`、`reviewer add`、`reviewer set`、`config validate`、ウィザード、
`doctor`（注記として。`--strict` は通ります）、`run`、`review run` で警告されます。同じ席が
project ファイルから来ると拒否されます。`run` は exit 2 で終わり、レビュアーはそのラウンドで
失敗し（ほかのレビュアーは走ります）、`config validate` と `doctor` がそれを報告します
（そのため `doctor --strict` は失敗します）。`config set --scope project architect.provider agy`
（`orchestrator.provider`、`<role>.model_tiers.<tier>.provider`、`reviewers[<n>].provider`、
`reviewers_extra[<n>].provider` も）、
`reviewer add --scope project --provider agy`、`reviewer set --scope project --provider agy` は、
何も書かずに exit 2 で終わります。拒否のメッセージは、その席を global に設定するコマンドを示します。

```bash
dev-orchestra config set --scope global architect.provider agy
dev-orchestra reviewer add --scope global --provider agy
```

`reviewers[i].<key>` を 1 つでも設定した project ファイルはパネル全体を持つので（リストは
丸ごと置き換わります）、そこへコピーされた global の agy レビュアーは project のものになり、
同じメッセージで拒否されます。グローバルファイルにレビュアーが無いとき、プリセットのフィットから来た
agy の枠や agy のグローバルの extra はコピーされません。書き込むコマンドがそれを外し、その旨を
表示します（[プリセット](#presets)）。project ファイルの `reviewers_extra` も、その `reviewers` と
同じように拒否されます。

**読み取り専用ステージを緩めるようなオプションは無視されるか、拒否されます。**
orchestrator、architect、すべてのレビュアーは、`permission_mode` や `sandbox` が何と
言っていても常に読み取り専用で実行されます -- この不変条件こそが、独立したレビューに
価値を与えるものです。`dev-orchestra doctor` は、その理由で無視しているオプションを
黙って捨てるのではなく一覧表示します。`options.args` にそれ以外の生引数があると、
そのロールの実行は拒否されます（終了コード 2。レビューのラウンドではそのレビュアーが
失敗になります）。`config validate` と `doctor` は、実行より前にそれを警告します。

permission mode を緩める代わりの方法は、CLI 自身の設定で特定のコマンドを
許可リストに入れることです（Claude Code なら、`.claude/settings.json` の
`Bash(pytest:*)` のような `permissions.allow` エントリ）。こちらのほうが範囲が狭く、
このスキルではなくプロジェクトの側に置かれます。

Claude の読み取り専用の実行は `--restricted` を付けて走るので、そこではプロジェクトと
ユーザーの `settings.json` はまったく読まれません。**`permissions.deny` も読まれません**。
`Read(./.env)` のような deny ルールでリポジトリ内の秘密をモデルから遠ざけていても、
architect やレビュアーの実行には効きません。管理設定（managed settings）は引き続き
効くので、そうしたルールはそこへ移してください。implementer と review fixer は、
これまでどおり設定ファイルを読みます。読み取り専用の実行が読めるのは作業ディレクトリと
`--add-dir` の中だけです。ワークスペースのコンテナをリポジトリの外に置いている場合
（`workspace.dir` を絶対パスにしている場合）は、global 設定のそのロールの
`options.args` に `--add-dir <container>` を足すか、`--extra` で渡してください。

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
意味するところです -- ただし、そのラウンドに見るもののあるロールのパネル全体です。`quality` を
含むどのレベルでも、読むもののない `test`、`architecture` の席 -- および `relevance: security`
でオプトインした `security` の席 -- は、ラウンドが高リスクでない限り休みます（`optimization.skip_unneeded_roles`。`references/reviews.md` の
「ラウンドが必要としないロール」を参照）。

**サイズだけが判定基準になることはありません。** 縮小されたパネルになるには、変更が
両方のしきい値を下回り、*かつ*高リスクなものに何も触れていない必要があります。auth ファイルの
1 行こそ、認可のバグがまさに入り込む形だからです。

```yaml
optimization:
  level: aggressive
  low_risk_max_files: 3
  low_risk_max_lines: 80
```

<a id="reviewers-that-run-only-on-high-risk-changes"></a>

### 高リスクな変更でだけ走るレビュアー

`security` レビュアーのような専門レビュアーを、ラウンドが高リスクと判定されたときだけ
コードレビューに加え、それ以外では費用がかからないようにできます:

```yaml
reviewers:
  - id: claude-general
    provider: claude
    role: general            # when: always (default)
  - id: claude-security
    provider: claude
    role: security
    when: high-risk          # always (default) | high-risk; code review, and a design panel of its own
optimization:
  extra_high_risk_paths: ["*/providers/*", "*/config.py"]   # added to high_risk_paths; default []
```

**`when: high-risk` のレビュアーがコードのラウンドで走るのは、変更が高リスクなパスに
一致したとき、オーケストレーターが `review run --high-risk` でそのラウンドを高リスクと
宣言したとき、または差分ラウンドか同じスナップショットの再実行で、自分自身の未解決の
accepted の指摘を確認し直す必要があるときです。それ以外では外されます。** コードのパネルで走る設計ラウンドでは常に走ります。独自の
設計パネルの席は、同じ判定で、plan のトークンに対して plan に加わります
（[上記](#a-design-panel-of-its-own-reviewdesignreviewers)）。`--only` で名前を
挙げれば走ります。判定はラウンドを `quality` に引き上げるのと同じものです。条件付きの
レビュアーを加えたり外したりする判断は、すべて理由とともに表示され記録されます。ラウンドが
どう判断するかは `references/reviews.md` を参照してください。

このようなパネルが何でもレビューできるように 2 つの規則があり、どちらかに反する設定は
`config validate`、`doctor`、`review run` のいずれも拒否します:

- **少なくとも 1 人のレビュアーは常に走らなければなりません。** すべてのレビュアーが
  `high-risk` のパネルでは、静かなラウンドをレビューする人が誰もいなくなります。
  最後の無条件のレビュアーに対する `reviewer set --when high-risk` と、その
  `reviewer remove` は拒否されます。
- **有効なパターンがなければなりません。** `high_risk_paths: []` で
  `extra_high_risk_paths` もなければ、`high-risk` のレビュアーには判定の材料がなく、
  決して走りません。

`extra_high_risk_paths` は、有効になっているほうの `high_risk_paths` のリストに追加されます。
そのため、リポジトリはデフォルトがすでにカバーしている 30 個をコピーせずに、デフォルトが
見逃している 1 つのパスを指定できます。**追加のパターンへの一致はすべて、`high_risk_paths` の
一致とまったく同じように、ラウンドを `quality` に引き上げ、red のツリーをゲートに通します。**
ですから、変更がパネル全体に値するパスだけを追加してください。設定レイヤー間では他のリストと
同様に置き換えられます: project でこれを設定すると、global の値は捨てられます。

導入は次の順序で行ってください: まずデフォルトが見逃しているパスを `extra_high_risk_paths` に
追加し、その後でレビュアーを `when: high-risk` に切り替えます。逆の順序だと、新しいパターンで
捉えるはずだったラウンドにそのレビュアーが参加しません。
`doctor` は、組み込みのパターンだけで判定される `high-risk` のレビュアーがあると注記するので、
最初の手順を飛ばして切り替えても気づかれないままにはなりません。

```bash
dev-orchestra reviewer set claude-security --when high-risk
dev-orchestra reviewer set claude-security --when always      # back to every round
```

<a id="reviewers-scoped-to-paths"></a>

#### パスで絞り込むレビュアー

`database` レビュアーのような特定分野の専門レビュアーを、変更がそのレビュアーの担当する
ファイルに触れたときだけコードレビューに加えることができます:

```yaml
reviewers:
  - id: codex-database
    provider: codex
    role: database
    when:
      paths:
        - "*migration*/*"
        - "*migrate*/*"
        - "*.sql"
```

**パスで絞り込んだレビュアーがコードのラウンドに加わるのは、変更されたパスが自分の
パターンのどれかに一致したとき、自分自身の未解決の accepted の指摘を確認し直す必要が
あるとき、または `--only` で名前を挙げられたときだけで、それ以外の理由では加わりません。**
他の条件付きのレビュアーと同じく、設計レビューでは毎回走り、加わったときはパネルを縮小
させず、加える・外す判断はすべて理由とともに表示され記録されます。「少なくとも 1 人の
レビュアーは常に走らなければならない」という規則では条件付きとして数えられ、自分の
パターンを持っているので、`high_risk_paths` に有効なパターンがなくても構いません。

- **特定分野の専門レビュアー向けであり、リスクを判断する役割向けではありません。**
  database、frontend、docs のレビュアーが関心を持つのはファイルの集まりですが、
  security レビュアーが関心を持つのはラウンドがどれだけ危ないかです。security など
  リスクを判断する役割は `when: high-risk`（または `always`）のままにしてください。
  パスで絞り込むと、security レビュアーは `.env`、`*secret*`、`*.tf`、
  `.github/workflows/*` への変更でも、ラウンドが引き上げられているのに参加しません。
  「高リスクなラウンドでも、これらのパスでも」という組み合わせの参加条件は用意して
  いません。専門レビュアーのパスを `extra_high_risk_paths` に加えてこれを実現しようと
  しないでください。それはリスクの方針を変えることになり、そのパスへの変更をすべて
  `quality` に引き上げ、1 人のレビュアーしか関心を持たないパスのために red のツリーを
  ゲートに通してしまいます。
- **一致してもレベルは引き上げられません。** レビュアーのパターンが表すのはそのレビュアーが
  何を知っているかであって、変更がどれだけ危ないかではありません。そのため一致しても
  レベルもゲートも動きません。引き上げは引き続き `high_risk_paths` と
  `extra_high_risk_paths` が決めます。同じパターンが両方にある場合（デフォルトには
  `*.sql`、`*migration*/*`、`*migrate*/*` が含まれます）、ラウンドは高リスクのリストに
  よって引き上げられ、レビュアーは自分のパターンによって加わり、それぞれに注記が出ます。
- **高リスクなラウンドだからといって加わりません。** 高リスクなパスへの一致と
  `review run --high-risk` が加えるのは `when: high-risk` のレビュアーだけです。どちらかが
  起きてパスで絞り込んだレビュアーが外されたときは、その注記にそう書かれます。
  1 ラウンドだけ含めるには、走らせたいレビュアーをすべて `--only <ids>` で挙げます。
  恒久的に直すには `reviewer set <id> --when high-risk` にします。
- **照合の対象は、レビュアーに見せる変更です**: レビューされるファイル、withheld の
  ファイル、リネーム元のパスです。オーケストレーター自身のファイルや、差分ラウンドで
  外される追跡外のファイルは、レビュアーに一切見せないので対象になりません。

パターンは `high_risk_paths` と同じ方法で照合されます:

- パス全体に対して照合し、`/` を含まないパターンはベース名にも照合するので、`*.sql` は
  どの深さでも一致します。
- `**` はありません: `db/**/*.sql` は `db/*/*.sql` として働き、ディレクトリのパターンには
  両方の形（`migrations/*` と `*/migrations/*`）か `*migration*/*` が必要です。
  `*migration*/*` は `migrate` には一致しない点に注意してください。
- **大文字と小文字を区別します。** 両方が混在するリポジトリでは両方を並べてください。
- 書いたとおりに扱われます: 決して一致しないパターンがあっても報告されません。

マッピングは上の例のようにブロック形式で書き、パターンは 1 行に 1 つ、`*` で始まる
パターンはすべて引用符で囲んでください。引用符のない `*.sql` は PyYAML では YAML の
エイリアスとして読まれ、PyYAML がない場合の同梱パーサーでは拒否されます。同梱パーサーは
インラインのマッピングと、フロー形式のリストの中で `[` を含むパターンも（引用符の有無に
かかわらず）拒否します:

```yaml
when:
  paths:
    - "*.sql"
    - "*.SQL"
    - "*[Mm]igration*/*"
```

`reviewer add` と `reviewer set` はこの形式で書き込みます。シェルに展開されないよう、
コマンドラインでも各パターンを引用符で囲んでください:

```bash
dev-orchestra reviewer add --provider codex --role database --when-paths "*migrate*/*" "*.sql"
dev-orchestra reviewer set codex-database --when-paths "*.sql"   # replaces the list
dev-orchestra reviewer set codex-database --when always          # back to every round
```

<a id="suggesting-path-scoped-reviewers-config-suggest-roles"></a>

#### パスで絞り込むレビュアーを提案させる（`config suggest-roles`）

`config suggest-roles` はプロジェクトのファイル名を読み、パスの集まりで担当を言い表せる
組み込みの専門レビュアー、つまり `database`、`frontend`、`backend` を、それぞれ専用の
`when.paths` 付きで提案します。モデルは呼ばず、トークンも使いません。`--write` を
付けない限り何も書き込みません:

```bash
dev-orchestra config suggest-roles            # what it would add, and why
dev-orchestra config suggest-roles --write    # add them to the project file's reviewers_extra
dev-orchestra config suggest-roles --json
```

提案ごとに、id、プロバイダーと family、パターン、提案の根拠になったファイル、パターンが
一覧のファイルのうち何件に一致するか、そのうち何件を `review.exclude` が withheld に
するかを表示します。続いて、提案しなかったロールをすべて理由とともに挙げます。
`--provider` で CLI を選びます（デフォルトは `claude` と `codex` のうち最初に
インストールされているもの）。`--model` で family を選びます（デフォルトは Claude なら
`sonnet`、Codex なら `recommended-coding`）。

**読むファイル。** git リポジトリの中では、`git ls-files` が挙げるものだけを読みます。
無視されたファイルと追跡外のファイルは読みません。`git ls-files` が失敗したとき、
タイムアウト（30 秒）したとき、16 MiB を超えて出力したときはエラー（exit 2）になり、
代わりにディレクトリを走査することはしません。git リポジトリの外ではディレクトリを
走査します。ドットで始まるディレクトリ、下に挙げるベンダーや生成物のディレクトリ、
8 階層より深いものは飛ばし、20,000 ファイル
または 10 秒で打ち切ります。その場合、割合はおおよそだと出力に書かれます。ほかに開く
ファイルは一覧のルートにある `package.json` だけで、それも追跡されていて、通常の
ファイルで、1 MiB 以下のときに限ります。

**2 つのファイル集合。** 一致件数と割合は、オーケストレーター自身のファイル
（`workspace.dir` とプロジェクトファイル）を除く、一覧のすべてのファイルに対して数えます。
`review.exclude` が withheld にするファイルも含みます。withheld のファイルでも、
パスで絞り込んだレビュアーをラウンドに加えるからです。規則が読む根拠からは、ドットで
始まるディレクトリの下、`node_modules`、`vendor`、`third_party`、`dist`、`build`、
`target`、`venv`、`__pycache__`、`generated`、`__generated__`、`gen` の下、テスト用の
ディレクトリ（`test`、`tests`、`__tests__`、`spec`、`testdata`、`fixtures`）の下に
あるファイルと、`review.exclude` が withheld にするファイルを除きます。根拠として読むのは
先頭から最大 20,000 ファイルで、それを超えると「evidence read from the first 20,000
files」と出力されます。

**規則:**

| ロール | 提案する条件 | パターン |
| --- | --- | --- |
| `database` | `migrations`、`migrate`、`alembic`、`prisma` のいずれかのディレクトリに 2 ファイル以上ある、`*.sql` が 2 ファイル以上ある、または `schema.prisma` がある | ディレクトリ名ごとに `<name>/*` と `*/<name>/*`、`*.sql`、`schema.prisma` |
| `frontend` | `*.tsx`、`*.jsx`、`*.vue`、`*.svelte` が 2 ファイル以上ある | 見つかった拡張子 |
| `backend` | `api`、`server`、`backend`、`handlers`、`routes`、`controllers` のいずれかのディレクトリに 2 ファイル以上ある | ディレクトリ名ごとに `<name>/*` と `*/<name>/*`。言語の拡張子だけのパターンは使いません |

- 名前は大文字小文字を問わず見つけ、パターンはリポジトリで使われている表記のまま、
  表記ごとに 1 つ（ディレクトリなら 1 組）書きます。`Migrations/` ディレクトリからは
  `Migrations/*` と `*/Migrations/*` ができます。
- `package.json` のフロントエンドの依存（react、vue、svelte、`@angular/core`、next、
  `@sveltejs/kit`、`@remix-run/*`）は根拠として挙げるだけです。コンポーネントの
  ファイルがなければ `frontend` は提案せず、理由にそう書きます。
- ファイルの半分以上がコンポーネントか SvelteKit の `+` で始まるルートファイルである
  ディレクトリは、`backend` の根拠に数えません。`package.json` が `@sveltejs/kit` か
  `@remix-run/` のパッケージを挙げているとき、または `package.json` を読まなかったうえで
  フロントエンドの兆候があるときは、`routes` を数えません。同じ名前に数えるディレクトリと
  数えないディレクトリがあるときは、数えるほうをフルパス（`backend/api/*`）で書き、
  数えないほうに一致しないようにします。
- 2 ファイルはディレクトリごとに数えます。1 ファイルずつの `migrations/` が 2 つあっても
  数えません。
- 有効なパネルにすでにそのロールがいるとき（フィットでも、一覧でも、どちらのファイルの
  extra でも）、パターンが 12 個を超えるとき、パターンが一覧のファイルの半分を超えて
  一致するときは、そのロールを提案しません。最後の場合はほとんどのラウンドに加わり、
  パネルが縮小されなくなるからです。
- `general`、`security`、`architecture`、`performance`、`test` は提案しません。これらが
  判断するのはリスクや変更全体であって、パスの集まりではありません。

**`--write` の書き込み先。** 現在のディレクトリから有効なプロジェクトファイルの
`reviewers_extra` に追記します。プロジェクトファイルがなければ、一覧のルート（`.git` の
あるディレクトリ）に `.dev-orchestra.yaml` を作ります。こうしてパネルはそれまでと同じものに
従い続けます。有効なプロジェクトファイルが一覧のルートにないとき（たとえば実行した
サブディレクトリにあるとき）は拒否します（exit 2、何も書き込みません）。書き込まない
実行ではその食い違いを表示します。ほかにも、`reviewers_extra` がリストでないとき、
有効なパネルがリストでないとき、プロジェクトのレビュアーを走らせられる CLI が
インストールされておらず `--provider` もないとき、そして他のパネルの書き込みと同じく、
書き込みによってパネルの問題が新たに生じるとき（条件付きのレビュアーしか残らない
`reviewers: []` など）は、書き込む前に拒否します。`--provider agy` は `--write` の有無に
かかわらず拒否します。

提案したレビュアーは、上の family で、そのパスに触れるすべてのコードのラウンドと、
すべての設計レビューのラウンドで走ります。リポジトリが変わったら、もう一度実行して
ください。パネルにすでにいるロールは、名前を挙げたうえで提案しません。

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

adapter は次の順に確認します。

1. インストール済みの CLI が提示しているもの — Claude は `claude --help` の alias、
   Codex は `codex debug models` と `$CODEX_HOME/config.toml`。
2. provider の現行の alias。
3. **family だけ** を並べた内蔵のフォールバック一覧（最後に確認した日付付き）。

どれでも family を検証できなければ、理由を示して実行を止めます。
`model list` で、このマシンで各情報源が何を提示しているかが分かります。

```bash
dev-orchestra model list
```

```
claude: installed
  fable    family=fable    source=cli-help
  opus     family=opus     source=cli-help
  sonnet   family=sonnet   source=cli-help
codex: installed
  CLI default (recommended coding model)  family=recommended-coding  source=cli-default
  gpt-6-astra (this CLI's configured model) family=gpt-6-astra       source=cli-config
  GPT-5.6-Terra                           family=gpt-5.6-terra       source=cli-catalog
```

`recommended-coding` は `-m` フラグを *付けない* ことで解決します。CLI 自身の現行の
既定値は、定義上いつも最新だからです。それ以外の Codex の family は、この一覧に出て
いなければなりません。adapter が受け付けるのは、CLI に設定されているモデルと、CLI 自身の
カタログが公開している slug だけで、それ以外は拒否します。adapter の詳細は
`references/providers.md` にあります。

<a id="what-the-user-asks-for-and-what-to-run"></a>

## ユーザーの依頼と実行するコマンド

コマンドの文法はスキルが持っています。こちらはその表現集です。

| ユーザーの発言 | 実行するもの |
| --- | --- |
| 「設定を見せて」 | `config show` |
| 「セットアップして」/「セットアップをやり直して」 | `config setup`、または `config setup --preset quality\|standard\|fast` |
| 「最高品質で」/「費用を抑えて」 | `config set preset quality`、`config set preset fast`（[プリセット](#presets)） |
| 「どのモデルが使える？」 | `model list` |
| 「実装には Claude Opus を使って」 | `config set implementer.model.family opus` |
| 「architect に Codex を使わせて」 | `config set architect.provider codex` **と**、Codex が受け付ける family |
| 「implementer がテストを実行できない」 | `config set --scope global implementer.options.permission_mode bypassPermissions`（agy では `config set --scope global implementer.options.skip_permissions true`）、またはその CLI 自身の設定でコマンドを許可リストに入れる |
| 「設計もレビューして」／「設計は必ずレビューして」 | `config set review.design.enabled true` |
| 「設計はレビューしないで」 | `config set review.design.enabled false` |
| 「plan の承認を求めないで」/ CI で実行する | `config set design.require_approval false` |
| 「Codex のセキュリティレビュアーを追加して」 | `reviewer add --provider codex --role security` |
| 「セキュリティレビュアーはリスクのある変更のときだけ走らせて」 | `reviewer set <id> --when high-risk`（[上記](#reviewers-that-run-only-on-high-risk-changes)を参照） |
| 「リスクのある変更では opus を使って」 | `reviewer set <id> --high-risk-model opus`（設計パネルには `--design` を付ける） |
| 「計画書は別のパネルでレビューして」 | `reviewer add --design …`（[独自の設計パネル](#a-design-panel-of-its-own-reviewdesignreviewers)を参照）、その後 `reviewer list --design` |
| 「セキュリティレビュアーは常に走らせて」 | 既定でそうなります。security の席が休むのは `relevance: security` のときだけで、`reviewer set <id> --relevance default` で外せます。どの席でも `reviewer set <id> --relevance always`。毎ラウンドすべてのロールを走らせるなら `config set optimization.skip_unneeded_roles false` |
| 「レビュアーを 3 人にして」 | もう一度 `reviewer add …`、その後 `reviewer list` |
| 「パフォーマンスのレビュアーを外して」 | `reviewer remove performance` |
| 「2 番目のレビュアーを変えて」 | `reviewer set 2 --provider … --role …` |
| 「このプロジェクトだけ」 | 書き込み系のコマンドに `--scope project` を付ける |
| 「自分の設定を元に戻して」 | `config reset --scope global`（あなたの上書きをクリアし、プリセットは残します。このマシンに合わせたプリセットだけが残ります） |
| 「このプロジェクトの上書きを捨てて」 | `config reset --scope project`（以後そのプロジェクトはグローバルレイヤーとそのプリセットに従います） |
| 「設定が古いバージョンのものだ」 | `config prune --dry-run`、その後 `config prune` |
| 「環境をチェックして」 | `doctor` |
| 「いつも日本語で返事して」（ほかの言語でも） | `config set language.reply ja`。その言語のタグで指定します（`ko`、`zh-TW`、`fr` など）。Claude Code のユーザー設定にフックが加わることを先にユーザーに伝えます。`config set language.reply null` で、ユーザーが書いている言語に戻り、フックも外れます |
| 「返答を書き直させないで」 | `config set language.rewrite false`（リマインダーは残ります） |

書き込みの後は必ず結果の設定を表示し、ユーザーが確認できるようにしてください。

<a id="editing"></a>

## 編集

```bash
dev-orchestra config show                    # effective configuration
dev-orchestra config show --scope project    # just the project layer
dev-orchestra config setup                   # interactive wizard
dev-orchestra config setup --preset standard # non-interactive, a preset fitted to the installed CLIs
dev-orchestra config setup --defaults        # non-interactive, recommended values
dev-orchestra config set implementer.model.family sonnet
dev-orchestra config set --scope project architect.provider codex
dev-orchestra config set reviewers[1].role security
dev-orchestra config reset                   # clear this layer's overrides (the global file keeps its preset)
dev-orchestra config reset --delete          # remove the file entirely
dev-orchestra config prune                   # drop values equal to what is inherited
dev-orchestra config validate
```

**保存された設定には、あなたが設定したものだけが入ります。** それ以外はすべて、設定の
読み込み時に下のレイヤーから解決されます。そのため、後のリリースで改善されたデフォルトは、
セットアップを実行した日の時点のコピーに隠されることなく、あなたの環境に届きます。したがって
`config setup --defaults` は `version: 1` だけを書き込みます: 推奨設定を選ぶということは、
何も上書きしないことを選ぶということで、そのファイルはプリセット `standard` で動きます。
`config setup --preset <name>` は `version` と `preset` を書き込み、プリセットが決めるキー以外に
ファイルが持っていた値は残します。`review.design.reviewers` のリストは消え、
`review.design.reviewers_extra` は残ります。ロールから消すのは `provider` と `model` だけです:
`options` や `model_tiers` が残るロールは設定されたままなのでフィットされず、表示される note が
その旨を伝えます。`config show --scope global|project` はそのレイヤーを
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

値が削除されるのは、組み込みのデフォルトとこのマシンでのプリセットのフィットが、どちらもその値で
一致するときだけです。そのため prune で有効な設定が変わることはありません。プリセットが決めるキーは、
ファイルが設定しなくなるとフィットの値になります: `quality` の下では `optimization.level: balanced`
やデフォルトの `opus` の implementer は残り、このマシンのフィットとだけ等しいロールやパネルも
残ります。ロールは丸ごと比較します。最後のフィールドを消すと、そのロールはフィットに渡るからです。

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
まず下のレイヤーからリストの残りをコピーします -- グローバルレイヤーなら組み込みのデフォルトと
プリセットのフィットから、プロジェクトレイヤーならグローバルレイヤーから。そのため、プロジェクトのパネルがあなたの
グローバルファイルに入り込むことはありません。リストの末尾を超えるインデックスは新しいエントリには
ならず、エラー（exit 2）になります。`reviewers_extra[0].role` は例外で、ファイル自身の extra を
編集し、何もコピーしません。`reviewers[...]`、`reviewers_extra[...]`、
`review.design.reviewers...` のパスは書き込む前に検査され、
新たに生じるパネルの問題は、書き込んで警告するのではなく拒否されます（exit 2）。

<a id="the-wizard"></a>

### ウィザード

初めて使うとき、スキルは設定がないことに気づいてウィザードを起動します。

```
AI Development Orchestrator setup

Detected CLIs:
  claude:  installed
  codex:   installed

Preset (fitted to the CLIs found above):
  1) quality  -- strongest models, Codex beside Claude on both panels, design review auto
  2) standard -- the built-in defaults: general on Claude and Codex, security on sonnet (opus on high-risk changes), test on sonnet (recommended)
  3) fast     -- lighter models, one reviewer plus a sonnet security one on high-risk changes; design review off
  4) customise each role
Choice [2]: 4

1. Orchestrator
   CLI:
     1) Claude Code (2.1.x) (recommended)
     2) Codex CLI (0.154.x)
   Model:
     1) sonnet [cli-help] (recommended)
     2) opus [cli-help]
     3) fable [cli-help]
     4) custom (type a family or exact model id)
...
5. External Reviewers
   How many reviewers? [4]
   reviewer #1  CLI / Model / Review role / id
   reviewer #2  CLI / Model / Review role / id
   reviewer #3  CLI / Model / Review role / id
   reviewer #4  CLI / Model / Review role / id
   Add another reviewer? [y/N]

Configuration
  Orchestrator    claude / sonnet / latest
  Architect       claude / fable  / latest
  Implementer     claude / opus   / latest
  Review Fixer    claude / opus   / latest
  Reviews
    1. claude / opus / latest / general / claude-general
    2. codex / recommended-coding / latest / general / codex-general
    3. claude / sonnet / latest / security / claude-security (opus when high-risk)
    4. claude / sonnet / latest / test / claude-test
    design review: auto  (review.design.enabled)
    optimization level: balanced  (optimization.level)
    skip unneeded roles: on  (optimization.skip_unneeded_roles)
    plan approval: required  (design.require_approval)
  Design reviews
    (the code panel; when conditions ignored)  (review.design.reviewers)

Save configuration? [Y/n]
```

ここでの設計パネルがコードのパネルのコピーなのは、回答が `reviewers` として保存されるからです。
プリセットをそのまま保存すれば、フィットした設計パネルが表示されます。

グローバルファイルでは、最初の質問がプリセットです。プリセットを選ぶと、それがこのマシンで
解決される設定（フィットした設計パネルを含む）と、フィットし直した点の note を表示して
`Save as is?` と尋ねます: yes なら、プリセットが決めるキー（`review.design.reviewers` を含む）
以外にファイルが持っていた値と一緒に `version` と `preset` を保存します。
no なら、プリセットのフィットを出発点にロールとレビュアーの質問に進み、そこから変えた点だけを
保存します: 変えたロールは丸ごと、変えたパネルはリストとして保存され、提示されたままにした
ものはすべてプリセットに従い続けます。リストとして保存したコードのパネルは、設計ラウンドも
プリセットの設計パネルから外すので、ウィザードはそう伝えます（`note: the reviewers differ from
preset standard's fit, so they are saved; design rounds then run them without when, not the
preset's design panel`）。レビュアーの質問は、CLI を変えない限りその席の `high_risk_model` を
残します。別の CLI を選ぶと、`reviewer set --provider` と同じく、1 行（`note: provider is now
codex; its high_risk_model was removed (reviewer set --high-risk-model sets another)`）を出して
high-risk のモデルを外します。`relevance` はロールを変えない限り残します（`always` はどのロールでも
残ります）。別のロールを選ぶと、1 行（`note: role is now general; its relevance security was
removed (reviewer set --relevance sets another)`）を出して外します。規則は 1 つのロールの仕事に
ついてのもので、general の席は規則を持たないからです。ファイルのそれ以外の設定は、デフォルトと等しい値も含めて
そのまま残ります。`customise each role` はプリセット導入前のウィザードと
同じで、すべての回答が保存されます。プロジェクトファイルでは、プリセットの質問はしません。

ウィザードは `reviewers_extra` について尋ねず、書き込みもしません: プリセットを選んだときも含めて、
ファイルはこのキーを持っていたとおりに残し、レビュアーの質問の前の 1 行がその旨を伝えます
（`This file's reviewers_extra (<ids>) is kept as it is; reviewer add/remove manage it.`）。
設計パネルについても尋ねません: `review.design.reviewers` とその extra はファイルが持っていた
とおりに残し、1 行がその旨を伝えます（`This file's review.design.reviewers is kept as it is; reviewer
add/set/remove --design manage it.`）。保存前のサマリーはどれも `load()` が解決する設定で、extra には
`(extra, global file)` の印が付き、設計パネルは `Design reviews` の下に表示されます。高リスク用の
モデルを持つ席は `(opus when high-risk)` と表示され、`relevance` を持つ席はそれが表示されます。レビュアーの質問への回答はこれまでどおり `reviewers` として保存され、パネルはそれに
従います。継承するパネルに従い続けたままレビュアーを足すには `reviewer add` を使ってください。

プリセット導入前にウィザードが書き込んだファイルには、すべてのロールとパネルが入っているので、
そこに `preset:` を足しても、届くのはファイルが設定していないものだけです。`config reset` は
それらを消して `preset` を残します。`config prune` が値を削除するのは、組み込みのデフォルトと
プリセットのフィットがどちらもその値で一致するときだけです。

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

**設計と独立レビューを別ベンダーに** — これを書いた時点で両 CLI が提供していた
モデルに当てはめた構成です。

```yaml
version: 1

orchestrator:
  provider: codex
  model:
    family: gpt-5.6-sol
    version: latest

architect:
  provider: claude
  model:
    family: fable
    version: latest

implementer:
  provider: claude
  model:
    family: opus
    version: latest

review_fixer:
  provider: claude
  model:
    family: opus
    version: latest

reviewers:
  - id: claude-review
    provider: claude
    model:
      family: opus
      version: latest
    role: general
  - id: codex-independent
    provider: codex
    model:
      family: gpt-5.6-terra
      version: latest
    role: general
```

コピーする前に `dev-orchestra model list` で family を確認してください。モデル名は
変わりますし、両 CLI はバージョンやアカウントによって提供するモデルが異なります。
各 adapter はインストール済みの CLI が認めない family を推測せずに拒否するので、
古い名前は黙って別のものを動かすのではなく、セットアップ時にはっきり失敗します。
大事なのは個々の family より形です。同じ系列の 2 つのモデルは、バグを生んだ盲点を
共有してしまいます。

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
アンカー、エイリアス、タグ、マージキー（`<<`）、複数ドキュメントのストリーム、ブロックスカラー
（`|`、`>`）は、わかりやすいエラーとともに拒否されます。YAML が予約している文字で始まる
引用符なしの値（`*.sql`、`&x`、`!x`、`@x`）も拒否されるので、引用符で囲んでください。
シーケンスの項目は `-` の後ろに空白をいくつ置いてもかまいませんが、項目の 2 行目以降は
1 行目と列を揃えてください。JSON は常に受け付けます。

ダブルクォートの中では、YAML と同じくバックスラッシュがエスケープの始まりです。Windows の
パスは `"C:\\work\\new"` と書くか、シングルクォート（`'C:\work\new'`）で囲んでください。
`"C:\work"` の `\w` のように YAML が定めていないエスケープは、そのまま残さずに拒否されます。
インデントには空白を使ってください。インデントのタブは拒否され、値の中のタブはそのまま残ります。
設定を書き出すコマンドは、数字や日付に見える文字列（`"123"`、`"1.0"`、`"2026-10-06"`）を
クォートするので、読み戻しても文字列のままです。
