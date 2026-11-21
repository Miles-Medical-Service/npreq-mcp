# Contributing

Issues and pull requests are welcome. Report a security problem as `SECURITY.md` describes, not in a public issue.

## How a pull request is merged

`main` is protected by the ruleset `main`: a pull request needs an approving review from the code owner
(`.github/CODEOWNERS`), and nothing is pushed to `main` directly.

Pull requests opened by the maintainers' own tooling are authored by the code owner's account. GitHub does not let
an author approve their own pull request, so those pull requests can never get the required review on GitHub. For
them the review happens before the merge, outside GitHub: an independent review approves the exact head commit, and
a separate go-ahead names that same head. The code owner then merges with **Merge without waiting for requirements
to be met (bypass rules)**, at that head and only then. The bypass is the intended route for these pull requests,
not an exception, and the merge is the code owner's own sign-off.

A pull request from anyone else gets the code owner's review on GitHub as usual.
