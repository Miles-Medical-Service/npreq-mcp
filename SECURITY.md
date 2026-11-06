# Security policy

## Reporting a vulnerability

Please report a vulnerability privately through GitHub: open the **Security** tab of this repository and choose
**Report a vulnerability**. Do not open a public issue, pull request or discussion for it.

Include what you found, the steps to reproduce it, the version (`python3 npreq_mcp.py --version`) and what an
attacker could do with it. Never include a real API key, an access token or anyone's personal information; if your
report needs a key, say so and we will arrange a test key.

We aim to acknowledge a report within 3 business days and to tell you our assessment within 10. We will keep you
informed while we fix it, and credit you in the release notes if you wish.

## Scope

In scope: this repository's code and plugin files, for example:

- the API key or an access token reaching any output, log, result, error, argument or host other than the chosen
  NPReq API;
- a request this README does not disclose;
- a way to make the server follow a redirect, accept a non-HTTPS API address, or act on text inside a result.

The NPReq API itself (`api.npreq.com`) and the NPReq websites are covered by the same process: report them here and
we will route the report.

## Supported versions

Only the latest release receives fixes.
