class StreamUI {
    constructor(baseUrl = window.location.origin) {
        this.baseUrl = baseUrl;
        this.eventSource = null;
        this.isStreaming = false;
        this.currentStreamId = null;
        this.onTokenCallback = null;
        this.onCompleteCallback = null;
        this.onErrorCallback = null;
        this.accumulatedText = '';
    }

    /**
     * Start streaming generation
     * @param {Object} request - Generation request parameters
     * @param {Function} onToken - Callback for each token received
     * @param {Function} onComplete - Callback when streaming completes
     * @param {Function} onError - Callback for errors
     */
    async startStream(request, onToken, onComplete, onError) {
        if (this.isStreaming) {
            this.stopStream();
        }

        this.onTokenCallback = onToken;
        this.onCompleteCallback = onComplete;
        this.onErrorCallback = onError;
        this.accumulatedText = '';
        this.isStreaming = true;
        this.currentStreamId = Date.now().toString();

        try {
            // Use fetch to initiate the streaming request
            const response = await fetch(`${this.baseUrl}/generate/stream`, {
                method: 'POST',
                headers: {
                    'Content-Type': 'application/json',
                },
                body: JSON.stringify(request)
            });

            if (!response.ok) {
                throw new Error(`HTTP ${response.status}: ${response.statusText}`);
            }

            // Read the stream
            const reader = response.body.getReader();
            const decoder = new TextDecoder();

            while (this.isStreaming) {
                const { done, value } = await reader.read();
                
                if (done) break;

                const chunk = decoder.decode(value, { stream: true });
                this._processChunk(chunk);
            }

        } catch (error) {
            console.error('Streaming error:', error);
            this.isStreaming = false;
            if (this.onErrorCallback) {
                this.onErrorCallback(error);
            }
        }
    }

    /**
     * Process incoming stream chunk
     * @param {string} chunk - Raw chunk data
     */
    _processChunk(chunk) {
        const lines = chunk.split('\n');
        
        for (const line of lines) {
            if (line.startsWith('data: ')) {
                try {
                    const data = JSON.parse(line.slice(6));
                    this._handleStreamData(data);
                } catch (error) {
                    console.warn('Failed to parse stream data:', line);
                }
            }
        }
    }

    /**
     * Handle parsed stream data
     * @param {Object} data - Parsed stream data
     */
    _handleStreamData(data) {
        if (data.error) {
            this.isStreaming = false;
            if (this.onErrorCallback) {
                this.onErrorCallback(new Error(data.error));
            }
            return;
        }

        // Update accumulated text
        if (data.accumulated_text) {
            this.accumulatedText = data.accumulated_text;
        }

        // Call token callback
        if (this.onTokenCallback && data.text) {
            this.onTokenCallback({
                text: data.text,
                accumulatedText: this.accumulatedText,
                tokensGenerated: data.tokens_generated,
                generationTime: data.generation_time,
                nodeId: data.node_id
            });
        }

        // Check if finished
        if (data.finished) {
            this.isStreaming = false;
            if (this.onCompleteCallback) {
                this.onCompleteCallback({
                    text: this.accumulatedText,
                    tokensGenerated: data.tokens_generated,
                    generationTime: data.generation_time,
                    nodeId: data.node_id
                });
            }
        }
    }

    /**
     * Stop the current stream
     */
    stopStream() {
        this.isStreaming = false;
        if (this.eventSource) {
            this.eventSource.close();
            this.eventSource = null;
        }
    }

    /**
     * Check if currently streaming
     * @returns {boolean}
     */
    isCurrentlyStreaming() {
        return this.isStreaming;
    }

    /**
     * Get accumulated text so far
     * @returns {string}
     */
    getAccumulatedText() {
        return this.accumulatedText;
    }
}

// Export for use in other scripts
window.StreamUI = StreamUI;
