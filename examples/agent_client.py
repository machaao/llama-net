#!/usr/bin/env python3
'''Drive the LlamaNet computer-use agent (Fara1.5-9B) from any application.

Standalone REST + SSE client for the agent API exposed by the inference node
(see docs/agent.md and docs/api-reference.md):

    POST /agent/sessions                create a session (goal + safety opts)
    POST /agent/sessions/{id}/run       start the loop (non-blocking)
    POST /agent/sessions/{id}/pause     pause at the next step
    POST /agent/sessions/{id}/resume    resume after a pause
    POST /agent/sessions/{id}/answer    answer a critical-point question
    POST /agent/sessions/{id}/stop      abort and tear down the browser
    GET  /agent/sessions/{id}           state + full trace log
    GET  /agent/sessions/{id}/events    SSE stream of per-step events

Requires only `requests` (already a LlamaNet dependency).

Usage:
    python examples/agent_client.py 'goto google.com and search for ai news'
    python examples/agent_client.py '...' --allowlist google.com --max-steps 20
    python examples/agent_client.py '...' --auto-answer 'yes, proceed'
'''

import argparse
import json
import logging
import sys
from typing import Any, Callable, Dict, Iterator, List, Optional

import requests

logger = logging.getLogger('agent_client')

# SSE event types that end the stream.
TERMINAL_EVENTS = ('completed', 'aborted', 'failed')


class AgentClient:
    '''Minimal REST + SSE client for the LlamaNet computer-use agent API.'''

    def __init__(self, base_url: str = 'http://localhost:8000', timeout: float = 30.0):
        self.base_url = base_url.rstrip('/')
        self.timeout = timeout
        self.session_id: Optional[str] = None

    # -- REST helpers ----------------------------------------------------

    def _api(self, method: str, path: str, body: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        resp = requests.request(method, self.base_url + path, json=body, timeout=self.timeout)
        if not resp.ok:
            raise RuntimeError(f'{method} {path} -> HTTP {resp.status_code}: {resp.text[:300]}')
        return resp.json()

    def create_session(
        self,
        goal: str,
        allowlist: Optional[List[str]] = None,
        blocklist: Optional[List[str]] = None,
        start_url: Optional[str] = None,
        headless: bool = True,
        max_steps: int = 30,
    ) -> str:
        data = self._api('POST', '/agent/sessions', {
            'goal': goal,
            'allowlist': allowlist or [],
            'blocklist': blocklist or [],
            'start_url': start_url,
            'headless': headless,
            'max_steps': max_steps,
        })
        self.session_id = data['session_id']
        logger.info('Session created: %s', self.session_id)
        return self.session_id

    def run(self) -> Dict[str, Any]:
        return self._api('POST', f'/agent/sessions/{self.session_id}/run')

    def pause(self) -> Dict[str, Any]:
        return self._api('POST', f'/agent/sessions/{self.session_id}/pause')

    def resume(self, answer: str = '') -> Dict[str, Any]:
        return self._api('POST', f'/agent/sessions/{self.session_id}/resume', {'answer': answer})

    def answer(self, text: str) -> Dict[str, Any]:
        return self._api('POST', f'/agent/sessions/{self.session_id}/answer', {'answer': text})

    def stop(self) -> Dict[str, Any]:
        return self._api('POST', f'/agent/sessions/{self.session_id}/stop')

    def get_state(self) -> Dict[str, Any]:
        return self._api('GET', f'/agent/sessions/{self.session_id}')

    # -- SSE event stream ------------------------------------------------

    def events(self, read_timeout: float = 120.0) -> Iterator[Dict[str, Any]]:
        '''Yield parsed SSE events from the session stream.

        Errors arrive as data events (parse_error, execution_error) — the
        HTTP stream itself stays 200 per SSE conventions. Heartbeats are
        yielded like any other event (filter them in your handler if noisy).
        '''
        url = f'{self.base_url}/agent/sessions/{self.session_id}/events'
        with requests.get(url, stream=True, timeout=(10, read_timeout)) as resp:
            if not resp.ok:
                raise RuntimeError(f'Event stream -> HTTP {resp.status_code}')
            for raw in resp.iter_lines(decode_unicode=True):
                if not raw or not raw.startswith('data: '):
                    continue
                payload = raw[6:].strip()
                if not payload:
                    continue
                try:
                    event = json.loads(payload)
                except json.JSONDecodeError:
                    logger.debug('Skipping unparsable SSE payload: %.80s', payload)
                    continue
                yield event
                if event.get('type') in TERMINAL_EVENTS:
                    break

    # -- High-level driver -----------------------------------------------

    def run_to_completion(
        self,
        on_event: Optional[Callable[[Dict[str, Any]], None]] = None,
        answer_fn: Optional[Callable[[str], str]] = None,
        auto_answer: Optional[str] = None,
    ) -> Dict[str, Any]:
        '''Start the loop and consume events until a terminal event.

        Args:
            on_event: optional callback invoked for every event.
            answer_fn: called with the agent's question at critical points
                (ask_user_question / irreversible action). Its return value
                is sent as the answer. If None and `auto_answer` is set,
                that string answers every question; if both are None the
                question is logged and the session stays paused.

        Returns the terminal event dict.
        '''
        self.run()
        terminal: Dict[str, Any] = {}
        for event in self.events():
            if on_event:
                on_event(event)
            etype = event.get('type')
            if etype == 'waiting_for_user':
                question = event.get('question') or event.get('reason') or ''
                if answer_fn is not None:
                    self.answer(answer_fn(question))
                elif auto_answer is not None:
                    self.answer(auto_answer)
                else:
                    logger.warning('Critical point — session paused: %s', question)
            elif etype in TERMINAL_EVENTS:
                terminal = event
        return terminal


def _print_event(event: Dict[str, Any]) -> None:
    '''Human-friendly console rendering of agent events.'''
    etype = event.get('type', '')
    if etype == 'step_screenshot':
        print(f"Step {event.get('index')}: {event.get('url') or 'about:blank'}")
    elif etype == 'thinking':
        content = (event.get('content') or '').replace('\n', ' ')[:160]
        print(f'  thinking: {content}')
    elif etype == 'action_executed':
        result = event.get('result') or {}
        action = result.get('action', result)
        print(f'  action: {json.dumps(action)[:160]}')
    elif etype == 'waiting_for_user':
        print(f"  ! needs input: {event.get('question') or event.get('reason') or ''}")
    elif etype == 'safety_blocked':
        print(f"  ! safety blocked: {event.get('reason') or ''}")
    elif etype in ('parse_error', 'execution_error'):
        print(f"  ! {etype}: {event.get('error') or ''}")
    elif etype in TERMINAL_EVENTS:
        print(f"{etype}: {event.get('summary') or event.get('error') or ''}")


def main() -> None:
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument('goal', help='Task for the agent, e.g. "open google.com and search for ai news"')
    parser.add_argument('--base-url', default='http://localhost:8000',
                        help='Inference node base URL (default: %(default)s)')
    parser.add_argument('--start-url', default=None, help='Optional page to open first')
    parser.add_argument('--allowlist', default='', help='Comma-separated allowed domains')
    parser.add_argument('--blocklist', default='', help='Comma-separated blocked domains')
    parser.add_argument('--headed', action='store_true', help='Show the browser window')
    parser.add_argument('--max-steps', type=int, default=30, help='Max actions before stopping')
    parser.add_argument('--auto-answer', default=None,
                        help='Answer to send at every critical point (use with care)')
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO,
                        format='%(asctime)s %(name)s %(levelname)s %(message)s')

    client = AgentClient(args.base_url)
    client.create_session(
        goal=args.goal,
        allowlist=[d.strip() for d in args.allowlist.split(',') if d.strip()],
        blocklist=[d.strip() for d in args.blocklist.split(',') if d.strip()],
        start_url=args.start_url,
        headless=not args.headed,
        max_steps=args.max_steps,
    )

    interactive = args.auto_answer is None and sys.stdin is not None and sys.stdin.isatty()
    terminal = client.run_to_completion(
        on_event=_print_event,
        answer_fn=(lambda question: input(f'Agent asks: {question}\n> ')) if interactive else None,
        auto_answer=args.auto_answer,
    )

    state = client.get_state()
    summary = state.get('final_summary') or terminal.get('summary') or '(none)'
    print(f'\nFinal summary: {summary}')
    print(f"Steps completed: {state.get('steps_completed', 0)}")
    print(f"Trace entries: {len(state.get('trace') or [])}")


if __name__ == '__main__':
    main()
