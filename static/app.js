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
        
        this.init();
    }
    
    init() {
        this.refreshNetworkStatus();
        this.setupEventListeners();
        
        // Refresh network status every 30 seconds
        setInterval(() => this.refreshNetworkStatus(), 30000);
    }
    
    setupEventListeners() {
        // No API mode selector needed - OpenAI only
    }
    
    async refreshNetworkStatus() {
        try {
            // Try to discover nodes through the current node
            const response = await fetch(`${this.baseUrl}/dht/status`);
            
            if (response.ok) {
                const dhtStatus = await response.json();
                await this.updateNetworkDisplay(dhtStatus);
            } else {
                this.showNetworkError('Unable to connect to LlamaNet node');
            }
        } catch (error) {
            console.error('Error refreshing network status:', error);
            this.showNetworkError('Network discovery failed');
        }
    }
    
    async updateNetworkDisplay(dhtStatus) {
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
            // Get nodes with separation between published and DHT contacts
            const nodesResponse = await fetch(`${this.baseUrl}/nodes`);
            const nodesData = await nodesResponse.json();
            
            // Get current node info
            const nodeResponse = await fetch(`${this.baseUrl}/info`);
            const nodeInfo = await nodeResponse.json();
            
            container.innerHTML = `
                <div class="mb-3">
                    <h6><i class="fas fa-server"></i> Current Node</h6>
                    <div class="node-item">
                        <div class="d-flex align-items-center">
                            <span class="node-status online"></span>
                            <div class="flex-grow-1">
                                <div class="fw-bold">${nodeInfo.node_id.substring(0, 12)}...</div>
                                <small class="text-muted">${nodeInfo.model}</small>
                                <div class="badge bg-primary">OpenAI Compatible</div>
                            </div>
                        </div>
                    </div>
                </div>
                
                <div class="mb-3">
                    <h6><i class="fas fa-network-wired"></i> DHT Network</h6>
                    <div class="small">
                        <div>Published Nodes: ${nodesData.total_published}</div>
                        <div>DHT Contacts: ${dhtStatus.contacts_count}</div>
                        <div>DHT Port: ${dhtStatus.dht_port}</div>
                    </div>
                </div>
                
                <div data-section="nodes">
                    <div class="d-flex justify-content-between align-items-center mb-2">
                        <h6><i class="fas fa-users"></i> Published Nodes</h6>
                        <button class="btn btn-sm btn-outline-primary" onclick="llamaNetUI.refreshNodesOnly()">
                            <i class="fas fa-sync-alt"></i>
                        </button>
                    </div>
                    ${this.renderPublishedNodes(nodesData.published_nodes)}
                </div>
            `;
            
        } catch (error) {
            console.error('Error getting nodes with models:', error);
            this.showNetworkError('Failed to get node information');
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
    
    renderPublishedNodes(nodes) {
        if (!nodes || nodes.length === 0) {
            return '<div class="text-muted small">No published nodes discovered</div>';
        }
        
        // Group nodes by model
        const nodesByModel = {};
        nodes.forEach(node => {
            const model = node.model || 'Unknown';
            if (!nodesByModel[model]) {
                nodesByModel[model] = [];
            }
            nodesByModel[model].push(node);
        });
        
        let html = '';
        Object.keys(nodesByModel).forEach(model => {
            const modelNodes = nodesByModel[model];
            html += `
                <div class="model-group mb-2">
                    <div class="fw-bold small text-primary">
                        <i class="fas fa-brain"></i> ${model} (${modelNodes.length})
                        <span class="badge bg-success ms-1">OpenAI</span>
                    </div>
                    ${modelNodes.map(node => {
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
                                        <div class="text-muted">Load: ${node.load.toFixed(2)} | TPS: ${node.tps.toFixed(1)}</div>
                                        <div class="text-muted small">${lastSeenText}</div>
                                    </div>
                                </div>
                            </div>
                        `;
                    }).join('')}
                </div>
            `;
        });
        
        return html;
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
    
    async refreshNodesOnly() {
        try {
            const container = document.getElementById('network-status');
            
            // Show loading state for nodes section only
            const nodesSection = container.querySelector('[data-section="nodes"]');
            if (nodesSection) {
                const header = nodesSection.querySelector('.d-flex');
                const content = nodesSection.querySelector('.d-flex').nextElementSibling;
                if (content) {
                    content.innerHTML = '<div class="text-center"><div class="spinner-border spinner-border-sm"></div> Refreshing nodes...</div>';
                }
            }
            
            // Get fresh node data
            const nodesResponse = await fetch(`${this.baseUrl}/nodes`);
            const nodesData = await nodesResponse.json();
            
            // Update just the nodes section content
            if (nodesSection) {
                const header = nodesSection.querySelector('.d-flex');
                const newContent = this.renderPublishedNodes(nodesData.published_nodes);
                nodesSection.innerHTML = header.outerHTML + newContent;
            }
            
            // Show success feedback
            this.showToast('success', `Found ${nodesData.total_published} published nodes`);
            
        } catch (error) {
            console.error('Error refreshing nodes:', error);
            this.showToast('error', 'Failed to refresh nodes');
            
            // Restore the refresh button on error
            const nodesSection = document.querySelector('[data-section="nodes"]');
            if (nodesSection) {
                const header = `
                    <div class="d-flex justify-content-between align-items-center mb-2">
                        <h6><i class="fas fa-users"></i> Published Nodes</h6>
                        <button class="btn btn-sm btn-outline-primary" onclick="llamaNetUI.refreshNodesOnly()">
                            <i class="fas fa-sync-alt"></i>
                        </button>
                    </div>
                `;
                nodesSection.innerHTML = header + '<div class="text-muted small">Error loading nodes</div>';
            }
        }
    }
    
    async showNodeInfo(nodeId) {
        // Show detailed information about a specific node
        const modal = new bootstrap.Modal(document.getElementById('nodeInfoModal'));
        modal.show();
        
        // Show loading state
        document.getElementById('node-info-details').innerHTML = `
            <div class="text-center">
                <div class="spinner-border text-primary" role="status"></div>
                <p class="mt-2">Loading node information...</p>
            </div>
        `;
        
        try {
            const response = await fetch(`${this.baseUrl}/node/${nodeId}`);
            
            if (response.ok) {
                const nodeInfo = await response.json();
                document.getElementById('node-info-details').innerHTML = this.renderNodeDetails(nodeInfo);
            } else {
                throw new Error(`HTTP ${response.status}: ${response.statusText}`);
            }
        } catch (error) {
            console.error('Error loading node info:', error);
            document.getElementById('node-info-details').innerHTML = `
                <div class="alert alert-danger">
                    <i class="fas fa-exclamation-triangle"></i>
                    Failed to load node information: ${error.message}
                </div>
            `;
        }
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
                        <strong>OpenAI Compatible:</strong>
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
                        <strong>API:</strong> <span class="badge bg-success">OpenAI Compatible</span>
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
        
        const requestBody = {
            model: 'llamanet',
            messages: messages,
            max_tokens: maxTokens,
            temperature: temperature,
            stream: streamingEnabled,
            stop: ["Human:", "User:", "\nHuman:", "\nUser:", "\n\nHuman:", "\n\nUser:"], // Comprehensive stop tokens
            strategy: strategy // Add strategy for load balancing
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
                    node_info: data.node_info
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
            `Tokens: ~${streamState.totalTokens}`,
            `API: OpenAI Compatible (Streaming)`
        ];

        console.log(streamState);
        
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
                api: 'OpenAI Compatible (Streaming)',
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
                        <strong>API:</strong> <span class="badge bg-success">OpenAI Compatible</span>
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
                        <strong>OpenAI Compatible:</strong>
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

// Initialize when DOM is loaded
document.addEventListener('DOMContentLoaded', () => {
    llamaNetUI = new LlamaNetUI();
});
