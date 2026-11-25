FROM node:22.23.4-bookworm-slim AS node-runtime
FROM python:3.12.16-slim-bookworm

ARG RUST_VERSION=1.99.1
ARG ESP_RUST_VERSION=1.97.0.0
ARG CLAUDE_CODE_VERSION=2.1.318
ARG IDLAB_CLIENT_VERSION=latest

COPY --from=node-runtime /usr/local/bin/node /usr/local/bin/node
COPY --from=node-runtime /usr/local/lib/node_modules /usr/local/lib/node_modules
RUN ln -s ../lib/node_modules/npm/bin/npm-cli.js /usr/local/bin/npm \
    && ln -s ../lib/node_modules/npm/bin/npx-cli.js /usr/local/bin/npx

ENV DEBIAN_FRONTEND=noninteractive
ENV CARGO_HOME=/opt/cargo
ENV RUSTUP_HOME=/opt/rustup
ENV CLAUDE_CONFIG_DIR=/root/.claude
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

# Normalize CRLF on input without forcing CRLF on checkout.
RUN git config --system core.autocrlf input \
    && git config --system core.whitespace cr-at-eol

RUN python -m pip install --no-cache-dir pytest==9.2.0 setuptools==80.9.0 wheel==0.45.1 \
    && python --version && node --version

# Install the released IDLab client only; the server runs independently.
COPY docker/install-idlab-client.py /opt/idlab/install-client.py
RUN python /opt/idlab/install-client.py "${IDLAB_CLIENT_VERSION}"

# ---------------------------------------------------------------------------
# Rust
# ---------------------------------------------------------------------------

RUN curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs \
    | sh -s -- \
        -y \
        --profile minimal \
        --default-toolchain "${RUST_VERSION}" \
        --no-modify-path

RUN rustup component add rustfmt clippy rust-src && rustup target add riscv32imc-unknown-none-elf riscv32imac-unknown-none-elf

# ---------------------------------------------------------------------------
# Espressif Rust toolchain
#
# ESP32-S3 = Xtensa LX7
# no --std: we explicitly want the no_std toolchain + GCC support.
# ---------------------------------------------------------------------------

COPY docker/esp-tools.json docker/install-esp-tools.py /opt/esp/
RUN python /opt/esp/install-esp-tools.py

RUN mkdir -p /opt/esp \
    && espup install \
        --stable-version "${RUST_VERSION}" \
        --toolchain-version "${ESP_RUST_VERSION}" \
        --export-file /opt/esp/export-esp.sh

# Make espup environment available in interactive shells.
RUN echo '. /opt/esp/export-esp.sh' >> /root/.bashrc \
    && echo '. /opt/esp/export-esp.sh' >> /etc/bash.bashrc

# ---------------------------------------------------------------------------
# ESP development tools
# ---------------------------------------------------------------------------

# espflash and esp-generate were installed from verified release binaries above.

# Installer Claude Code via npm
RUN npm install -g "@anthropic-ai/claude-code@${CLAUDE_CODE_VERSION}"

COPY docker/smoke-test.sh /usr/local/bin/rust-dev-smoke
RUN chmod +x /usr/local/bin/rust-dev-smoke

WORKDIR /workspace

CMD ["bash"]
