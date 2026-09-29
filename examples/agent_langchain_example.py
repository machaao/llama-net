#!/usr/bin/env python3
'''Drive the LlamaNet computer-use agent from LangChain.

Two integrations in one example:

1. ChatOpenAI pointed at a LlamaNet node or gateway (OpenAI-compatible /v1)
   for planning, reasoning, and tool selection.
2. The computer-use agent exposed as a LangChain tool, so an LLM can
   delegate browser work (navigate, search, read pages, fill forms) to the
   local Fara1.5-9B harness and act on the result.

The chat model does NOT need vision or computer-use skills — the browser
work happens inside the tool on the inference node. Any OpenAI-compatible
model (local or hosted) that supports tool calling works as the orchestrator.

Requires:
    pip install langchain-openai langchain-core requests

Environment:
    LLAMANET_BASE_URL   node/gateway base URL (default http://localhost:8000)
    LLAMANET_API_KEY    API key for the gateway (any string for a local node)
    LLAMANET_MODEL      chat model name served by the node/gateway

Run:
    python examples/agent_langchain_example.py 'find the install command on pypi.org/fastapi'
    python examples/agent_langchain_example.py '...' --use-llm
'''

import logging
import os
import sys
from typing import List

from langchain_core.messages import HumanMessage, SystemMessage, ToolMessage
from langchain_core.tools import tool
from langchain_openai import ChatOpenAI

# Reuse the dependency-free agent client from the same directory.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from agent_client import AgentClient  # noqa: E402

logger = logging.getLogger('agent_langchain')

BASE_URL = os.environ.get('LLAMANET_BASE_URL', 'http://localhost:8000')
API_KEY = os.environ.get('LLAMANET_API_KEY', 'not-needed')
MODEL = os.environ.get('LLAMANET_MODEL', 'llamanet')

MAX_TOOL_ROUNDS = 8


def _ask_human(question: str) -> str:
    '''Answer a critical-point question from the agent.

    Interactive when a TTY is available; otherwise declines — the safe
    default for irreversible actions (payments, submissions, deletes).
    '''
    if sys.stdin is not None and sys.stdin.isatty():
        return input('Agent asks: {}\n> '.format(question))
    logger.warning('Non-interactive run - declining: %s', question)
    return 'no'


@tool
def computer_use(goal: str, start_url: str = '', allowlist: str = '', max_steps: int = 30) -> str:
    '''Complete a task in a real web browser (navigate, search, read, fill forms).

    Use this whenever the task requires browsing the web. The browser runs
    on the local LlamaNet node with a Fara1.5-9B computer-use model. The
    agent pauses at critical points (personal info, payments, irreversible
    actions) and its questions are relayed for an answer.

    Args:
        goal: what to accomplish in the browser, as a short imperative task.
        start_url: optional page to open first (e.g. https://pypi.org).
        allowlist: optional comma-separated domains to stay within
            (e.g. 'pypi.org,docs.python.org').
        max_steps: maximum browser actions before stopping (default 30).
    '''
    client = AgentClient(BASE_URL)
    client.create_session(
        goal=goal,
        allowlist=[d.strip() for d in allowlist.split(',') if d.strip()],
        start_url=start_url or None,
        headless=True,
        max_steps=max_steps,
    )
    terminal = client.run_to_completion(answer_fn=_ask_human)
    state = client.get_state()
    summary = (
        state.get('final_summary')
        or terminal.get('summary')
        or terminal.get('error')
        or '(no summary)'
    )
    return 'Result: {} (steps: {})'.format(summary, state.get('steps_completed', 0))


def run_direct(task: str) -> None:
    '''Run the browser agent directly — no LLM orchestrator required.

    This is the simplest path when you know the goal upfront and don't
    need an LLM to decompose it.
    '''
    result = computer_use.invoke({'goal': task})
    print(result)


def run_with_llm(task: str) -> None:
    '''Let an LLM orchestrate: it decides when to delegate to the browser tool.

    Requires the chat model to support OpenAI tool calling. On each round
    the model either answers or issues tool calls; tool results are fed
    back as ToolMessage turns until it produces a final answer.
    '''
    llm = ChatOpenAI(
        model=MODEL,
        base_url=BASE_URL + '/v1',
        api_key=API_KEY,
        temperature=0.2,
    )
    tools: List = [computer_use]
    llm_with_tools = llm.bind_tools(tools)
    tools_by_name = {t.name: t for t in tools}

    messages = [
        SystemMessage(content=(
            'You are an assistant with a computer_use tool that drives a real '
            'web browser. Delegate any web work to the tool, then summarize '
            'its result for the user. Keep answers concise.'
        )),
        HumanMessage(content=task),
    ]

    for _ in range(MAX_TOOL_ROUNDS):
        ai = llm_with_tools.invoke(messages)
        messages.append(ai)
        if not ai.tool_calls:
            print('Assistant:', ai.content)
            return
        for call in ai.tool_calls:
            print('Tool call: {} -> {}'.format(call['name'], call['args']))
            result = tools_by_name[call['name']].invoke(call['args'])
            messages.append(ToolMessage(content=str(result), tool_call_id=call['id']))

    print('Reached the tool-call limit ({} rounds).'.format(MAX_TOOL_ROUNDS))


def main() -> None:
    import argparse

    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument('task', help='Task description, e.g. "find the fastapi install command"')
    parser.add_argument('--use-llm', action='store_true',
                        help='Orchestrate with an LLM via tool calling '
                             '(default: run the browser agent directly)')
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO,
                        format='%(asctime)s %(name)s %(levelname)s %(message)s')

    if args.use_llm:
        run_with_llm(args.task)
    else:
        run_direct(args.task)


if __name__ == '__main__':
    main()
