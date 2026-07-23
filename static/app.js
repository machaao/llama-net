class MarkdownRenderer {
    constructor() {
        this.initializeMarked();
        this.initializeHighlight();
    }
    
    initializeMarked() {
        // Configure marked with safe defaults
        marked.setOptions({
            highlight: (code, lang) => {
                if (lang && hljs.getLanguage(lang)) {
                    try {
                        return hljs.highlight(code, { language: lang }).value;
                    } catch (err) {
                        console.warn('Highlight.js error:', err);
                    }
                }
                return hljs.highlightAuto(code).value;
            },
            langPrefix: 'hljs language-',
            breaks: true,
            gfm: true,
            sanitize: false, // We'll handle sanitization separately
            smartLists: true,
            smartypants: true
        });
    }
    
    initializeHighlight() {
        // Initialize highlight.js
        if (typeof hljs !== 'undefined') {
            hljs.configure({
                languages: ['javascript', 'python', 'bash', 'json', 'html', 'css', 'markdown', 'sql', 'yaml']
            });
        }
    }
    
    render(text) {
        if (!text || typeof text !== 'string') {
            return '';
        }
        
        try {
            // Basic sanitization - remove script tags and dangerous attributes
            const sanitized = this.sanitizeHtml(text);
            
            // Render markdown
            const rendered = marked.parse(sanitized);
            
            return rendered;
        } catch (error) {
            console.error('Markdown rendering error:', error);
            return this.escapeHtml(text);
        }
    }
    
    sanitizeHtml(html) {
        // Basic HTML sanitization - remove dangerous elements and attributes
        return html
            .replace(/<script\b[^<]*(?:(?!<\/script>)<[^<]*)*<\/script>/gi, '')
            .replace(/<iframe\b[^<]*(?:(?!<\/iframe>)<[^<]*)*<\/iframe>/gi, '')
            .replace(/on\w+\s*=\s*["'][^"']*["']/gi, '')
            .replace(/javascript:/gi, '');
    }
    
    escapeHtml(text) {
        const div = document.createElement('div');
        div.textContent = text;
        return div.innerHTML;
    }
    
    renderInline(text) {
        // For streaming - render inline markdown elements
        if (!text || typeof text !== 'string') {
            return '';
        }
        
        try {
            // Handle inline elements only for streaming
            return marked.parseInline(this.sanitizeHtml(text));
        } catch (error) {
            return this.escapeHtml(text);
        }
    }
}

class LlamaNetUI {
    constructor() {
        this.baseUrl = window.location.origin;
        this.nodes = [];
        this.selectedNode = null;
        this.chatHistory = [];
        this.markdownRenderer = new MarkdownRenderer();
        
        // System prompt properties
        this.systemPrompt = '';
        this.systemPromptPresets = {
            'helpful': 'You are a helpful AI assistant. Provide clear, accurate, and useful responses to help users with their questions and tasks.',
            'creative': 'You are a creative writing assistant. Help users with storytelling, creative writing, brainstorming ideas, and artistic expression. Be imaginative and inspiring.',
            'technical': 'You are a technical expert and programming assistant. Provide detailed, accurate technical information, code examples, and solutions to programming problems.',
            'teacher': 'You are a patient and encouraging teacher. Explain concepts clearly, break down complex topics into simple steps, and help users learn effectively.',
            'analyst': 'You are a data analyst and research assistant. Help users analyze information, interpret data, identify patterns, and draw meaningful conclusions.'
        };
        
        // SSE-only properties (NO POLLING) - Updated for unified SSE manager
        this.eventSource = null;
        this.isConnected = false;
        this.reconnectAttempts = 0;
        this.maxReconnectAttempts = 5;
        this.reconnectDelay = 1000;
        
        // Node tracking - Updated for consolidated validation
        this.activeNodes = new Map();
        this.nodeStats = {
            totalNodes: 0,
            modelsAvailable: new Set(),
            networkHealth: 'unknown'
        };
        
        // Event-driven node status tracking (using consolidated event manager)
        this.nodeStatuses = new Map();
        this.nodeLastEvent = new Map();
        this.nodeEventTypes = new Map();
        
        // SSE connection info - Updated for unified SSE manager
        this.connectionInfo = null;
        this.lastUpdateTime = 0;
        this.connectionStatus = 'connecting';
        this.errorCount = 0;
        
        // Local node detection
        this.localNodeId = null;

        // Restore selected model from localStorage
        this.selectedModel = localStorage.getItem('llamanet_selected_model') || null;
        
        this.init();
    }
    
    async init() {
        // Load system prompt from localStorage
        this.loadSystemPrompt();
        
        // Start ONLY SSE-based network monitoring using unified SSE manager
        this.startUnifiedSSENetworkMonitoring();
        
        // Detect local node ID from /info endpoint
        fetch(`${this.baseUrl}/info`).then(r => r.ok ? r.json() : null).then(data => {
            if (data?.node_id) this.localNodeId = data.node_id;
        }).catch(() => {});

        // Load tunnel status
        this.loadTunnelStatus();
        
        // Start wake-from-sleep detection (local health check only)
        this.startWakeDetection();
        
        // ONE-TIME initial network status load (not polling)
        this.loadInitialNetworkStatus();
        
        this.setupEventListeners();
        
        // Restore selected model UI if available
        if (this.selectedModel) {
            setTimeout(() => {
                this.updateChatInterface(this.selectedModel);
                const selectedGroup = document.querySelector(`[data-model="${this.selectedModel}"]`);
                if (selectedGroup) {
                    selectedGroup.classList.add('selected-model');
                }
            }, 1000);
        }
        
        // Initialize clear history button state
        setTimeout(() => {
            this.updateClearHistoryButton();
        }, 100);
        
        // Handle page visibility changes - SSE only
        document.addEventListener('visibilitychange', () => {
            if (document.hidden) {
                console.log('Page hidden - SSE continues running');
            } else {
                // Reconnect SSE if needed
                if (!this.isConnected) {
                    this.startUnifiedSSENetworkMonitoring();
                }
            }
        });
    }
    
    // System Prompt Methods
    loadSystemPrompt() {
        try {
            const saved = localStorage.getItem('llamanet_system_prompt');
            if (saved) {
                this.systemPrompt = saved;
                const input = document.getElementById('system-prompt-input');
                if (input) {
                    input.value = this.systemPrompt;
                    this.updateSystemPromptUI();
                }
            }
        } catch (error) {
            console.warn('Could not load system prompt from localStorage:', error);
        }
    }
    
    saveSystemPrompt() {
        try {
            localStorage.setItem('llamanet_system_prompt', this.systemPrompt);
        } catch (error) {
            console.warn('Could not save system prompt to localStorage:', error);
        }
    }
    
    updateSystemPromptUI() {
        const input = document.getElementById('system-prompt-input');
        const status = document.getElementById('system-prompt-status');
        const toggle = document.getElementById('system-prompt-toggle');
        const chatMessages = document.getElementById('chat-messages');
        
        if (input) {
            if (this.systemPrompt.trim()) {
                input.classList.add('has-content');
            } else {
                input.classList.remove('has-content');
            }
        }
        
        if (status) {
            if (this.systemPrompt.trim()) {
                status.innerHTML = '<i class="fas fa-circle text-primary" style="font-size: 0.5rem;"></i><small class="text-primary">Custom Active</small>';
                status.classList.add('active');
            } else {
                status.innerHTML = '<i class="fas fa-circle text-secondary" style="font-size: 0.5rem;"></i><small class="text-muted">Default</small>';
                status.classList.remove('active');
            }
        }
        
        if (toggle) {
            if (this.systemPrompt.trim()) {
                toggle.classList.add('active');
                toggle.innerHTML = '<i class="fas fa-cog"></i> System Prompt <i class="fas fa-check-circle ms-1"></i>';
            } else {
                toggle.classList.remove('active');
                toggle.innerHTML = '<i class="fas fa-cog"></i> System Prompt';
            }
        }
        
        // Update chat container visual indicator
        if (chatMessages) {
            if (this.systemPrompt.trim()) {
                chatMessages.classList.add('chat-system-prompt-active');
            } else {
                chatMessages.classList.remove('chat-system-prompt-active');
            }
        }
        
        // Update character count
        this.updateCharacterCount();
    }
    
    updateCharacterCount() {
        const input = document.getElementById('system-prompt-input');
        if (!input) return;
        
        const length = input.value.length;
        const maxLength = 2000; // Reasonable limit for system prompts
        
        // Remove existing character count
        const existingCount = input.parentNode.querySelector('.system-prompt-char-count');
        if (existingCount) {
            existingCount.remove();
        }
        
        // Add character count if there's content
        if (length > 0) {
            const countDiv = document.createElement('div');
            countDiv.className = 'system-prompt-char-count';
            
            if (length > maxLength * 0.9) {
                countDiv.classList.add('danger');
            } else if (length > maxLength * 0.7) {
                countDiv.classList.add('warning');
            }
            
            countDiv.textContent = `${length} / ${maxLength} characters`;
            input.parentNode.appendChild(countDiv);
        }
    }
    
    
    setupEventListeners() {
        // No API mode selector needed - OpenAI only
    }

    async startWakeDetection() {
        // Lightweight local health check every 30s to detect wake-from-sleep events.
        // This is NOT network polling — it checks the local /health endpoint only.
        this._wakeWarningDismissed = false;

        setInterval(async () => {
            try {
                const resp = await fetch(`${this.baseUrl}/health`);
                if (resp.ok) {
                    const data = await resp.json();
                    if (data.heartbeat && data.heartbeat.wake_events > 0 && !this._wakeWarningDismissed) {
                        const banner = document.getElementById('wake-warning-banner');
                        if (banner) {
                            banner.style.display = 'block';
                        }
                    }
                }
            } catch (e) {
                // Ignore — server may not be ready
            }
        }, 30000);
    }

    // ── Pool Methods ──

    async evictPoolModel(modelName) {
        if (!modelName || !confirm('Unload ' + modelName + ' from pool?')) return;
        try {
            const resp = await fetch(`${this.baseUrl}/models/pool/evict`, {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ model_name: modelName })
            });
            const data = await resp.json();
            if (data.success) {
                this.showToast('success', 'Unloaded: ' + modelName);
                await this.loadPoolStatus();
            } else {
                this.showToast('error', 'Failed: ' + (data.message || 'Unknown error'));
            }
        } catch (e) {
            this.showToast('error', 'Evict failed: ' + e.message);
        }
    }

    async loadPoolStatus() {
        try {
            const resp = await fetch(`${this.baseUrl}/models/pool`);
            if (resp.ok) {
                const data = await resp.json();
                if (data.enabled) {
                    this.updatePoolStatusBar(data);
                    this.updateModelSwitcher(data);
                    this.updatePoolCountBadge(data);
                    this.renderPoolModels(data);
                }
                return data;
            }
        } catch (e) {
            console.debug('Pool status not available:', e);
        }
        return null;
    }

    updatePoolStatusBar(poolData) {
        const bar = document.getElementById('pool-status-bar');
        if (!bar || !poolData) return;

        const used = poolData.used_slots || 0;
        const max = poolData.max_models || 0;
        const memoryUsed = poolData.memory_used_gb || 0;

        const dots = [];
        for (let i = 0; i < max; i++) {
            const slot = poolData.slots?.[i];
            if (slot?.is_active) {
                dots.push('<span class="pool-slot-indicator active" title="Active: ' + this.escapeHtml(slot.model_name) + '"></span>');
            } else if (slot) {
                dots.push('<span class="pool-slot-indicator loaded" title="Loaded: ' + this.escapeHtml(slot.model_name) + '"></span>');
            } else {
                dots.push('<span class="pool-slot-indicator empty" title="Empty slot"></span>');
            }
        }

        bar.innerHTML = 'Pool: ' + dots.join('') + ' ' + used + '/' + max + (memoryUsed > 0 ? ' \u00b7 ' + memoryUsed + ' GB' : '');
    }

    updateModelSwitcher(poolData) {
        const select = document.getElementById('pool-model-select');
        if (!select || !poolData?.slots?.length) return;

        if (poolData.slots.length <= 1) {
            select.classList.add('d-none');
            return;
        }

        select.classList.remove('d-none');
        select.innerHTML = poolData.slots.map(slot =>
            '<option value="' + this.escapeHtml(slot.model_name) + '" data-path="' + this.escapeHtml(slot.model_path) + '"' + (slot.is_active ? ' selected' : '') + '>' + this.escapeHtml(slot.model_name) + (slot.is_active ? ' \u26a1' : '') + '</option>'
        ).join('');
    }

    updatePoolCountBadge(poolData) {
        const badge = document.getElementById('poolCountBadge');
        if (!badge) return;
        const used = poolData?.used_slots || 0;
        if (used > 0) {
            badge.style.display = 'inline';
            badge.textContent = used;
        } else {
            badge.style.display = 'none';
        }
    }

    renderPoolModels(poolData) {
        const container = document.getElementById('poolModelsList');
        if (!container || !poolData) return;

        if (!poolData.enabled || poolData.used_slots === 0) {
            container.innerHTML = '<div class="text-center text-muted py-4">' +
                '<i class="fas fa-layer-group fa-2x mb-2"></i>' +
                '<p>No models loaded in pool</p>' +
                '<p class="small">Select a model from the Local Models tab to load it into the pool.</p>' +
                '</div>';
            return;
        }

        const memoryPercent = poolData.memory_percent || 0;
        const barClass = memoryPercent < 60 ? 'safe' : memoryPercent < 85 ? 'warning' : 'danger';
        const lruCandidate = poolData.lru_candidate;

        let html = '<div class="mb-3">';
        html += '<div class="d-flex justify-content-between align-items-center mb-2">';
        html += '<span class="small fw-bold"><i class="fas fa-memory"></i> Memory: ' +
            (poolData.memory_used_gb || 0) + ' / ' + (poolData.memory_budget_gb || 0) + ' GB</span>';
        html += '<span class="small text-muted">' + poolData.used_slots + '/' + poolData.max_models + ' slots</span>';
        html += '</div>';
        html += '<div class="pool-memory-bar"><div class="pool-memory-bar-fill ' + barClass +
            '" style="width: ' + memoryPercent + '%"></div></div>';
        html += '</div>';

        (poolData.slots || []).forEach(function(slot) {
            var isActive = slot.is_active;
            var isLRU = slot.model_name === lruCandidate;
            var itemClass = 'pool-model-item';
            if (isActive) itemClass += ' active-model';
            if (isLRU && !isActive) itemClass += ' lru-candidate';

            html += '<div class="' + itemClass + '">';
            html += '<div class="d-flex justify-content-between align-items-start">';
            html += '<div>';
            html += '<h6 class="mb-1">';
            if (isActive) {
                html += '<span class="pool-slot-indicator active"></span> ';
            } else {
                html += '<span class="pool-slot-indicator loaded"></span> ';
            }
            html += llamaNetUI.escapeHtml(slot.model_name);
            if (isActive) html += ' <span class="badge bg-primary ms-1">Active</span>';
            if (isLRU && !isActive) html += ' <span class="badge bg-warning text-dark ms-1">LRU</span>';
            html += '</h6>';
            html += '<div class="small text-muted">';
            html += '<div><i class="fas fa-hdd"></i> ' + (slot.size_display || 'Unknown') + '</div>';
            html += '<div><i class="fas fa-clock"></i> Last used: ' + (slot.last_accessed_ago < 60 ?
                Math.round(slot.last_accessed_ago) + 's ago' :
                Math.round(slot.last_accessed_ago / 60) + 'm ago') + '</div>';
            html += '<div><i class="fas fa-redo"></i> Accessed: ' + (slot.access_count || 0) + ' times</div>';
            if (slot.metrics) {
                html += '<div><i class="fas fa-bolt"></i> ' + ((slot.metrics.tps || 0)).toFixed(1) + ' TPS</div>';
            }
            html += '</div></div>';
            html += '<div class="d-flex gap-1">';
            if (!isActive) {
                html += '<button class="btn btn-sm btn-primary" onclick="llamaNetUI.switchPoolModel(\'' +
                    llamaNetUI.escapeHtml(slot.model_name) + '\', \'' + llamaNetUI.escapeHtml(slot.model_path) + '\')"><i class="fas fa-check"></i> Use</button>';
            }
            html += '<button class="btn btn-sm btn-outline-danger" onclick="llamaNetUI.evictPoolModel(\'' +
                llamaNetUI.escapeHtml(slot.model_name) + '\')"><i class="fas fa-times"></i> Unload</button>';
            html += '</div></div>';
            html += '</div>';
        });

        container.innerHTML = html;
    }

    async switchPoolModel(modelName, modelPath) {
        if (!modelName) return;
        try {
            const overlay = document.getElementById('model-reload-overlay');
            overlay.style.display = 'flex';

            const resp = await fetch(this.baseUrl + '/models/select', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ model_path: modelPath || modelName, load_mode: 'pool' })
            });

            const data = await resp.json();
            overlay.style.display = 'none';

            if (data.success) {
                const mode = data.data?.mode;
                if (mode === 'instant_switch') {
                    this.showToast('success', '\u26a1 Switched to ' + modelName + ' (instant)');
                } else {
                    this.showToast('success', data.message || ('Loaded ' + modelName));
                }
                this.selectedModel = modelName;
                this.updateChatInterface(modelName);
                localStorage.setItem('llamanet_selected_model', modelName);
                await this.loadPoolStatus();
            } else {
                this.showToast('error', 'Failed: ' + (data.message || 'Unknown error'));
            }
        } catch (e) {
            document.getElementById('model-reload-overlay').style.display = 'none';
            this.showToast('error', 'Switch failed: ' + e.message);
        }
    }

    async loadTunnelStatus() {
        try {
            const resp = await fetch(`${this.baseUrl}/tunnel/status`).catch(() => null);
            if (!resp || !resp.ok) return;
            if (resp.ok) {
                const data = await resp.json();
                const badge = document.getElementById('tunnel-status');
                const urlSpan = document.getElementById('tunnel-url');
                if (badge && urlSpan) {
                    if (data.active) {
                        badge.classList.remove('d-none');
                        urlSpan.textContent = data.url.replace('https://', '');
                        badge.title = `Tunnel: ${data.url}\nType: ${data.type}\nPeers: ${data.peers_count}\nClick to copy`;
                        badge.onclick = () => {
                            navigator.clipboard.writeText(data.url).then(() => {
                                this.showToast('success', `Tunnel URL copied: ${data.url}`);
                            });
                        };
                    }
                }
            }
        } catch (e) {
            // Tunnel endpoint may not exist on older deployments
        }
    }
    
    async loadInitialNetworkStatus() {
        this.showNetworkLoading();

        try {
            const modelsResponse = await fetch(`${this.baseUrl}/v1/models/network`);

            if (modelsResponse.ok) {
                const modelsData = await modelsResponse.json();
                this.updateActiveNodesFromAPI(modelsData);
                this.updateNetworkDisplayRealTime();
                console.log('✅ Initial network status loaded');
            } else {
                this.showNetworkError('Unable to connect to LlamaNet node');
            }
        } catch (error) {
            console.error('Error loading initial network status:', error);
            this.showNetworkError('Network discovery failed');
        }
    }
    
    startUnifiedSSENetworkMonitoring() {
        if (this.eventSource) {
            this.eventSource.close();
        }
        
        console.log('🔗 Starting unified SSE network monitoring...');
        this.updateSSEStatus('connecting', 'Establishing connection...');
        
        this.eventSource = new EventSource(`${this.baseUrl}/events/network`);
        
        this.eventSource.onopen = () => {
            console.log('✅ Unified SSE connected');
            this.isConnected = true;
            this.reconnectAttempts = 0;
            this.reconnectDelay = 1000;
            this.updateConnectionIndicator(true);
            this.connectionStatus = 'connected';
            this.errorCount = 0;
            this.updateSSEStatus('connected', 'Real-time updates active via unified SSE');
        };
        
        this.eventSource.onmessage = (event) => {
            try {
                const data = JSON.parse(event.data);
                this.handleUnifiedSSENetworkEvent(data);
            } catch (e) {
                console.error('Error parsing unified SSE event:', e);
            }
        };
        
        this.eventSource.onerror = (error) => {
            console.warn('❌ Unified SSE connection error:', error);
            this.isConnected = false;
            this.updateConnectionIndicator(false);
            
            if (this.reconnectAttempts < this.maxReconnectAttempts) {
                this.connectionStatus = 'error';
                this.updateSSEStatus('error', `Reconnecting to unified SSE... (attempt ${this.reconnectAttempts + 1}/${this.maxReconnectAttempts})`);
                
                this.reconnectAttempts++;
                const delay = this.reconnectDelay * Math.pow(2, this.reconnectAttempts - 1);
                
                console.log(`🔄 Reconnecting unified SSE in ${delay}ms (attempt ${this.reconnectAttempts}/${this.maxReconnectAttempts})`);
                
                setTimeout(() => {
                    if (!this.isConnected) {
                        this.startUnifiedSSENetworkMonitoring();
                    }
                }, delay);
            } else {
                this.connectionStatus = 'failed';
                this.updateSSEStatus('failed', 'Unified SSE connection failed - please refresh page');
                console.error('❌ Max unified SSE reconnection attempts reached');
                this.showToast('error', 'Lost connection to real-time updates. Please refresh the page.');
            }
        };
    }
    
    handleUnifiedSSENetworkEvent(data) {
        // Process unified SSE events with enhanced validation
        switch (data.type) {
            case 'connected':
                console.log('📡 Unified SSE network monitoring connected', data.server_info);
                this.showToast('success', 'Connected to unified real-time network updates');
                
                // Store connection info with unified SSE details
                this.connectionInfo = {
                    id: data.connection_id,
                    serverInfo: data.server_info,
                    connectedAt: data.timestamp,
                    sseVersion: data.server_info?.sse_version || '2.0',
                    features: data.server_info?.features || []
                };
                break;
                
            case 'node_joined':
            case 'node_updated':
                if (data.node_info) {
                    const normalizedNode = this.normalizeNodeDataWithValidation(data.node_info);
                    if (normalizedNode) {
                        // Detect model name change (hot-reload)
                        const previousNode = this.activeNodes.get(normalizedNode.node_id);
                        const modelChanged = previousNode && previousNode.model !== normalizedNode.model
                            && normalizedNode.model !== 'unknown';

                        this.activeNodes.set(normalizedNode.node_id, normalizedNode);
                        
                        // Set status based on event
                        this.nodeStatuses.set(normalizedNode.node_id, 'online');
                        this.nodeLastEvent.set(normalizedNode.node_id, Date.now());
                        this.nodeEventTypes.set(normalizedNode.node_id, data.type);
                        
                        const eventIcon = data.type === 'node_joined' ? '🆕' : '🔄';
                        const eventAction = data.type.split('_')[1];
                        
                        console.log(`${eventIcon} Node ${eventAction} (Unified SSE): ${normalizedNode.node_id.substring(0, 8)}... (${normalizedNode.model})`);
                        
                        if (data.type === 'node_joined') {
                            this.showToast('success', `🆕 Node joined: ${normalizedNode.node_id.substring(0, 8)}... (${normalizedNode.model})`);
                        }
                        
                        // Handle model change (hot-reload / switch)
                        if (modelChanged) {
                            console.log('🔄 Model changed: ' + previousNode.model + ' → ' + normalizedNode.model);
                            this.selectedModel = normalizedNode.model;
                            this.updateChatInterface(normalizedNode.model);
                            localStorage.setItem('llamanet_selected_model', normalizedNode.model);
                            this.showToast('success', '🔄 Model switched to: ' + normalizedNode.model);
                        }

                        // Refresh pool status on model changes
                        if (modelChanged || data.source === 'pool_load' || data.source === 'instant_switch') {
                            this.loadPoolStatus();
                        }
                        
                        this.updateNetworkDisplayRealTime();
                    }
                }
                break;
                
            case 'node_left':
                if (data.node_info) {
                    const nodeId = data.node_info.node_id;
                    
                    // Mark as offline via event (using consolidated validation)
                    this.nodeStatuses.set(nodeId, 'offline');
                    this.nodeLastEvent.set(nodeId, Date.now());
                    this.nodeEventTypes.set(nodeId, 'node_left');
                    
                    // Keep node in activeNodes for a short time to show "offline" status
                    setTimeout(() => {
                        this.activeNodes.delete(nodeId);
                        this.nodeStatuses.delete(nodeId);
                        this.nodeLastEvent.delete(nodeId);
                        this.nodeEventTypes.delete(nodeId);
                        this.updateNetworkDisplayRealTime();
                    }, 30000);
                    
                    console.log(`👋 Node left (Unified SSE): ${nodeId.substring(0, 8)}...`);
                    this.showToast('warning', `👋 Node left: ${nodeId.substring(0, 8)}...`);
                    this.updateNetworkDisplayRealTime();
                }
                break;
                
            case 'network_changed':
                console.log('🌐 Network topology changed (Unified SSE)');
                // Force refresh using consolidated network utilities
                this.refreshNetworkDataOnTopologyChange();
                break;
                
            case 'heartbeat':
                // Unified SSE heartbeat - keep connection alive
                this.lastUpdateTime = Date.now();
                this.connectionStatus = 'connected';
                
                // Update connection info with unified SSE data
                if (data.active_connections !== undefined) {
                    this.connectionInfo = {
                        ...this.connectionInfo,
                        activeConnections: data.active_connections,
                        uptime: data.uptime,
                        lastHeartbeat: data.timestamp,
                        unifiedSSE: true
                    };
                }
                
                this.updateConnectionIndicatorWithHeartbeat(data);
                break;
                
            case 'error':
                console.error('Unified SSE network event error:', data.message);
                this.showToast('error', `Network error: ${data.message}`);
                this.connectionStatus = 'error';
                break;
                
            default:
                console.log('Unknown unified SSE network event:', data);
        }
    }
    
    normalizeNodeDataWithValidation(nodeData) {
        // Use consolidated validation utilities
        try {
            // Basic structure validation
            if (!nodeData || typeof nodeData !== 'object') {
                console.warn('Invalid node data structure:', nodeData);
                return null;
            }
            
            // Only require node_id — url (tunnel) is the primary address
            if (!nodeData.node_id) {
                console.warn('Missing node_id in node data:', nodeData);
                return null;
            }
            
            // Return normalized and validated data with all NodeInfo fields
            return {
                node_id: nodeData.node_id,
                url: nodeData.url || '',
                model: nodeData.model || nodeData.model_name || nodeData.id || 'unknown',
                load: parseFloat(nodeData.load) || 0,
                tps: parseFloat(nodeData.tps) || 0,
                uptime: parseInt(nodeData.uptime) || 0,
                last_seen: parseInt(nodeData.last_seen) || Math.floor(Date.now() / 1000),
                dht_port: null,
                
                // Event-driven metadata from NodeInfo model
                event_driven: nodeData.event_driven !== undefined ? nodeData.event_driven : true,
                last_significant_change: nodeData.last_significant_change || null,
                change_reason: nodeData.change_reason || null,
                
                // Tunnel URL (primary address)
                
                // Additional metadata from NodeInfo model
                cpu_info: nodeData.cpu_info || null,
                ram_total: nodeData.ram_total || null,
                gpu_info: nodeData.gpu_info || null,
                context_size: nodeData.context_size || null,
                
                // Performance metrics from node
                ttft: parseFloat(nodeData.ttft) || null,
                latency: parseFloat(nodeData.latency) || null,
                total_tokens: parseInt(nodeData.total_tokens) || 0,
                
                // UI validation metadata
                validated: true,
                validation_timestamp: Date.now()
            };
            
        } catch (error) {
            console.error('Error validating node data:', error, nodeData);
            return null;
        }
    }
    
    updateNetworkDisplayRealTime() {
        const container = document.getElementById('network-status');
        if (!container) return;
        
        // ALWAYS use activeNodes as source of truth (has latest SSE + API data)
        const nodes = Array.from(this.activeNodes.values());
        
        // Group nodes by model, merging fresh metrics from activeNodes
        const modelGroups = {};
        nodes.forEach(node => {
            const model = node.model || 'unknown';
            if (!modelGroups[model]) {
                modelGroups[model] = [];
            }
            modelGroups[model].push(node);
        });
        
        // Calculate network stats using server-compatible structure
        const totalNodes = nodes.length;
        const avgLoad = nodes.length > 0 ? nodes.reduce((sum, n) => sum + n.load, 0) / nodes.length : 0;
        const totalTps = nodes.reduce((sum, n) => sum + n.tps, 0);
        const onlineNodes = nodes.filter(n => {
            const eventStatus = this.nodeStatuses.get(n.node_id);
            return eventStatus === 'online' || (Date.now() / 1000) - n.last_seen < 60;
        }).length;
        
        // Create network summary compatible with server format
        const networkSummary = {
            total_models: Object.keys(modelGroups).length,
            total_nodes: totalNodes,
            avg_network_load: avgLoad,
            total_network_tps: totalTps,
            timestamp: Date.now() / 1000
        };
        
        // Update network stats with server-compatible structure
        this.nodeStats = {
            totalNodes,
            onlineNodes,
            modelsAvailable: new Set(Object.keys(modelGroups)),
            networkHealth: this.calculateNetworkHealth(avgLoad, onlineNodes),
            networkSummary: networkSummary  // Store server-compatible summary
        };
        
        // Create enhanced content with refresh timestamp
        const refreshTime = new Date().toLocaleTimeString();
        const newContent = `
            <div class="mb-3">
                <h6>
                    <i class="fas fa-server"></i> Local Node
                    <span class="live-indicator ms-2" title="Real-time updates via SSE">
                        <i class="fas fa-circle text-success live-pulse" style="font-size: 0.5rem;"></i>
                    </span>
                    <small class="text-muted ms-2" id="sse-status">Live</small>
                </h6>
                <div class="small mb-2">
                    <div><i class="fas fa-server"></i> Node: <span class="metric-value">${nodes.length > 0 ? nodes[0].node_id.substring(0, 8) + '...' : 'N/A'}</span></div>
                    <div><i class="fas fa-brain"></i> Models Loaded: <span class="metric-value">${this.nodeStats.modelsAvailable.size}</span></div>
                    <div><i class="fas fa-heartbeat"></i> Health: ${this.getHealthBadge(this.nodeStats.networkHealth)}</div>
                    <div class="text-muted mt-1">
                        <i class="fas fa-clock"></i> Last refresh: ${refreshTime}
                        ${this.isConnected ? '<i class="fas fa-broadcast-tower ms-2 text-success" title="Real-time updates active"></i>' : ''}
                    </div>
                </div>
            </div>
            
            <div class="mb-3">
                <h6><i class="fas fa-brain"></i> Available Models</h6>
                ${Object.keys(modelGroups).length > 0 ? this.renderModelGroupsRealTime(modelGroups) : '<div class="text-muted small">No models discovered on network</div>'}
            </div>
        `;
        
        // Smooth update with maintained selection state
        const selectedModel = document.querySelector('.model-group.selected-model')?.dataset.model;
        
        container.style.opacity = '0.9';
        setTimeout(() => {
            container.innerHTML = newContent;
            container.style.opacity = '1';
            
            // Restore selected model state
            if (selectedModel) {
                const selectedGroup = document.querySelector(`[data-model="${selectedModel}"]`);
                if (selectedGroup) {
                    selectedGroup.classList.add('selected-model');
                }
            }
            
            // Add subtle refresh animation
            container.style.transform = 'scale(1.01)';
            setTimeout(() => {
                container.style.transform = 'scale(1)';
            }, 200);
        }, 100);
    }
    
    renderModelGroupsRealTime(modelGroups) {
        if (Object.keys(modelGroups).length === 0) {
            return '<div class="text-muted small">No models discovered on network</div>';
        }
        
        return Object.entries(modelGroups).map(([modelName, nodes]) => {
            const avgLoad = nodes.reduce((sum, n) => sum + n.load, 0) / nodes.length;
            const totalTps = nodes.reduce((sum, n) => sum + n.tps, 0);
            const availability = this.getAvailability(nodes.length);
            const availabilityClass = this.getAvailabilityClass(availability);
            
            return `
                <div class="model-group mb-2" data-model="${modelName}">
                    <div class="d-flex justify-content-between align-items-center mb-1">
                        <div class="fw-bold small text-primary">
                            <i class="fas fa-brain"></i> ${modelName}
                            <span class="badge bg-${availabilityClass} ms-1">${availability}</span>
                        </div>
                        <button class="btn btn-sm btn-outline-primary" onclick="llamaNetUI.selectModel('${modelName}')" title="Select this model"><i class="fas fa-check"></i></button>
                    </div>
                    <div class="model-nodes" style="max-height: 150px; overflow-y: auto;">
                        ${this.renderModelNodesRealTime(nodes)}
                    </div>
                </div>
            `;
        }).join('');
    }
    
    renderModelNodesRealTime(nodes) {
        return nodes.map(node => {
            // Use event-driven status instead of time-based calculation
            const eventStatus = this.nodeStatuses.get(node.node_id) || 'unknown';
            const lastEventTime = this.nodeLastEvent.get(node.node_id) || 0;
            const lastEventType = this.nodeEventTypes.get(node.node_id) || '';
            
            let statusClass, statusTitle;
            
            switch (eventStatus) {
                case 'online':
                    statusClass = 'online';
                    statusTitle = lastEventType === 'node_joined' ? 'Online (joined)' : 'Online (active)';
                    break;
                case 'offline':
                    statusClass = 'offline';
                    statusTitle = 'Offline (left network)';
                    break;
                case 'unknown':
                default:
                    // Fallback for nodes discovered before events started
                    const timeSinceLastSeen = (Date.now() / 1000) - node.last_seen;
                    if (timeSinceLastSeen < 60) {
                        statusClass = 'online';
                        statusTitle = 'Online (discovered)';
                        // Set status for future updates
                        this.nodeStatuses.set(node.node_id, 'online');
                        this.nodeLastEvent.set(node.node_id, Date.now());
                    } else {
                        statusClass = 'warning';
                        statusTitle = 'Status unknown';
                    }
                    break;
            }
            
            const lastSeenText = this.formatLastSeen(node.last_seen);
            const uptimeText = node.uptime ? `${Math.floor(node.uptime / 60)}m` : 'Unknown';
            const eventAge = lastEventTime ? this.formatEventAge(lastEventTime) : '';
            
            return `
                <div class="node-item small ms-2 clickable-node event-updated" data-node-id="${node.node_id}" onclick="llamaNetUI.showNodeInfo('${node.node_id}')" style="cursor: pointer;">
                    <div class="d-flex align-items-center">
                        <span class="node-status ${statusClass}" title="${statusTitle}${eventAge ? ` - Event: ${eventAge}` : ''}"></span>
                        <div class="flex-grow-1">
                            <div class="fw-bold">
                                ${node.node_id.substring(0, 8)}... 
                                <i class="fas fa-info-circle text-primary ms-1 node-info-icon" title="Click for details"></i>
                                ${eventStatus === 'offline' ? '<i class="fas fa-times-circle text-danger ms-1" title="Node left network"></i>' : ''}
                                ${lastEventType === 'node_joined' ? '<i class="fas fa-plus-circle text-success ms-1" title="Recently joined"></i>' : ''}
                            </div>
                            <div class="text-muted small">
                                <div><i class="fas fa-globe"></i> ${this.getNodeAddress(node)}</div>
                                <div><i class="fas fa-clock"></i> Up: ${uptimeText} | ${lastSeenText}</div>
                                ${this.renderNodeMetricsBadge(node)}
                                ${eventAge ? `<div><i class="fas fa-broadcast-tower"></i> Event: ${eventAge}</div>` : ''}
                            </div>
                        </div>
                    </div>
                </div>
            `;
        }).join('');
    }
    
    formatEventAge(eventTime) {
        const now = Date.now();
        const diff = (now - eventTime) / 1000;
        
        if (diff < 5) return 'just now';
        if (diff < 60) return `${Math.floor(diff)}s ago`;
        if (diff < 3600) return `${Math.floor(diff / 60)}m ago`;
        return `${Math.floor(diff / 3600)}h ago`;
    }
    
    formatMetricTime(value) {
        const num = typeof value === 'number' ? value : parseFloat(value);
        if (!num || num <= 0) return 'N/A';
        if (num < 1) return `${(num * 1000).toFixed(0)}ms`;
        return `${num.toFixed(2)}s`;
    }
    
    renderNodeMetricsBadge(node) {
        if (!node) return '';
        const parts = [];
        
        // TPS (always show)
        const tps = this._safeMetric(node.tps, 0);
        parts.push(`<span class="node-metric-badge"><i class="fas fa-bolt"></i> ${tps.toFixed(1)} TPS</span>`);
        
        // TTFT (show if we have a value)
        const ttft = this._safeMetric(node.ttft, null);
        if (ttft !== null && ttft > 0) {
            const ttftDisplay = ttft < 1 ? `${(ttft * 1000).toFixed(0)}ms` : `${ttft.toFixed(2)}s`;
            parts.push(`<span class="node-metric-badge"><i class="fas fa-stopwatch"></i> ${ttftDisplay} TTFT</span>`);
        }
        
        // Latency (show if we have a value)
        const latency = this._safeMetric(node.latency, null);
        if (latency !== null && latency > 0) {
            const latDisplay = latency < 1 ? `${(latency * 1000).toFixed(0)}ms` : `${latency.toFixed(2)}s`;
            parts.push(`<span class="node-metric-badge"><i class="fas fa-tachometer-alt"></i> ${latDisplay} Latency</span>`);
        }
        
        // Total tokens (show if > 0)
        const totalTokens = this._safeMetric(node.total_tokens, 0);
        if (totalTokens > 0) {
            const tokenDisplay = totalTokens >= 1000000 ? `${(totalTokens / 1000000).toFixed(1)}M` :
                                 totalTokens >= 1000 ? `${(totalTokens / 1000).toFixed(1)}K` :
                                 `${totalTokens}`;
            parts.push(`<span class="node-metric-badge"><i class="fas fa-coins"></i> ${tokenDisplay} Tokens</span>`);
        }
        
        return parts.length > 0 ? `<div class="node-metrics-container">${parts.join('')}</div>` : '';
    }
    
    _safeMetric(value, defaultVal) {
        if (value === null || value === undefined) return defaultVal;
        const num = typeof value === 'number' ? value : parseFloat(value);
        return isNaN(num) ? defaultVal : num;
    }
    
    calculateNetworkHealth(avgLoadOrSummary, nodeCount) {
        let avgLoad, totalNodes;
        
        // Handle both formats: (avgLoad, nodeCount) and (networkSummary object)
        if (typeof avgLoadOrSummary === 'object' && avgLoadOrSummary !== null) {
            // networkSummary object format
            avgLoad = avgLoadOrSummary.avg_network_load || 0;
            totalNodes = avgLoadOrSummary.total_nodes || 0;
        } else {
            // Direct parameters format
            avgLoad = avgLoadOrSummary || 0;
            totalNodes = nodeCount || 0;
        }
        
        if (totalNodes === 0) return 'no_nodes';
        if (totalNodes === 1) return 'limited';
        if (avgLoad < 0.3 && totalNodes >= 3) return 'excellent';
        if (avgLoad < 0.7 && totalNodes >= 2) return 'good';
        if (totalNodes >= 2) return 'fair';
        return 'poor';
    }
    
    getAvailability(nodeCount) {
        if (nodeCount >= 3) return 'high';
        if (nodeCount >= 2) return 'medium';
        return 'low';
    }
    
    getAvailabilityClass(availability) {
        const classes = { 'high': 'success', 'medium': 'warning', 'low': 'danger' };
        return classes[availability] || 'secondary';
    }
    
    updateConnectionIndicator(connected) {
        const indicators = document.querySelectorAll('.live-indicator');
        indicators.forEach(indicator => {
            if (connected) {
                indicator.innerHTML = '<i class="fas fa-circle text-success" style="font-size: 0.5rem;"></i>';
                indicator.title = 'Real-time updates active';
            } else {
                indicator.innerHTML = '<i class="fas fa-circle text-danger" style="font-size: 0.5rem;"></i>';
                indicator.title = 'Real-time updates disconnected';
            }
        });
    }
    
    updateSSEStatus(status, details = '') {
        const statusElement = document.getElementById('sse-status');
        if (statusElement) {
            let statusText = '';
            let statusClass = '';
            
            switch (status) {
                case 'connected':
                    statusText = 'Live';
                    statusClass = 'text-success';
                    break;
                case 'connecting':
                    statusText = 'Connecting...';
                    statusClass = 'text-warning';
                    break;
                case 'error':
                    statusText = 'Reconnecting...';
                    statusClass = 'text-warning';
                    break;
                case 'failed':
                    statusText = 'Failed';
                    statusClass = 'text-danger';
                    break;
                case 'disconnected':
                    statusText = 'Disconnected';
                    statusClass = 'text-danger';
                    break;
                default:
                    statusText = 'Unknown';
                    statusClass = 'text-muted';
            }
            
            // Clear existing classes and apply new ones
            statusElement.className = `text-muted ms-2 ${statusClass}`;
            statusElement.textContent = statusText;
            
            if (details) {
                statusElement.title = details;
            }
        }
    }
    
    updateConnectionIndicatorWithHeartbeat(heartbeatData) {
        const indicators = document.querySelectorAll('.live-indicator');
        indicators.forEach(indicator => {
            if (this.isConnected) {
                const uptime = heartbeatData.uptime ? Math.floor(heartbeatData.uptime / 60) : 0;
                const connections = heartbeatData.active_connections || 1;
                
                indicator.innerHTML = '<i class="fas fa-circle text-success live-pulse" style="font-size: 0.5rem;"></i>';
                indicator.title = `Live updates active (${uptime}m uptime, ${connections} connections)`;
            } else {
                indicator.innerHTML = '<i class="fas fa-circle text-danger" style="font-size: 0.5rem;"></i>';
                indicator.title = 'Real-time updates disconnected';
            }
        });
    }
    
    async refreshNetworkDataOnTopologyChange() {
        try {
            const modelsResponse = await fetch(`${this.baseUrl}/v1/models/network`).catch(() => null);

            if (modelsResponse && modelsResponse.ok) {
                const modelsData = await modelsResponse.json();
                this.updateActiveNodesFromAPI(modelsData);
                this.updateNetworkDisplayRealTime();
                console.log('🔄 Network data refreshed');
            } else {
                this.updateNetworkDisplayRealTime();
            }
        } catch (error) {
            console.error('Error refreshing network data:', error);
            this.updateNetworkDisplayRealTime();
        }
    }
    
    updateActiveNodesFromAPI(modelsData) {
        // Preserve existing event-driven status before rebuild
        const previousNodes = new Map(this.activeNodes);
        
        // Clear and rebuild from fresh API data
        this.activeNodes.clear();
        
        if (modelsData.data) {
            modelsData.data.forEach(model => {
                const modelName = model.id;
                
                if (model.nodes) {
                    model.nodes.forEach(node => {
                        const nodeWithModel = {
                            ...node,
                            model: modelName,
                            model_name: modelName
                        };
                        
                        const normalizedNode = this.normalizeNodeDataWithValidation(nodeWithModel);
                        if (normalizedNode) {
                            this.activeNodes.set(normalizedNode.node_id, normalizedNode);
                            
                            // Preserve existing SSE event status if available
                            const prevStatus = this.nodeStatuses.get(normalizedNode.node_id);
                            if (prevStatus && prevStatus !== 'unknown') {
                                // Keep existing SSE-driven status — don't overwrite with time-based
                                this.nodeLastEvent.set(normalizedNode.node_id, Date.now());
                            } else {
                                // Fallback to time-based status for new/unknown nodes
                                const timeSinceLastSeen = (Date.now() / 1000) - normalizedNode.last_seen;
                                if (timeSinceLastSeen < 60) {
                                    this.nodeStatuses.set(normalizedNode.node_id, 'online');
                                    this.nodeLastEvent.set(normalizedNode.node_id, Date.now());
                                    this.nodeEventTypes.set(normalizedNode.node_id, 'topology_refresh');
                                } else {
                                    this.nodeStatuses.set(normalizedNode.node_id, 'unknown');
                                }
                            }
                            
                            // Detect model change from API refresh
                            const prevNode = previousNodes.get(normalizedNode.node_id);
                            if (prevNode && prevNode.model !== normalizedNode.model && normalizedNode.model !== 'unknown') {
                                console.log(`📊 Model change detected via API: ${prevNode.model} → ${normalizedNode.model}`);
                            }
                        }
                    });
                }
            });
        }
        
        console.log(`📊 Updated activeNodes from API: ${this.activeNodes.size} nodes`);
    }
    
    stopSSENetworkMonitoring() {
        if (this.eventSource) {
            this.eventSource.close();
            this.eventSource = null;
        }
        this.isConnected = false;
        this.updateConnectionIndicator(false);
        this.updateSSEStatus('disconnected', 'Connection closed');
    }
    
    async refreshNetworkStatus() {
        // Manual refresh - maintain SSE-based UX while validating data
        try {
            this.showUpdateIndicator(true);
            
            // Get fresh data from API for validation and potential updates
            const [modelsResponse, statsResponse] = await Promise.all([
                fetch(`${this.baseUrl}/v1/models/network`).catch(() => null),
                fetch(`${this.baseUrl}/models/statistics`).catch(() => null)
            ]);
            
            // Check if SSE is still connected
            if (!this.isConnected) {
                console.log('🔄 SSE disconnected during refresh, reconnecting...');
                this.startUnifiedSSENetworkMonitoring();
            }
            
            // If we got fresh API data, use it to update/validate our display
            if (modelsResponse && modelsResponse.ok) {
                const modelsData = await modelsResponse.json();
                let statsData = null;
                
                if (statsResponse && statsResponse.ok) {
                    statsData = await statsResponse.json();
                }
                
                // Update activeNodes from fresh API data
                this.updateActiveNodesFromAPI(modelsData);
                
                // Force update the display with fresh data
                this.updateNetworkDisplayRealTime();
                
                // Update network stats if we have them
                if (statsData) {
                    this.updateNetworkStatsFromAPI(statsData);
                }
                
                console.log('🔄 Network status refreshed successfully');
                this.showToast('success', `Network refreshed: ${modelsData.total_nodes} nodes, ${modelsData.total_models} models`);
                
            } else {
                // API failed, but we can still refresh the SSE-based display
                console.warn('⚠️ API refresh failed, updating SSE-based display');
                this.updateNetworkDisplayRealTime();
                this.showToast('warning', 'Partial refresh - API unavailable, SSE data maintained');
            }
            
            // Reset error count on successful refresh
            this.errorCount = 0;
            
        } catch (error) {
            console.error('Error refreshing network status:', error);
            
            // On error, try to reconnect SSE and update current display
            if (!this.isConnected) {
                console.log('🔄 Attempting SSE reconnection due to refresh error...');
                this.startSSENetworkMonitoring();
            }
            
            // Still try to update the current display
            this.updateNetworkDisplayRealTime();
            
            this.showToast('error', `Refresh failed: ${error.message}`);
        } finally {
            this.showUpdateIndicator(false);
        }
    }
    
    async updateNetworkDisplay(dhtStatus, modelsData, statsData) {
        this.previousModelStats = this.currentModelStats || {};
        this.currentModelStats = (statsData && statsData.models) || {};
        
        this.previousNodeStates = this.currentNodeStates || {};
        this.currentNodeStates = {};
        
        // Build current node states
        if (modelsData.data) {
            modelsData.data.forEach(model => {
                if (model.nodes) {
                    model.nodes.forEach(node => {
                        this.currentNodeStates[node.node_id] = {
                            lastSeen: node.last_seen,
                            load: node.load,
                            tps: node.tps
                        };
                    });
                }
            });
        }
        
        await this.updateNetworkDisplaySmooth(dhtStatus, modelsData, statsData);
    }
    
    async updateNetworkDisplaySmooth(dhtStatus, modelsData, statsData) {
        const container = document.getElementById('network-status');
        
        try {
            // Get current node info
            const nodeResponse = await fetch(`${this.baseUrl}/info`);
            const nodeInfo = await nodeResponse.json();
            
            // Create new content
            const newContent = `
                <div class="mb-3">
                    <h6>
                        <i class="fas fa-server"></i> Current Node
                        <span class="update-timestamp small text-muted ms-2" title="Last updated: ${new Date().toLocaleTimeString()}">
                            <i class="fas fa-clock"></i>
                        </span>
                    </h6>
                    <div class="node-item">
                        <div class="d-flex align-items-center">
                            <span class="node-status online"></span>
                            <div class="flex-grow-1">
                                <div class="fw-bold">${nodeInfo.node_id.substring(0, 12)}...</div>
                                <small class="text-muted">${nodeInfo.model}</small>
                            </div>
                        </div>
                    </div>
                </div>
                
                <div class="mb-3">
                    <h6>
                        <i class="fas fa-brain"></i> Available Models
                        <span class="live-indicator ms-2" title="Live updates every ${this.updateFrequency/1000}s">
                            <i class="fas fa-circle text-success" style="font-size: 0.5rem;"></i>
                        </span>
                    </h6>
                    <div class="small mb-2">
                        <div>Total Models: <span class="metric-value">${modelsData.total_models}</span></div>
                        <div>Total Nodes: <span class="metric-value">${modelsData.total_nodes}</span></div>
                        <div>Network Health: ${this.getHealthBadge(statsData.network_summary)}</div>
                    </div>
                    ${this.renderAvailableModelsWithAnimation(modelsData.data, statsData.models)}
                </div>
                
            `;
            
            // Smooth update with fade transition
            container.style.opacity = '0.7';
            setTimeout(() => {
                container.innerHTML = newContent;
                container.style.opacity = '1';
                
                // Highlight changed metrics
                this.highlightChangedMetrics(container);
            }, 150);
            
        } catch (error) {
            console.error('Error updating network display:', error);
            this.showNetworkError('Failed to get network information');
        }
    }
    
    renderNodeList(contacts) {
        if (!contacts || contacts.length === 0) {
            return '<div class="text-muted small">No other nodes discovered</div>';
        }
        
        return contacts.map(contact => {
            const isRecent = (Date.now() / 1000) - contact.last_seen < 60;
            const statusClass = isRecent ? 'online' : 'warning';
            
            return `
                <div class="node-item small">
                    <div class="d-flex align-items-center">
                        <span class="node-status ${statusClass}"></span>
                        <div class="flex-grow-1">
                            <div>${contact.node_id.substring(0, 8)}...</div>
                            <div class="text-muted">${contact.ip}:${contact.port}</div>
                        </div>
                    </div>
                </div>
            `;
        }).join('');
    }
    
    renderAvailableModels(models, modelStats) {
        if (!models || models.length === 0) {
            return '<div class="text-muted small">No models discovered on network</div>';
        }
        
        return models.map(model => {
            const stats = modelStats[model.id] || {};
            const availability = stats.availability || 'unknown';
            const avgLoad = stats.avg_load || 0;
            const totalTps = stats.total_tps || 0;
            
            const availabilityClass = {
                'high': 'success',
                'medium': 'warning', 
                'low': 'danger',
                'unknown': 'secondary'
            }[availability] || 'secondary';
            
            return `
                <div class="model-group mb-2" data-model="${model.id}">
                    <div class="d-flex justify-content-between align-items-center mb-1">
                        <div class="fw-bold small text-primary">
                            <i class="fas fa-brain"></i> ${model.id}
                            <span class="badge bg-${availabilityClass} ms-1">${availability}</span>
                        </div>
                        <button class="btn btn-sm btn-outline-primary" onclick="llamaNetUI.selectModel('${model.id}')" title="Select this model">
                            <i class="fas fa-check"></i>
                        </button>
                    </div>
                    <div class="model-nodes" style="max-height: 150px; overflow-y: auto;">
                        ${this.renderModelNodes(model.nodes)}
                    </div>
                    <div class="mt-1">
                        <button class="btn btn-sm btn-outline-info" onclick="llamaNetUI.showModelDetails('${model.id}')" title="View model details">
                            <i class="fas fa-info-circle"></i> Details
                        </button>
                    </div>
                </div>
            `;
        }).join('');
    }
    
    renderAvailableModelsWithAnimation(models, modelStats) {
        if (!models || models.length === 0) {
            return '<div class="text-muted small">No models discovered on network</div>';
        }
        
        return models.map(model => {
            const stats = modelStats[model.id] || {};
            const availability = stats.availability || 'unknown';
            const avgLoad = stats.avg_load || 0;
            const totalTps = stats.total_tps || 0;
            
            const availabilityClass = {
                'high': 'success',
                'medium': 'warning', 
                'low': 'danger',
                'unknown': 'secondary'
            }[availability] || 'secondary';
            
            // Add animation classes for new or updated models
            const animationClass = this.isModelUpdated(model.id, stats) ? 'model-updated' : '';
            
            return `
                <div class="model-group mb-2 ${animationClass}" data-model="${model.id}">
                    <div class="d-flex justify-content-between align-items-center mb-1">
                        <div class="fw-bold small text-primary">
                            <i class="fas fa-brain"></i> ${model.id}
                            <span class="badge bg-${availabilityClass} ms-1">${availability}</span>
                            ${this.getModelChangeIndicator(model.id, stats)}
                        </div>
                        <button class="btn btn-sm btn-outline-primary" onclick="llamaNetUI.selectModel('${model.id}')" title="Select this model">
                            <i class="fas fa-check"></i>
                        </button>
                    </div>
                    <div class="model-nodes" style="max-height: 150px; overflow-y: auto;">
                        ${this.renderModelNodesWithStatus(model.nodes)}
                    </div>
                    <div class="mt-1">
                        <button class="btn btn-sm btn-outline-info" onclick="llamaNetUI.showModelDetails('${model.id}')" title="View model details">
                            <i class="fas fa-info-circle"></i> Details
                        </button>
                    </div>
                </div>
            `;
        }).join('');
    }
    
    renderModelNodes(nodes) {
        if (!nodes || nodes.length === 0) {
            return '<div class="text-muted small">No nodes available</div>';
        }
        
        return nodes.map(node => {
            const isRecent = (Date.now() / 1000) - node.last_seen < 60;
            const statusClass = isRecent ? 'online' : 'warning';
            const lastSeenText = this.formatLastSeen(node.last_seen);
            
            return `
                <div class="node-item small ms-2 clickable-node" data-node-id="${node.node_id}" onclick="llamaNetUI.showNodeInfo('${node.node_id}')" style="cursor: pointer;">
                    <div class="d-flex align-items-center">
                        <span class="node-status ${statusClass}" title="Last seen: ${lastSeenText}"></span>
                        <div class="flex-grow-1">
                            <div class="fw-bold">${node.node_id.substring(0, 8)}... <i class="fas fa-info-circle text-primary ms-1" title="Click for details"></i></div>
                            <div class="text-muted"><i class="fas fa-globe"></i> ${this.getNodeAddress(node)}</div>
                            <div class="text-muted small">${lastSeenText}</div>
                        </div>
                    </div>
                </div>
            `;
        }).join('');
    }
    
    renderModelNodesWithStatus(nodes) {
        if (!nodes || nodes.length === 0) {
            return '<div class="text-muted small">No nodes available</div>';
        }
        
        return nodes.map(node => {
            // Use event-driven status instead of time-based calculation
            const eventStatus = this.nodeStatuses.get(node.node_id) || 'unknown';
            const lastEventTime = this.nodeLastEvent.get(node.node_id) || 0;
            const lastEventType = this.nodeEventTypes.get(node.node_id) || '';
            
            let statusClass, statusTitle;
            
            switch (eventStatus) {
                case 'online':
                    statusClass = 'online';
                    statusTitle = lastEventType === 'node_joined' ? 'Online (joined)' : 'Online (active)';
                    break;
                case 'offline':
                    statusClass = 'offline';
                    statusTitle = 'Offline (left network)';
                    break;
                case 'unknown':
                default:
                    // Fallback for nodes discovered before events started
                    const timeSinceLastSeen = (Date.now() / 1000) - node.last_seen;
                    if (timeSinceLastSeen < 60) {
                        statusClass = 'online';
                        statusTitle = 'Online (discovered)';
                        // Set status for future updates
                        this.nodeStatuses.set(node.node_id, 'online');
                        this.nodeLastEvent.set(node.node_id, Date.now());
                    } else {
                        statusClass = 'warning';
                        statusTitle = 'Status unknown';
                    }
                    break;
            }
            
            const lastSeenText = this.formatLastSeen(node.last_seen);
            const nodeChangeClass = this.isNodeUpdated(node.node_id) ? 'node-updated' : '';
            const eventAge = lastEventTime ? this.formatEventAge(lastEventTime) : '';
            
            return `
                <div class="node-item small ms-2 clickable-node ${nodeChangeClass}" data-node-id="${node.node_id}" onclick="llamaNetUI.showNodeInfo('${node.node_id}')" style="cursor: pointer;">
                    <div class="d-flex align-items-center">
                        <span class="node-status ${statusClass}" title="${statusTitle}${eventAge ? ` - Event: ${eventAge}` : ''}"></span>
                        <div class="flex-grow-1">
                            <div class="fw-bold">
                                ${node.node_id.substring(0, 8)}... 
                                <i class="fas fa-info-circle text-primary ms-1 node-info-icon" title="Click for details"></i>
                                ${eventStatus === 'offline' ? '<i class="fas fa-times-circle text-danger ms-1" title="Node left network"></i>' : ''}
                                ${lastEventType === 'node_joined' ? '<i class="fas fa-plus-circle text-success ms-1" title="Recently joined"></i>' : ''}
                            </div>
                            <div class="text-muted small">
                                <div><i class="fas fa-home"></i> local</div>
                                <div><i class="fas fa-clock"></i> Up: ${node.uptime ? `${Math.floor(node.uptime / 60)}m` : 'Unknown'} | ${lastSeenText}</div>
                                ${this.renderNodeMetricsBadge(node)}
                                ${eventAge ? `<div><i class="fas fa-broadcast-tower"></i> Event: ${eventAge}</div>` : ''}
                            </div>
                        </div>
                    </div>
                </div>
            `;
        }).join('');
    }
    
    getHealthBadge(networkHealth) {
        const healthConfig = {
            'excellent': { class: 'success', text: 'Excellent', icon: 'fas fa-check-circle' },
            'good': { class: 'success', text: 'Good', icon: 'fas fa-check-circle' },
            'fair': { class: 'warning', text: 'Fair', icon: 'fas fa-exclamation-circle' },
            'limited': { class: 'warning', text: 'Limited', icon: 'fas fa-exclamation-triangle' },
            'poor': { class: 'danger', text: 'Poor', icon: 'fas fa-times-circle' },
            'no_nodes': { class: 'secondary', text: 'No Nodes', icon: 'fas fa-question-circle' },
            'unknown': { class: 'secondary', text: 'Unknown', icon: 'fas fa-question-circle' }
        };
        
        const config = healthConfig[networkHealth] || healthConfig['unknown'];
        
        return `<span class="badge bg-${config.class}"><i class="${config.icon} me-1"></i>${config.text}</span>`;
    }
    
    formatLastSeen(lastSeen) {
        const now = Date.now() / 1000;
        const diff = now - lastSeen;
        
        if (diff < 60) {
            return 'Just now';
        } else if (diff < 3600) {
            const minutes = Math.floor(diff / 60);
            return `${minutes}m ago`;
        } else {
            const hours = Math.floor(diff / 3600);
            return `${hours}h ago`;
        }
    }
    
    async selectModel(modelId) {
        try {
            // Update the current model selection
            this.selectedModel = modelId;
            
            // Update UI to show selection
            document.querySelectorAll('.model-group').forEach(group => {
                group.classList.remove('selected-model');
            });
            
            const selectedGroup = document.querySelector(`[data-model="${modelId}"]`);
            if (selectedGroup) {
                selectedGroup.classList.add('selected-model');
            }
            
            // Show success message
            this.showToast('success', `Selected model: ${modelId}`);
            
            // Update chat interface to show selected model
            this.updateChatInterface(modelId);
            
            // Store selection in localStorage for persistence
            localStorage.setItem('llamanet_selected_model', modelId);
            
        } catch (error) {
            console.error('Error selecting model:', error);
            this.showToast('error', 'Failed to select model');
        }
    }
    
    updateChatInterface(modelId) {
        // Update the chat header to show selected model
        const chatHeader = document.querySelector('.card-header h5');
        if (chatHeader) {
            chatHeader.innerHTML = `
                <i class="fas fa-comments"></i> Chat Interface - Model: ${modelId}
            `;
        }
        
        // Add model info to the welcome message
        const chatContainer = document.getElementById('chat-messages');
        const welcomeMsg = chatContainer.querySelector('.text-center.text-muted');
        if (welcomeMsg) {
            welcomeMsg.innerHTML = `
                <i class="fas fa-robot fa-2x mb-2"></i>
                <p>Welcome to LlamaNet! Using model: <strong>${modelId}</strong></p>
                <p class="small">Start a conversation below.</p>
            `;
        }
    }
    
    async showModelDetails(modelId) {
        // Show detailed model information modal
        const modal = new bootstrap.Modal(document.getElementById('nodeInfoModal'));
        
        // Update modal title
        document.querySelector('#nodeInfoModal .modal-title').innerHTML = `<i class="fas fa-brain"></i> Model Information: ${modelId}`;
        
        // Show loading state
        document.getElementById('node-info-details').innerHTML = `
            <div class="text-center">
                <div class="spinner-border text-primary" role="status"></div>
                <p class="mt-2">Loading model information...</p>
            </div>
        `;
        
        modal.show();
        
        try {
            // Get detailed model statistics
            const statsResponse = await fetch(`${this.baseUrl}/models/statistics`);
            
            if (statsResponse.ok) {
                const statsData = await statsResponse.json();
                const modelStats = statsData.models[modelId];
                
                if (modelStats) {
                    document.getElementById('node-info-details').innerHTML = 
                        this.renderModelDetailsView(modelId, modelStats, statsData.network_summary);
                } else {
                    throw new Error('Model not found in statistics');
                }
            } else {
                throw new Error(`HTTP ${statsResponse.status}: ${statsResponse.statusText}`);
            }
        } catch (error) {
            console.error('Error loading model details:', error);
            document.getElementById('node-info-details').innerHTML = `
                <div class="alert alert-danger">
                    <i class="fas fa-exclamation-triangle"></i>
                    Failed to load model information: ${error.message}
                </div>
            `;
        }
    }
    
    renderModelDetailsView(modelId, modelStats, networkSummary) {
        const availability = modelStats.availability || 'unknown';
        const availabilityClass = {
            'high': 'success',
            'medium': 'warning',
            'low': 'danger',
            'unknown': 'secondary'
        }[availability] || 'secondary';
        
        return `
            <div class="row">
                <div class="col-md-6">
                    <h6><i class="fas fa-brain"></i> Model Overview</h6>
                    <div class="network-detail-item">
                        <strong>Model ID:</strong> ${modelId}<br>
                        <strong>Availability:</strong> <span class="badge bg-${availabilityClass}">${availability}</span><br>
                        <strong>Node Count:</strong> ${modelStats.node_count}<br>
                        <strong>Average Load:</strong> ${modelStats.avg_load.toFixed(3)}<br>
                        <strong>Total Capacity:</strong> ${modelStats.total_tps.toFixed(1)} TPS<br>
                    </div>
                    
                    ${modelStats.best_node ? `
                    <h6 class="mt-3"><i class="fas fa-star"></i> Best Performing Node</h6>
                    <div class="network-detail-item">
                        <strong>Node ID:</strong> ${modelStats.best_node.node_id.substring(0, 12)}...<br>
                        <strong>Address:</strong> ${this.getNodeAddress(modelStats.best_node)}<br>
                        <strong>Load:</strong> ${modelStats.best_node.load.toFixed(3)}<br>
                        <strong>TPS:</strong> ${modelStats.best_node.tps.toFixed(1)}<br>
                        <strong>TTFT:</strong> ${this.formatMetricTime(modelStats.best_node.ttft)}<br>
                        <strong>Latency:</strong> ${this.formatMetricTime(modelStats.best_node.latency)}<br>
                        <strong>Uptime:</strong> ${Math.floor(modelStats.best_node.uptime / 60)} minutes
                    </div>
                    ` : ''}
                </div>
                
                <div class="col-md-6">
                    <h6><i class="fas fa-chart-bar"></i> Network Context</h6>
                    <div class="network-detail-item">
                        <strong>Total Network Models:</strong> ${networkSummary.total_models}<br>
                        <strong>Total Network Nodes:</strong> ${networkSummary.total_nodes}<br>
                        <strong>Network Average Load:</strong> ${networkSummary.avg_network_load.toFixed(3)}<br>
                        <strong>Total Network Capacity:</strong> ${networkSummary.total_network_tps.toFixed(1)} TPS
                    </div>
                    
                    <h6 class="mt-3"><i class="fas fa-users"></i> All Nodes for ${modelId}</h6>
                    <div class="network-detail-item" style="max-height: 200px; overflow-y: auto;">
                        ${modelStats.nodes.map(node => `
                            <div class="d-flex justify-content-between align-items-center mb-1 p-1 border-bottom">
                                <div>
                                    <small class="fw-bold">${node.node_id.substring(0, 8)}...</small><br>
                                    <small class="text-muted"><i class="fas fa-globe"></i> ${this.getNodeAddress(node)}</small>
                                </div>
                            </div>
                        `).join('')}
                    </div>
                </div>
            </div>
            
            <div class="mt-3">
                <div class="d-flex gap-2">
                    <button class="btn btn-primary" onclick="llamaNetUI.selectModel('${modelId}'); bootstrap.Modal.getInstance(document.getElementById('nodeInfoModal')).hide();">
                        <i class="fas fa-check"></i> Select This Model
                    </button>
                    <button class="btn btn-outline-secondary" onclick="llamaNetUI.refreshModelDetails('${modelId}')">
                        <i class="fas fa-sync-alt"></i> Refresh
                    </button>
                </div>
            </div>
        `;
    }
    
    
    async refreshModelDetails(modelId) {
        // Refresh the model details view
        await this.showModelDetails(modelId);
    }
    
    async showNodeInfo(nodeId) {
        const modal = new bootstrap.Modal(document.getElementById('nodeInfoModal'));
        
        // Update modal title
        document.querySelector('#nodeInfoModal .modal-title').innerHTML = `<i class="fas fa-server"></i> Node Information: ${nodeId.substring(0, 12)}...`;
        
        // Show loading state
        document.getElementById('node-info-details').innerHTML = `
            <div class="text-center">
                <div class="spinner-border text-primary" role="status"></div>
                <p class="mt-2">Loading node information...</p>
            </div>
        `;
        
        modal.show();
        
        try {
            // Try to get from real-time data first
            const realtimeNode = this.activeNodes.get(nodeId);
            
            // Then get detailed info from API
            const response = await fetch(`${this.baseUrl}/node/${nodeId}`);
            
            if (response.ok) {
                const nodeInfo = await response.json();
                
                // Merge real-time data with detailed info
                if (realtimeNode) {
                    nodeInfo.realtime_data = realtimeNode;
                }
                
                document.getElementById('node-info-details').innerHTML = this.renderNodeDetails(nodeInfo);
            } else {
                // Fallback to real-time data if API fails
                if (realtimeNode) {
                    document.getElementById('node-info-details').innerHTML = this.renderNodeDetailsFromRealtime(realtimeNode);
                } else {
                    throw new Error(`HTTP ${response.status}: ${response.statusText}`);
                }
            }
        } catch (error) {
            console.error('Error loading node info:', error);
            
            // Try to show what we have from real-time data
            const realtimeNode = this.activeNodes.get(nodeId);
            if (realtimeNode) {
                document.getElementById('node-info-details').innerHTML = `
                    <div class="alert alert-warning">
                        <i class="fas fa-exclamation-triangle"></i>
                        Could not load complete node information. Showing available data from real-time updates.
                    </div>
                    ${this.renderNodeDetailsFromRealtime(realtimeNode)}
                `;
            } else {
                document.getElementById('node-info-details').innerHTML = `
                    <div class="alert alert-danger">
                        <i class="fas fa-exclamation-triangle"></i>
                        Failed to load node information: ${error.message}
                    </div>
                `;
            }
        }
    }
    
    renderNodeDetailsFromRealtime(nodeData) {
        const lastSeenText = new Date(nodeData.last_seen * 1000).toLocaleString();
        const uptimeText = nodeData.uptime ? `${Math.floor(nodeData.uptime / 60)} minutes` : 'Unknown';
        
        return `
            <div class="row">
                <div class="col-md-6">
                    <h6><i class="fas fa-server"></i> Node Information (Real-time)</h6>
                    <div class="network-detail-item">
                        <strong>Node ID:</strong> ${nodeData.node_id}<br>
                        <strong>Address:</strong> Routed via gateway<br>
                        <strong>Model:</strong> ${nodeData.model}<br>
                        <strong>Last Seen:</strong> ${lastSeenText}
                    </div>
                    
                    <h6 class="mt-3"><i class="fas fa-chart-line"></i> Performance Metrics</h6>
                    <div class="network-detail-item">
                        <strong>Load:</strong> ${nodeData.load.toFixed(2)}<br>
                        <strong>TPS:</strong> ${nodeData.tps.toFixed(1)}<br>
                        <strong>TTFT:</strong> ${this.formatMetricTime(nodeData.ttft)}<br>
                        <strong>Latency:</strong> ${this.formatMetricTime(nodeData.latency)}<br>
                        <strong>Uptime:</strong> ${uptimeText}
                    </div>
                </div>
                <div class="col-md-6">
                    <div class="alert alert-info">
                        <i class="fas fa-info-circle"></i>
                        <strong>Real-time Data:</strong> This information is from live network updates. 
                        Click refresh to get complete node details.
                    </div>
                    
                    <button class="btn btn-primary" onclick="llamaNetUI.refreshNodeInfo('${nodeData.node_id}')">
                        <i class="fas fa-sync-alt"></i> Get Complete Info
                    </button>
                </div>
            </div>
        `;
    }
    
    async refreshNodeInfo(nodeId) {
        await this.showNodeInfo(nodeId);
    }
    
    renderNodeDetails(nodeInfo) {
        // Render detailed node information
        const isCurrentNode = nodeInfo.is_current_node;
        const statusBadge = nodeInfo.status === 'online' ? 
            '<span class="badge bg-success">Online</span>' : 
            '<span class="badge bg-warning">Stale</span>';
        
        const lastSeenText = nodeInfo.last_seen ? 
            new Date(nodeInfo.last_seen * 1000).toLocaleString() : 'Unknown';
        
        let systemInfoHtml = '';
        if (nodeInfo.system) {
            const ramInfo = nodeInfo.system.ram || {};
            systemInfoHtml = `
                <div class="col-md-6">
                    <h6 class="mt-3"><i class="fas fa-microchip"></i> System Information</h6>
                    <div class="network-detail-item">
                        <strong>CPU:</strong> ${nodeInfo.system.cpu || 'Unknown'}<br>
                        <strong>RAM:</strong> ${ramInfo.total_gb ? `${ramInfo.total_gb} GB total, ${ramInfo.available_gb} GB available` : 'Unknown'}<br>
                        <strong>GPU:</strong> ${nodeInfo.system.gpu || 'None detected'}<br>
                        <strong>Platform:</strong> ${nodeInfo.system.platform || 'Unknown'}
                    </div>
                </div>
            `;
        }
        
        let endpointsHtml = '';
        if (nodeInfo.endpoints) {
            endpointsHtml = `
                <div class="mt-3">
                    <h6><i class="fas fa-link"></i> Available Endpoints</h6>
                    <div class="network-detail-item">
                        <ul class="list-unstyled small mt-2">
                            ${nodeInfo.endpoints.map(ep => `<li><span class="api-endpoint">${ep}</span></li>`).join('')}
                        </ul>
                    </div>
                </div>
            `;
        }
        
        return `
            <div class="row">
                <div class="col-md-6">
                    <h6><i class="fas fa-server"></i> Node Information</h6>
                    <div class="network-detail-item">
                        <strong>Node ID:</strong> ${nodeInfo.node_id}<br>
                        <strong>Status:</strong> ${statusBadge} ${isCurrentNode ? '<span class="badge bg-primary ms-1">Current Node</span>' : ''}<br>
                        <strong>Address:</strong> ${isCurrentNode ? 'Local node' : 'Routed via gateway'}<br>
                        <strong>Model:</strong> ${nodeInfo.model}<br>
                        ${nodeInfo.model_path ? `<strong>Model Path:</strong> ${nodeInfo.model_path}<br>` : ''}
                        <strong>Last Seen:</strong> ${lastSeenText}
                    </div>
                    
                    <h6 class="mt-3"><i class="fas fa-chart-line"></i> Performance Metrics</h6>
                    <div class="network-detail-item">
                        <strong>Load:</strong> ${nodeInfo.load ? nodeInfo.load.toFixed(2) : '0.00'}<br>
                        <strong>TPS:</strong> ${nodeInfo.tps ? nodeInfo.tps.toFixed(1) : '0.0'}<br>
                        <strong>TTFT:</strong> ${this.formatMetricTime(nodeInfo.ttft)}<br>
                        <strong>Latency:</strong> ${this.formatMetricTime(nodeInfo.latency)}<br>
                        <strong>Uptime:</strong> ${nodeInfo.uptime ? `${Math.floor(nodeInfo.uptime / 60)} minutes` : 'Unknown'}<br>
                        ${nodeInfo.total_tokens ? `<strong>Total Tokens:</strong> ${nodeInfo.total_tokens.toLocaleString()}<br>` : ''}
                    </div>
                </div>
                
                ${systemInfoHtml}
            </div>
            
            ${endpointsHtml}
            
            ${isCurrentNode ? '' : `
                <div class="mt-3">
                    <div class="alert alert-info">
                        <i class="fas fa-info-circle"></i>
                        <strong>Remote Node:</strong> This node is part of the distributed LlamaNet network. 
                        You can send requests to it using the same OpenAI-compatible API endpoints.
                    </div>
                </div>
            `}
        `;
    }
    
    showToast(type, message) {
        // Create a simple toast notification
        const toast = document.createElement('div');
        toast.className = `alert alert-${type === 'success' ? 'success' : 'danger'} position-fixed`;
        toast.style.cssText = 'top: 20px; right: 20px; z-index: 9999; min-width: 300px;';
        toast.innerHTML = `
            <i class="fas fa-${type === 'success' ? 'check-circle' : 'exclamation-triangle'}"></i>
            ${message}
            <button type="button" class="btn-close" onclick="this.parentElement.remove()"></button>
        `;
        document.body.appendChild(toast);
        
        // Auto-remove after 3 seconds
        setTimeout(() => {
            if (toast.parentElement) {
                toast.remove();
            }
        }, 3000);
    }
    
    showNetworkLoading() {
        const container = document.getElementById('network-status');
        if (container) {
            container.innerHTML = `
                <div class="text-center">
                    <div class="spinner-border text-primary" role="status">
                        <span class="visually-hidden">Loading...</span>
                    </div>
                    <p class="mt-2">Discovering nodes...</p>
                </div>
            `;
        }
    }
    
    showNetworkError(message) {
        const container = document.getElementById('network-status');
        container.innerHTML = `
            <div class="text-center text-danger">
                <i class="fas fa-exclamation-triangle"></i>
                <p class="small">${message}</p>
            </div>
        `;
    }
    
    async sendMessage() {
        const input = document.getElementById('message-input');
        const message = input.value.trim();
        
        if (!message) return;
        
        // Clear input and disable send button
        input.value = '';
        this.toggleSendButton(false);
        
        // Add user message to chat
        this.addMessageToChat('user', message);
        
        try {
            const response = await this.sendOpenAIMessage(message);
            
            // Only add to chat if response exists and it's not from streaming
            const streamingEnabled = document.getElementById('enable-streaming')?.checked || false;
            if (response && !streamingEnabled) {
                this.addMessageToChat('assistant', response.text, response.metadata);
            } else if (!response && !streamingEnabled) {
                this.addMessageToChat('system', 'Failed to get response from the network');
            }
        } catch (error) {
            console.error('Error sending message:', error);
            this.addMessageToChat('system', `Error: ${error.message}`);
            this.showError(`Failed to send message: ${error.message}`);
        } finally {
            this.toggleSendButton(true);
        }
    }
    
    async sendOpenAIMessage(message) {
        const maxTokens = parseInt(document.getElementById('max-tokens').value) || 150;
        const temperature = parseFloat(document.getElementById('temperature').value) || 0.7;
        const streamingEnabled = document.getElementById('enable-streaming')?.checked || false;
        const strategy = document.getElementById('load-strategy')?.value || 'round_robin';
        
        // Build chat history for context
        const messages = [];
        
        // Add system message (custom or default)
        const systemMessage = this.systemPrompt.trim() || 'You are a helpful AI assistant. Provide clear, concise responses.';
        messages.push({ 
            role: 'system', 
            content: systemMessage
        });
        
        // Add recent chat history (last 6 exchanges to keep context manageable)
        const recentHistory = this.chatHistory.slice(-12); // 6 exchanges = 12 messages
        recentHistory
            .filter(msg => msg.role === 'user' || msg.role === 'assistant')
            .forEach(msg => messages.push({ role: msg.role, content: msg.content }));
        
        // Add current message
        messages.push({ role: 'user', content: message });
        
        // Use selected model if available, otherwise default
        const modelToUse = this.selectedModel || 'llamanet';
        
        const requestBody = {
            model: modelToUse,  // Use selected model
            messages: messages,
            max_tokens: maxTokens,
            temperature: temperature,
            stream: streamingEnabled,
            strategy: strategy,
            target_model: this.selectedModel  // Add explicit target model parameter
        };

        if (streamingEnabled) {
            return await this.sendOpenAIStreamingMessage(requestBody);
        } else {
            const response = await fetch(`${this.baseUrl}/v1/chat/completions`, {
                method: 'POST',
                headers: {
                    'Content-Type': 'application/json'
                },
                body: JSON.stringify(requestBody)
            });
            
            if (!response.ok) {
                throw new Error(`HTTP ${response.status}: ${response.statusText}`);
            }
            
            const data = await response.json();
            
            // Enhanced metadata
            const metadata = {
                id: data.id,
                tokens: data.usage.total_tokens,
                api: 'OpenAI Compatible',
                node_info: data.node_info,
                model_used: modelToUse,
                system_prompt_used: this.systemPrompt.trim() ? 'Custom' : 'Default'
            };
            
            return {
                text: this.cleanResponse(data.choices[0].message.content),
                metadata: metadata
            };
        }
    }

    async sendOpenAIStreamingMessage(requestBody) {
        return new Promise((resolve, reject) => {
            const streamState = {
                accumulatedText: '',
                responseId: '',
                totalTokens: 0,
                messageDiv: null,
                bubbleDiv: null
            };
            
            // Initialize UI
            this.initializeStreamingUI(streamState);
            
            // Define event handlers as a map
            const handlers = new Map([
                ['token', (data) => this.handleOpenAIToken(data, streamState)],
                ['complete', () => this.handleOpenAIComplete(streamState, resolve)],
                ['error', (error) => this.handleOpenAIError(error, streamState, reject)]
            ]);
            
            // Start streaming with functional approach
            this.processOpenAIStream(requestBody, handlers);
        });
    }

    initializeStreamingUI(streamState) {
        const chatContainer = document.getElementById('chat-messages');
        
        streamState.messageDiv = document.createElement('div');
        streamState.messageDiv.className = 'message assistant';
        
        streamState.bubbleDiv = document.createElement('div');
        streamState.bubbleDiv.className = 'message-bubble';
        streamState.bubbleDiv.innerHTML = '<i class="fas fa-robot me-2"></i><div class="streaming-text"></div><span class="streaming-cursor">▋</span>';
        
        streamState.messageDiv.appendChild(streamState.bubbleDiv);
        chatContainer.appendChild(streamState.messageDiv);
        chatContainer.scrollTop = chatContainer.scrollHeight;
        
        // Remove welcome message if it exists
        const welcomeMsg = chatContainer.querySelector('.text-center.text-muted');
        if (welcomeMsg) {
            welcomeMsg.remove();
        }
        
        // Update clear history button state
        this.updateClearHistoryButton();
    }

    handleOpenAIToken(data, streamState) {
        if (data.content) {
            streamState.accumulatedText += data.content;
            const textContainer = streamState.bubbleDiv.querySelector('.streaming-text');
            if (textContainer) {
                // Render accumulated markdown content
                const renderedContent = this.markdownRenderer.render(streamState.accumulatedText);
                textContainer.innerHTML = `<div class="markdown-content streaming-markdown">${renderedContent}</div>`;
                
                // Highlight any new code blocks
                this.highlightCodeBlocks(textContainer);
            }
            document.getElementById('chat-messages').scrollTop = document.getElementById('chat-messages').scrollHeight;
        }
        
        if (data.id) {
            streamState.responseId = data.id;
        }
        
        // Capture node info from any chunk that contains it
        if (data.node_info) {
            streamState.nodeInfo = data.node_info;
        }
    }

    handleOpenAIComplete(streamState, resolve) {
        // Remove streaming cursor
        const cursor = streamState.bubbleDiv.querySelector('.streaming-cursor');
        if (cursor) {
            cursor.remove();
        }
        
        // Estimate tokens (rough approximation)
        streamState.totalTokens = Math.ceil(streamState.accumulatedText.split(' ').length * 1.3);
        
        // Build metadata parts
        const metadataParts = [
            `ID: ${streamState.responseId.substring(0, 8)}...`,
            `Tokens: ~${streamState.totalTokens}`
        ];

        // Add node info if available with proper styling
        if (streamState.nodeInfo) {
            const processingType = streamState.nodeInfo.processing_node === 'forwarded' ? 'via' : 'on';
            const nodeDisplay = `Node: ${processingType} ${streamState.nodeInfo.node_id.substring(0, 8)}... (${streamState.nodeInfo.ip}:${streamState.nodeInfo.port})`;
            metadataParts.push(`<span class="node-info">${nodeDisplay}</span>`);
        }
        
        // Add metadata
        const metadataHtml = `<div class="message-meta">${metadataParts.join(' • ')}</div>`;
        streamState.messageDiv.insertAdjacentHTML('beforeend', metadataHtml);
        
        // Store in chat history
        this.chatHistory.push({ 
            role: 'assistant', 
            content: streamState.accumulatedText, 
            timestamp: Date.now() 
        });
        
        resolve({
            text: streamState.accumulatedText,
            metadata: {
                id: streamState.responseId,
                tokens: streamState.totalTokens,
                api: 'Streaming',
                node_info: streamState.nodeInfo
            },
            isStreaming: true // Flag to indicate this was handled by streaming
        });
    }

    handleOpenAIError(error, streamState, reject) {
        // Remove streaming cursor and show error
        const cursor = streamState.bubbleDiv.querySelector('.streaming-cursor');
        if (cursor) {
            cursor.remove();
        }
        
        const textSpan = streamState.bubbleDiv.querySelector('.streaming-text');
        if (textSpan) {
            textSpan.textContent = streamState.accumulatedText + ' [Error: ' + error.message + ']';
            textSpan.style.color = 'red';
        }
        
        reject(error);
    }

    async processOpenAIStream(requestBody, handlers) {
        try {
            const response = await fetch(`${this.baseUrl}/v1/chat/completions`, {
                method: 'POST',
                headers: {
                    'Content-Type': 'application/json',
                },
                body: JSON.stringify(requestBody)
            });

            if (!response.ok) {
                throw new Error(`HTTP ${response.status}: ${response.statusText}`);
            }

            // Create async iterator for stream processing
            const streamProcessor = this.createOpenAIStreamProcessor(response.body);
            
            // Process chunks functionally
            await this.processStreamChunks(streamProcessor, handlers);
            
        } catch (error) {
            console.error('OpenAI streaming error:', error);
            handlers.get('error')(error);
        }
    }

    async* createOpenAIStreamProcessor(body) {
        const reader = body.getReader();
        const decoder = new TextDecoder();
        let buffer = '';

        try {
            const processChunk = async () => {
                const { done, value } = await reader.read();
                if (done) return null;
                
                buffer += decoder.decode(value, { stream: true });
                const lines = buffer.split('\n');
                buffer = lines.pop() || '';
                
                return lines.filter(line => line.startsWith('data: ')).map(line => line.slice(6).trim()).filter(data => data && data !== '[DONE]');
            };

            let chunk;
            while ((chunk = await processChunk()) !== null) {
                yield* chunk.map(data => {
                    try {
                        return JSON.parse(data);
                    } catch (error) {
                        console.warn('Failed to parse OpenAI stream chunk:', data);
                        return null;
                    }
                }).filter(parsed => parsed !== null);
            }
        } finally {
            reader.releaseLock();
        }
    }

    async processStreamChunks(streamProcessor, handlers) {
        const tokenHandler = handlers.get('token');
        const completeHandler = handlers.get('complete');
        
        try {
            for await (const chunk of streamProcessor) {
                const processedData = this.processOpenAIChunk(chunk);
                if (processedData) {
                    tokenHandler(processedData);
                    
                    if (processedData.finished) {
                        completeHandler();
                        break;
                    }
                }
            }
            
            // Ensure completion is called if no explicit finish signal
            completeHandler();
            
        } catch (error) {
            handlers.get('error')(error);
        }
    }

    processOpenAIChunk(chunk) {
        // Handle OpenAI streaming format
        if (chunk.choices && chunk.choices.length > 0) {
            const choice = chunk.choices[0];
            
            if (choice.delta) {
                const delta = choice.delta;
                
                return {
                    content: delta.content || '',
                    role: delta.role || null,
                    id: chunk.id || '',
                    finished: choice.finish_reason !== null,
                    node_info: chunk.node_info || null
                };
            }
        }
        
        return null;
    }
    
    addMessageToChat(role, content, metadata = null) {
        const chatContainer = document.getElementById('chat-messages');
        const messageDiv = document.createElement('div');
        messageDiv.className = `message ${role}`;
        
        // Store in chat history
        this.chatHistory.push({ role, content, timestamp: Date.now() });
        
        let metadataHtml = '';
        if (metadata) {
            const parts = [];
            if (metadata.tokens) parts.push(`Tokens: ${metadata.tokens}`);
            if (metadata.api) parts.push(`API: ${metadata.api}`);
            if (metadata.id) parts.push(`ID: ${metadata.id.substring(0, 8)}...`);
            if (metadata.system_prompt_used) parts.push(`System: ${metadata.system_prompt_used}`);
            
            // Add node information display
            if (metadata.node_info) {
                const nodeInfo = metadata.node_info;
                const processingType = nodeInfo.processing_node === 'forwarded' ? 'via' : 'on';
                const nodeDisplay = `Node: ${processingType} ${nodeInfo.node_id.substring(0, 8)}... (${nodeInfo.ip}:${nodeInfo.port})`;
                parts.push(`<span class="node-info">${nodeDisplay}</span>`);
            }
            
            if (parts.length > 0) {
                metadataHtml = `<div class="message-meta">${parts.join(' • ')}</div>`;
            }
        }
        
        const roleIcon = role === 'user' ? 'fas fa-user' : 
                        role === 'assistant' ? 'fas fa-robot' : 'fas fa-info-circle';
        
        // Render content based on role
        let renderedContent;
        if (role === 'assistant') {
            // Render markdown for assistant responses
            renderedContent = this.markdownRenderer.render(content);
            messageDiv.innerHTML = `
                <div class="message-bubble">
                    <i class="${roleIcon} me-2"></i>
                    <div class="markdown-content">${renderedContent}</div>
                </div>
                ${metadataHtml}
            `;
        } else {
            // Keep user messages as plain text
            renderedContent = this.escapeHtml(content);
            messageDiv.innerHTML = `
                <div class="message-bubble">
                    <i class="${roleIcon} me-2"></i>${renderedContent}
                </div>
                ${metadataHtml}
            `;
        }
        
        chatContainer.appendChild(messageDiv);
        
        // Highlight code blocks
        this.highlightCodeBlocks(messageDiv);
        
        // Add copy buttons to code blocks
        this.addCopyButtons(messageDiv);
        
        chatContainer.scrollTop = chatContainer.scrollHeight;
        
        // Remove welcome message if it exists
        const welcomeMsg = chatContainer.querySelector('.text-center.text-muted');
        if (welcomeMsg) {
            welcomeMsg.remove();
        }
    }
    
    toggleSendButton(enabled) {
        const button = document.getElementById('send-btn');
        if (enabled) {
            button.disabled = false;
            button.innerHTML = '<i class="fas fa-paper-plane"></i> Send';
        } else {
            button.disabled = true;
            button.innerHTML = '<div class="loading-spinner"></div> Sending...';
        }
    }
    
    handleKeyPress(event) {
        if (event.key === 'Enter' && !event.shiftKey) {
            event.preventDefault();
            this.sendMessage();
        }
    }
    
    async showNetworkModal() {
        const modal = new bootstrap.Modal(document.getElementById('networkModal'));
        modal.show();

        try {
            const [infoResponse, statusResponse] = await Promise.all([
                fetch(`${this.baseUrl}/info`),
                fetch(`${this.baseUrl}/status`)
            ]);

            const info = await infoResponse.json();
            const status = await statusResponse.json();

            document.getElementById('network-details').innerHTML = this.renderNetworkDetails(info, {}, status);
        } catch (error) {
            document.getElementById('network-details').innerHTML = `
                <div class="alert alert-danger">
                    <i class="fas fa-exclamation-triangle"></i>
                    Failed to load network details: ${error.message}
                </div>
            `;
        }
    }
    
    renderNetworkDetails(info, dht, status) {
        const system = info.system || {};
        const ram = system.ram || {};

        return `
            <div class="row">
                <div class="col-md-6">
                    <h6><i class="fas fa-server"></i> Node Information</h6>
                    <div class="network-detail-item">
                        <strong>Node ID:</strong> ${info.node_id}<br>
                        <strong>Model:</strong> ${info.model}<br>
                        ${info.model_path ? `<strong>Model Path:</strong> ${info.model_path}<br>` : ''}
                    </div>

                    <h6 class="mt-3"><i class="fas fa-chart-line"></i> Performance</h6>
                    <div class="network-detail-item">
                        <strong>Load:</strong> ${status.load || 0}<br>
                        <strong>TPS:</strong> ${status.tps || 0}<br>
                        <strong>TTFT:</strong> ${this.formatMetricTime(status.ttft)}<br>
                        <strong>Latency:</strong> ${this.formatMetricTime(status.latency)}<br>
                        <strong>Uptime:</strong> ${status.uptime || 0}s<br>
                        <strong>Total Tokens:</strong> ${status.total_tokens || 0}
                    </div>
                </div>

                <div class="col-md-6">
                    <h6><i class="fas fa-cogs"></i> System Info</h6>
                    <div class="network-detail-item">
                        <strong>CPU:</strong> ${system.cpu || 'Unknown'}<br>
                        <strong>RAM:</strong> ${ram.total_gb ? ram.total_gb + ' GB' : 'Unknown'}<br>
                        <strong>GPU:</strong> ${system.gpu || 'None'}<br>
                        <strong>Platform:</strong> ${system.platform || 'Unknown'}
                    </div>
                </div>
            </div>

            ${info.endpoints ? `
            <div class="mt-3">
                <h6><i class="fas fa-list"></i> Available Endpoints</h6>
                <ul class="list-unstyled small">
                    ${info.endpoints.map(ep => `<li><span class="api-endpoint">${ep}</span></li>`).join('')}
                </ul>
            </div>` : ''}
        `;
    }
    
    showError(message) {
        const toast = document.getElementById('errorToast');
        const toastBody = document.getElementById('errorToastBody');
        toastBody.textContent = message;
        
        const bsToast = new bootstrap.Toast(toast);
        bsToast.show();
    }
    
    cleanResponse(text) {
        // Remove any leaked conversation format
        return text
            .replace(/^(Human:|User:|Assistant:)\s*/i, '')
            .replace(/\n\n(Human:|User:).*$/s, '')
            .replace(/\n(Human:|User:).*$/s, '')
            .trim();
    }
    
    cleanResponse(text) {
        // Remove any leaked conversation format
        return text
            .replace(/^(Human:|User:|Assistant:)\s*/i, '')
            .replace(/\n\n(Human:|User:).*$/s, '')
            .replace(/\n(Human:|User:).*$/s, '')
            .trim();
    }
    
    highlightCodeBlocks(element) {
        if (typeof hljs !== 'undefined') {
            const codeBlocks = element.querySelectorAll('pre code');
            codeBlocks.forEach(block => {
                hljs.highlightElement(block);
            });
        }
    }

    addCopyButtons(element) {
        const codeBlocks = element.querySelectorAll('pre');
        codeBlocks.forEach(pre => {
            // Wrap in container for positioning
            const wrapper = document.createElement('div');
            wrapper.className = 'code-block-wrapper';
            pre.parentNode.insertBefore(wrapper, pre);
            wrapper.appendChild(pre);
            
            // Add copy button
            const copyBtn = document.createElement('button');
            copyBtn.className = 'copy-code-btn';
            copyBtn.innerHTML = '<i class="fas fa-copy"></i>';
            copyBtn.title = 'Copy code';
            
            copyBtn.addEventListener('click', () => {
                const code = pre.querySelector('code');
                const text = code ? code.textContent : pre.textContent;
                
                navigator.clipboard.writeText(text).then(() => {
                    copyBtn.innerHTML = '<i class="fas fa-check"></i>';
                    copyBtn.style.backgroundColor = '#28a745';
                    
                    setTimeout(() => {
                        copyBtn.innerHTML = '<i class="fas fa-copy"></i>';
                        copyBtn.style.backgroundColor = '#6c757d';
                    }, 2000);
                }).catch(err => {
                    console.error('Failed to copy code:', err);
                });
            });
            
            wrapper.appendChild(copyBtn);
        });
    }
    
    escapeHtml(text) {
        const div = document.createElement('div');
        div.textContent = text;
        return div.innerHTML;
    }
    
    getNodeAddress(node) {
        return 'local';
    }
    
    // SSE-only real-time updates (no polling)
    // All real-time updates now handled through SSE events
    // Polling methods removed to prevent redundant network calls
    
    // Helper methods for tracking changes and animations
    isModelUpdated(modelId, currentStats) {
        if (!this.previousModelStats) return false;
        
        const previousStats = this.previousModelStats[modelId];
        if (!previousStats) return true; // New model
        
        // Check for significant changes
        return (
            Math.abs(currentStats.avg_load - previousStats.avg_load) > 0.1 ||
            Math.abs(currentStats.total_tps - previousStats.total_tps) > 1.0 ||
            currentStats.node_count !== previousStats.node_count
        );
    }
    
    isNodeUpdated(nodeId) {
        if (!this.previousNodeStates) return false;
        
        const previousState = this.previousNodeStates[nodeId];
        return !previousState || previousState.lastSeen !== this.currentNodeStates?.[nodeId]?.lastSeen;
    }
    
    getModelChangeIndicator(modelId, stats) {
        if (this.isModelUpdated(modelId, stats)) {
            return '<i class="fas fa-circle text-warning ms-1" style="font-size: 0.4rem;" title="Recently updated"></i>';
        }
        return '';
    }
    
    getNodeChangeIndicator(nodeId) {
        if (this.isNodeUpdated(nodeId)) {
            return '<i class="fas fa-circle text-info ms-1" style="font-size: 0.4rem;" title="Recently seen"></i>';
        }
        return '';
    }
    
    showUpdateIndicator(show) {
        const indicator = document.querySelector('.live-indicator');
        if (indicator) {
            if (show) {
                indicator.innerHTML = '<i class="fas fa-sync-alt fa-spin text-primary" style="font-size: 0.5rem;" title="Refreshing..."></i>';
            } else {
                // Restore SSE status indicator based on current connection state
                const statusIcon = this.isConnected ? 
                    '<i class="fas fa-circle text-success live-pulse" style="font-size: 0.5rem;" title="Real-time updates active"></i>' :
                    '<i class="fas fa-circle text-warning" style="font-size: 0.5rem;" title="Connecting..."></i>';
                indicator.innerHTML = statusIcon;
            }
        }
        
        // Update refresh button state
        const refreshBtn = document.querySelector('button[onclick="refreshNetworkStatus()"]');
        if (refreshBtn) {
            if (show) {
                refreshBtn.disabled = true;
                refreshBtn.innerHTML = '<i class="fas fa-sync-alt fa-spin"></i> Refreshing...';
            } else {
                refreshBtn.disabled = false;
                refreshBtn.innerHTML = '<i class="fas fa-sync-alt"></i> Refresh';
            }
        }
    }
    
    updateConnectionStatus(status) {
        this.connectionStatus = status;
        
        // Update UI indicators based on connection status
        const statusIndicators = document.querySelectorAll('.connection-status');
        statusIndicators.forEach(indicator => {
            indicator.className = `connection-status badge bg-${this.getStatusColor(status)}`;
            indicator.textContent = this.getStatusText(status);
        });
    }
    
    getStatusColor(status) {
        switch (status) {
            case 'connected': return 'success';
            case 'warning': return 'warning';
            case 'error': return 'danger';
            default: return 'secondary';
        }
    }
    
    getStatusText(status) {
        switch (status) {
            case 'connected': return 'Live';
            case 'warning': return 'Partial';
            case 'error': return 'Offline';
            default: return 'Unknown';
        }
    }
    
    handleSSEError() {
        // Handle SSE connection errors with exponential backoff
        const baseDelay = 2000; // 2 seconds
        const maxDelay = 30000; // 30 seconds
        
        if (!this.errorCount) this.errorCount = 0;
        this.errorCount++;
        
        const delay = Math.min(baseDelay * Math.pow(2, this.errorCount - 1), maxDelay);
        
        console.log(`SSE reconnection scheduled in ${delay}ms (attempt ${this.errorCount})`);
        
        setTimeout(() => {
            if (!this.isConnected) {
                this.startUnifiedSSENetworkMonitoring();
            }
        }, delay);
    }
    
    highlightChangedMetrics(container) {
        // Add subtle animation to changed metrics
        const metricValues = container.querySelectorAll('.metric-value');
        metricValues.forEach(metric => {
            metric.style.transition = 'background-color 0.3s ease';
            metric.style.backgroundColor = 'rgba(13, 110, 253, 0.1)';
            
            setTimeout(() => {
                metric.style.backgroundColor = 'transparent';
            }, 1000);
        });
    }
    
    // Update cleanup method to only handle SSE (no polling cleanup)
    validateNetworkStats(dhtData, modelsData) {
        const sseNodeCount = this.activeNodes.size;
        const apiNodeCount = modelsData.total_nodes || 0;

        console.log(`📊 Network validation: SSE nodes: ${sseNodeCount}, API nodes: ${apiNodeCount}`);

        if (Math.abs(sseNodeCount - apiNodeCount) > 2) {
            console.warn(`⚠️ Node count discrepancy: SSE=${sseNodeCount}, API=${apiNodeCount}`);
        }
    }
    
    updateHealthBadgeOnly(networkHealth) {
        // Update only the health badge without disrupting the node list
        const healthElements = document.querySelectorAll('[data-health-badge]');
        healthElements.forEach(element => {
            element.innerHTML = this.getHealthBadge(networkHealth);
        });
    }
    
    updateNetworkStatsFromAPI(statsData) {
        // Update network statistics from API data
        if (statsData && statsData.network_summary) {
            const summary = statsData.network_summary;
            
            // Update internal stats with server-compatible structure
            this.nodeStats = {
                ...this.nodeStats,
                totalNodes: summary.total_nodes || this.nodeStats.totalNodes,
                networkHealth: this.calculateNetworkHealth(summary),
                networkSummary: summary  // Store complete server summary
            };
            
            // Update any displayed network summary info
            const healthElements = document.querySelectorAll('[data-health-badge]');
            healthElements.forEach(element => {
                element.innerHTML = this.getHealthBadge(this.nodeStats.networkHealth);
            });
            
            console.log('📊 Network stats updated from API');
        }
    }
    
    cleanup() {
        this.stopUnifiedSSENetworkMonitoring();
        
        // Clear any remaining timers (non-polling)
        if (this.debounceTimers) {
            this.debounceTimers.forEach(timer => clearTimeout(timer));
            this.debounceTimers.clear();
        }
        
        console.log('🧹 UI cleanup completed (consolidated)');
    }
    
    stopUnifiedSSENetworkMonitoring() {
        if (this.eventSource) {
            this.eventSource.close();
            this.eventSource = null;
        }
        this.isConnected = false;
        this.updateConnectionIndicator(false);
        this.updateSSEStatus('disconnected', 'Unified SSE connection closed');
    }
    
    updateServiceStatusIndicator(servicesData) {
        // Update UI to show service initialization status
        const serviceStatus = servicesData.service_initialization;
        const dhtJoinStatus = servicesData.dht_join_status;
        
        // Add service status indicator to the UI
        const statusContainer = document.querySelector('.navbar .d-flex');
        if (statusContainer) {
            // Remove existing service indicator
            const existingIndicator = statusContainer.querySelector('.service-status-indicator');
            if (existingIndicator) {
                existingIndicator.remove();
            }
            
            // Add new service status indicator
            const allReady = serviceStatus.ready_count === serviceStatus.total_count;
            const joinSent = dhtJoinStatus.join_event_sent;
            
            let statusClass = 'success';
            let statusText = 'All Services Ready';
            let statusIcon = 'fas fa-check-circle';
            
            if (!allReady) {
                statusClass = 'warning';
                statusText = `Services: ${serviceStatus.ready_count}/${serviceStatus.total_count}`;
                statusIcon = 'fas fa-clock';
            } else if (!joinSent) {
                statusClass = 'info';
                statusText = 'Join Pending';
                statusIcon = 'fas fa-hourglass-half';
            }
            
            const indicator = document.createElement('span');
            indicator.className = `service-status-indicator badge bg-${statusClass} me-2`;
            indicator.innerHTML = `<i class="${statusIcon} me-1"></i>${statusText}`;
            indicator.title = `Service Status: ${statusText}${joinSent ? ' (DHT join sent)' : ' (DHT join pending)'}`;
            
            statusContainer.insertBefore(indicator, statusContainer.firstChild);
        }
    }
    
    validateNodeInfo(nodeInfo) {
        // Validate node info structure using consolidated validation patterns
        if (!nodeInfo || typeof nodeInfo !== 'object') {
            return null;
        }
        
        // Check required fields
        const requiredFields = ['node_id', 'ip', 'port'];
        for (const field of requiredFields) {
            if (!nodeInfo[field]) {
                console.warn(`Missing ${field} in node info:`, nodeInfo);
                return null;
            }
        }
        
        return nodeInfo;
    }
    
    updateSSEStatus(status, details = '') {
        const statusElement = document.getElementById('sse-status');
        if (statusElement) {
            let statusText = '';
            let statusClass = '';
            
            switch (status) {
                case 'connected':
                    statusText = 'Live (Unified)';
                    statusClass = 'text-success';
                    break;
                case 'connecting':
                    statusText = 'Connecting...';
                    statusClass = 'text-warning';
                    break;
                case 'error':
                    statusText = 'Reconnecting...';
                    statusClass = 'text-warning';
                    break;
                case 'failed':
                    statusText = 'Failed';
                    statusClass = 'text-danger';
                    break;
                case 'disconnected':
                    statusText = 'Disconnected';
                    statusClass = 'text-danger';
                    break;
                default:
                    statusText = 'Unknown';
                    statusClass = 'text-muted';
            }
            
            // Clear existing classes and apply new ones
            statusElement.className = `text-muted ms-2 ${statusClass}`;
            statusElement.textContent = statusText;
            
            if (details) {
                statusElement.title = details;
            }
        }
    }
    
    clearChatHistory() {
        try {
            // Validate that we have chat history to clear
            if (!this.hasChatHistory()) {
                this.showToast('info', 'No chat history to clear');
                return;
            }
            
            const historyCount = this.getChatHistoryCount();
            
            // Clear the chat messages container
            const chatContainer = document.getElementById('chat-messages');
            if (chatContainer) {
                // Add fade-out animation
                chatContainer.style.transition = 'opacity 0.3s ease';
                chatContainer.style.opacity = '0.5';
                
                setTimeout(() => {
                    // Clear all messages
                    chatContainer.innerHTML = '';
                    
                    // Restore welcome message with current model info and system prompt status
                    let welcomeMessage = this.selectedModel ? 
                        `Welcome to LlamaNet! Using model: <strong>${this.selectedModel}</strong>` :
                        'Welcome to LlamaNet! Distributed AI inference network.';
                    
                    if (this.systemPrompt.trim()) {
                        welcomeMessage += '<br><small class="text-primary"><i class="fas fa-cog"></i> Custom system prompt active</small>';
                    }
                    
                    chatContainer.innerHTML = `
                        <div class="text-center text-muted">
                            <i class="fas fa-robot fa-2x mb-2"></i>
                            <p>${welcomeMessage}</p>
                            <p class="small">Start a conversation below.</p>
                        </div>
                    `;
                    
                    // Restore opacity
                    chatContainer.style.opacity = '1';
                }, 150);
            }
            
            // Clear internal chat history
            this.chatHistory = [];
            
            // Clear any stored chat history in localStorage (future-proofing)
            try {
                localStorage.removeItem('llamanet_chat_history');
                localStorage.removeItem('llamanet_chat_timestamp');
            } catch (e) {
                // Ignore localStorage errors (private browsing, etc.)
                console.debug('Could not clear localStorage chat history:', e);
            }
            
            // Update clear history button state
            this.updateClearHistoryButton();
            
            // Show success notification with count
            this.showToast('success', `Chat history cleared (${historyCount} messages removed)`);
            
            // Log the action for debugging
            console.log(`🗑️ Chat history cleared by user (${historyCount} messages removed)`);
            
            // Focus back to input for better UX
            setTimeout(() => {
                const messageInput = document.getElementById('message-input');
                if (messageInput) {
                    messageInput.focus();
                }
            }, 200);
            
        } catch (error) {
            console.error('Error clearing chat history:', error);
            this.showToast('error', 'Failed to clear chat history: ' + error.message);
        }
    }
    
    getChatHistoryCount() {
        return this.chatHistory ? this.chatHistory.length : 0;
    }
    
    hasChatHistory() {
        return this.getChatHistoryCount() > 0;
    }
    
    updateClearHistoryButton() {
        const clearButton = document.getElementById('clear-history-btn');
        if (clearButton) {
            const hasHistory = this.hasChatHistory();
            
            // Update button state
            clearButton.disabled = !hasHistory;
            
            // Update button text and icon based on state
            if (hasHistory) {
                clearButton.innerHTML = '<i class="fas fa-trash-alt"></i> Clear History';
                clearButton.title = `Clear all chat messages (${this.getChatHistoryCount()} messages)`;
                clearButton.classList.remove('btn-outline-secondary');
                clearButton.classList.add('btn-outline-warning');
            } else {
                clearButton.innerHTML = '<i class="fas fa-trash-alt"></i> No History';
                clearButton.title = 'No chat messages to clear';
                clearButton.classList.remove('btn-outline-warning');
                clearButton.classList.add('btn-outline-secondary');
            }
        }
    }
}

class ModelDownloaderUI {
    constructor() {
        this.baseUrl = window.location.origin;
        this.searchResults = [];
        this.activeDownloads = new Map();
        this.localModels = [];
        this.downloadEventSources = new Map();
    }

    searchModels() {
        const input = document.getElementById('modelSearchInput');
        const query = input.value.trim();
        this._debouncedSearch(query);
    }

    quickSearch(query) {
        const input = document.getElementById('modelSearchInput');
        input.value = query;
        this._debouncedSearch(query);
    }

    _initSearchAutocomplete() {
        const input = document.getElementById('modelSearchInput');
        if (!input || this._autocompleteInitialized) return;
        this._autocompleteInitialized = true;
        this._searchTimer = null;
        this._lastQuery = null;

        input.addEventListener('input', () => {
            const query = input.value.trim();
            if (this._searchTimer) clearTimeout(this._searchTimer);
            this._searchTimer = setTimeout(() => {
                if (query !== this._lastQuery) {
                    this._lastQuery = query;
                    this._executeSearch(query);
                }
            }, 300);
        });

        input.addEventListener('focus', () => {
            const dropdown = document.getElementById('searchAutocomplete');
            if (dropdown && dropdown.children.length > 0) {
                dropdown.style.display = 'block';
            }
        });

        document.addEventListener('click', (e) => {
            const dropdown = document.getElementById('searchAutocomplete');
            if (dropdown && !e.target.closest('#modelSearchInput') && !e.target.closest('#searchAutocomplete')) {
                dropdown.style.display = 'none';
            }
        });
    }

    _debouncedSearch(query) {
        if (this._searchTimer) clearTimeout(this._searchTimer);
        this._lastQuery = query;
        this._executeSearch(query);
    }

    async _executeSearch(query) {
        const resultsDiv = document.getElementById('modelSearchResults');
        const dropdown = document.getElementById('searchAutocomplete');
        const isTrending = !query;

        if (!query) {
            resultsDiv.innerHTML = '<div class="text-center py-3"><div class="spinner-border text-primary" role="status"></div><p class="mt-2">Loading trending models...</p></div>';
            if (dropdown) dropdown.style.display = 'none';
        }

        try {
            const response = await fetch(`${this.baseUrl}/models/search?q=${encodeURIComponent(query)}`);
            const data = await response.json();
            if (data.success && data.data) {
                this.searchResults = data.data;
                this.renderSearchResults(isTrending);
                if (dropdown) dropdown.style.display = 'none';
            } else {
                resultsDiv.innerHTML = '<div class="text-center text-muted py-4"><p>No models found</p></div>';
            }
        } catch (error) {
            if (!isTrending) {
                resultsDiv.innerHTML = `<div class="alert alert-danger">Search failed: ${error.message}</div>`;
            }
        }
    }

    async loadTrendingModels() {
        this._initSearchAutocomplete();
        await this._executeSearch('');
    }

    renderSearchResults(isTrending = false) {
        const resultsDiv = document.getElementById('modelSearchResults');
        if (!this.searchResults.length) {
            resultsDiv.innerHTML = '<div class="text-center text-muted py-4"><p>No models found</p></div>';
            return;
        }
        const header = isTrending ? '<div class="mb-2"><span class="badge bg-primary me-1"><i class="fas fa-fire"></i> Trending</span><small class="text-muted">Popular GGUF models on Hugging Face</small></div>' : '';
        resultsDiv.innerHTML = header + this.searchResults.map(model => {
            const sizeDisplay = model.size_estimate && model.size_estimate.estimated
                ? `<span class="me-3" title="Estimated Q4_K_M size"><i class="fas fa-hdd"></i> ${model.size_estimate.label}</span>`
                : '';
            return `
            <div class="model-search-result-item border rounded p-3 mb-2">
                <div class="d-flex justify-content-between align-items-start">
                    <div class="flex-grow-1">
                        <h6 class="mb-1"><i class="fas fa-brain text-primary"></i> <span class="fw-bold">${this.escapeHtml(model.repo_id)}</span></h6>
                        <div class="text-muted small mb-2">
                            ${sizeDisplay}
                            <span class="me-3"><i class="fas fa-download"></i> ${this.formatNumber(model.downloads)}</span>
                            <span class="me-3"><i class="fas fa-heart"></i> ${this.formatNumber(model.likes)}</span>
                            ${(model.tags || []).slice(0, 5).map(t => `<span class="badge bg-light text-dark me-1">${this.escapeHtml(t)}</span>`).join('')}
                        </div>
                    </div>
                    <div class="d-flex gap-2">
                        <button class="btn btn-sm btn-outline-info" onclick="modelDownloader.showModelDetails('${this.escapeHtml(model.repo_id)}')"><i class="fas fa-info-circle"></i> Details</button>
                        <button class="btn btn-sm btn-primary" onclick="modelDownloader.showDownloadDialog('${this.escapeHtml(model.repo_id)}')"><i class="fas fa-download"></i> Download</button>
                    </div>
                </div>
            </div>`;
        }).join('');
    }

    async showModelDetails(repoId) {
        const content = document.getElementById('modelDetailsContent');
        content.innerHTML = '<div class="text-center py-3"><div class="spinner-border text-primary" role="status"></div><p class="mt-2">Loading...</p></div>';
        const modal = new bootstrap.Modal(document.getElementById('modelDetailsModal'));
        modal.show();

        try {
            const response = await fetch(`${this.baseUrl}/models/details/${encodeURIComponent(repoId)}`);
            const data = await response.json();
            if (data.success && data.data) {
                const info = data.data;
                const ggufFiles = info.gguf_files || [];
                content.innerHTML = `
                    <div class="row">
                        <div class="col-md-6">
                            <h6><i class="fas fa-brain"></i> Model Overview</h6>
                            <div class="network-detail-item">
                                <strong>Repository:</strong> ${this.escapeHtml(repoId)}<br>
                                <strong>Downloads:</strong> ${this.formatNumber(info.downloads || 0)}<br>
                                <strong>Likes:</strong> ${this.formatNumber(info.likes || 0)}<br>
                                <strong>SHA:</strong> <code>${(info.sha || '').substring(0, 12)}</code>
                            </div>
                        </div>
                        <div class="col-md-6">
                            <h6><i class="fas fa-file-archive"></i> GGUF Files (${ggufFiles.length})</h6>
                            <div class="network-detail-item" style="max-height: 300px; overflow-y: auto;">
                                ${ggufFiles.length > 0 ? ggufFiles.map(f => {
                                    const fileName = typeof f === 'string' ? f : f.filename;
                                    const fileSize = typeof f === 'string' ? null : f.size_gb;
                                    return `
                                    <div class="d-flex justify-content-between align-items-center mb-1 p-1 border-bottom">
                                        <span class="small"><i class="fas fa-file"></i> ${this.escapeHtml(fileName)}</span>
                                        <div class="d-flex align-items-center gap-2">
                                            ${fileSize ? `<span class="badge bg-light text-dark">${fileSize} GB</span>` : ''}
                                            ${['Q4_K_M','Q4_K_S','Q5_K_M'].some(p => fileName.toUpperCase().includes(p)) ? '<span class="badge bg-success">Recommended</span>' : ''}
                                        </div>
                                    </div>`;
                                }).join('') : '<div class="text-muted">No GGUF files found</div>'}
                            </div>
                        </div>
                    </div>
                    <div class="mt-3 text-center">
                        <div class="d-flex justify-content-center gap-2 align-items-center">
                            <label class="form-label mb-0">Quantization:</label>
                            <select id="detailQuantSelect" class="form-select form-select-sm" style="width: auto;">
                                <option value="Q4_K_M">Q4_K_M (Recommended)</option>
                                <option value="Q5_K_M">Q5_K_M</option>
                                <option value="Q8_0">Q8_0 (Higher Quality)</option>
                            </select>
                            <button class="btn btn-primary" onclick="bootstrap.Modal.getInstance(document.getElementById('modelDetailsModal')).hide(); modelDownloader.startDownload('${this.escapeHtml(repoId)}', document.getElementById('detailQuantSelect').value);">
                                <i class="fas fa-download"></i> Download
                            </button>
                        </div>
                    </div>`;
            } else {
                content.innerHTML = '<div class="alert alert-warning">Could not load details</div>';
            }
        } catch (error) {
            content.innerHTML = `<div class="alert alert-danger">Error: ${error.message}</div>`;
        }
    }

    showDownloadDialog(repoId) {
        const model = this.searchResults.find(m => m.repo_id === repoId);
        const sizeHint = model && model.size_estimate && model.size_estimate.estimated
            ? `\nEstimated Q4_K_M size: ${model.size_estimate.label}`
            : '';
        const quant = prompt(`Select quantization for ${repoId}:${sizeHint}\n\n1. Q4_K_M (Recommended)\n2. Q5_K_M (Better quality)\n3. Q8_0 (High quality)\n\nEnter choice (default: Q4_K_M):`, 'Q4_K_M');
        if (quant !== null) this.startDownload(repoId, quant.trim() || 'Q4_K_M');
    }

    async startDownload(repoId, quantization = 'Q4_K_M') {
        try {
            const response = await fetch(`${this.baseUrl}/models/download`, {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ repo_id: repoId, quantization })
            });
            const data = await response.json();
            if (data.success && data.data) {
                this.showToast('success', `Download started: ${repoId}:${quantization}`);
                document.getElementById('downloads-tab').click();
                this.trackDownloadProgress(data.data.download_id);
            } else {
                this.showToast('error', `Failed: ${data.message || 'Unknown error'}`);
            }
        } catch (error) {
            this.showToast('error', `Download error: ${error.message}`);
        }
    }

    trackDownloadProgress(downloadId) {
        const existing = this.downloadEventSources.get(downloadId);
        if (existing) existing.close();

        const eventSource = new EventSource(`${this.baseUrl}/models/download/status?download_id=${downloadId}`);
        this.downloadEventSources.set(downloadId, eventSource);

        eventSource.onmessage = (event) => {
            try {
                const data = JSON.parse(event.data);
                if (data.type === 'heartbeat') return;
                this.activeDownloads.set(downloadId, data);
                this.renderDownloads();

                if (data.status === 'completed') {
                    this.showToast('success', `Download complete: ${data.repo_id}`);
                    eventSource.close();
                    this.downloadEventSources.delete(downloadId);
                    this.loadLocalModels();
                } else if (data.status === 'failed' || data.status === 'cancelled') {
                    this.showToast('error', `Download ${data.status}: ${data.error || data.repo_id}`);
                    eventSource.close();
                    this.downloadEventSources.delete(downloadId);
                }
            } catch (e) {
                console.error('Download progress parse error:', e);
            }
        };
        eventSource.onerror = () => { eventSource.close(); this.downloadEventSources.delete(downloadId); };
    }

    renderDownloads() {
        const container = document.getElementById('activeDownloadsList');
        const badge = document.getElementById('activeDownloadsBadge');
        if (this.activeDownloads.size === 0) {
            container.innerHTML = '<div class="text-center text-muted py-4"><i class="fas fa-cloud-download-alt fa-2x mb-2"></i><p>No active downloads</p></div>';
            badge.style.display = 'none';
            return;
        }
        badge.style.display = 'inline';
        badge.textContent = this.activeDownloads.size;

        container.innerHTML = Array.from(this.activeDownloads.entries()).map(([id, dl]) => {
            const percent = dl.percent || 0;
            const isTerminal = ['completed', 'failed', 'cancelled'].includes(dl.status);
            const progressClass = dl.status === 'completed' ? 'bg-success' : dl.status === 'failed' ? 'bg-danger' : '';
            const etaDisplay = dl.eta_formatted && !isTerminal
                ? ` &bull; ETA: ${dl.eta_formatted}`
                : '';
            return `
                <div class="download-item border rounded p-3 mb-2">
                    <div class="d-flex justify-content-between align-items-center mb-2">
                        <div>
                            <strong><i class="fas fa-file-archive"></i> ${this.escapeHtml(dl.repo_id || id)}</strong>
                            <span class="badge bg-secondary ms-2">${this.escapeHtml(dl.quantization || '')}</span>
                            <span class="badge ${dl.status === 'completed' ? 'bg-success' : dl.status === 'failed' ? 'bg-danger' : 'bg-primary'} ms-1">${dl.status}</span>
                        </div>
                        ${!isTerminal ? `<button class="btn btn-sm btn-outline-danger" onclick="modelDownloader.cancelDownload('${id}')"><i class="fas fa-times"></i> Cancel</button>` : ''}
                    </div>
                    <div class="progress mb-1" style="height: 20px;">
                        <div class="progress-bar progress-bar-striped ${!isTerminal ? 'progress-bar-animated' : ''} ${progressClass}" role="progressbar" style="width: ${percent}%">${percent}%</div>
                    </div>
                    <div class="small text-muted">
                        ${this.formatBytes(dl.bytes_downloaded || 0)} / ${this.formatBytes(dl.total_bytes || 0)}
                        ${!isTerminal ? ` &bull; ${this.formatBytes(dl.speed || 0)}/s` : ''}
                        ${etaDisplay}
                        ${dl.error ? ` <span class="text-danger">&bull; ${this.escapeHtml(dl.error)}</span>` : ''}
                    </div>
                </div>`;
        }).join('');
    }

    async cancelDownload(downloadId) {
        try {
            await fetch(`${this.baseUrl}/models/download/${downloadId}`, { method: 'DELETE' });
            this.activeDownloads.delete(downloadId);
            this.renderDownloads();
            this.showToast('info', 'Download cancelled');
        } catch (error) {
            this.showToast('error', `Cancel failed: ${error.message}`);
        }
    }

    async loadLocalModels() {
        const container = document.getElementById('localModelsList');
        try {
            const response = await fetch(`${this.baseUrl}/models/local`);
            const data = await response.json();
            if (data.success && data.data) {
                this.localModels = data.data;
                this.renderLocalModels(data.disk_usage);
            } else {
                container.innerHTML = '<div class="alert alert-warning">Could not load local models</div>';
            }
        } catch (error) {
            container.innerHTML = `<div class="alert alert-danger">Error: ${error.message}</div>`;
        }
    }

    renderLocalModels(diskUsage) {
        const container = document.getElementById('localModelsList');
        if (!this.localModels.length) {
            container.innerHTML = '<div class="text-center text-muted py-4"><i class="fas fa-hdd fa-2x mb-2"></i><p>No models downloaded yet</p><p class="small">Use the Search tab to find and download models</p></div>';
            return;
        }
        const diskHtml = diskUsage ? `<div class="mb-3 p-2 bg-light rounded d-flex justify-content-between"><span><i class="fas fa-hdd"></i> ${this.escapeHtml(diskUsage.cache_dir)}</span><span><strong>${diskUsage.model_count}</strong> models &bull; <strong>${diskUsage.total_size_gb} GB</strong></span></div>` : '';
        container.innerHTML = diskHtml + this.localModels.map(model => `
            <div class="local-model-item border rounded p-3 mb-2 ${model.exists ? '' : 'border-warning'}">
                <div class="d-flex justify-content-between align-items-start">
                    <div class="flex-grow-1">
                        <h6 class="mb-1"><i class="fas fa-file-archive ${model.exists ? 'text-success' : 'text-warning'}"></i> ${this.escapeHtml(model.filename || model.repo_id)}</h6>
                        <div class="small text-muted mb-1">
                            <div><strong>Source:</strong> ${this.escapeHtml(model.repo_id)}</div>
                            <div><strong>Size:</strong> ${model.size_gb} GB</div>
                            ${model.exists ? '' : '<div class="text-warning"><i class="fas fa-exclamation-triangle"></i> File missing</div>'}
                        </div>
                    </div>
                    <div class="d-flex gap-2">
                        ${model.exists ? `<button class="btn btn-sm btn-primary" onclick="modelDownloader.selectModel('${this.escapeHtml(model.local_path)}')"><i class="fas fa-check"></i> Use</button>` : ''}
                        <button class="btn btn-sm btn-outline-danger" onclick="modelDownloader.deleteModel('${this.escapeHtml(model.id)}')"><i class="fas fa-trash"></i></button>
                    </div>
                </div>
                <div class="small text-muted mt-1"><code class="text-break">${this.escapeHtml(model.local_path)}</code></div>
            </div>
        `).join('');
    }

    async selectModel(modelPath) {
        const overlay = document.getElementById('model-reload-overlay');
        overlay.style.display = 'flex';
        try {
            const response = await fetch(`${this.baseUrl}/models/select`, {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ model_path: modelPath, load_mode: 'pool' })
            });
            const data = await response.json();
            if (data.success) {
                this.showToast('success', data.message);
                const managerModal = bootstrap.Modal.getInstance(document.getElementById('modelManagerModal'));
                if (managerModal) managerModal.hide();
                // Hide no-model banner if it was showing
                const banner = document.getElementById('no-model-banner');
                if (banner) banner.style.display = 'none';

                // Update model info in main UI after hot-reload
                if (typeof llamaNetUI !== 'undefined' && data.data && data.data.model_name) {
                    llamaNetUI.selectedModel = data.data.model_name;
                    llamaNetUI.updateChatInterface(data.data.model_name);
                    localStorage.setItem('llamanet_selected_model', data.data.model_name);

                    // Refresh pool and network status after model loads
                    setTimeout(() => {
                        llamaNetUI.loadPoolStatus();
                        llamaNetUI.refreshNetworkDataOnTopologyChange();
                    }, 3000);
                }
            } else {
                this.showToast('error', `Failed: ${data.message}`);
            }
        } catch (error) {
            this.showToast('error', `Error: ${error.message}`);
        } finally {
            overlay.style.display = 'none';
        }
    }

    async deleteModel(modelId) {
        if (!confirm('Delete this model from disk?')) return;
        try {
            const response = await fetch(`${this.baseUrl}/models/local/${encodeURIComponent(modelId)}`, { method: 'DELETE' });
            const data = await response.json();
            if (data.success) {
                this.showToast('success', 'Model deleted');
                this.loadLocalModels();
            } else {
                this.showToast('error', 'Failed to delete model');
            }
        } catch (error) {
            this.showToast('error', `Error: ${error.message}`);
        }
    }

    show() {
        const modal = new bootstrap.Modal(document.getElementById('modelManagerModal'));
        modal.show();
        document.getElementById('local-tab').addEventListener('shown.bs.tab', () => this.loadLocalModels());
        document.getElementById('downloads-tab').addEventListener('shown.bs.tab', () => this.renderDownloads());
        document.getElementById('pool-tab').addEventListener('shown.bs.tab', () => {
            if (typeof llamaNetUI !== 'undefined') {
                llamaNetUI.loadPoolStatus();
            }
        });
        this.loadTrendingModels();
        this._initSearchAutocomplete();
    }

    showToast(type, message) {
        if (typeof llamaNetUI !== 'undefined' && llamaNetUI.showToast) {
            llamaNetUI.showToast(type, message);
        }
    }

    formatNumber(num) {
        if (num >= 1000000) return (num / 1000000).toFixed(1) + 'M';
        if (num >= 1000) return (num / 1000).toFixed(1) + 'K';
        return num.toString();
    }

    formatBytes(bytes) {
        if (bytes === 0) return '0 B';
        const k = 1024;
        const sizes = ['B', 'KB', 'MB', 'GB'];
        const i = Math.floor(Math.log(bytes) / Math.log(k));
        return parseFloat((bytes / Math.pow(k, i)).toFixed(1)) + ' ' + sizes[i];
    }

    escapeHtml(text) {
        const div = document.createElement('div');
        div.textContent = text || '';
        return div.innerHTML;
    }
}

// Global functions for HTML event handlers
let llamaNetUI;
let modelDownloader;

function showModelManager() {
    if (!modelDownloader) {
        modelDownloader = new ModelDownloaderUI();
    }
    modelDownloader.show();
}

// System Prompt Functions
function toggleSystemPrompt() {
    const section = document.getElementById('system-prompt-section');
    const toggle = document.getElementById('system-prompt-toggle');
    
    if (section.style.display === 'none' || !section.style.display) {
        section.style.display = 'block';
        section.classList.add('show');
        toggle.innerHTML = '<i class="fas fa-cog"></i> Hide System Prompt';
        
        // Focus on the textarea
        setTimeout(() => {
            const input = document.getElementById('system-prompt-input');
            if (input) {
                input.focus();
            }
        }, 100);
    } else {
        section.style.display = 'none';
        section.classList.remove('show');
        toggle.innerHTML = llamaNetUI.systemPrompt.trim() ? 
            '<i class="fas fa-cog"></i> System Prompt <i class="fas fa-check-circle ms-1"></i>' :
            '<i class="fas fa-cog"></i> System Prompt';
    }
}

function applySystemPrompt() {
    const input = document.getElementById('system-prompt-input');
    if (input && llamaNetUI) {
        llamaNetUI.systemPrompt = input.value.trim();
        llamaNetUI.saveSystemPrompt();
        llamaNetUI.updateSystemPromptUI();
        
        // Show confirmation
        if (llamaNetUI.systemPrompt) {
            llamaNetUI.showToast('success', 'Custom system prompt applied');
        } else {
            llamaNetUI.showToast('info', 'System prompt cleared - using default');
        }
        
        // Hide the section
        toggleSystemPrompt();
    }
}

function clearSystemPrompt() {
    const input = document.getElementById('system-prompt-input');
    const presets = document.getElementById('system-prompt-presets');
    
    if (input) {
        input.value = '';
    }
    if (presets) {
        presets.value = '';
    }
    
    if (llamaNetUI) {
        llamaNetUI.systemPrompt = '';
        llamaNetUI.saveSystemPrompt();
        llamaNetUI.updateSystemPromptUI();
        llamaNetUI.showToast('info', 'System prompt cleared');
    }
}

function sendMessage() {
    llamaNetUI.sendMessage();
}

function handleKeyPress(event) {
    llamaNetUI.handleKeyPress(event);
}

function refreshNetworkStatus() {
    llamaNetUI.refreshNetworkStatus();
}

function showNetworkModal() {
    llamaNetUI.showNetworkModal();
}

function clearChatHistory() {
    // Check if there's actually history to clear
    if (llamaNetUI && !llamaNetUI.hasChatHistory()) {
        llamaNetUI.showToast('info', 'No chat history to clear');
        return;
    }
    
    // Show confirmation modal
    const modal = new bootstrap.Modal(document.getElementById('clearHistoryModal'));
    modal.show();
}

function confirmClearHistory() {
    if (llamaNetUI) {
        llamaNetUI.clearChatHistory();
    }
}

function dismissWakeWarning() {
    const banner = document.getElementById('wake-warning-banner');
    if (banner) banner.style.display = 'none';
    if (llamaNetUI) llamaNetUI._wakeWarningDismissed = true;
}

function refreshNodeInfo(nodeId) {
    if (llamaNetUI) {
        llamaNetUI.refreshNodeInfo(nodeId);
    }
}

// Initialize when DOM is loaded
document.addEventListener('DOMContentLoaded', async () => {
    console.log('🚀 Initializing LlamaNet UI...');
    
    llamaNetUI = new LlamaNetUI();
    modelDownloader = new ModelDownloaderUI();

    // Check if server is in no-model mode
    try {
        const healthResp = await fetch(`${window.location.origin}/health`);
        if (healthResp.ok) {
            const health = await healthResp.json();
            if (health.status === 'no_model' || health.no_model_mode) {
                const banner = document.getElementById('no-model-banner');
                if (banner) banner.style.display = 'block';
                setTimeout(() => showModelManager(), 500);
            }
        }
    } catch (e) {
        // Server might not be ready yet
    }
    
    // Setup system prompt event listeners
    const presets = document.getElementById('system-prompt-presets');
    const input = document.getElementById('system-prompt-input');
    
    if (presets && input) {
        presets.addEventListener('change', (e) => {
            const selectedPreset = e.target.value;
            if (selectedPreset && llamaNetUI && llamaNetUI.systemPromptPresets[selectedPreset]) {
                input.value = llamaNetUI.systemPromptPresets[selectedPreset];
                llamaNetUI.updateCharacterCount();
            } else if (selectedPreset === 'custom') {
                // Keep current content for custom option
                input.focus();
            }
        });
        
        // Update character count on input
        input.addEventListener('input', () => {
            if (llamaNetUI) {
                llamaNetUI.updateCharacterCount();
            }
        });
    }
    
    // Ensure initial network status is loaded
    try {
        // Wait a bit for the UI to initialize
        await new Promise(resolve => setTimeout(resolve, 100));
        
        // Force initial network data load
        await llamaNetUI.loadInitialNetworkStatus();
        
        // Also call the refresh methods for complete initialization
        await llamaNetUI.refreshNetworkDataOnTopologyChange();
        
        console.log('✅ LlamaNet UI initialization complete');
    } catch (error) {
        console.warn('Initial network data refresh failed:', error);
        // Still show something in the network status div
        llamaNetUI.showNetworkError('Failed to load initial network data');
    }
});

// Update the window beforeunload handler
window.addEventListener('beforeunload', () => {
    if (llamaNetUI) {
        llamaNetUI.cleanup();
    }
});
