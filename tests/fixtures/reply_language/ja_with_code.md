設定を変えました。次のコマンドで確かめられます。

```sh
$ python scripts/dev_orchestra.py config set language.reply ja
language.reply = 'ja'  (global: ~/.config/dev-orchestra/config.yaml)
Everything else in this block is output that stays exactly as the tool printed it, in English.
```

> The configuration reference says: "Set it once and every later session follows it, whatever the user writes in."

詳しくは https://github.com/istb16/dev-orchestra/blob/main/references/configuration.md#language を見てください。設定ファイルは ~/.config/dev-orchestra/config.yaml です。"Reply language" という行が doctor の出力に出ます。`--fast` を付けても表示されます。

PS> dev-orchestra doctor --fast --json
$ git status --short

<!-- reviewers never see this comment, which is written in English on purpose to test the stripping -->
問題があれば知らせてください。
