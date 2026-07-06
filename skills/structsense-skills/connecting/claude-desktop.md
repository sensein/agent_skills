# Connecting structsense-skills to Claude Desktop

Claude Desktop is the third major Claude runtime, alongside Claude Code (CLI) and claude.ai (web). It looks like a local app on your machine, but it has a **split execution model** that surprises most users — and is the reason your local `http://localhost:8000` mapping service appears "unreachable" even when you can hit it with curl in another terminal.

## The split execution model — read this first

| What runs where | Location | Can reach your `localhost`? |
|---|---|---|
| **The Claude chat UI** | Your machine (the app window) | (Doesn't make HTTP calls) |
| **The conversation LLM** | Anthropic's cloud | No |
| **Python / Bash / code interpreter** | Anthropic's cloud sandbox (look for paths like `/home/claude/work/...`) | **No** |
| **File uploads & downloads** | Your machine ↔ the cloud sandbox over the API | (Files, not network) |
| **MCP servers you configure** | **Your machine** | **Yes** |

The giveaway in your skill output:

```
"input_path": "/home/claude/work/paper.txt"
```

That `/home/claude/work/` path is Anthropic's cloud sandbox — not your machine. Anything that runs in that sandbox cannot dial back to your `localhost:8000` because the sandbox is in a different network namespace.

**The fix is not to "make the sandbox reach my localhost"** (you can't). The fix is to **expose the mapper via an MCP server that runs on your machine**, so Claude Desktop calls your machine through MCP instead of through the sandbox.

## What you have to set up

Claude Desktop reads MCP server configuration from a JSON file. On macOS:

```
~/Library/Application Support/Claude/claude_desktop_config.json
```

On Windows:

```
%APPDATA%\Claude\claude_desktop_config.json
```

Add an entry that launches a small MCP server which proxies HTTP calls to your local `/map/batch`. The pattern is the one in [`connecting/mcp-server.md`](mcp-server.md) — here's the Claude-Desktop-specific configuration:

```json
{
  "mcpServers": {
    "structsense-skills": {
      "command": "python3",
      "args": [
        "/absolute/path/to/structsense-skills/connecting/mcp_server.py"
      ],
      "env": {
        "LOCAL_CONCEPT_MAPPING_URL": "http://localhost:8000",
        "OPENROUTER_API_KEY":        "sk-or-v1-...",
        "BIOPORTAL_API_KEY":         "..."
      }
    }
  }
}
```

Then in Claude Desktop:

1. Quit the app entirely (Cmd-Q / right-click tray → Quit). Don't just close the window.
2. Reopen. Look for the 🔌 icon in the input field — clicking it should list `structsense-skills` and its tools.
3. Ask Claude to use the tool: *"use structsense-skills/extract_ner on this paper, with the local mapper at http://localhost:8000"*.

Claude will now call the MCP server (on your machine) instead of trying to dial localhost from the cloud sandbox. The MCP server has network access to localhost and will return the mapped concepts.

## Why this is necessary even for the simple curl case

Looking at your existing output:

```
The skill's default local hybrid mapper at http://localhost:8000 isn't
reachable from this runtime (probe returned connection-refused), and no
BioPortal key is set. I used the no-key EBI OLS client instead…
```

That message is correct and honest. Claude in the cloud sandbox genuinely cannot reach your localhost. It fell back to OLS (which is on the public internet, so the sandbox CAN reach it) and was upfront about the substitution. The judge scores were the heuristic ones because no LLM API key was set in the sandbox.

To get the full pipeline behavior you want — local mapper + full LLM judge — you have three options, in order of effort:

1. **MCP bridge** (recommended) — the config above. Five minutes to set up; Claude Desktop calls your machine's services natively.
2. **Tunnel** (`ngrok http 8000`, `cloudflared tunnel`, Tailscale Funnel) — the cloud sandbox CAN reach a public URL. Set `LOCAL_CONCEPT_MAPPING_URL=https://<your-tunnel>.ngrok.io` and the existing skill code Just Works.
3. **Switch runtime to Claude Code** (CLI) — Claude Code runs Bash on your machine, no sandbox in between. The skill's existing `curl ${MAPPER_URL}/docs` probe works directly.

For a one-off run, option 2 (a tunnel) is fastest. For ongoing use, option 1 (MCP) is cleanest.

## What works without any of the above

Even without an MCP bridge or tunnel, the skill still produces useful output from Claude Desktop:

- All extraction (LLM-driven NER, mask-recall) works fully — the LLM is in the cloud already, doesn't need your machine.
- The OLS fallback (public, no key) provides ontology mapping for anatomy / cells / diseases / chemicals — anything OLS hosts.
- Heuristic judge scores (span-validation + ontology grounding + specificity) work — no API key needed.
- The normalizer canonicalizes the result (`paper_final.json` with `source_metadata`, `entities_grouped`, `stats`, `totals` block).

What you lose without the bridge:

- The local hybrid mapper's richer coverage (it tends to be tuned to your domain and has higher-fidelity matching for unusual terms).
- Gene mappings (OLS doesn't host HGNC; BioPortal or your local service does).
- Real LLM-judge scores (only matters when you specifically want a separate model to score the alignment).

## Recognizing a Claude Desktop run in your output

If you're not sure which runtime produced an output file, look for these signs:

| Signal | Runtime |
|---|---|
| `input_path: /home/claude/work/...` | Claude Desktop or claude.ai web (cloud sandbox) |
| `input_path: /Users/<you>/...` or `/home/<you>/...` | Claude Code (your machine) |
| `source_model: llm_ner:claude-opus-4-8` (or similar Anthropic model) and no `source_model: <hf_id>` items | LLM-only extraction, no HF ensemble (the HF models require `transformers` + weights, usually only present on your machine) |
| `alignment.cascade_history: ["ols"]` | Public fallback used; the local mapper wasn't reachable |
| `alignment.mapper_used: "local_hybrid"` with `mapper_url: "http://localhost:..."` | Local mapper reached (via Claude Code or MCP bridge) |

## Troubleshooting

| Symptom | Likely cause | Fix |
|---|---|---|
| The MCP server doesn't show up after editing the config | App not fully quit, or JSON syntax error | Quit and reopen; validate JSON with `jq . < claude_desktop_config.json`. |
| MCP server appears but tools fail with "connection refused" | The MCP process can't reach the local mapper either (firewall, wrong port) | From a terminal: `curl http://localhost:8000/docs` — if that fails, the mapper isn't running where you think. |
| Claude says "local mapper unreachable" even with MCP configured | Claude is still trying the sandbox path. Be explicit in your message. | "Use the structsense-skills/extract_ner MCP tool" — naming the tool forces the MCP route. |
| You get the OLS-fallback output anyway | Same as above + the model wasn't told to insist on the local mapper | Add to your message: "Do NOT fall back to OLS. If the MCP tool fails, ask me for an alternate URL." |
| Output has `paper_title`/`doi` on every entity | Old skill version, or the model emitted the legacy shape | Run `python -m scripts.normalize_result <file> --input <text> --llm-model <model>` — idempotent. |
