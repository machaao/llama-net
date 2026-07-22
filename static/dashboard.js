class DashboardApp {
    constructor() { this.baseUrl = window.location.origin; this.user = null; this.init(); }
    async init() { await this.checkAuth(); await this.loadApiKeys(); await this.loadMyNodes(); }
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
        if (!keys.length) { container.innerHTML = '<div class="text-center text-muted py-3"><p>No API keys yet. Create one to get started!</p></div>'; return; }
        container.innerHTML = keys.map(key => `<div class="d-flex justify-content-between align-items-center p-2 border-bottom">
            <div><code class="me-2">${this.escapeHtml(key.key_prefix)}</code><span class="text-muted small">${this.escapeHtml(key.name || 'default')}</span></div>
            <div class="d-flex align-items-center gap-2">
                ${key.last_used ? `<span class="text-muted small">Last used: ${new Date(key.last_used).toLocaleDateString()}</span>` : '<span class="text-muted small">Never used</span>'}
                <span class="badge ${key.is_active ? 'bg-success' : 'bg-secondary'}">${key.is_active ? 'Active' : 'Revoked'}</span>
                ${key.is_active ? `<button class="btn btn-sm btn-outline-danger" onclick="dashApp.revokeApiKey('${key.id}')"><i class="fas fa-times"></i></button>` : ''}
            </div></div>`).join('');
    }
    async createApiKey() {
        const name = prompt('Name for this API key:', 'default'); if (name === null) return;
        try { const resp = await fetch(`${this.baseUrl}/auth/api-keys`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, credentials: 'include', body: JSON.stringify({ name }) }); const data = await resp.json(); if (data.key) { alert(`Your API key (copy it now, it won't be shown again):\n\n${data.key}`); await this.loadApiKeys(); } }
        catch (e) { alert('Failed to create API key'); }
    }
    async revokeApiKey(keyId) { if (!confirm('Revoke this API key?')) return; try { await fetch(`${this.baseUrl}/auth/api-keys/${keyId}`, { method: 'DELETE', credentials: 'include' }); await this.loadApiKeys(); } catch (e) { alert('Failed to revoke'); } }
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
            </div>
            <div class="mt-2"><code class="small">${this.escapeHtml(node.url)}</code></div></div>`).join('');
    }
    async deregisterNode(nodeHash) { if (!confirm('Deregister this node?')) return; try { await fetch(`${this.baseUrl}/api/nodes/${nodeHash}`, { method: 'DELETE', credentials: 'include' }); await this.loadMyNodes(); } catch (e) { alert('Failed to deregister'); } }
    copyRegisterCmd() { this.copyToClipboard(document.getElementById('register-cmd').textContent); }
    copyRegisterTunnelCmd() { this.copyToClipboard(document.getElementById('register-tunnel-cmd').textContent); }
    copyToClipboard(text) { navigator.clipboard.writeText(text).then(() => { const t = document.createElement('div'); t.className = 'alert alert-success position-fixed'; t.style.cssText = 'top:20px;right:20px;z-index:9999;'; t.textContent = 'Copied!'; document.body.appendChild(t); setTimeout(() => t.remove(), 2000); }); }
    escapeHtml(text) { const div = document.createElement('div'); div.textContent = text || ''; return div.innerHTML; }
}
const dashApp = new DashboardApp();
