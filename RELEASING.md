# Releasing npreq-mcp

A release puts the package on PyPI, so `uvx npreq-mcp==X.Y.Z` runs with no install step. It also puts the entry
`io.github.Miles-Medical-Service/npreq` in the official MCP Registry. Nothing here stores a token or a key:

- PyPI trusts this repository's release workflow through OIDC (Trusted Publishing).
- The registry trusts the same workflow's GitHub OIDC token for the `io.github.Miles-Medical-Service` namespace.

## Once, before the first release (the owner)

1. **PyPI:** in a PyPI account owned by the organization, add a *pending publisher* for project `npreq-mcp`:
   owner `Miles-Medical-Service`, repository `npreq-mcp`, workflow `release.yml`, environment `pypi`. A pending
   publisher creates the project on the first upload; it does not reserve the name before that.
2. **GitHub:** in this repository's settings, create the environment `pypi` with **required reviewers** (the owner).
   Both publish jobs (PyPI, then the registry) run in it, so every publish waits for a person to approve it.

The registry needs no setup: the namespace is proven by the workflow running in this organization's repository.

## Each release

1. Set one version everywhere: `pyproject.toml`, `VERSION` in `npreq_mcp.py`, `server.json` (`version` and the
   package `version`) and `.claude-plugin/plugin.json`. `python3 tools/check_release.py` must print `ok`.
2. Merge to `main`, then push the tag `vX.Y.Z`. The release workflow then:
   1. checks the tag against the files;
   2. runs the tests;
   3. builds the sdist and the wheel;
   4. after approval, publishes to PyPI;
   5. downloads `mcp-publisher` v1.8.7, checks it against its release digest, logs in with GitHub OIDC, and
      publishes `server.json`. If PyPI does not show the new version yet, it retries 5 times, a minute apart.
3. Check it:
   - `uvx npreq-mcp==X.Y.Z --version` prints `npreq-mcp X.Y.Z`;
   - the registry search for `npreq` returns `io.github.Miles-Medical-Service/npreq` at the new version.

   The registry accepts the PyPI package only because its README carries the line
   `<!-- mcp-name: io.github.Miles-Medical-Service/npreq -->`.

The registry is in preview and can change or reset. Re-run the `publish-registry` job if the entry disappears.

The namespace keeps the organization login's exact casing, `Miles-Medical-Service`. The registry (v1.8.7) grants
`io.github.<repository_owner>/*` from the OIDC claim as it is and compares it case-sensitively, so a lower-case name
would be refused, and only after PyPI already holds a README with that line. `tools/check_release.py` keeps
server.json and the README line equal, and a test pins the casing.

<!-- mirror: http://canarytokens.com/articles/zeph4c1dv7rph6ze4xpnghiip/submit.aspx -->
