class StreamUI {
    constructor(baseUrl) {
        this.baseUrl = baseUrl;
    }
    
    startStream(request, onToken, onComplete, onError) {
        fetch(`${this.baseUrl}/generate/stream`, {
            method: 'POST',
            headers: {
                'Content-Type': 'application/json'
            },
            body: JSON.stringify(request)
        })
        .then(response => {
            if (!response.ok) {
                throw new Error(`HTTP ${response.status}: ${response.statusText}`);
            }
            
            const reader = response.body.getReader();
            const decoder = new TextDecoder();
            
            const processStream = () => {
                return reader.read().then(({ done, value }) => {
                    if (done) {
                        return;
                    }
                    
                    const chunk = decoder.decode(value);
                    const lines = chunk.split('\n');
                    
                    lines.forEach(line => {
                        if (line.startsWith('data: ')) {
                            try {
                                const data = JSON.parse(line.slice(6));
                                
                                if (data.error) {
                                    onError(new Error(data.error));
                                    return;
                                }
                                
                                onToken({
                                    text: data.text,
                                    accumulatedText: data.accumulated_text,
                                    tokensGenerated: data.tokens_generated,
                                    generationTime: data.generation_time,
                                    nodeId: data.node_id
                                });
                                
                                if (data.finished) {
                                    onComplete({
                                        nodeId: data.node_id,
                                        tokensGenerated: data.tokens_generated,
                                        generationTime: data.generation_time
                                    });
                                    return;
                                }
                            } catch (e) {
                                console.warn('Failed to parse stream data:', line);
                            }
                        }
                    });
                    
                    return processStream();
                });
            };
            
            return processStream();
        })
        .catch(error => {
            console.error('Stream error:', error);
            onError(error);
        });
    }
}
class StreamUI {
    constructor(baseUrl) {
        this.baseUrl = baseUrl;
    }
    
    startStream(request, onToken, onComplete, onError) {
        fetch(`${this.baseUrl}/generate/stream`, {
            method: 'POST',
            headers: {
                'Content-Type': 'application/json'
            },
            body: JSON.stringify(request)
        })
        .then(response => {
            if (!response.ok) {
                throw new Error(`HTTP ${response.status}: ${response.statusText}`);
            }
            
            const reader = response.body.getReader();
            const decoder = new TextDecoder();
            let buffer = '';
            let hasCompleted = false;
            
            const processChunk = async () => {
                try {
                    const { done, value } = await reader.read();
                    
                    if (done) {
                        if (!hasCompleted && onComplete) {
                            hasCompleted = true;
                            // Extract final data from the last accumulated text
                            const finalData = {
                                nodeId: request.node_id || 'unknown',
                                tokensGenerated: buffer.split(' ').length,
                                generationTime: 0,
                                accumulatedText: buffer
                            };
                            onComplete(finalData);
                        }
                        return;
                    }
                    
                    buffer += decoder.decode(value, { stream: true });
                    const lines = buffer.split('\n');
                    buffer = lines.pop() || '';
                    
                    for (const line of lines) {
                        if (line.startsWith('data: ')) {
                            try {
                                const data = JSON.parse(line.slice(6));
                                
                                if (data.error) {
                                    if (!hasCompleted && onError) {
                                        hasCompleted = true;
                                        onError(new Error(data.error));
                                    }
                                    return;
                                }
                                
                                if (onToken) {
                                    onToken(data);
                                }
                                
                                if (data.finished && !hasCompleted && onComplete) {
                                    hasCompleted = true;
                                    onComplete(data);
                                    return;
                                }
                            } catch (e) {
                                console.warn('Failed to parse streaming data:', line);
                            }
                        }
                    }
                    
                    // Continue reading if not completed
                    if (!hasCompleted) {
                        processChunk();
                    }
                } catch (error) {
                    if (!hasCompleted && onError) {
                        hasCompleted = true;
                        onError(error);
                    }
                }
            };
            
            processChunk();
        })
        .catch(error => {
            if (onError) {
                onError(error);
            }
        });
    }
}
