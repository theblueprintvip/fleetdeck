# Redline MCP server

Contract scanning, redlining, a clause library and version comparison for Claude, on the same
14-trap engine as the Redline web app, SMS and email.

## Install

```
pip install -r requirements.txt
claude mcp add redline -- python /absolute/path/to/redline/mcp-server/server.py
claude mcp list
```

Claude Desktop or a project `.mcp.json`:

```json
{
  "mcpServers": {
    "redline": { "command": "python", "args": ["/absolute/path/to/redline/mcp-server/server.py"], "env": {} }
  }
}
```

Over HTTP instead of stdio: `python server.py --transport streamable-http --port 8000` (endpoint `/mcp`).

## Try it in Claude

- Analyze this contract with Redline: [paste contract]
- Redline this, aggressive tone, buyer side
- Give me a limitation of liability clause
- Compare v1 vs v2 - what got better?

## Tools

| Tool | What it does |
| --- | --- |
| `analyze_contract(text, side, country)` | Risk 12–99, every trap with the triggering sentence and a safe rewrite |
| `redline_contract(text, side, tone)` | Strike-and-insert redline plus the clean text; `tone` is soft, balanced or aggressive |
| `get_clause(clause_type, side, country)` | Ready-to-use clauses: liability, indemnity, termination, confidentiality, IP, payment, law, disputes, renewal, non-solicitation, force majeure, assignment |
| `compare_versions(v1, v2, side)` | Traps fixed and introduced, risk change, sentences added and removed |
| `country_note(country)` | Legal-system note for 195 countries |
| `list_traps()` | The 14 traps and their weights |

`side` is `buyer` (the paying customer), `seller` (the provider) or `neutral`. A clause that only
hurts the other side, such as an IP assignment when you are the buyer, is not counted against you.

`kb.json` is generated from the web app's engine by `node scripts/build-kb.mjs`; don't edit it by hand.

Redline flags common contract risks. It is not legal advice.
