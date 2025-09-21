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
