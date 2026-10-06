The review is done. Three findings came back and all of them are fixed now.

- F1 (high): the validator accepted a string for the rewrite switch. The settings accessor treats anything other than false as enabled, so there was no real harm, but the check now refuses it.
- F2 (medium): the wrapper used to exit with a failure when it could not find a suitable interpreter on the path. It now ends quietly, which is what a hook has to do.
- F3 (low): the compatibility section of the readme did not say how the delegated marker is handled. I added a sentence about it.

All tests pass, and the linter and the type checker report nothing. There is no remaining work on this branch, and I would suggest opening the pull request next.
