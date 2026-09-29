/* Fara1.5-9B computer-use agent console logic */
class AgentConsole {
    constructor() {
        this.baseUrl = window.location.origin;
        this.sessionId = null;
        this.eventSource = null;
        this._logInitialized = false;
    }

    async _api(method, path, body) {
        const opts = { method, credentials: 'include' };
        if (body !== undefined) {
            opts.headers = { 'Content-Type': 'application/json' };
            opts.body = JSON.stringify(body);
        }
        const resp = await fetch(`${this.baseUrl}${path}`, opts);
        if (!resp.ok) {
            let detail = resp.statusText;
            try { const j = await resp.json(); detail = j.detail || j.error || detail; } catch (e) {}
            throw new Error(detail);
        }
        return resp.json();
    }

    async startSession() {
        const goal = document.getElementById('agent-goal').value.trim();
        if (!goal) { this._toast('error', 'Enter a goal first'); return; }

        const allowlist = document.getElementById('agent-allowlist').value
            .split(',').map(s => s.trim()).filter(Boolean);
        const blocklist = document.getElementById('agent-blocklist').value
            .split(',').map(s => s.trim()).filter(Boolean);
        const startUrl = document.getElementById('agent-start-url').value.trim();
        const headless = document.getElementById('agent-headless').checked;

        try {
            const data = await this._api('POST', '/agent/sessions', {
                goal, allowlist, blocklist, start_url: startUrl || null, headless,
            });
            this.sessionId = data.session_id;
            this._ensureLog();
            this._appendLog('system', 'Session created. Starting…', '');
            this._toast('success', 'Session started: ' + this.sessionId);

            // Immediately run the session — no separate Run step.
            await this._api('POST', `/agent/sessions/${this.sessionId}/run`);
            this._setControls('running');
            this._openEvents();
        } catch (e) {
            this._toast('error', 'Failed to start session: ' + e.message);
        }
    }

    async pauseSession() {
        if (!this.sessionId) return;
        await this._api('POST', `/agent/sessions/${this.sessionId}/pause`);
        this._setControls('paused');
    }

    async resumeSession() {
        if (!this.sessionId) return;
        await this._api('POST', `/agent/sessions/${this.sessionId}/resume`, { answer: '' });
        this._setControls('running');
        this._openEvents();
    }

    async sendAnswer() {
        if (!this.sessionId) return;
        const answer = document.getElementById('agent-answer').value.trim();
        await this._api('POST', `/agent/sessions/${this.sessionId}/answer`, { answer });
        document.getElementById('agent-answer').value = '';
        document.getElementById('agent-answer-box').classList.add('d-none');
        this._setControls('running');
        this._openEvents();
    }

    async stopSession() {
        if (!this.sessionId) return;
        try {
            await this._api('POST', `/agent/sessions/${this.sessionId}/stop`);
        } catch (e) { /* ignore */ }
        this._closeEvents();
        this._setControls('idle');
        this._setStatus('Stopped', 'secondary');
    }

    // ── SSE event stream ─────────────────────────────────────────────
    _openEvents() {
        this._closeEvents();
        this.eventSource = new EventSource(`${this.baseUrl}/agent/sessions/${this.sessionId}/events`);
        this.eventSource.onmessage = (ev) => {
            try {
                const data = JSON.parse(ev.data);
                this._handleEvent(data);
            } catch (e) { /* ignore */ }
        };
        this.eventSource.onerror = () => {
            // SSE stream ends when a terminal event has fired.
            if (this.eventSource) {
                this.eventSource.close();
                this.eventSource = null;
            }
        };
    }

    _closeEvents() {
        if (this.eventSource) { this.eventSource.close(); this.eventSource = null; }
    }

    _handleEvent(data) {
        const type = data.type;
        this._ensureLog();

        switch (type) {
            case 'state':
                // Initial snapshot from the server.
                break;
            case 'run_started':
                this._setStatus('Running', 'primary');
                this._setControls('running');
                break;
            case 'session_started':
                this._setStatus('Running', 'primary');
                this._appendLog('system', 'Browser session started',
                    data.goal ? 'Goal: ' + data.goal : '');
                break;
            case 'step_screenshot':
                if (data.screenshot) {
                    document.getElementById('agent-screenshot').src = data.screenshot;
                }
                if (data.url) {
                    document.getElementById('agent-url').textContent = data.url;
                }
                this._appendLog('system', `Step ${data.index}`,
                    data.url ? `URL: ${data.url}${data.title ? ' — ' + data.title : ''}` : '');
                break;
            case 'thinking':
                this._appendLog('thinking', 'Thinking…', data.content || '');
                break;
            case 'model_response':
                // Skip raw dump; parsed thinking/action events carry the detail.
                break;
            case 'action_executed':
                this._appendLog('action', 'Action', this._formatAction(data.result || data));
                break;
            case 'click_missed':
                this._appendLog('paused', `Click missed at (${data.x}, ${data.y})`,
                    data.element
                        ? `Landed on <${data.element.tag}> — not interactive. URL unchanged: ${data.url || 'about:blank'}`
                        : `No element at point. URL unchanged: ${data.url || 'about:blank'}`);
                break;
            case 'action_repeat_warning':
                this._appendLog('paused', `Repeated action ×${data.count} — no effect`,
                    'The same action keeps failing. Nudging the model to try a different approach.');
                break;
            case 'waiting_for_user':
                this._setStatus('Waiting for user', 'warning');
                this._setControls('waiting');
                this._showAnswerBox(data.question || data.reason || '');
                this._appendLog('paused', 'Paused — needs input', data.question || data.reason || '');
                break;
            case 'safety_blocked':
                this._setStatus('Safety hold', 'warning');
                this._setControls('waiting');
                this._appendLog('paused', 'Safety blocked', data.reason || '');
                break;
            case 'parse_retry':
                this._appendLog('system', `Retrying parse (attempt ${data.attempt})`, data.error || '');
                break;
            case 'parse_error':
                {
                    const detail = data.error || '';
                    const raw = data.raw || '';
                    this._appendLog('error', 'Parse error', detail + (raw ? '\n\nModel output:\n' + raw : ''));
                }
                break;
            case 'execution_error':
                this._appendLog('error', 'Error', data.error || '');
                break;
            case 'completed':
                this._setStatus('Completed', 'success');
                this._setControls('idle');
                this._appendLog('system', 'Task completed', data.summary || '');
                break;
            case 'aborted':
                this._setStatus('Stopped', 'secondary');
                this._setControls('idle');
                this._appendLog('system', 'Session stopped', '');
                break;
            case 'failed':
                this._setStatus('Failed', 'danger');
                this._setControls('idle');
                this._appendLog('error', 'Failed', data.error || '');
                break;
            case 'heartbeat':
                break;
            default:
                break;
        }
    }

    _formatAction(data) {
        if (!data) return '';
        if (data.action) {
            return `${data.action} ${JSON.stringify(data.arguments || {})}`;
        }
        return JSON.stringify(data);
    }

    // ── UI helpers ──────────────────────────────────────────────────
    _ensureLog() {
        const log = document.getElementById('agent-log');
        if (!this._logInitialized) {
            log.innerHTML = '';
            this._logInitialized = true;
        }
    }

    _appendLog(kind, title, body) {
        const log = document.getElementById('agent-log');
        const entry = document.createElement('div');
        entry.className = `step-entry ${kind}`;
        const header = document.createElement('div');
        header.className = 'step-header';
        header.textContent = title;
        entry.appendChild(header);
        if (body) {
            if (kind === 'thinking') {
                const rc = document.createElement('div');
                rc.className = 'reasoning-content';
                rc.textContent = body;
                entry.appendChild(rc);
            } else if (kind === 'action') {
                const code = document.createElement('div');
                code.className = 'action-code';
                code.textContent = body;
                entry.appendChild(code);
            } else {
                const txt = document.createElement('div');
                txt.className = 'step-text';
                txt.textContent = body;
                entry.appendChild(txt);
            }
        }
        log.appendChild(entry);
        log.scrollTop = log.scrollHeight;
    }

    _setStatus(text, style) {
        const box = document.getElementById('agent-status');
        box.innerHTML = `<span class="badge bg-${style}">${text}</span>`;
    }

    _setControls(state) {
        const start = document.getElementById('agent-start-btn');
        const pause = document.getElementById('agent-pause-btn');
        const resume = document.getElementById('agent-resume-btn');
        const stop = document.getElementById('agent-stop-btn');

        const map = {
            idle:    { start: false, pause: false, resume: false, stop: false },
            created: { start: true,  pause: false, resume: false, stop: false },
            running: { start: false, pause: true,  resume: false, stop: true },
            paused:  { start: false, pause: false, resume: true,  stop: true },
            waiting: { start: false, pause: false, resume: false, stop: true },
        }[state] || { start: false, pause: false, resume: false, stop: false };

        start.disabled = !map.start;
        pause.disabled = !map.pause;
        resume.disabled = !map.resume;
        stop.disabled = !map.stop;
    }

    _showAnswerBox(question) {
        const box = document.getElementById('agent-answer-box');
        box.classList.remove('d-none');
        const answer = document.getElementById('agent-answer');
        answer.placeholder = question || 'Your answer / confirmation';
        answer.focus();
    }

    _toast(type, message) {
        const toast = document.createElement('div');
        toast.className = `alert alert-${type === 'success' ? 'success' : 'danger'} position-fixed`;
        toast.style.cssText = 'top:20px;right:20px;z-index:9999;min-width:300px;';
        toast.innerHTML = `<i class="fas fa-${type === 'success' ? 'check-circle' : 'exclamation-triangle'}"></i> ${this._esc(message)}`;
        document.body.appendChild(toast);
        setTimeout(() => toast.remove(), 3500);
    }

    _esc(text) {
        const div = document.createElement('div');
        div.textContent = text || '';
        return div.innerHTML;
    }
}

const agentConsole = new AgentConsole();