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
        const createForm = document.getElementById('create-key-form');
        const keyBanner = document.getElementById('new-key-banner');
        let preHtml = '';
        if (createForm) preHtml += createForm.outerHTML;
        if (keyBanner) preHtml += keyBanner.outerHTML;

        if (!keys.length) { container.innerHTML = preHtml + '<div class="text-center text-muted py-3"><p>No API keys yet. Create one to get started!</p></div>'; return; }
        container.innerHTML = preHtml + keys.map(key => `<div class="d-flex justify-content-between align-items-center p-2 border-bottom">
            <div><code class="me-2">${this.escapeHtml(key.key_prefix)}</code><span class="text-muted small">${this.escapeHtml(key.name || 'default')}</span></div>
            <div class="d-flex align-items-center gap-2">
                ${key.last_used ? `<span class="text-muted small">Last used: ${new Date(key.last_used).toLocaleDateString()}</span>` : '<span class="text-muted small">Never used</span>'}
                <span class="badge ${key.is_active ? 'bg-success' : 'bg-secondary'}">${key.is_active ? 'Active' : 'Revoked'}</span>
                ${key.is_active ? `<button class="btn btn-sm btn-outline-danger" data-revoke-key="${key.id}" onclick="dashApp.revokeApiKey('${key.id}')"><i class="fas fa-times"></i></button>` : ''}
            </div></div>`).join('');
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
            </div>
            <div class="mt-2"><code class="small">${this.escapeHtml(node.url)}</code></div></div>`).join('');
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
}
const dashApp = new DashboardApp();
