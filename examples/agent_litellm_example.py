#!/usr/bin/env python3
'''Use LlamaNet with LiteLLM — unified OpenAI-compatible access + agent driver.

LiteLLM speaks the OpenAI protocol, which is exactly what LlamaNet nodes and
gateways expose on /v1. Point litellm.completion() at any LlamaNet base URL
(local node, tunnel URL, or self-hosted gateway) and it just works —
including streaming and reasoning content.

Two integrations in one example:

1. `litellm.completion()` for chat — works against a node or the gateway
   with any model in the pool (set `model` to the exact model name from
   `GET /v1/models`).
2. The computer-use agent driven alongside — LiteLLM summarizes the
   browser session trace when it completes.

Requires:
    pip install litellm requests

Environment:
    LLAMANET_BASE_URL   node/gateway base URL (default http://localhost:8000)
    LLAMANET_API_KEY    API key for the gateway (any string for a local node)
    LLAMANET_MODEL      model name served by the node/gateway

Run:
    python examples/agent_litellm_example.py chat 'explain quantization in one paragraph'
    python examples/agent_litellm_example.py chat '...' --stream
    python examples/agent_litellm_example.py agent 'goto pypi.org and find the fastapi install command'
    python examples/agent_litellm_example.py agent '...' --summarize
'''

import argparse
import logging
import os
import sys

import litellm

# Reuse the dependency-free agent client from the same directory.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from agent_client import AgentClient  # noqa: E402

logger = logging.getLogger('agent_litellm')

BASE_URL = os.environ.get('LLAMANET_BASE_URL', 'http://localhost:8000')
API_KEY = os.environ.get('LLAMANET_API_KEY', 'not-needed')
MODEL = os.environ.get('LLAMANET_MODEL', 'llamanet')


def _completion_kwargs() -> dict:
    '''Common litellm kwargs targeting the LlamaNet OpenAI-compatible API.

    LiteLLM prefixes custom OpenAI endpoints with model='openai/<model>'.
    The api_key is accepted but not enforced by local nodes; gateways
    require an ln-... key.
    '''
    return {
        'model': 'openai/' + MODEL,
        'api_base': BASE_URL.rstrip('/') + '/v1',
        'api_key': API_KEY,
    }


def run_chat(prompt: str, stream: bool = False) -> str:
    '''Send a chat completion through LiteLLM to a LlamaNet node/gateway.'''
    messages = [{'role': 'user', 'content': prompt}]
    kwargs = _completion_kwargs()
    kwargs['messages'] = messages

    if not stream:
        response = litellm.completion(**kwargs)
        content = response.choices[0].message.content
        print(content)
        return content or ''

    # Streaming — print tokens as they arrive. Reasoning models emit
    # reasoning_content deltas before the final content.
    collected = []
    response = litellm.completion(stream=True, **kwargs)
    for chunk in response:
        delta = chunk.choices[0].delta if chunk.choices else None
        if delta is None:
            continue
        reasoning = getattr(delta, 'reasoning_content', None)
        if reasoning:
            print('[thinking] ' + reasoning, end='', flush=True)
        if delta.content:
            print(delta.content, end='', flush=True)
            collected.append(delta.content)
    print()
    return ''.join(collected)


def run_agent(goal: str, summarize: bool = False) -> None:
    '''Drive the computer-use agent, optionally summarizing the trace with LiteLLM.'''
    client = AgentClient(BASE_URL)
    client.create_session(goal=goal, headless=True)

    def _answer(question: str) -> str:
        if sys.stdin is not None and sys.stdin.isatty():
            return input('Agent asks: {}\n> '.format(question))
        logger.warning('Non-interactive run - declining: %s', question)
        return 'no'

    def _on_event(event: dict) -> None:
        etype = event.get('type', '')
        if etype == 'step_screenshot':
            print('Step {}: {}'.format(event.get('index'), event.get('url') or 'about:blank'))
        elif etype == 'thinking':
            content = (event.get('content') or '').replace('\n', ' ')[:120]
            print('  thinking: ' + content)
        elif etype == 'action_executed':
            result = event.get('result') or {}
            print('  action: {}'.format(result.get('action', result)))
        elif etype == 'waiting_for_user':
            print('  ! needs input: {}'.format(event.get('question') or ''))
        elif etype in ('completed', 'aborted', 'failed'):
            print('{}: {}'.format(etype, event.get('summary') or event.get('error') or ''))

    terminal = client.run_to_completion(on_event=_on_event, answer_fn=_answer)
    state = client.get_state()
    summary = state.get('final_summary') or terminal.get('summary') or '(none)'
    print('\nFinal summary: ' + summary)

    if summarize:
        # Feed the trace to LiteLLM for a compact report.
        trace_text = '\n'.join(
            '{}: {}'.format(item.get('type', 'action'),
                            item.get('action', item.get('content', item.get('reason', ''))))
            for item in (state.get('trace') or [])
        )[:6000]
        report = run_chat(
            'Summarize what this browser automation session did, its outcome, '
            'and anything noteworthy in 5 bullet points:\n\n' + (trace_text or summary),
            stream=False,
        )
        with open('agent_session_report.md', 'w') as f:
            f.write(report or '')
        print('Report written to agent_session_report.md')


def main() -> None:
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument('mode', choices=['chat', 'agent'],
                        help='chat: LiteLLM completion; agent: computer-use session')
    parser.add_argument('prompt', help='Chat prompt or agent goal')
    parser.add_argument('--stream', action='store_true', help='Stream the chat response')
    parser.add_argument('--summarize', action='store_true',
                        help='After an agent run, summarize the trace with LiteLLM')
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO,
                        format='%(asctime)s %(name)s %(levelname)s %(message)s')

    if args.mode == 'chat':
        run_chat(args.prompt, stream=args.stream)
    else:
        run_agent(args.prompt, summarize=args.summarize)


if __name__ == '__main__':
    main()
