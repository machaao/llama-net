from setuptools import setup, find_packages

setup(
    name="llamanet",
    version="0.1.0",
    packages=find_packages(),
    install_requires=[
        "fastapi>=0.68.0,<1.0.0",
        "uvicorn>=0.15.0,<1.0.0",
        "pydantic>=2.0.0,<3.0.0",
        "requests>=2.26.0,<3.0.0",
        "llama-cpp-python>=0.2.20",
        "psutil>=5.8.0,<6.0.0",
        "pynvml>=11.4.1",
        "kademlia>=2.2.2,<3.0.0",
        "aiohttp>=3.8.0,<4.0.0",
        "p2pd",
        "ipaddress>=1.0.23"
    ],
    author="LlamaNet Team",
    author_email="example@example.com",
    description="Decentralized Inference Swarm for llama.cpp",
    keywords="llm, inference, decentralized",
    url="https://github.com/yourusername/llamanet",
    license="Apache-2.0",
    classifiers=[
        "Development Status :: 3 - Alpha",
        "Intended Audience :: Developers",
        "Programming Language :: Python :: 3",
    ],
    python_requires=">=3.8",
)
