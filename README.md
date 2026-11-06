# npreq-mcp: the NPReq API as MCP tools

<!-- mcp-name: io.github.Miles-Medical-Service/npreq -->

NPReq gives nurse practitioners and the tools they use curated NP/APRN state requirement data for 56 US
jurisdictions: practice authority, licensure, continuing education (CE) rules, renewal, prescribing and board
contacts, each value with the official source it came from. This repository holds two things:

- **npreq-mcp**, a local MCP server (one Python file, standard library only) that gives AI agents read-only access to
  that data with **your own NPReq API key**;
- **the `npreq` plugin for Claude Code**, which runs that server, asks for your key once and keeps it in your
  system's credential store, and adds a skill that tells Claude when to use each tool.

NPReq data is for information. It is **not for clinical or licensing decisions**: always confirm a requirement with
the state board of nursing before you act on it.

You need an NPReq API key (https://npreq.com/) and Python 3.10 or newer as `python3` on your PATH.

## Install the plugin (Claude Code)

The plugin needs **Claude Code v2.1.271 or later** and works in **Claude Code only**. (Cowork skips a plugin server
whose required, sensitive setting has no default, and an API key cannot have one.)

```
/plugin marketplace add Miles-Medical-Service/npreq-mcp
/plugin install npreq@npreq
```

On Claude Code 2.1.275 or later, one line does both: `/plugin install npreq --marketplace Miles-Medical-Service/npreq-mcp`.

**On Windows**, the plugin starts the server with `python3`. On many Windows machines `python3` is only the Microsoft
Store alias, which opens the Store instead of running Python. Before you enable the plugin, run `python3 --version`
in a terminal: it must print Python 3.10 or newer. If it does not, install Python from python.org or the Microsoft
Store, or turn off the `python3.exe` App Execution Alias (Settings > Apps > Advanced app settings) once a real
`python3` is on your PATH.

When the plugin is enabled, Claude Code asks for two settings:

| Setting | What it is |
|---|---|
| NPReq API key | Your key (`npq_live_...`, or `npq_test_...` for staging). Masked when typed and stored in your system's credential store, not in a settings file. |
| NPReq API | A choice of `https://api.npreq.com` (the default) or `https://api-staging.npreq.com`. There is no free-text address, because the key is sent there. |

The plugin passes these two values to the server. It does not read an API key that is already set in your shell.
The API choice also appears in `/config`; the key does not, because it is a sensitive setting.

The tools then appear as `mcp__plugin_npreq_npreq__<tool>`.

## The tools

Every tool is exactly one read-only request to the NPReq REST API. No tool writes anything.

| Tool | Request it makes | Scope your key needs | Arguments |
|---|---|---|---|
| `get_data_version` | `GET /v1/meta` | none (public) | none |
| `list_states` | `GET /v1/states` | `states:read` | none |
| `get_state` | `GET /v1/states/{code}` | `states:read` | `code`: two capital letters (`TX`, `PR`) |
| `get_state_ce` | `GET /v1/states/{code}/ce` | `ce:read` | `code`: two capital letters |
| `search_requirements` | `GET /v1/search?q=` | `search` | `q`: 2 to 64 letters, digits, spaces, hyphens, apostrophes or periods |

A result is the API's JSON reply. An API error comes back as a tool error with the HTTP status, the API's error code
and a fixed message; for `429` and `503` it says how many seconds to wait.

## Everything the plugin runs, sends and fetches

- **Runs:** `python3 <plugin folder>/npreq_mcp.py`, a local process that talks to Claude Code over stdin and stdout.
  It runs nothing else, installs nothing, and writes no files.
- **Sends, only to the API you chose (`https://api.npreq.com` or `https://api-staging.npreq.com`), over HTTPS:**
  1. `POST /v1/token` with your API key in the `Authorization` header. This is the only request that carries the
     key. The reply is an access token that lasts 6 hours; the server keeps it in memory only and asks for a new one
     when fewer than 5 minutes are left, or once after a data request is refused with `401`.
  2. The five `GET` requests in the table above, each with the access token (never the key) in the `Authorization`
     header, plus the state code or search text you or Claude passed as the tool's argument.

  Every request also carries `Accept: application/json` and `User-Agent: npreq-mcp/<version>`.
- **Fetches:** only the API's JSON replies to those requests (at most 16 MB each). It never follows a redirect,
  since a redirect would carry the `Authorization` header to another address, and it checks the TLS certificate.
- **Nothing else:** no telemetry, no analytics, no update checks, no other host.

## Privacy

- The NPReq API receives your key (at `/v1/token` only), the tool arguments (a state code or a search text), your
  IP address and the `User-Agent` above. It counts your requests against your plan's minute limit and monthly
  quota. What NPReq keeps and for how long is in the privacy policy: https://npreq.com/privacy.html. Use of the API
  is under the terms: https://npreq.com/terms.html.
- Do not put personal or patient information in a search: the search text is sent to the API.
- The server never writes your key or a token to its output, its log (one line per tool call, with the tool name and
  the status, on stderr), a tool result or an error. It takes the key out of its own environment once it has read it.
- Results are curated requirement data with sources. The tools and the skill tell Claude to treat them as data, not
  instructions.

## What happens when something is refused

- **A refused key is held.** Once `/v1/token` answers `401` or `403`, the server sends nothing more with the key.
  Every tool that needs a token answers at once with that status, and only `get_data_version` keeps working until
  you fix the key (or its IP allowlist) and restart. This is because every refusal counts toward the API's lockout
  of your IP address (10 in 10 minutes locks it for 15 minutes), so a retrying client can never lock you out.
- A `429` or `5xx` at `/v1/token` is temporary and is not held.
- A revoked key, a lapsed subscription, or a scope your plan lacks stops the tools on the next call: the API checks
  the key behind every token on every request.

## Without the plugin

Any MCP client that starts a local stdio server can run `npreq_mcp.py` directly. Set `NPREQ_API_KEY` in that
client's server configuration (never on a command line or in a prompt), and optionally `NPREQ_API_BASE`, which
accepts only an `https://` origin and defaults to `https://api.npreq.com`. For example, in Claude Desktop's
`claude_desktop_config.json`:

```json
{
  "mcpServers": {
    "npreq": {
      "command": "python3",
      "args": ["/path/to/npreq-mcp/npreq_mcp.py"],
      "env": { "NPREQ_API_KEY": "<your key>" }
    }
  }
}
```

On Windows, use `python` (or `py -3`) instead of `python3`, and a full path with forward slashes. Installed with
`pip install .` from this folder, the server is also the `npreq-mcp` command.

The NPReq API documentation, with more client setups, is at https://account.npreq.com/account/docs.

## Protocol

Newline-delimited JSON-RPC 2.0 on stdio. A request whose `_meta` carries `io.modelcontextprotocol/protocolVersion`
`2026-07-28` is served statelessly by that revision. An `initialize` request selects the handshake of `2025-11-25`,
`2025-06-18` or `2025-03-26` for the life of the process. Any other version gets error `-32022` with the supported
list. The server offers tools only: no resources, prompts, sampling or elicitation.

## Tests

`python3 tests/test_npreq_mcp.py` runs on Python 3.10 and 3.13 with no network: the API is a fake, and the
child-process tests point the server at an address that refuses at once. They cover:

- the five routes and their argument checks;
- the error map;
- token renewal and the single retry, and the refused key held for the session;
- that redirects are never followed;
- that the key never appears in output, logs, results or arguments;
- the plugin's settings, and that the key the plugin passes wins over one set in the shell.

## Security and licence

Report a vulnerability as described in [SECURITY.md](SECURITY.md); please do not open a public issue for it.
MIT licence: see [LICENSE](LICENSE).
