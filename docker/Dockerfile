FROM node:22-bookworm-slim AS node-runtime
FROM python:3.12-slim-bookworm

COPY --from=node-runtime /usr/local/bin/node /usr/local/bin/node
COPY --from=node-runtime /usr/local/lib/node_modules /usr/local/lib/node_modules
RUN ln -s ../lib/node_modules/npm/bin/npm-cli.js /usr/local/bin/npm \
    && ln -s ../lib/node_modules/npm/bin/npx-cli.js /usr/local/bin/npx

ENV DEBIAN_FRONTEND=noninteractive
ENV CARGO_HOME=/opt/cargo
ENV RUSTUP_HOME=/opt/rustup
ENV PATH=/opt/cargo/bin:${PATH}

RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    gcc \
    clang \
    cmake \
    ninja-build \
    pkg-config \
    libudev-dev \
    libssl-dev \
    ca-certificates \
    curl \
    git \
    make \
    && rm -rf /var/lib/apt/lists/*

COPY bench/requirements.txt /tmp/bench-requirements.txt
RUN python -m pip install --no-cache-dir -r /tmp/bench-requirements.txt 'pytest>=8,<10' \
    && python --version && node --version

# ---------------------------------------------------------------------------
# Rust
# ---------------------------------------------------------------------------

RUN curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs \
    | sh -s -- \
        -y \
        --profile minimal \
        --no-modify-path

# ---------------------------------------------------------------------------
# Espressif Rust toolchain
#
# ESP32-S3 = Xtensa LX7
# no --std: we explicitly want the no_std toolchain + GCC support.
# ---------------------------------------------------------------------------

RUN cargo install espup --locked

RUN mkdir -p /opt/esp \
    && espup install \
        --export-file /opt/esp/export-esp.sh

# Make espup environment available in interactive shells.
RUN echo '. /opt/esp/export-esp.sh' >> /root/.bashrc \
    && echo '. /opt/esp/export-esp.sh' >> /etc/bash.bashrc

# ---------------------------------------------------------------------------
# ESP development tools
# ---------------------------------------------------------------------------

RUN cargo install espflash --locked \
    && cargo install esp-generate --locked

# ---------------------------------------------------------------------------
# Web tooling
#
# The browser will access the ESP32 through Web Serial.
# The container only builds and serves firmware/web assets.
# ---------------------------------------------------------------------------

RUN npm install -g http-server

# Installer Claude Code via npm
RUN npm install -g @anthropic-ai/claude-code

WORKDIR /workspace

EXPOSE 8080

CMD ["bash"]
