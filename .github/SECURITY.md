# Security Policy

## Supported Versions

| Version | Supported          |
| ------- | ------------------ |
| 0.1.x   | :white_check_mark: |

## Reporting a Vulnerability

If you discover a security vulnerability in LlamaNet, please report it responsibly.

**Do NOT open a public GitHub issue for security vulnerabilities.**

Instead, please email: **security@llamanet.app**

### What to include:

- Description of the vulnerability
- Steps to reproduce
- Potential impact
- Suggested fix (if you have one)

### Response timeline:

- **Acknowledgment:** Within 48 hours
- **Initial assessment:** Within 5 business days
- **Fix or mitigation:** Depends on severity, typically within 14 days

### Scope:

Security reports are welcome for:
- The LlamaNet gateway (`llamanet.app`)
- Inference node API endpoints
- Authentication and authorization flows
- Dependency vulnerabilities
- SSRF, injection, or data exposure risks

### Out of scope:

- Social engineering attacks
- Denial of service (rate limiting is in place)
- Issues in third-party services (Hugging Face, Cloudflare, Supabase)

## Security Best Practices for Node Operators

- Keep your LlamaNet installation updated
- Use named Cloudflare tunnels (not quick tunnels) for production
- Don't expose your node directly to the internet without a tunnel
- Use API keys for authentication — don't share raw session tokens
- Monitor your node's resource usage for unexpected spikes
