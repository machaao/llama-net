class LlamaNetUI {
    constructor() {
        this.baseUrl = window.location.origin;
        this.nodes = [];
        this.selectedNode = null;
        this.chatHistory = [];
        this.streamUI = new StreamUI(this.baseUrl);
        
        this.init();
    }
    
    init() {
        this.refreshNetworkStatus();
        this.setupEventListeners();
        
        // Refresh network status every 30 seconds
        setInterval(() => this.refreshNetworkStatus(), 30000);
    }
    
    setupEventListeners() {
        // API mode change
        document.querySelectorAll('input[name="apiMode"]').forEach(radio => {
            radio.addEventListener('change', () => {
                this.updateUIForAPIMode();
            });
        });
    }
    
    updateUIForAPIMode() {
        const mode = document.querySelector('input[name="apiMode"]:checked').value;
        // Could add mode-specific UI changes here
        console.log(`Switched to ${mode} mode`);
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
        
        // Get current node info
        try {
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
                            </div>
                        </div>
                    </div>
                </div>
                
                <div class="mb-3">
                    <h6><i class="fas fa-network-wired"></i> DHT Network</h6>
                    <div class="small">
                        <div>Contacts: ${dhtStatus.contacts_count}</div>
                        <div>Storage Keys: ${dhtStatus.storage_keys.length}</div>
                        <div>DHT Port: ${dhtStatus.dht_port}</div>
                    </div>
                </div>
                
                <div>
                    <h6><i class="fas fa-users"></i> Connected Nodes</h6>
                    ${this.renderNodeList(dhtStatus.contacts)}
                </div>
            `;
        } catch (error) {
            console.error('Error getting node info:', error);
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
        
        // Get API mode - this determines which endpoint to use
        const apiMode = document.querySelector('input[name="apiMode"]:checked').value;
        
        try {
            let response;
            // Only call the endpoint that matches the selected API mode
            if (apiMode === 'openai') {
                response = await this.sendOpenAIMessage(message);
            } else {
                response = await this.sendLlamaNetMessage(message);
            }
            
            if (response) {
                this.addMessageToChat('assistant', response.text, response.metadata);
            } else {
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
    
    async sendLlamaNetMessage(message) {
        const maxTokens = parseInt(document.getElementById('max-tokens').value) || 150;
        const temperature = parseFloat(document.getElementById('temperature').value) || 0.7;
        const streamingEnabled = document.getElementById('enable-streaming')?.checked || false;
        
        const request = {
            prompt: message,
            max_tokens: maxTokens,
            temperature: temperature
        };

        if (streamingEnabled) {
            // Use LlamaNet streaming endpoint
            return await this.sendStreamingMessage(request);
        } else {
            // Use LlamaNet non-streaming endpoint
            const response = await fetch(`${this.baseUrl}/generate`, {
                method: 'POST',
                headers: {
                    'Content-Type': 'application/json'
                },
                body: JSON.stringify(request)
            });
            
            if (!response.ok) {
                throw new Error(`HTTP ${response.status}: ${response.statusText}`);
            }
            
            const data = await response.json();
            return {
                text: data.text,
                metadata: {
                    node_id: data.node_id,
                    tokens: data.tokens_generated,
                    time: data.generation_time,
                    api: 'LlamaNet'
                }
            };
        }
    }

    async sendStreamingMessage(request) {
        return new Promise((resolve, reject) => {
            let currentMessageDiv = null;
            let currentBubbleDiv = null;
            let accumulatedText = '';
            
            // Create initial message bubble
            const chatContainer = document.getElementById('chat-messages');
            currentMessageDiv = document.createElement('div');
            currentMessageDiv.className = 'message assistant';
            
            currentBubbleDiv = document.createElement('div');
            currentBubbleDiv.className = 'message-bubble';
            currentBubbleDiv.innerHTML = '<i class="fas fa-robot me-2"></i><span class="streaming-text"></span><span class="streaming-cursor">▋</span>';
            
            currentMessageDiv.appendChild(currentBubbleDiv);
            chatContainer.appendChild(currentMessageDiv);
            chatContainer.scrollTop = chatContainer.scrollHeight;
            
            // Remove welcome message if it exists
            const welcomeMsg = chatContainer.querySelector('.text-center.text-muted');
            if (welcomeMsg) {
                welcomeMsg.remove();
            }
            
            this.streamUI.startStream(
                request,
                // onToken callback
                (data) => {
                    accumulatedText = data.accumulatedText;
                    const textSpan = currentBubbleDiv.querySelector('.streaming-text');
                    if (textSpan) {
                        textSpan.textContent = accumulatedText;
                    }
                    chatContainer.scrollTop = chatContainer.scrollHeight;
                },
                // onComplete callback
                (data) => {
                    // Remove streaming cursor
                    const cursor = currentBubbleDiv.querySelector('.streaming-cursor');
                    if (cursor) {
                        cursor.remove();
                    }
                    
                    // Add metadata
                    const metadataHtml = `<div class="message-meta">Node: ${data.nodeId.substring(0, 8)}... • Tokens: ${data.tokensGenerated} • Time: ${data.generationTime.toFixed(2)}s • API: LlamaNet (Streaming)</div>`;
                    currentMessageDiv.insertAdjacentHTML('beforeend', metadataHtml);
                    
                    // Store in chat history
                    this.chatHistory.push({ 
                        role: 'assistant', 
                        content: accumulatedText, 
                        timestamp: Date.now() 
                    });
                    
                    resolve({
                        text: accumulatedText,
                        metadata: {
                            node_id: data.nodeId,
                            tokens: data.tokensGenerated,
                            time: data.generationTime,
                            api: 'LlamaNet (Streaming)'
                        }
                    });
                },
                // onError callback
                (error) => {
                    // Remove streaming cursor and show error
                    const cursor = currentBubbleDiv.querySelector('.streaming-cursor');
                    if (cursor) {
                        cursor.remove();
                    }
                    
                    const textSpan = currentBubbleDiv.querySelector('.streaming-text');
                    if (textSpan) {
                        textSpan.textContent = accumulatedText + ' [Error: ' + error.message + ']';
                        textSpan.style.color = 'red';
                    }
                    
                    reject(error);
                }
            );
        });
    }
    
    async sendOpenAIMessage(message) {
        const maxTokens = parseInt(document.getElementById('max-tokens').value) || 150;
        const temperature = parseFloat(document.getElementById('temperature').value) || 0.7;
        const streamingEnabled = document.getElementById('enable-streaming')?.checked || false;
        
        // Build chat history for context
        const messages = [
            { role: 'system', content: 'You are a helpful assistant.' }
        ];
        
        // Add recent chat history (last 5 exchanges)
        const recentHistory = this.chatHistory.slice(-10);
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
            stream: streamingEnabled
        };

        if (streamingEnabled) {
            // Use OpenAI streaming endpoint
            return await this.sendOpenAIStreamingMessage(requestBody);
        } else {
            // Use OpenAI non-streaming endpoint
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
                text: data.choices[0].message.content,
                metadata: {
                    id: data.id,
                    tokens: data.usage.total_tokens,
                    api: 'OpenAI Compatible'
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
        streamState.bubbleDiv.innerHTML = '<i class="fas fa-robot me-2"></i><span class="streaming-text"></span><span class="streaming-cursor">▋</span>';
        
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
            const textSpan = streamState.bubbleDiv.querySelector('.streaming-text');
            if (textSpan) {
                textSpan.textContent = streamState.accumulatedText;
            }
            document.getElementById('chat-messages').scrollTop = document.getElementById('chat-messages').scrollHeight;
        }
        
        if (data.id) {
            streamState.responseId = data.id;
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
        
        // Add metadata
        const metadataHtml = `<div class="message-meta">ID: ${streamState.responseId.substring(0, 8)}... • Tokens: ~${streamState.totalTokens} • API: OpenAI Compatible (Streaming)</div>`;
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
                api: 'OpenAI Compatible (Streaming)'
            }
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
                    finished: choice.finish_reason !== null
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
            if (metadata.node_id) parts.push(`Node: ${metadata.node_id.substring(0, 8)}...`);
            if (metadata.tokens) parts.push(`Tokens: ${metadata.tokens}`);
            if (metadata.time) parts.push(`Time: ${metadata.time.toFixed(2)}s`);
            if (metadata.api) parts.push(`API: ${metadata.api}`);
            if (metadata.id) parts.push(`ID: ${metadata.id.substring(0, 8)}...`);
            
            if (parts.length > 0) {
                metadataHtml = `<div class="message-meta">${parts.join(' • ')}</div>`;
            }
        }
        
        const roleIcon = role === 'user' ? 'fas fa-user' : 
                        role === 'assistant' ? 'fas fa-robot' : 'fas fa-info-circle';
        
        messageDiv.innerHTML = `
            <div class="message-bubble">
                <i class="${roleIcon} me-2"></i>${this.escapeHtml(content)}
            </div>
            ${metadataHtml}
        `;
        
        chatContainer.appendChild(messageDiv);
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
        return `
            <div class="row">
                <div class="col-md-6">
                    <h6><i class="fas fa-server"></i> Node Information</h6>
                    <div class="network-detail-item">
                        <strong>Node ID:</strong> ${info.node_id}<br>
                        <strong>Model:</strong> ${info.model}<br>
                        <strong>Model Path:</strong> ${info.model_path}<br>
                        <strong>DHT Port:</strong> ${info.dht_port}
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
                <h6><i class="fas fa-list"></i> Available Endpoints</h6>
                <div class="row">
                    <div class="col-md-6">
                        <strong>LlamaNet:</strong>
                        <ul class="list-unstyled small">
                            ${info.endpoints.llamanet.map(ep => `<li><span class="api-endpoint">${ep}</span></li>`).join('')}
                        </ul>
                    </div>
                    <div class="col-md-6">
                        <strong>OpenAI Compatible:</strong>
                        <ul class="list-unstyled small">
                            ${info.endpoints.openai.map(ep => `<li><span class="api-endpoint">${ep}</span></li>`).join('')}
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
