class LandingApp {
    constructor() {
        this.baseUrl = window.location.origin;
        this.user = null;
        this.eventSource = null;
        this._sseConnected = false;
        this._refreshTimer = null;
        this._initFallbackTimer = null;
        this.init();
    }
    async init() {
        await this.checkAuth();
        this.connectSSE();
        // Fallback: if SSE initial_state doesn't arrive within 3s, use API
        this._initFallbackTimer = setTimeout(() => {
            if (!this._sseConnected) {
                this.loadNetworkStats();
                this.loadModels();
            }
        }, 3000);
        const searchInput = document.getElementById('model-search');
        if (searchInput) {
            searchInput.addEventListener('keypress', (e) => { if (e.key === 'Enter') this.searchModels(); });
        }
    }
    connectSSE() {
        if (this.eventSource) this.eventSource.close();
        this.eventSource = new EventSource(`${this.baseUrl}/events/network`);
        this.eventSource.onopen = () => {
            this._sseConnected = true;
            if (this._initFallbackTimer) { clearTimeout(this._initFallbackTimer); this._initFallbackTimer = null; }
            this.updateSSEStatus('connected');
            console.log('✅ SSE connected to gateway');
        };
        this.eventSource.onmessage = (event) => {
            try { this.handleSSEEvent(JSON.parse(event.data)); } catch (e) { console.error('SSE parse error:', e); }
        };
        this.eventSource.onerror = () => {
            this._sseConnected = false;
            this.updateSSEStatus('disconnected');
        };
    }
    handleSSEEvent(data) {
        switch (data.type) {
            case 'initial_state':
                if (data.models) this.renderModels(data.models);
                if (data.stats) this.updateStatsDisplay(data.stats);
                break;
            case 'node_joined':
                this.debouncedRefresh();
                this.showToast(`🆕 New node online: ${this.escapeHtml(data.model_name || '')}`);
                break;
            case 'node_updated':
                // Detect model name change (hot-reload)
                const prevNode = this._lastKnownNodes?.[data.node_hash];
                const modelChanged = prevNode && prevNode !== data.model_name && data.model_name;
                this.debouncedRefresh();
                if (modelChanged) {
                    this.showToast(`🔄 Node ${data.node_hash?.substring(0, 8)}... switched to ${this.escapeHtml(data.model_name)}`);
                    // Track model change for future detection
                    if (!this._lastKnownNodes) this._lastKnownNodes = {};
                    this._lastKnownNodes[data.node_hash] = data.model_name;
                }
                break;
            case 'node_left':
                this.debouncedRefresh();
                this.showToast(`👋 Node disconnected: ${this.escapeHtml(data.model_name || '')}`);
                if (this._lastKnownNodes) delete this._lastKnownNodes[data.node_hash];
                break;
            case 'heartbeat':
                break;
        }
    }
    debouncedRefresh() {
        if (this._refreshTimer) clearTimeout(this._refreshTimer);
        this._refreshTimer = setTimeout(() => {
            this.loadModels();
            this.loadNetworkStats();
        }, 500);
    }
    updateSSEStatus(status) {
        const el = document.getElementById('sse-status-indicator');
        if (!el) return;
        if (status === 'connected') {
            el.className = 'badge bg-success me-2';
            el.innerHTML = '<i class="fas fa-circle" style="font-size:0.5rem"></i> Live';
        } else {
            el.className = 'badge bg-secondary me-2';
            el.innerHTML = '<i class="fas fa-circle" style="font-size:0.5rem"></i> Offline';
        }
    }
    updateStatsDisplay(stats) {
        this.animateCounter('stat-nodes', stats.total_nodes || 0);
        this.animateCounter('stat-models', stats.total_models || 0);
        this.animateCounter('stat-tps', stats.total_tps || 0, true);
        const loadEl = document.getElementById('stat-load');
        if (loadEl) loadEl.textContent = (stats.avg_load || 0).toFixed(2);
        const tokensEl = document.getElementById('stat-tokens');
        if (tokensEl) tokensEl.textContent = this.formatTokens(stats.total_tokens || 0);
    }
    formatTokens(tokens) {
        if (tokens >= 1000000000) return (tokens / 1000000000).toFixed(1) + 'B';
        if (tokens >= 1000000) return (tokens / 1000000).toFixed(1) + 'M';
        if (tokens >= 1000) return (tokens / 1000).toFixed(1) + 'K';
        return tokens.toString();
    }
    async checkAuth() {
        try {
            const resp = await fetch(`${this.baseUrl}/auth/me`, { credentials: 'include' });
            if (resp.ok) { const data = await resp.json(); this.user = data.user; this.updateNavbar(true); }
        } catch (e) {}
    }
    signInWithGoogle() { window.location.href = `${this.baseUrl}/auth/google`; }
    updateNavbar(isAuthenticated) {
        const signin = document.getElementById('nav-signin');
        const dashboard = document.getElementById('nav-dashboard');
        const userSpan = document.getElementById('nav-user');
        const avatar = document.getElementById('nav-avatar');
        if (isAuthenticated && this.user) {
            if (signin) signin.classList.add('d-none');
            if (dashboard) { dashboard.classList.remove('d-none'); dashboard.href = '/dashboard'; }
            if (userSpan && avatar && this.user.avatar_url) { userSpan.classList.remove('d-none'); avatar.src = this.user.avatar_url; avatar.alt = this.user.full_name || ''; }
        }
    }
    async loadNetworkStats() {
        try {
            const resp = await fetch(`${this.baseUrl}/api/network/stats`);
            if (resp.ok) { const stats = await resp.json(); this.updateStatsDisplay(stats); }
        } catch (e) { console.warn('Failed to load stats:', e); }
    }
    animateCounter(elementId, target, isFloat) {
        const el = document.getElementById(elementId); if (!el) return;
        const duration = 1000, startTime = Date.now();
        const step = () => {
            const progress = Math.min((Date.now() - startTime) / duration, 1);
            const eased = 1 - Math.pow(1 - progress, 3);
            el.textContent = isFloat ? (target * eased).toFixed(1) : Math.round(target * eased);
            if (progress < 1) requestAnimationFrame(step);
        };
        requestAnimationFrame(step);
    }
    async loadModels() {
        try {
            const resp = await fetch(`${this.baseUrl}/api/models`);
            if (resp.ok) { const data = await resp.json(); this.renderModels(data.models || []); }
        } catch (e) {
            document.getElementById('model-results').innerHTML = '<div class="col-md-8 text-center text-muted">No models available yet</div>';
        }
    }
    async searchModels() {
        const query = document.getElementById('model-search').value.trim();
        const resultsDiv = document.getElementById('model-results');
        resultsDiv.innerHTML = '<div class="col-md-8 text-center"><div class="spinner-border text-primary"></div></div>';
        try {
            const url = query ? `${this.baseUrl}/api/models/search?q=${encodeURIComponent(query)}` : `${this.baseUrl}/api/models`;
            const resp = await fetch(url);
            if (resp.ok) { const data = await resp.json(); this.renderModels(data.models || []); }
        } catch (e) { resultsDiv.innerHTML = '<div class="col-md-8 text-center text-danger">Search failed</div>'; }
    }
    quickSearch(query) { document.getElementById('model-search').value = query; this.searchModels(); }
    renderModels(models) {
        const resultsDiv = document.getElementById('model-results');
        
        // Track known nodes for model change detection
        if (!this._lastKnownNodes) this._lastKnownNodes = {};
        models.forEach(m => {
            (m.nodes || []).forEach(n => {
                this._lastKnownNodes[n.node_hash] = m.model_name;
            });
        });
        
        if (!models.length) { resultsDiv.innerHTML = '<div class="col-md-8 text-center text-muted py-4"><i class="fas fa-search fa-2x mb-2"></i><p>No models found. Be the first to <a href="https://github.com/machaao/llama-net">run a node</a>!</p></div>'; return; }
        resultsDiv.innerHTML = '<div class="col-md-8">' + models.map(model => {
            const avgLoad = model.avg_load || 0;
            const loadClass = avgLoad < 0.3 ? 'load-low' : avgLoad < 0.7 ? 'load-med' : 'load-high';
            const nodesHtml = (model.nodes || []).map(node => `
                <div class="node-row">
                    <span class="status-dot ${node.load < 0.8 ? 'online' : 'busy'}"></span>
                    <span class="node-hash me-2">${(node.node_hash || '').substring(0, 12)}</span>
                    <span class="node-metric tps me-1">${(node.tps || 0).toFixed(1)} TPS</span>
                    <span class="node-metric ${loadClass} me-1">${(node.load || 0).toFixed(2)} load</span>
                    ${node.gpu_info ? `<span class="text-muted small">${this.escapeHtml(node.gpu_info)}</span>` : ''}
                </div>`).join('');
            return `<div class="model-card">
                <div class="d-flex justify-content-between align-items-start">
                    <div><span class="status-dot online"></span><span class="model-name">${this.escapeHtml(model.model_name)}</span></div>
                    <button class="btn btn-sm btn-outline-primary copy-api-btn" onclick="app.copyApiCommand('${this.escapeHtml(model.model_slug)}', '${this.escapeHtml(model.model_name)}')"><i class="fas fa-copy"></i> API</button>
                </div>
                <div class="model-stats">
                    <span class="model-stat"><i class="fas fa-server"></i> <span class="value">${model.node_count}</span> nodes</span>
                    <span class="model-stat"><i class="fas fa-bolt"></i> <span class="value">${(model.total_tps || 0).toFixed(1)}</span> TPS</span>
                    <span class="model-stat"><i class="fas fa-tachometer-alt"></i> <span class="value">${(model.avg_load || 0).toFixed(2)}</span> load</span>
                    ${model.avg_ttft ? `<span class="model-stat"><i class="fas fa-stopwatch"></i> <span class="value">${(model.avg_ttft * 1000).toFixed(0)}ms</span> TTFT</span>` : ''}
                </div>${nodesHtml}</div>`;
        }).join('') + '</div>';
    }
    copyApiCommand(modelSlug, modelName) {
        this.copyToClipboard(`import openai\n\nclient = openai.OpenAI(\n    base_url="https://llamanet.app/v1",\n    api_key="your-api-key"\n)\n\nresponse = client.chat.completions.create(\n    model="${modelName}",\n    messages=[{"role": "user", "content": "Hello!"}]\n)\n\nprint(response.choices[0].message.content)`, 'API command copied!');
    }
    copyCode(button) { const pre = button.closest('.code-block').querySelector('pre code'); this.copyToClipboard(pre.textContent, 'Copied!'); }
    copyToClipboard(text, message) { navigator.clipboard.writeText(text).then(() => this.showToast(message)).catch(() => { const ta = document.createElement('textarea'); ta.value = text; document.body.appendChild(ta); ta.select(); document.execCommand('copy'); document.body.removeChild(ta); this.showToast(message); }); }
    showToast(message) { const toast = document.createElement('div'); toast.className = 'alert alert-success position-fixed'; toast.style.cssText = 'top:20px;right:20px;z-index:9999;min-width:250px;'; toast.innerHTML = `<i class="fas fa-check-circle"></i> ${this.escapeHtml(message)}`; document.body.appendChild(toast); setTimeout(() => toast.remove(), 2000); }
    escapeHtml(text) { const div = document.createElement('div'); div.textContent = text || ''; return div.innerHTML; }
}
const app = new LandingApp();
