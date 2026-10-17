FROM python:3.13-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
        git \
        curl \
        ca-certificates \
        bubblewrap \
        socat \
        neovim \
        jq \
        openssh-client \
    && rm -rf /var/lib/apt/lists/*

# gh is required by this repo's Git conventions (branch creation, PR merge — see
# CLAUDE.md), which are meant to be run from inside this container. Installed from
# GitHub's official apt repository, since Debian ships no `gh` package.
RUN curl -fsSL https://cli.github.com/packages/githubcli-archive-keyring.gpg \
        -o /usr/share/keyrings/githubcli-archive-keyring.gpg \
    && chmod go+r /usr/share/keyrings/githubcli-archive-keyring.gpg \
    && echo "deb [arch=$(dpkg --print-architecture) signed-by=/usr/share/keyrings/githubcli-archive-keyring.gpg] https://cli.github.com/packages stable main" \
        > /etc/apt/sources.list.d/github-cli.list \
    && apt-get update \
    && apt-get install -y --no-install-recommends gh \
    && rm -rf /var/lib/apt/lists/*

RUN pip install --no-cache-dir lefthook

RUN useradd --create-home --shell /bin/bash vscode \
    && mkdir -p /home/vscode/.claude \
    && chown vscode:vscode /home/vscode/.claude
USER vscode

RUN curl -fsSL https://ntn.dev | bash \
    && mkdir -p /home/vscode/.local/bin \
    && NTN_BIN="$(bash -lc 'command -v ntn')" \
    && [ "$NTN_BIN" = "/home/vscode/.local/bin/ntn" ] || ln -sf "$NTN_BIN" /home/vscode/.local/bin/ntn

WORKDIR /workspace
