from setuptools import setup, find_packages

setup(
    name="llamanet",
    version="0.1.0",
    packages=find_packages(),
    install_requires=[
        "fastapi>=0.68.0",
        "uvicorn>=0.15.0",
        "pydantic>=1.8.2",
        "requests>=2.26.0",
        "llama-cpp-python>=0.1.0",
        "psutil>=5.8.0",
        "gputil>=1.4.0",
        "kademlia>=2.2.2"
    ],
    author="LlamaNet Team",
    author_email="example@example.com",
    description="Decentralized Inference Swarm for llama.cpp",
    keywords="llm, inference, decentralized",
    url="https://github.com/yourusername/llamanet",
    license="MIT",
    classifiers=[
        "Development Status :: 3 - Alpha",
        "Intended Audience :: Developers",
        "Programming Language :: Python :: 3",
    ],
    python_requires=">=3.8",
)
