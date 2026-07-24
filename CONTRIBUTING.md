# Contributing to LlamaNet

Thank you for your interest in contributing to LlamaNet! This document provides guidelines and instructions for contributing.

## Code of Conduct

Please be respectful to all participants. We are committed to providing a welcoming and inclusive experience for everyone.

## Getting Started

### Prerequisites

- Python 3.9+
- Git
- A GPU (NVIDIA or Apple Silicon) for inference testing, or CPU-only mode

### Local Development Setup

1. **Clone the repository:**
   ```bash
   git clone https://github.com/machaao/llama-net.git
   cd llama-net
   ```

2. **Create a virtual environment:**
   ```bash
   python3 -m venv venv
   source venv/bin/activate  # On Windows: venv\Scripts\activate
   ```

3. **Install dependencies:**
   ```bash
   pip install -e .
   pip install -r requirements-inference.txt
   ```

4. **Run the development server:**
   ```bash
   sh start-app.sh --tunnel --bootstrap-peers https://llamanet.app
   ```

## How to Contribute

### Reporting Bugs

- Check [existing issues](https://github.com/machaao/llama-net/issues) to avoid duplicates
- Use the **Bug Report** issue template
- Include steps to reproduce, expected behavior, and actual behavior
- Include your OS, Python version, and GPU hardware

### Suggesting Features

- Use the **Feature Request** issue template
- Explain the use case and why it would benefit the community
- If possible, sketch out the API or UI you envision

### Submitting Pull Requests

1. **Fork the repository** and create a branch from `main`:
   ```bash
   git checkout -b feature/your-feature-name
   ```

2. **Make your changes:**
   - Follow existing code style and conventions
   - Add docstrings for new functions and classes
   - Keep commits focused and well-described

3. **Test your changes:**
   - Run the inference node locally and verify the Web UI works
   - Test with at least one model (e.g., a small GGUF model)
   - Ensure existing endpoints still work (`/v1/chat/completions`, `/v1/models`, `/health`)

4. **Submit your PR:**
   - Reference any related issues (e.g., `Fixes #42`)
   - Describe what your changes do and why
   - Include screenshots for UI changes

## Code Style

- **Python:** Follow PEP 8. Use type hints where appropriate.
- **JavaScript:** Use ES6+ features. Avoid inline JavaScript in HTML files.
- **HTML/CSS:** No inline styles or scripts. Use external stylesheets and scripts.
- **Logging:** Use `get_logger(__name__)` from `common/utils.py` — never bare `print()`.

## Project Structure

```
llama-net/
├── common/             # Shared utilities, models, SSE helpers
├── inference_node/     # Inference node (runs on GPU owner's machine)
├── landing/            # Gateway / landing page server
├── static/             # Web UI (HTML, CSS, JS)
├── start-app.sh        # Main entrypoint (POSIX shell)
└── requirements*.txt   # Dependencies
```

## Architecture Notes

- **Gateway-centric:** All peer communication goes through `llamanet.app`. No P2P or DHT.
- **Tunnel-only:** Every node needs a public HTTPS URL (Cloudflare tunnel).
- **SSE for real-time:** Network status updates use Server-Sent Events.
- **OpenAI-compatible API:** `/v1/chat/completions` and `/v1/completions` must stay compatible.

## Questions?

Open a [Discussion](https://github.com/machaao/llama-net/discussions) on GitHub if you have questions about contributing, architecture, or how something works.

## License

By contributing, you agree that your contributions will be licensed under the [Apache 2.0 License](LICENSE).
