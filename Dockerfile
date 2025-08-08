FROM ubuntu:22.04

# Set environment for non-interactive installation
ENV DEBIAN_FRONTEND=noninteractive

# Install system dependencies including Python 3.11
RUN apt-get update && apt-get install -y \
    software-properties-common \
    ca-certificates \
    curl \
    gnupg \
    && add-apt-repository -y ppa:deadsnakes/ppa \
    && apt-get update && apt-get install -y \
    python3.11 \
    python3.11-dev \
    python3.11-venv \
    git \
    build-essential \
    && ln -sf /usr/bin/python3.11 /usr/bin/python \
    && ln -sf /usr/bin/python3.11 /usr/bin/python3 \
    && rm -rf /var/lib/apt/lists/*

# Install pip directly instead of using system package
RUN curl -sSL https://bootstrap.pypa.io/get-pip.py | python3.11

# Create and activate virtual environment
RUN python3.11 -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

# Install Docker CLI separately
RUN curl -fsSL https://download.docker.com/linux/ubuntu/gpg | gpg --dearmor -o /usr/share/keyrings/docker-archive-keyring.gpg \
    && echo "deb [arch=amd64 signed-by=/usr/share/keyrings/docker-archive-keyring.gpg] https://download.docker.com/linux/ubuntu jammy stable" | tee /etc/apt/sources.list.d/docker.list > /dev/null \
    && apt-get update && apt-get install -y docker-ce-cli \
    && rm -rf /var/lib/apt/lists/*

# Set up working directory
WORKDIR /workspace

# Clone inspect_ai repository
ARG INSPECT_AI_REF=main
RUN git clone https://github.com/UKGovernmentBEIS/inspect_ai.git . \
    && git checkout ${INSPECT_AI_REF}

# Install Python dependencies in virtual environment
RUN pip install --only-binary=all -e ".[dev]"

# Set environment variables for Docker builds
ENV DOCKER_BUILDKIT=1
ENV BUILDKIT_PROGRESS=plain