class DashboardApp {
    constructor() { this.baseUrl = window.location.origin; this.user = null; this.init(); }
    async init() { await this.checkAuth(); await this.loadApiKeys(); await this.loadMyNodes(); await this.loadLiveModels(); await this.loadTokenUsage(); }
    async checkAuth() {
        try {
            const resp = await fetch(`${this.baseUrl}/auth/me`, { credentials: 'include' });
            if (!resp.ok) { window.location.href = '/'; return; }
            const data = await resp.json(); this.user = data.user;
            document.getElementById('user-info').textContent = this.user.full_name || this.user.email;
            document.getElementById('welcome-msg').textContent = `Welcome back, ${this.user.full_name || this.user.email}!`;
        } catch (e) { window.location.href = '/'; }
    }
    async signOut() {
        await fetch(`${this.baseUrl}/auth/logout`, { method: 'POST', credentials: 'include' });
        localStorage.removeItem('supabase_access_token'); localStorage.removeItem('supabase_refresh_token');
        window.location.href = '/';
    }
    async loadApiKeys() {
        const container = document.getElementById('api-keys-list');
        try { const resp = await fetch(`${this.baseUrl}/auth/api-keys`, { credentials: 'include' }); if (!resp.ok) throw new Error(); const data = await resp.json(); this.renderApiKeys(data.keys || []); }
        catch (e) { container.innerHTML = '<div class="alert alert-danger">Failed to load API keys</div>'; }
    }
    renderApiKeys(keys) {
        const container = document.getElementById('api-keys-list');
        const createForm = document.getElementById('create-key-form');
        const keyBanner = document.getElementById('new-key-banner');
        let preHtml = '';
        if (createForm) preHtml += createForm.outerHTML;
        if (keyBanner) preHtml += keyBanner.outerHTML;

        if (!keys.length) { container.innerHTML = preHtml + '<div class="text-center text-muted py-3"><p>No API keys yet. Create one to get started!</p></div>'; return; }
        container.innerHTML = preHtml + keys.map(key => `<div class="d-flex justify-content-between align-items-start p-2 border-bottom">
            <div class="flex-grow-1">
                <div class="d-flex align-items-center">
                    <code class="me-2">${this.escapeHtml(key.key_prefix)}</code>
                    <span class="text-muted small">${this.escapeHtml(key.name || 'default')}</span>
                </div>
                <div class="d-flex align-items-center gap-2 mt-1">
                    ${key.last_used ? `<span class="text-muted small">Last used: ${new Date(key.last_used).toLocaleDateString()}</span>` : '<span class="text-muted small">Never used</span>'}
                    <span class="badge ${key.is_active ? 'bg-success' : 'bg-secondary'}">${key.is_active ? 'Active' : 'Revoked'}</span>
                </div>
                <div id="token-usage-${key.id}"></div>
            </div>
            <div class="d-flex align-items-center gap-2">
                ${key.is_active ? `<button class="btn btn-sm btn-outline-danger" data-revoke-key="${key.id}" onclick="dashApp.revokeApiKey('${key.id}')"><i class="fas fa-times"></i></button>` : ''}
            </div></div>`).join('');

        // Load token usage for each active key
        this.loadTokenUsage();
    }
    async createApiKey() {
        const container = document.getElementById('api-keys-list');
        const existingForm = document.getElementById('create-key-form');
        if (existingForm) { existingForm.remove(); return; }

        const formHtml = `
            <div id="create-key-form" class="border rounded p-3 mb-3 bg-light">
                <div class="input-group input-group-sm">
                    <span class="input-group-text"><i class="fas fa-key"></i> Name</span>
                    <input type="text" class="form-control" id="new-key-name" value="default" placeholder="Key name">
                    <button class="btn btn-primary" onclick="dashApp.submitCreateKey()"><i class="fas fa-plus"></i> Create</button>
                    <button class="btn btn-outline-secondary" onclick="document.getElementById('create-key-form').remove()"><i class="fas fa-times"></i></button>
                </div>
            </div>`;
        container.insertAdjacentHTML('afterbegin', formHtml);
        document.getElementById('new-key-name').focus();
        document.getElementById('new-key-name').select();
    }
    async submitCreateKey() {
        const nameInput = document.getElementById('new-key-name');
        const name = (nameInput ? nameInput.value.trim() : '') || 'default';
        const form = document.getElementById('create-key-form');
        const container = document.getElementById('api-keys-list');
        try {
            const resp = await fetch(`${this.baseUrl}/auth/api-keys`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, credentials: 'include', body: JSON.stringify({ name }) });
            const data = await resp.json();
            if (data.key) {
                if (form) form.remove();
                await this.loadApiKeys();
                const banner = document.createElement('div');
                banner.className = 'alert alert-success d-flex align-items-center justify-content-between mb-3';
                banner.id = 'new-key-banner';
                banner.innerHTML = `
                    <div><i class="fas fa-check-circle me-2"></i><strong>Key created:</strong> <code class="user-select-all">${this.escapeHtml(data.key)}</code></div>
                    <div class="d-flex gap-2">
                        <button class="btn btn-sm btn-outline-success" onclick="dashApp.copyToClipboard('${this.escapeHtml(data.key)}', 'Key copied!')"><i class="fas fa-copy"></i> Copy</button>
                        <button class="btn btn-sm btn-outline-secondary" onclick="this.closest('.alert').remove()"><i class="fas fa-times"></i></button>
                    </div>`;
                container.insertAdjacentElement('afterbegin', banner);
            }
        } catch (e) {
            if (form) form.remove();
            this.showAlert('danger', 'Failed to create API key');
        }
    }
    async revokeApiKey(keyId) {
        const btn = document.querySelector(`[data-revoke-key="${keyId}"]`);
        if (btn) {
            if (btn.dataset.confirmed === 'true') {
                try {
                    await fetch(`${this.baseUrl}/auth/api-keys/${keyId}`, { method: 'DELETE', credentials: 'include' });
                    await this.loadApiKeys();
                } catch (e) { this.showAlert('danger', 'Failed to revoke key'); }
                return;
            }
            btn.dataset.confirmed = 'true';
            btn.innerHTML = '<i class="fas fa-check"></i> Confirm';
            btn.classList.remove('btn-outline-danger');
            btn.classList.add('btn-danger');
            setTimeout(() => {
                if (btn.dataset.confirmed === 'true') {
                    btn.dataset.confirmed = 'false';
                    btn.innerHTML = '<i class="fas fa-times"></i>';
                    btn.classList.remove('btn-danger');
                    btn.classList.add('btn-outline-danger');
                }
            }, 3000);
            return;
        }
    }
    async loadMyNodes() {
        const container = document.getElementById('my-nodes-list');
        try { const resp = await fetch(`${this.baseUrl}/api/nodes/mine`, { credentials: 'include' }); if (!resp.ok) throw new Error(); const data = await resp.json(); this.renderMyNodes(data.nodes || []); }
        catch (e) { container.innerHTML = '<div class="alert alert-danger">Failed to load nodes</div>'; }
    }
    renderMyNodes(nodes) {
        const container = document.getElementById('my-nodes-list');
        if (!nodes.length) { container.innerHTML = '<div class="text-center text-muted py-3"><p>No nodes registered yet.</p></div>'; return; }
        container.innerHTML = nodes.map(node => `<div class="model-card">
            <div class="d-flex justify-content-between align-items-start">
                <div><span class="status-dot ${node.status === 'active' ? 'online' : ''}"></span><span class="node-hash">${(node.node_hash || '').substring(0, 12)}</span><span class="text-muted ms-2">${this.escapeHtml(node.model_name)}</span></div>
                <button class="btn btn-sm btn-outline-danger" onclick="dashApp.deregisterNode('${node.node_hash}')"><i class="fas fa-trash"></i></button>
            </div>
            <div class="model-stats mt-2">
                <span class="model-stat"><i class="fas fa-bolt"></i> <span class="value">${(node.tps || 0).toFixed(1)}</span> TPS</span>
                <span class="model-stat"><i class="fas fa-tachometer-alt"></i> <span class="value">${(node.load || 0).toFixed(2)}</span> load</span>
                ${node.gpu_info ? `<span class="model-stat"><i class="fas fa-microchip"></i> ${this.escapeHtml(node.gpu_info)}</span>` : ''}
            </div></div>`).join('');
    }
    async deregisterNode(nodeHash) { if (!confirm('Deregister this node?')) return; try { await fetch(`${this.baseUrl}/api/nodes/${nodeHash}`, { method: 'DELETE', credentials: 'include' }); await this.loadMyNodes(); } catch (e) { alert('Failed to deregister'); } }
    copyRegisterCmd() { this.copyToClipboard(document.getElementById('register-cmd').textContent); }
    copyRegisterTunnelCmd() { this.copyToClipboard(document.getElementById('register-tunnel-cmd').textContent); }
    copyToClipboard(text) { navigator.clipboard.writeText(text).then(() => { const t = document.createElement('div'); t.className = 'alert alert-success position-fixed'; t.style.cssText = 'top:20px;right:20px;z-index:9999;'; t.textContent = 'Copied!'; document.body.appendChild(t); setTimeout(() => t.remove(), 2000); }); }
    escapeHtml(text) { const div = document.createElement('div'); div.textContent = text || ''; return div.innerHTML; }
    copyToClipboard(text, message) {
        navigator.clipboard.writeText(text).then(() => {
            this.showAlert('success', message || 'Copied!');
        }).catch(() => {
            const ta = document.createElement('textarea');
            ta.value = text; document.body.appendChild(ta); ta.select();
            document.execCommand('copy'); document.body.removeChild(ta);
            this.showAlert('success', message || 'Copied!');
        });
    }
    showAlert(type, message) {
        const toast = document.createElement('div');
        toast.className = `alert alert-${type} position-fixed`;
        toast.style.cssText = 'top:20px;right:20px;z-index:9999;min-width:250px;';
        toast.innerHTML = `<i class="fas fa-${type === 'success' ? 'check-circle' : 'exclamation-triangle'}"></i> ${this.escapeHtml(message)}`;
        document.body.appendChild(toast);
        setTimeout(() => toast.remove(), 2500);
    }
    async loadLiveModels() {
        try {
            const resp = await fetch(`${this.baseUrl}/api/models`);
            if (!resp.ok) throw new Error();
            const data = await resp.json();
            const models = data.models || [];
            const select = document.getElementById('live-model-select');
            const loading = document.getElementById('live-models-loading');
            const content = document.getElementById('live-models-content');
            const empty = document.getElementById('live-models-empty');
            loading.style.display = 'none';
            if (!models.length) { empty.style.display = 'block'; return; }
            content.style.display = 'block';
            select.innerHTML = models.map(m =>
                `<option value="${this.escapeHtml(m.model_slug)}" data-model-name="${this.escapeHtml(m.model_name)}" data-node-count="${m.node_count}" data-total-tps="${(m.total_tps || 0).toFixed(1)}" data-avg-load="${(m.avg_load || 0).toFixed(2)}">${this.escapeHtml(m.model_name)} (${m.node_count} node${m.node_count > 1 ? 's' : ''})</option>`
            ).join('');
            select.addEventListener('change', () => this.updateLiveModelInfo());
            this.updateLiveModelInfo();
        } catch (e) {
            document.getElementById('live-models-loading').innerHTML = '<span class="text-muted">Could not load models</span>';
        }
    }
    async loadTokenUsage() {
        try {
            const resp = await fetch(`${this.baseUrl}/auth/token-usage`, { credentials: 'include' });
            if (!resp.ok) return;
            const data = await resp.json();
            this.renderTokenUsage(data.keys || [], data.budget || 500000);
        } catch (e) {
            console.debug('Token usage not available:', e);
        }
    }

    renderTokenUsage(keys, budget) {
        keys.forEach(keyUsage => {
            const container = document.getElementById(`token-usage-${keyUsage.key_id}`);
            if (!container) return;

            const percent = keyUsage.percent || 0;
            const consumed = keyUsage.tokens_consumed || 0;
            const requests = keyUsage.requests_count || 0;
            const barClass = percent >= 100 ? 'exceeded' : percent >= 80 ? 'warning' : 'safe';
            const textClass = percent >= 100 ? 'exceeded' : percent >= 80 ? 'warning' : '';

            let html = '<div class="token-budget-bar">';
            html += `<div class="token-budget-fill ${barClass}" style="width: ${Math.min(percent, 100)}%"></div>`;
            html += '</div>';

            if (percent >= 100) {
                html += `<div class="token-usage-text exceeded">`;
                html += `<i class="fas fa-ban"></i> ${consumed.toLocaleString()} / ${budget.toLocaleString()} tokens — BUDGET EXCEEDED`;
                html += `</div>`;
            } else if (percent >= 80) {
                html += `<div class="token-usage-text warning">`;
                html += `<i class="fas fa-exclamation-triangle"></i> ${consumed.toLocaleString()} / ${budget.toLocaleString()} tokens (${percent}%) · ${requests.toLocaleString()} requests`;
                html += `</div>`;
            } else {
                html += `<div class="token-usage-text">`;
                html += `<i class="fas fa-chart-bar"></i> ${consumed.toLocaleString()} / ${budget.toLocaleString()} tokens (${percent}%) · ${requests.toLocaleString()} requests`;
                html += `</div>`;
            }

            container.innerHTML = html;
        });
    }

    updateLiveModelInfo() {
        const select = document.getElementById('live-model-select');
        const info = document.getElementById('live-model-info');
        if (!select || !select.selectedOptions.length) return;
        const opt = select.selectedOptions[0];
        const nodes = opt.dataset.nodeCount || 0;
        const tps = opt.dataset.totalTps || 0;
        const load = opt.dataset.avgLoad || 0;
        info.innerHTML = `<span class="badge bg-info me-1"><i class="fas fa-server"></i> ${nodes} node${nodes > 1 ? 's' : ''}</span><span class="badge bg-success me-1"><i class="fas fa-bolt"></i> ${tps} TPS</span><span class="badge bg-${parseFloat(load) < 0.5 ? 'success' : 'warning'}"><i class="fas fa-tachometer-alt"></i> ${load} load</span>`;
    }
    async sendLiveTest() {
        const select = document.getElementById('live-model-select');
        const prompt = document.getElementById('live-prompt').value.trim();
        const maxTokens = parseInt(document.getElementById('live-max-tokens').value) || 256;
        const temperature = parseFloat(document.getElementById('live-temperature').value) || 0.7;
        const sendBtn = document.getElementById('live-send-btn');
        const responseDiv = document.getElementById('live-response');
        const responseText = document.getElementById('live-response-text');
        const responseMeta = document.getElementById('live-response-meta');
        if (!select.value || !prompt) { this.showAlert('danger', 'Select a model and enter a prompt'); return; }
        const modelName = select.selectedOptions[0].dataset.modelName;
        sendBtn.disabled = true;
        sendBtn.innerHTML = '<div class="spinner-border spinner-border-sm"></div> Sending...';
        responseDiv.style.display = 'block';
        responseText.innerHTML = '<span class="text-muted"><i class="fas fa-spinner fa-spin"></i> Waiting for response...</span>';
        responseMeta.textContent = '';
        const startTime = Date.now();
        try {
            const apiKey = localStorage.getItem('supabase_access_token') || '';
            const resp = await fetch(`${this.baseUrl}/v1/chat/completions`, {
                method: 'POST',
                headers: { 'Content-Type': 'application/json', ...(apiKey ? { 'Authorization': `Bearer ${apiKey}` } : {}) },
                body: JSON.stringify({
                    model: modelName,
                    messages: [{ role: 'user', content: prompt }],
                    max_tokens: maxTokens,
                    temperature: temperature,
                    stream: true
                })
            });
            if (!resp.ok) {
                const errData = await resp.json().catch(() => ({}));
                const errMsg = errData.error?.message || errData.detail || `HTTP ${resp.status}`;
                responseText.innerHTML = `<span class="text-danger"><i class="fas fa-exclamation-triangle"></i> ${this.escapeHtml(errMsg)}</span>`;
                if (resp.status === 401) {
                    responseText.innerHTML += '<br><small class="text-muted">Sign in with Google to get an API key, or use the inference node directly at localhost:8000</small>';
                }
                return;
            }

            let accumulatedText = '';
            let reasoningText = '';
            let responseId = '';
            let nodeInfo = null;

            for await (const chunk of this._parseSSEStream(resp)) {
                if (chunk.choices && chunk.choices.length > 0) {
                    const delta = chunk.choices[0].delta || {};
                    if (delta.reasoning_content) {
                        reasoningText += delta.reasoning_content;
                    }
                    if (delta.content) {
                        accumulatedText += delta.content;
                    }
                }
                if (chunk.id) responseId = chunk.id;
                if (chunk.node_info) nodeInfo = chunk.node_info;

                // Render accumulated content with markdown
                let html = '';
                if (reasoningText) {
                    html += this._renderReasoningBlock(reasoningText);
                }
                if (accumulatedText) {
                    html += `<div class="markdown-content">${this._renderMarkdown(accumulatedText)}</div>`;
                } else if (!reasoningText) {
                    html = '<span class="text-muted"><i class="fas fa-spinner fa-spin"></i> Thinking...</span>';
                }
                responseText.innerHTML = html;
                this._highlightCodeBlocks(responseText);
                responseDiv.scrollTop = responseDiv.scrollHeight;
            }

            const elapsed = ((Date.now() - startTime) / 1000).toFixed(1);

            // Final render with markdown
            let finalHtml = '';
            if (reasoningText) {
                finalHtml += this._renderReasoningBlock(reasoningText);
            }
            if (accumulatedText) {
                finalHtml += `<div class="markdown-content">${this._renderMarkdown(accumulatedText)}</div>`;
            }
            if (finalHtml) responseText.innerHTML = finalHtml;
            this._highlightCodeBlocks(responseText);

            const metaParts = [`⏱️ ${elapsed}s`];
            const estimatedTokens = Math.ceil(accumulatedText.split(' ').length * 1.3);
            metaParts.push(`~${estimatedTokens} tokens`);
            if (responseId) metaParts.push(`ID: ${responseId.substring(0, 8)}...`);
            if (nodeInfo) metaParts.push(`via ${nodeInfo.node_id?.substring(0, 8)}...`);
            responseMeta.textContent = metaParts.join(' · ');
        } catch (e) {
            if (e.name === 'AbortError') return;
            responseText.innerHTML = `<span class="text-danger"><i class="fas fa-exclamation-triangle"></i> ${this.escapeHtml(e.message)}</span>`;
        } finally {
            sendBtn.disabled = false;
            sendBtn.innerHTML = '<i class="fas fa-paper-plane"></i> Send';
        }
    }
    clearLiveTest() {
        document.getElementById('live-prompt').value = '';
        document.getElementById('live-response').style.display = 'none';
        document.getElementById('live-response-text').innerHTML = '';
        document.getElementById('live-response-meta').textContent = '';
    }

    // ── Reusable Streaming Utilities ──────────────────────────────

    async *_parseSSEStream(response) {
        const reader = response.body.getReader();
        const decoder = new TextDecoder();
        let buffer = '';
        try {
            while (true) {
                const { done, value } = await reader.read();
                if (done) break;
                buffer += decoder.decode(value, { stream: true });
                const lines = buffer.split('\n');
                buffer = lines.pop() || '';
                for (const line of lines) {
                    const trimmed = line.trim();
                    if (!trimmed.startsWith('data: ')) continue;
                    const data = trimmed.slice(6);
                    if (data === '[DONE]') return;
                    try {
                        yield JSON.parse(data);
                    } catch (e) {
                        // Skip unparseable chunks
                    }
                }
            }
        } finally {
            reader.releaseLock();
        }
    }

    _renderMarkdown(text) {
        if (!text || typeof text !== 'string') return '';
        try {
            const sanitized = text
                .replace(/<script\b[^<]*(?:(?!<\/script>)<[^<]*)*<\/script>/gi, '')
                .replace(/<iframe\b[^<]*(?:(?!<\/iframe>)<[^<]*)*<\/iframe>/gi, '')
                .replace(/on\w+\s*=\s*["'][^"']*["']/gi, '')
                .replace(/javascript:/gi, '');
            return marked.parse(sanitized);
        } catch (e) {
            return this.escapeHtml(text);
        }
    }

    _highlightCodeBlocks(element) {
        if (typeof hljs !== 'undefined') {
            element.querySelectorAll('pre code').forEach(block => {
                hljs.highlightElement(block);
            });
        }
    }

    _renderReasoningBlock(text) {
        const escaped = this.escapeHtml(text);
        return `<div class="reasoning-block" style="border:1px solid rgba(13,110,253,.2);border-radius:.5rem;background:rgba(13,110,253,.03);margin-bottom:.75rem;overflow:hidden;">
            <div class="reasoning-header" onclick="this.nextElementSibling.style.display=this.nextElementSibling.style.display==='none'?'block':'none';this.querySelector('i:first-child').className=this.nextElementSibling.style.display==='none'?'fas fa-chevron-right':'fas fa-chevron-down';" style="display:flex;align-items:center;gap:.5rem;padding:.5rem .75rem;cursor:pointer;font-size:.8rem;font-weight:500;color:#0d6efd;">
                <i class="fas fa-chevron-right" style="font-size:.65rem;width:1rem;text-align:center;"></i>
                <i class="fas fa-brain"></i> Thinking...
            </div>
            <div class="reasoning-content" style="display:none;padding:.5rem .75rem .75rem;font-size:.82rem;color:#6c757d;line-height:1.5;white-space:pre-wrap;word-wrap:break-word;border-top:1px solid rgba(13,110,253,.1);max-height:300px;overflow-y:auto;">${escaped}</div>
        </div>`;
    }

    copyExample(button) {
        const pre = button.closest('.code-block').querySelector('pre code');
        this.copyToClipboard(pre.textContent, 'Copied!');
    }
}
const dashApp = new DashboardApp();
