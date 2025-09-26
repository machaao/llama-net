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
        
        // SSE-only properties (NO POLLING)
        this.eventSource = null;
        this.isConnected = false;
        this.reconnectAttempts = 0;
        this.maxReconnectAttempts = 5;
        this.reconnectDelay = 1000;
        
        // Node tracking
        this.activeNodes = new Map();
        this.nodeStats = {
            totalNodes: 0,
            modelsAvailable: new Set(),
            networkHealth: 'unknown'
        };
        
        // Event-driven node status tracking (not time-based)
        this.nodeStatuses = new Map(); // node_id -> 'online'|'offline'|'unknown'
        this.nodeLastEvent = new Map(); // node_id -> timestamp of last event
        this.nodeEventTypes = new Map(); // node_id -> last event type
        
        // SSE connection info
        this.connectionInfo = null;
        this.lastUpdateTime = 0;
        this.connectionStatus = 'connecting';
        this.errorCount = 0;
        
        // Remove ALL polling-related properties
        // NO: this.updateInterval
        // NO: this.isUpdating
        // NO: this.updateFrequency
        // NO: this.periodicUpdateTimer
        
        // Restore selected model from localStorage
        this.selectedModel = localStorage.getItem('llamanet_selected_model') || null;
        
        this.init();
    }
    
    init() {
        // Start ONLY SSE-based network monitoring
        this.startSSENetworkMonitoring();
        
        // ONE-TIME initial network status load (not polling)
        this.loadInitialNetworkStatus();
        
        this.setupEventListeners();
        
        // NO POLLING - All updates via SSE
        
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
                // Keep SSE running
                console.log('Page hidden - SSE continues running');
            } else {
                // Reconnect SSE if needed
                if (!this.isConnected) {
                    this.startSSENetworkMonitoring();
                }
            }
        });
    }
    
    setupEventListeners() {
        // No API mode selector needed - OpenAI only
    }
    
    async loadInitialNetworkStatus() {
        // Load initial network status once, then rely on SSE for all updates
        try {
            const [dhtResponse, modelsResponse, statsResponse] = await Promise.all([
                fetch(`${this.baseUrl}/dht/status`),
                fetch(`${this.baseUrl}/v1/models/network`),
                fetch(`${this.baseUrl}/models/statistics`)
            ]);
            
            if (dhtResponse.ok && modelsResponse.ok && statsResponse.ok) {
                const dhtStatus = await dhtResponse.json();
                const modelsData = await modelsResponse.json();
                const statsData = await statsResponse.json();
                
                await this.updateNetworkDisplay(dhtStatus, modelsData, statsData);
            } else {
                this.showNetworkError('Unable to connect to LlamaNet node');
            }
        } catch (error) {
            console.error('Error loading initial network status:', error);
            this.showNetworkError('Network discovery failed');
        }
    }
    
    startSSENetworkMonitoring() {
        if (this.eventSource) {
            this.eventSource.close();
        }
        
        console.log('🔗 Starting SSE network monitoring...');
        this.updateSSEStatus('connecting', 'Establishing connection...');
        
        this.eventSource = new EventSource(`${this.baseUrl}/events/network`);
        
        this.eventSource.onopen = () => {
            console.log('✅ SSE connected');
            this.isConnected = true;
            this.reconnectAttempts = 0;
            this.reconnectDelay = 1000;
            this.updateConnectionIndicator(true);
            this.connectionStatus = 'connected';
            this.errorCount = 0;
            this.updateSSEStatus('connected', 'Real-time updates active');
        };
        
        this.eventSource.onmessage = (event) => {
            try {
                const data = JSON.parse(event.data);
                this.handleSSENetworkEvent(data);
            } catch (e) {
                console.error('Error parsing SSE event:', e);
            }
        };
        
        this.eventSource.onerror = (error) => {
            console.warn('❌ SSE connection error:', error);
            this.isConnected = false;
            this.updateConnectionIndicator(false);
            
            if (this.reconnectAttempts < this.maxReconnectAttempts) {
                this.connectionStatus = 'error';
                this.updateSSEStatus('error', `Reconnecting... (attempt ${this.reconnectAttempts + 1}/${this.maxReconnectAttempts})`);
                
                this.reconnectAttempts++;
                const delay = this.reconnectDelay * Math.pow(2, this.reconnectAttempts - 1);
                
                console.log(`🔄 Reconnecting SSE in ${delay}ms (attempt ${this.reconnectAttempts}/${this.maxReconnectAttempts})`);
                
                setTimeout(() => {
                    if (!this.isConnected) {
                        this.startSSENetworkMonitoring();
                    }
                }, delay);
            } else {
                this.connectionStatus = 'failed';
                this.updateSSEStatus('failed', 'Connection failed - please refresh page');
                console.error('❌ Max SSE reconnection attempts reached');
                this.showToast('error', 'Lost connection to real-time updates. Please refresh the page.');
            }
        };
    }
    
    handleSSENetworkEvent(data) {
        // Process SSE events only - no polling
        switch (data.type) {
            case 'connected':
                console.log('📡 SSE network monitoring connected', data.server_info);
                this.showToast('success', 'Connected to real-time network updates');
                
                // Store connection info
                this.connectionInfo = {
                    id: data.connection_id,
                    serverInfo: data.server_info,
                    connectedAt: data.timestamp
                };
                break;
                
            case 'node_joined':
            case 'node_updated':
                if (data.node_info) {
                    const normalizedNode = this.normalizeNodeData(data.node_info);
                    this.activeNodes.set(normalizedNode.node_id, normalizedNode);
                    
                    // Set status based on event (not time)
                    this.nodeStatuses.set(normalizedNode.node_id, 'online');
                    this.nodeLastEvent.set(normalizedNode.node_id, Date.now());
                    this.nodeEventTypes.set(normalizedNode.node_id, data.type);
                    
                    const eventIcon = data.type === 'node_joined' ? '🆕' : '🔄';
                    const eventAction = data.type.split('_')[1];
                    
                    console.log(`${eventIcon} Node ${eventAction} (SSE): ${normalizedNode.node_id.substring(0, 8)}... (${normalizedNode.model})`);
                    
                    if (data.type === 'node_joined') {
                        this.showToast('success', `🆕 Node joined: ${normalizedNode.node_id.substring(0, 8)}... (${normalizedNode.model})`);
                    }
                    
                    this.updateNetworkDisplayRealTime();
                }
                break;
                
            case 'node_left':
                if (data.node_info) {
                    const nodeId = data.node_info.node_id;
                    
                    // Mark as offline via event (not time)
                    this.nodeStatuses.set(nodeId, 'offline');
                    this.nodeLastEvent.set(nodeId, Date.now());
                    this.nodeEventTypes.set(nodeId, 'node_left');
                    
                    // Keep node in activeNodes for a short time to show "offline" status
                    // Remove after 30 seconds
                    setTimeout(() => {
                        this.activeNodes.delete(nodeId);
                        this.nodeStatuses.delete(nodeId);
                        this.nodeLastEvent.delete(nodeId);
                        this.nodeEventTypes.delete(nodeId);
                        this.updateNetworkDisplayRealTime();
                    }, 30000);
                    
                    console.log(`👋 Node left (SSE): ${nodeId.substring(0, 8)}...`);
                    this.showToast('warning', `👋 Node left: ${nodeId.substring(0, 8)}...`);
                    this.updateNetworkDisplayRealTime();
                }
                break;
                
            case 'network_changed':
                console.log('🌐 Network topology changed (SSE)');
                this.updateNetworkDisplayRealTime();
                break;
                
            case 'heartbeat':
                // SSE heartbeat - keep connection alive
                this.lastUpdateTime = Date.now();
                this.connectionStatus = 'connected';
                
                // Update connection info
                if (data.active_connections !== undefined) {
                    this.connectionInfo = {
                        ...this.connectionInfo,
                        activeConnections: data.active_connections,
                        uptime: data.uptime,
                        lastHeartbeat: data.timestamp
                    };
                }
                
                this.updateConnectionIndicatorWithHeartbeat(data);
                break;
                
            case 'error':
                console.error('SSE network event error:', data.message);
                this.showToast('error', `Network error: ${data.message}`);
                this.connectionStatus = 'error';
                break;
                
            default:
                console.log('Unknown SSE network event:', data);
        }
    }
    
    normalizeNodeData(nodeData) {
        // Ensure consistent data structure
        return {
            node_id: nodeData.node_id || nodeData.id,
            ip: nodeData.ip || 'unknown',
            port: nodeData.port || nodeData.http_port || 8000,
            model: nodeData.model || 'unknown',
            load: nodeData.load || 0,
            tps: nodeData.tps || 0,
            uptime: nodeData.uptime || 0,
            last_seen: nodeData.last_seen || Math.floor(Date.now() / 1000),
            dht_port: nodeData.dht_port
        };
    }
    
    updateNetworkDisplayRealTime() {
        const container = document.getElementById('network-status');
        if (!container) return;
        
        const nodes = Array.from(this.activeNodes.values());
        
        // Group nodes by model
        const modelGroups = {};
        nodes.forEach(node => {
            if (!modelGroups[node.model]) {
                modelGroups[node.model] = [];
            }
            modelGroups[node.model].push(node);
        });
        
        // Calculate network stats
        const totalNodes = nodes.length;
        const avgLoad = nodes.length > 0 ? nodes.reduce((sum, n) => sum + n.load, 0) / nodes.length : 0;
        const totalTps = nodes.reduce((sum, n) => sum + n.tps, 0);
        const onlineNodes = nodes.filter(n => {
            const eventStatus = this.nodeStatuses.get(n.node_id);
            return eventStatus === 'online' || (Date.now() / 1000) - n.last_seen < 60;
        }).length;
        
        // Update network stats
        this.nodeStats = {
            totalNodes,
            onlineNodes,
            modelsAvailable: new Set(Object.keys(modelGroups)),
            networkHealth: this.calculateNetworkHealth(avgLoad, totalNodes)
        };
        
        // Create enhanced content with refresh timestamp
        const refreshTime = new Date().toLocaleTimeString();
        const newContent = `
            <div class="mb-3">
                <h6>
                    <i class="fas fa-server"></i> Network Status
                    <span class="live-indicator ms-2" title="Real-time updates via SSE">
                        <i class="fas fa-circle text-success live-pulse" style="font-size: 0.5rem;"></i>
                    </span>
                    <small class="text-muted ms-2" id="sse-status">Live</small>
                </h6>
                <div class="small mb-2">
                    <div><i class="fas fa-network-wired"></i> Total Nodes: <span class="metric-value">${totalNodes}</span> (${onlineNodes} online)</div>
                    <div><i class="fas fa-brain"></i> Models Available: <span class="metric-value">${this.nodeStats.modelsAvailable.size}</span></div>
                    <div><i class="fas fa-heartbeat"></i> Network Health: <span class="network-health-badge">${this.getHealthBadge(this.nodeStats.networkHealth)}</span></div>
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
                        <button class="btn btn-sm btn-outline-primary" onclick="llamaNetUI.selectModel('${modelName}')" title="Select this model">
                            <i class="fas fa-check"></i>
                        </button>
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
                                <div><i class="fas fa-network-wired"></i> ${node.ip}:${node.port}</div>
                                <div><i class="fas fa-clock"></i> Up: ${uptimeText} | ${lastSeenText}</div>
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
    
    calculateNetworkHealth(avgLoad, nodeCount) {
        if (nodeCount === 0) return 'no_nodes';
        if (avgLoad < 0.3 && nodeCount >= 2) return 'excellent';
        if (avgLoad < 0.7) return 'good';
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
        // Manual refresh - maintain SSE-based UX
        try {
            this.showUpdateIndicator(true);
            
            // Instead of loading initial status, just refresh the current SSE-based display
            // and optionally fetch additional data for validation
            
            // Get current network stats for validation (don't overwrite display)
            const [dhtResponse, modelsResponse] = await Promise.all([
                fetch(`${this.baseUrl}/dht/status`).catch(() => null),
                fetch(`${this.baseUrl}/v1/models/network`).catch(() => null)
            ]);
            
            // Validate SSE connection and data freshness
            if (!this.isConnected) {
                console.log('🔄 SSE disconnected during refresh, reconnecting...');
                this.startSSENetworkMonitoring();
            }
            
            // Force a fresh update of the current SSE-based display
            this.updateNetworkDisplayRealTime();
            
            // Show validation info if available
            if (dhtResponse && dhtResponse.ok && modelsResponse && modelsResponse.ok) {
                const dhtData = await dhtResponse.json();
                const modelsData = await modelsResponse.json();
                
                // Update stats for validation but maintain SSE-based node list
                this.validateNetworkStats(dhtData, modelsData);
            }
            
            this.showToast('success', 'Network status refreshed (SSE maintained)');
            
        } catch (error) {
            console.error('Error refreshing network status:', error);
            
            // On error, try to reconnect SSE
            if (!this.isConnected) {
                console.log('🔄 Attempting SSE reconnection due to refresh error...');
                this.startSSENetworkMonitoring();
            }
            
            this.showToast('error', 'Refresh failed - SSE connection maintained');
        } finally {
            this.showUpdateIndicator(false);
        }
    }
    
    async updateNetworkDisplay(dhtStatus, modelsData, statsData) {
        // Store previous stats for change detection
        this.previousModelStats = this.currentModelStats || {};
        this.currentModelStats = statsData.models || {};
        
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
        
        if (!dhtStatus.running) {
            container.innerHTML = `
                <div class="text-center text-warning">
                    <i class="fas fa-exclamation-triangle"></i>
                    <p>DHT not running</p>
                </div>
            `;
            return;
        }
        
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
                
                <div class="mb-3">
                    <h6><i class="fas fa-network-wired"></i> DHT Network</h6>
                    <div class="small">
                        <div>DHT Contacts: <span class="metric-value">${dhtStatus.contacts_count}</span></div>
                        <div>DHT Port: ${dhtStatus.dht_port}</div>
                    </div>
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
                            <div class="text-muted">${node.ip}:${node.port}</div>
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
            const currentTime = Date.now() / 1000;
            const isRecent = currentTime - node.last_seen < 60;
            const isVeryRecent = currentTime - node.last_seen < 30;
            
            let statusClass = 'offline';
            let statusTitle = 'Offline';
            
            if (isVeryRecent) {
                statusClass = 'online';
                statusTitle = 'Online (very recent)';
            } else if (isRecent) {
                statusClass = 'online';
                statusTitle = 'Online';
            } else if (currentTime - node.last_seen < 120) {
                statusClass = 'warning';
                statusTitle = 'Stale';
            }
            
            const lastSeenText = this.formatLastSeen(node.last_seen);
            const nodeChangeClass = this.isNodeUpdated(node.node_id) ? 'node-updated' : '';
            
            return `
                <div class="node-item small ms-2 clickable-node ${nodeChangeClass}" data-node-id="${node.node_id}" onclick="llamaNetUI.showNodeInfo('${node.node_id}')" style="cursor: pointer;">
                    <div class="d-flex align-items-center">
                        <span class="node-status ${statusClass}" title="${statusTitle} - Last seen: ${lastSeenText}"></span>
                        <div class="flex-grow-1">
                            <div class="fw-bold">
                                ${node.node_id.substring(0, 8)}... 
                                <i class="fas fa-info-circle text-primary ms-1" title="Click for details"></i>
                                ${this.getNodeChangeIndicator(node.node_id)}
                            </div>
                            <div class="text-muted">${node.ip}:${node.port}</div>
                            <div class="text-muted small">${lastSeenText}</div>
                        </div>
                    </div>
                </div>
            `;
        }).join('');
    }
    
    getHealthBadge(networkHealth) {
        const healthConfig = {
            'excellent': { class: 'success', text: 'Excellent', icon: 'fas fa-check-circle' },
            'good': { class: 'warning', text: 'Good', icon: 'fas fa-exclamation-circle' },
            'poor': { class: 'danger', text: 'Poor', icon: 'fas fa-times-circle' },
            'no_nodes': { class: 'secondary', text: 'No Nodes', icon: 'fas fa-question-circle' }
        };
        
        const config = healthConfig[networkHealth] || healthConfig['no_nodes'];
        
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
            chatHeader.innerHTML = `<i class="fas fa-comments"></i> Chat Interface - Model: ${modelId}`;
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
            const response = await fetch(`${this.baseUrl}/models/statistics`);
            
            if (response.ok) {
                const statsData = await response.json();
                const modelStats = statsData.models[modelId];
                
                if (modelStats) {
                    document.getElementById('node-info-details').innerHTML = this.renderModelDetailsView(modelId, modelStats, statsData.network_summary);
                } else {
                    throw new Error('Model not found in statistics');
                }
            } else {
                throw new Error(`HTTP ${response.status}: ${response.statusText}`);
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
                        <strong>Address:</strong> ${modelStats.best_node.ip}:${modelStats.best_node.port}<br>
                        <strong>Load:</strong> ${modelStats.best_node.load.toFixed(3)}<br>
                        <strong>TPS:</strong> ${modelStats.best_node.tps.toFixed(1)}<br>
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
                                    <small class="text-muted">${node.ip}:${node.port}</small>
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
                        <strong>Address:</strong> ${nodeData.ip}:${nodeData.port}<br>
                        <strong>Model:</strong> ${nodeData.model}<br>
                        <strong>DHT Port:</strong> ${nodeData.dht_port || 'Unknown'}<br>
                        <strong>Last Seen:</strong> ${lastSeenText}
                    </div>
                    
                    <h6 class="mt-3"><i class="fas fa-chart-line"></i> Performance Metrics</h6>
                    <div class="network-detail-item">
                        <strong>Load:</strong> ${nodeData.load.toFixed(2)}<br>
                        <strong>TPS:</strong> ${nodeData.tps.toFixed(1)}<br>
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
                        <strong>Address:</strong> ${nodeInfo.ip}:${nodeInfo.port}<br>
                        <strong>DHT Port:</strong> ${nodeInfo.dht_port || 'Unknown'}<br>
                        <strong>Model:</strong> ${nodeInfo.model}<br>
                        ${nodeInfo.model_path ? `<strong>Model Path:</strong> ${nodeInfo.model_path}<br>` : ''}
                        <strong>Last Seen:</strong> ${lastSeenText}
                    </div>
                    
                    <h6 class="mt-3"><i class="fas fa-chart-line"></i> Performance Metrics</h6>
                    <div class="network-detail-item">
                        <strong>Load:</strong> ${nodeInfo.load ? nodeInfo.load.toFixed(2) : '0.00'}<br>
                        <strong>TPS:</strong> ${nodeInfo.tps ? nodeInfo.tps.toFixed(1) : '0.0'}<br>
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
        
        // Build chat history for context - OPTIMIZED
        const messages = [
            { role: 'system', content: 'You are a helpful AI assistant. Provide clear, concise responses.' }
        ];
        
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
            stop: ["Human:", "User:", "\nHuman:", "\nUser:", "\n\nHuman:", "\n\nUser:"],
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
            return {
                text: this.cleanResponse(data.choices[0].message.content),
                metadata: {
                    id: data.id,
                    tokens: data.usage.total_tokens,
                    api: 'OpenAI Compatible',
                    node_info: data.node_info,
                    model_used: modelToUse
                }
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
        
        // Load detailed network information
        try {
            const [infoResponse, dhtResponse, statusResponse] = await Promise.all([
                fetch(`${this.baseUrl}/info`),
                fetch(`${this.baseUrl}/dht/status`),
                fetch(`${this.baseUrl}/status`)
            ]);
            
            const info = await infoResponse.json();
            const dht = await dhtResponse.json();
            const status = await statusResponse.json();
            
            document.getElementById('network-details').innerHTML = this.renderNetworkDetails(info, dht, status);
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
        const cleanupStats = dht.cleanup_stats || {};
        const lastCleanup = cleanupStats.last_cleanup ? 
            new Date(cleanupStats.last_cleanup * 1000).toLocaleTimeString() : 'Never';
        
        return `
            <div class="row">
                <div class="col-md-6">
                    <h6><i class="fas fa-server"></i> Node Information</h6>
                    <div class="network-detail-item">
                        <strong>Node ID:</strong> ${info.node_id}<br>
                        <strong>Model:</strong> ${info.model}<br>
                        <strong>Model Path:</strong> ${info.model_path}<br>
                        <strong>DHT Port:</strong> ${info.dht_port}<br>
                    </div>
                    
                    <h6 class="mt-3"><i class="fas fa-chart-line"></i> Performance</h6>
                    <div class="network-detail-item">
                        <strong>Load:</strong> ${status.load}<br>
                        <strong>TPS:</strong> ${status.tps}<br>
                        <strong>Uptime:</strong> ${status.uptime}s<br>
                        <strong>Total Tokens:</strong> ${status.total_tokens}
                    </div>
                </div>
                
                <div class="col-md-6">
                    <h6><i class="fas fa-network-wired"></i> DHT Status</h6>
                    <div class="network-detail-item">
                        <strong>Running:</strong> ${dht.running ? '✅ Yes' : '❌ No'}<br>
                        <strong>Contacts:</strong> ${dht.contacts_count}<br>
                        <strong>Storage Keys:</strong> ${dht.storage_keys.length}<br>
                        <strong>Bootstrap Nodes:</strong> ${dht.bootstrap_nodes.length}
                    </div>
                    
                    <h6 class="mt-3"><i class="fas fa-broom"></i> Cleanup Status</h6>
                    <div class="network-detail-item">
                        <strong>Last Cleanup:</strong> ${lastCleanup}<br>
                        <strong>Cleanup Interval:</strong> ${cleanupStats.cleanup_interval || 30}s<br>
                        <strong>Active Contacts:</strong> ${cleanupStats.routing_table_stats?.active_contacts || 0}<br>
                        <strong>Stale Contacts:</strong> ${cleanupStats.routing_table_stats?.stale_contacts || 0}
                    </div>
                    
                    <h6 class="mt-3"><i class="fas fa-cogs"></i> System Info</h6>
                    <div class="network-detail-item">
                        <strong>CPU:</strong> ${info.system.cpu}<br>
                        <strong>RAM:</strong> ${Math.round(info.system.ram.total / 1024 / 1024 / 1024)} GB<br>
                        <strong>GPU:</strong> ${info.system.gpu || 'None'}<br>
                        <strong>Platform:</strong> ${info.system.platform}
                    </div>
                </div>
            </div>
            
            <div class="mt-3">
                <h6><i class="fas fa-list"></i> Contact Details</h6>
                <div class="table-responsive">
                    <table class="table table-sm">
                        <thead>
                            <tr>
                                <th>Node ID</th>
                                <th>Address</th>
                                <th>Last Seen</th>
                                <th>Status</th>
                            </tr>
                        </thead>
                        <tbody>
                            ${dht.contacts.map(contact => `
                                <tr>
                                    <td><code>${contact.node_id.substring(0, 12)}...</code></td>
                                    <td>${contact.ip}:${contact.port}</td>
                                    <td>${contact.seconds_ago}s ago</td>
                                    <td><span class="badge bg-${contact.status === 'active' ? 'success' : 'warning'}">${contact.status}</span></td>
                                </tr>
                            `).join('')}
                        </tbody>
                    </table>
                </div>
            </div>
            
            <div class="mt-3">
                <h6><i class="fas fa-list"></i> Available Endpoints</h6>
                <div class="row">
                    <div class="col-12">
                        <ul class="list-unstyled small">
                            ${info.endpoints.map(ep => `<li><span class="api-endpoint">${ep}</span></li>`).join('')}
                        </ul>
                    </div>
                </div>
            </div>
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
                // Restore SSE status indicator
                const statusIcon = this.isConnected ? 
                    '<i class="fas fa-circle text-success live-pulse" style="font-size: 0.5rem;" title="Real-time updates active"></i>' :
                    '<i class="fas fa-circle text-warning" style="font-size: 0.5rem;" title="Connecting..."></i>';
                indicator.innerHTML = statusIcon;
            }
        }
        
        // Also update refresh button state
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
                this.startRealTimeNetworkMonitoring();
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
        // Validate current SSE data against fresh API data without disrupting UX
        const sseNodeCount = this.activeNodes.size;
        const apiNodeCount = modelsData.total_nodes || 0;
        
        // Log validation results
        console.log(`📊 Network validation: SSE nodes: ${sseNodeCount}, API nodes: ${apiNodeCount}`);
        
        // If there's a significant discrepancy, log it but don't disrupt UX
        if (Math.abs(sseNodeCount - apiNodeCount) > 2) {
            console.warn(`⚠️ Node count discrepancy detected: SSE=${sseNodeCount}, API=${apiNodeCount}`);
            
            // Optionally show a subtle warning without disrupting the display
            if (sseNodeCount === 0 && apiNodeCount > 0) {
                this.showToast('warning', 'SSE may be missing some nodes - data will sync automatically');
            }
        }
        
        // Update network health indicator based on fresh data
        if (dhtData.running !== undefined) {
            const healthStatus = this.calculateNetworkHealth(
                modelsData.total_nodes > 0 ? 0.5 : 1.0, // Rough estimate
                modelsData.total_nodes || 0
            );
            
            // Update only the health badge without touching node list
            this.updateHealthBadgeOnly(healthStatus);
        }
    }
    
    updateHealthBadgeOnly(networkHealth) {
        // Update only the health badge without disrupting the node list
        const healthBadges = document.querySelectorAll('.network-health-badge');
        healthBadges.forEach(badge => {
            badge.outerHTML = this.getHealthBadge(networkHealth);
        });
    }
    
    cleanup() {
        this.stopSSENetworkMonitoring();
        
        // NO POLLING CLEANUP NEEDED
        // Clear any remaining timers (non-polling)
        if (this.debounceTimers) {
            this.debounceTimers.forEach(timer => clearTimeout(timer));
            this.debounceTimers.clear();
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
                    
                    // Restore welcome message with current model info
                    const welcomeMessage = this.selectedModel ? 
                        `Welcome to LlamaNet! Using model: <strong>${this.selectedModel}</strong>` :
                        'Welcome to LlamaNet! Distributed AI inference network.';
                    
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

// Global functions for HTML event handlers
let llamaNetUI;

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

function refreshNodeInfo(nodeId) {
    if (llamaNetUI) {
        llamaNetUI.refreshNodeInfo(nodeId);
    }
}

// Initialize when DOM is loaded
document.addEventListener('DOMContentLoaded', () => {
    llamaNetUI = new LlamaNetUI();
});

// Update the window beforeunload handler
window.addEventListener('beforeunload', () => {
    if (llamaNetUI) {
        llamaNetUI.cleanup();
    }
});
