# Connecting structsense-skills as an MCP server

The Model Context Protocol (MCP) lets you expose `scripts/pipeline.py` as a tool callable from any MCP-aware client: Claude Code, ChatGPT desktop, the Anthropic SDK, the OpenAI Agents SDK, Cursor, and others. This guide shows the simplest setup.

## What you get

An MCP server with these tools:

- `extract_ner` — full NER pipeline (extract → mask-recall → align → judge → group → stats).
- `extract_resources` — research-resource extraction.
- `extract_structured` — schema-driven extraction.
- `normalize_result` — repair a legacy result JSON.

The MCP server runs as a long-lived process. Clients connect to it (over stdio, HTTP, or SSE depending on transport) and call tools by name.

## Option A: Minimal stdio server with `mcp` Python SDK

```bash
pip install mcp openai anthropic json-repair jsonschema requests
# Optional for the HF ensemble:
pip install transformers torch
```

Create `connecting/mcp_server.py` (referenced here; not included so you can adapt freely):

```python
"""Minimal MCP server exposing the structsense-skills pipeline.

Run with:
    python connecting/mcp_server.py
or via Claude Code's mcpServers config (see below).
"""
import asyncio, sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
from pipeline import run
from normalize_result import normalize

from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import Tool, TextContent
import json

server = Server("structsense-skills")


@server.list_tools()
async def list_tools() -> list[Tool]:
    return [
        Tool(
            name="extract_ner",
            description=("Extract named entities and key terms from a passage "
                         "using the structsense-skills pipeline. Returns a "
                         "result with source_metadata at top level, "
                         "entities[] (one per occurrence), entities_grouped[] "
                         "(per-entity index), and stats."),
            inputSchema={
                "type": "object",
                "required": ["text", "extractor_model"],
                "properties": {
                    "text":            {"type": "string"},
                    "extractor_model": {"type": "string"},
                    "judge_model":     {"type": "string"},
                    "mapper":          {"type": "string", "enum": ["local","bioportal","ols","none"], "default": "local"},
                    "mapper_url":      {"type": "string", "default": "http://localhost:8000"},
                    "ner_profile":     {"type": "string"},
                    "chunk_size":      {"type": "integer", "default": 2000},
                    "max_workers":     {"type": "integer", "default": 8},
                    "paper_title":     {"type": "string"},
                    "doi":             {"type": "string"},
                },
            },
        ),
        Tool(
            name="normalize_result",
            description="Normalize a legacy structsense-skills result JSON to "
                        "the canonical 0.3.0 shape. Idempotent.",
            inputSchema={
                "type": "object",
                "required": ["result"],
                "properties": {
                    "result":     {"type": "object"},
                    "llm_model":  {"type": "string"},
                    "input_path": {"type": "string"},
                },
            },
        ),
    ]


@server.call_tool()
async def call_tool(name: str, arguments: dict) -> list[TextContent]:
    if name == "extract_ner":
        result = run(
            text=arguments["text"],
            task="ner",
            extractor_model=arguments["extractor_model"],
            mapper_backend=arguments.get("mapper", "local"),
            judge_model=arguments.get("judge_model"),
            chunk_size=arguments.get("chunk_size", 2000),
            max_workers=arguments.get("max_workers", 8),
            skip_judge=arguments.get("judge_model") is None,
            local_mapping_url=arguments.get("mapper_url", "http://localhost:8000"),
            ask_user=None,                  # MCP can't prompt
            input_path=None,
            ner_ensemble_profile=arguments.get("ner_profile"),
        )
        # Inject the caller-provided paper_title / doi if any
        if arguments.get("paper_title") or arguments.get("doi"):
            sm = result.setdefault("source_metadata", {})
            if arguments.get("paper_title"): sm["paper_title"] = arguments["paper_title"]
            if arguments.get("doi"):         sm["doi"]         = arguments["doi"]
        return [TextContent(type="text", text=json.dumps(result, indent=2, default=str))]

    if name == "normalize_result":
        result = arguments["result"]
        normalize(result,
                  llm_model=arguments.get("llm_model"),
                  input_path=arguments.get("input_path"))
        return [TextContent(type="text", text=json.dumps(result, indent=2, default=str))]

    return [TextContent(type="text", text=json.dumps({"error": f"unknown tool {name}"}))]


async def main():
    async with stdio_server() as (read, write):
        await server.run(read, write, server.create_initialization_options())


if __name__ == "__main__":
    asyncio.run(main())
```

## Option B: Configure in Claude Code

Add to your Claude Code MCP config (`~/.claude/mcp.json` or your project's `.claude/settings.json`):

```json
{
  "mcpServers": {
    "structsense-skills": {
      "command": "python",
      "args": ["/abs/path/to/structsense-skills/connecting/mcp_server.py"],
      "env": {
        "OPENROUTER_API_KEY": "${OPENROUTER_API_KEY}",
        "ANTHROPIC_API_KEY":  "${ANTHROPIC_API_KEY}",
        "BIOPORTAL_API_KEY":  "${BIOPORTAL_API_KEY}"
      }
    }
  }
}
```

Restart Claude Code. The tools `extract_ner` and `normalize_result` should now be available — you'll see them in `/tools` or when Claude offers tool use.

## Option C: Run as an HTTP server (for ChatGPT Custom GPT Actions)

For the OpenAI Custom GPT integration (see `connecting/custom-gpt.md`), wrap the same calls in a FastAPI app:

```python
# connecting/http_server.py
from fastapi import FastAPI
from pydantic import BaseModel
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
from pipeline import run

app = FastAPI(title="structsense-skills", version="0.3.0")


class ExtractReq(BaseModel):
    text: str
    task: str = "ner"
    extractor_model: str
    judge_model: str | None = None
    mapper: str = "local"
    mapper_url: str = "http://localhost:8000"
    ner_profile: str | None = None
    chunk_size: int = 2000
    max_workers: int = 8
    paper_title: str | None = None
    doi: str | None = None


@app.post("/extract")
def extract(req: ExtractReq):
    result = run(
        text=req.text,
        task=req.task,
        extractor_model=req.extractor_model,
        mapper_backend=None if req.mapper == "none" else req.mapper,
        judge_model=req.judge_model,
        chunk_size=req.chunk_size,
        max_workers=req.max_workers,
        skip_judge=req.judge_model is None,
        local_mapping_url=req.mapper_url,
        ask_user=None,
        input_path=None,
        ner_ensemble_profile=req.ner_profile,
    )
    if req.paper_title or req.doi:
        sm = result.setdefault("source_metadata", {})
        if req.paper_title: sm["paper_title"] = req.paper_title
        if req.doi:         sm["doi"]         = req.doi
    return result
```

Run it:

```bash
pip install fastapi uvicorn
uvicorn connecting.http_server:app --host 0.0.0.0 --port 8080
```

Add a Custom GPT Action pointing at `http://your-host:8080/extract`. See `connecting/custom-gpt.md` step 5 for the OpenAPI spec.

## API keys

The MCP / HTTP server reads keys from its own env — never from arguments. Set them in the server's launch env:

```bash
export OPENROUTER_API_KEY=sk-or-v1-...
export BIOPORTAL_API_KEY=...
```

Or pass through `mcpServers.<name>.env` in the Claude Code config (above), which expands `${VAR}` from the shell that launched Claude Code.

## Why MCP and not just a CLI?

- **Persistent process**: HF model weights load once, not per request.
- **Multiple clients**: Claude Code, Cursor, ChatGPT desktop, your own scripts can all talk to the same instance.
- **Strong typing**: the JSON Schemas declared on each tool prevent the kinds of accidental misuse (wrong model strings, missing required fields) that bug LLMs.
- **Streaming-friendly**: long-running ensemble runs can stream progress back to the client.

Skip MCP if you only call the pipeline from one Python script — the CLI is simpler.

## Verifying the connection

From Claude Code, after restarting:

> "list MCP tools"

You should see `structsense-skills/extract_ner` and `structsense-skills/normalize_result`.

> "use the structsense-skills extract_ner tool on this text: [paste]"

The result should arrive as a single JSON blob with `source_metadata`, `entities`, `entities_grouped`, `stats`, etc.
