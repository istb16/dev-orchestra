レビューが終わりました。結果は以下のとおりです。

The review panel returned three findings. The first one is about the validator, which accepted a string where a boolean was expected; the accessor treats anything other than false as enabled, so the behaviour was right, but the check is now strict. The second one is about the wrapper script, which used to exit with a failure when it could not find a suitable interpreter on the path, and now ends quietly instead. The third one asked for a sentence in the compatibility section of the readme about how the delegated marker is handled, and that sentence has been added as well.

All tests pass, and the linter and the type checker report nothing at all on the changed files.
