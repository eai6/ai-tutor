"""Annotator agent orchestrator — local v0.

Runs Claude (Sonnet 4.6 by default) in an agent loop, with
chrome-devtools-mcp registered as a tool source. The agent navigates a
running Django dashboard via Chrome and fills annotation forms exactly
the way a human admin would.

This is the local prototype that proves the agent loop works before we
containerize for GitHub Actions. See:
    memory/automated_annotator_agent_plan.md (Phase 2)
    ops/annotator_agent/agent_prompt.md (system prompt)

USAGE:
    # Boot the dev server first:
    venv/bin/python manage.py runserver 8000

    # Then run the agent (in a second shell):
    venv/bin/python -m ops.annotator_agent.orchestrator \\
        --base-url http://127.0.0.1:8000 \\
        --max-items 1 \\
        --persona struggler

REQUIREMENTS:
    - ANTHROPIC_API_KEY in env
    - Local admin login credentials in env (ANNOTATOR_LOGIN_USER /
      ANNOTATOR_LOGIN_PASS) — defaults match the local dev super-admin.
    - chrome-devtools-mcp installed (`npx @modelcontextprotocol/...`
      runs on demand).

DESIGN:
    - We spawn chrome-devtools-mcp via stdio, list its tools, and pass
      them to Claude as native tool definitions.
    - The agent loop iterates: send messages → Claude responds with
      tool_use blocks → we route each to the MCP server → send results
      back → repeat until Claude stops requesting tools.
    - One JSON-line transcript per agent step is written to
      ``ops/annotator_agent/transcripts/`` so a human can audit later.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import pathlib
import sys
import time
from contextlib import AsyncExitStack
from datetime import datetime
from typing import Any

import anthropic
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

# Load .env so ANTHROPIC_API_KEY etc. are available when running outside
# Django (Django auto-loads via django-environ in config/settings.py).
try:
    from dotenv import load_dotenv
    _ROOT = pathlib.Path(__file__).resolve().parents[2]
    load_dotenv(_ROOT / '.env', override=False)
except ImportError:
    pass


logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(name)s: %(message)s',
)
logger = logging.getLogger('annotator_agent')


# Defaults match the local dev super-admin per
# auto-memory/reference_local_admin_credentials.md.
DEFAULT_LOGIN_USER = 'admin'
DEFAULT_LOGIN_PASS = 'benchmark-temp-2026'

DEFAULT_MODEL = 'claude-sonnet-4-5'  # Sonnet 4.6 ID — see CLAUDE.md
DEFAULT_MAX_TURNS_PER_ITEM = 50

# Annotator-role override the agent appends to every annotation URL so
# its annotations land in the llm_judge cohort, not the human one. The
# benchmark_annotate view honours these as query-string params (and
# carries them through the save-and-next redirect chain).
ANNOTATOR_ROLE = 'llm_judge'
ANNOTATOR_MODEL_TAG = 'claude-sonnet-4-5'

THIS_DIR = pathlib.Path(__file__).parent
TRANSCRIPT_DIR = THIS_DIR / 'transcripts'
PROMPT_PATH = THIS_DIR / 'agent_prompt.md'


def load_system_prompt(base_url: str, login_user: str, login_pass: str,
                       persona: str | None, max_items: int) -> str:
    """Inject runtime context (URL, creds, scope) into the cached prompt body.

    The bulk of the prompt is static and prompt-cacheable. The runtime
    bits go after the cached section.
    """
    static = PROMPT_PATH.read_text()
    role_qs = (
        f"?annotator_role={ANNOTATOR_ROLE}"
        f"&annotator_model={ANNOTATOR_MODEL_TAG}"
    )
    runtime = f"""

---

## Runtime context for this run

- **Base URL**: {base_url}
- **Login**: username `{login_user}` / password `{login_pass}` at \
{base_url}/admin/login/
- **Scope**: annotate up to {max_items} unannotated benchmark item(s)
- **Persona filter**: """ + (
        f"only items with stratum=`synthetic_{persona}`"
        if persona else
        "no filter — take items in the order shown"
    ) + f"""
- **Role tagging**: When you click into a benchmark item to annotate \
it, append `{role_qs}` to the URL — e.g. \
`{base_url}/dashboard/benchmark/MATH_S20_T481/{role_qs}`. The view \
honours this as a query string and tags your annotation under the \
`{ANNOTATOR_ROLE}` cohort instead of the human cohort. The save-and-\
next redirect carries the override forward automatically.

When you finish annotating (or hit the cap), navigate to \
{base_url}/dashboard/benchmark/scores/, set the notes field to \
something like 'agent run @ <commit>', click 'Score now', and read \
the pass rate from the next page. End your run by reporting that \
number to the user."""
    return static + runtime


def mcp_tool_to_anthropic(mcp_tool) -> dict:
    """Translate an MCP Tool definition to Anthropic's tool schema.

    MCP and Anthropic tool shapes are nearly identical (name, description,
    inputSchema). The two main differences:
    - MCP uses ``inputSchema``; Anthropic uses ``input_schema``.
    - MCP names can contain ``-`` and ``/``; Anthropic restricts to
      ``[a-zA-Z0-9_-]{1,64}``. chrome-devtools tools are already
      compliant, but we sanitize defensively.
    """
    name = mcp_tool.name.replace('/', '_')[:64]
    return {
        'name': name,
        'description': mcp_tool.description or '',
        'input_schema': mcp_tool.inputSchema or {'type': 'object', 'properties': {}},
    }


async def run_agent(
    *,
    base_url: str,
    login_user: str,
    login_pass: str,
    persona: str | None,
    max_items: int,
    model: str,
    max_steps: int,
    transcript_path: pathlib.Path,
) -> dict:
    """Run the agent loop end to end. Returns a result summary dict."""
    api_key = os.environ.get('ANTHROPIC_API_KEY')
    if not api_key:
        raise SystemExit("ANTHROPIC_API_KEY not set in env.")

    # Spawn chrome-devtools-mcp as a stdio subprocess. The npx package
    # auto-installs on first run.
    # --isolated uses a fresh, throwaway profile so we don't conflict
    # with any existing chrome-devtools-mcp instance running for the
    # interactive Claude Code session on the same workstation.
    # --headless because the agent doesn't need a visible window.
    mcp_params = StdioServerParameters(
        command='npx',
        args=[
            '-y', 'chrome-devtools-mcp@latest',
            '--isolated',
            '--headless',
            '--viewport', '1280x900',
        ],
        env={**os.environ},
    )

    anthropic_client = anthropic.Anthropic(api_key=api_key)
    transcript = transcript_path.open('w')
    def log_step(kind: str, payload: Any) -> None:
        transcript.write(json.dumps({
            'ts': datetime.utcnow().isoformat(),
            'kind': kind,
            'payload': payload,
        }, default=str) + '\n')
        transcript.flush()

    try:
        async with AsyncExitStack() as stack:
            stdio_ctx = await stack.enter_async_context(stdio_client(mcp_params))
            read_stream, write_stream = stdio_ctx
            session = await stack.enter_async_context(
                ClientSession(read_stream, write_stream)
            )
            await session.initialize()

            tools_result = await session.list_tools()
            mcp_tools = tools_result.tools
            logger.info("Loaded %d tools from chrome-devtools-mcp", len(mcp_tools))
            tools_by_name = {t.name: t for t in mcp_tools}
            anthropic_tools = [mcp_tool_to_anthropic(t) for t in mcp_tools]

            system_prompt = load_system_prompt(
                base_url=base_url, login_user=login_user,
                login_pass=login_pass, persona=persona, max_items=max_items,
            )
            log_step('start', {
                'model': model,
                'tool_count': len(anthropic_tools),
                'tool_names': [t['name'] for t in anthropic_tools[:30]],
                'persona': persona,
                'max_items': max_items,
            })

            messages: list[dict] = [{
                'role': 'user',
                'content': (
                    f"Begin annotating. The dev server is at {base_url}. "
                    f"Log in, then annotate up to {max_items} unannotated "
                    f"benchmark item(s)" + (
                        f" with stratum=synthetic_{persona}." if persona
                        else "."
                    ) + " Then run 'Score now' and report the pass rate."
                ),
            }]

            for step in range(max_steps):
                logger.info("Agent step %d", step + 1)
                response = anthropic_client.messages.create(
                    model=model,
                    max_tokens=2048,
                    system=[{
                        'type': 'text',
                        'text': system_prompt,
                        'cache_control': {'type': 'ephemeral'},
                    }],
                    tools=anthropic_tools,
                    messages=messages,
                )
                log_step('agent_response', {
                    'stop_reason': response.stop_reason,
                    'content_blocks': [
                        {
                            'type': b.type,
                            **({'text': b.text[:300]} if b.type == 'text' else {}),
                            **({'name': b.name, 'input': b.input}
                               if b.type == 'tool_use' else {}),
                        }
                        for b in response.content
                    ],
                    'usage': {
                        'input': response.usage.input_tokens,
                        'output': response.usage.output_tokens,
                    },
                })

                # Append assistant response to history.
                messages.append({
                    'role': 'assistant',
                    'content': [b.model_dump() for b in response.content],
                })

                if response.stop_reason == 'end_turn':
                    final_text = '\n'.join(
                        b.text for b in response.content if b.type == 'text'
                    )
                    log_step('end', {'final_text': final_text[:1000]})
                    return {
                        'reason': 'end_turn',
                        'steps': step + 1,
                        'final_text': final_text,
                    }
                if response.stop_reason != 'tool_use':
                    log_step('end', {'reason': response.stop_reason})
                    return {
                        'reason': response.stop_reason,
                        'steps': step + 1,
                    }

                # Execute every tool_use block in the response.
                tool_results = []
                for block in response.content:
                    if block.type != 'tool_use':
                        continue
                    mcp_name = block.name
                    if mcp_name not in tools_by_name:
                        # Sanitization mismatch — try a forward lookup
                        # against the cleaned-name map.
                        for orig_name in tools_by_name:
                            if orig_name.replace('/', '_')[:64] == mcp_name:
                                mcp_name = orig_name
                                break
                    log_step('tool_call', {
                        'tool_use_id': block.id, 'name': mcp_name,
                        'input': block.input,
                    })
                    try:
                        result = await session.call_tool(
                            mcp_name, block.input or {},
                        )
                        # MCP returns a content list (text/image blocks).
                        # Concatenate text for the agent's view.
                        text_parts = []
                        for c in result.content:
                            if hasattr(c, 'text'):
                                text_parts.append(c.text)
                            else:
                                text_parts.append(json.dumps(
                                    c.model_dump() if hasattr(c, 'model_dump')
                                    else str(c)
                                ))
                        result_text = '\n'.join(text_parts)
                        log_step('tool_result', {
                            'tool_use_id': block.id,
                            'is_error': bool(result.isError),
                            'text_excerpt': result_text[:400],
                        })
                        tool_results.append({
                            'type': 'tool_result',
                            'tool_use_id': block.id,
                            'content': result_text or '<no content>',
                            'is_error': bool(result.isError),
                        })
                    except Exception as exc:
                        logger.exception("Tool %s failed", mcp_name)
                        log_step('tool_error', {
                            'tool_use_id': block.id, 'error': str(exc),
                        })
                        tool_results.append({
                            'type': 'tool_result',
                            'tool_use_id': block.id,
                            'content': f"Tool error: {exc}",
                            'is_error': True,
                        })

                messages.append({'role': 'user', 'content': tool_results})

            log_step('end', {'reason': 'max_steps'})
            return {'reason': 'max_steps', 'steps': max_steps}

    finally:
        transcript.close()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--base-url', default='http://127.0.0.1:8000')
    parser.add_argument('--login-user', default=DEFAULT_LOGIN_USER)
    parser.add_argument('--login-pass', default=DEFAULT_LOGIN_PASS)
    parser.add_argument('--persona', default='struggler',
                        help='Stratum persona to filter (or empty string for any).')
    parser.add_argument('--max-items', type=int, default=1)
    parser.add_argument('--max-steps', type=int, default=DEFAULT_MAX_TURNS_PER_ITEM,
                        help='Hard cap on agent loop iterations.')
    parser.add_argument('--model', default=DEFAULT_MODEL)
    args = parser.parse_args()

    TRANSCRIPT_DIR.mkdir(parents=True, exist_ok=True)
    ts = time.strftime('%Y%m%d-%H%M%S')
    transcript_path = TRANSCRIPT_DIR / f'run-{ts}.jsonl'

    persona = args.persona or None
    logger.info(
        "Starting annotator agent base_url=%s persona=%s max_items=%d transcript=%s",
        args.base_url, persona, args.max_items, transcript_path,
    )
    result = asyncio.run(run_agent(
        base_url=args.base_url,
        login_user=args.login_user,
        login_pass=args.login_pass,
        persona=persona,
        max_items=args.max_items,
        model=args.model,
        max_steps=args.max_steps,
        transcript_path=transcript_path,
    ))
    logger.info("Done: %s", json.dumps(result, default=str)[:500])
    print('\n=== AGENT RESULT ===')
    print(json.dumps(result, indent=2, default=str))
    print(f'Transcript: {transcript_path}')


if __name__ == '__main__':
    main()
