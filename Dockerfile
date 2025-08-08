FROM ubuntu:22.04

# Install system dependencies including Python 3.11 and Docker
RUN apt-get update && apt-get install -y \
    software-properties-common \
    ca-certificates \
    gnupg \
    lsb-release \
    && add-apt-repository ppa:deadsnakes/ppa \
    && curl -fsSL https://download.docker.com/linux/ubuntu/gpg | gpg --dearmor -o /usr/share/keyrings/docker-archive-keyring.gpg \
    && echo "deb [arch=$(dpkg --print-architecture) signed-by=/usr/share/keyrings/docker-archive-keyring.gpg] https://download.docker.com/linux/ubuntu $(lsb_release -cs) stable" | tee /etc/apt/sources.list.d/docker.list > /dev/null \
    && apt-get update && apt-get install -y \
    python3.11 \
    python3.11-dev \
    python3.11-venv \
    python3-pip \
    git \
    build-essential \
    curl \
    docker-ce-cli \
    && ln -sf /usr/bin/python3.11 /usr/bin/python \
    && ln -sf /usr/bin/python3.11 /usr/bin/python3 \
    && rm -rf /var/lib/apt/lists/*

# Set up working directory
WORKDIR /workspace

# Clone inspect_ai repository
ARG INSPECT_AI_REF=main
RUN git clone https://github.com/UKGovernmentBEIS/inspect_ai.git . \
    && git checkout ${INSPECT_AI_REF}

# Install Python dependencies
RUN python -m pip install --upgrade pip \
    && pip install --only-binary=all -e ".[dev]"

# Set environment variables for Docker builds
ENV DOCKER_BUILDKIT=1
ENV BUILDKIT_PROGRESS=plain