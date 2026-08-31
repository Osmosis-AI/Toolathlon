#!/bin/bash

# Usage instructions
usage() {
    echo "Usage: $0 [--sudo|--no-sudo]"
    echo "  --sudo      : Install with sudo (default)"
    echo "  --no-sudo   : Install to user local bin directory (~/bin) without sudo"
    exit 1
}

# Default: use sudo
USE_SUDO=1

# Parse arguments
if [ "$1" == "--no-sudo" ]; then
    USE_SUDO=0
elif [ "$1" == "--sudo" ] || [ -z "$1" ]; then
    USE_SUDO=1
elif [ -n "$1" ]; then
    usage
fi

if [ $USE_SUDO -eq 1 ]; then
    INSTALL_BIN_DIR="/usr/local/bin"
    INSTALL_KIND_PATH="/usr/local/bin/kind"
    INSTALL_KUBECTL_PATH="/usr/local/bin/kubectl"
    SUDO="sudo"
else
    mkdir -p ~/bin
    INSTALL_BIN_DIR="$HOME/bin"
    INSTALL_KIND_PATH="$HOME/bin/kind"
    INSTALL_KUBECTL_PATH="$HOME/bin/kubectl"
    SUDO=""
    # Ensure user's bin directory is in PATH
    if [[ ":$PATH:" != *":$HOME/bin:"* ]]; then
        echo "Please add \$HOME/bin to your PATH, for example add the following to your ~/.bashrc or ~/.zshrc:"
        echo 'export PATH="$HOME/bin:$PATH"'
    fi
fi

KIND_VERSION="v0.20.0"
KIND_SHA256="513a7213d6d3332dd9ef27c24dab35e5ef10a04fa27274fe1c14d8a246493ded"
KUBECTL_VERSION="v1.27.3"
KUBECTL_SHA256="fba6c062e754a120bc8105cde1344de200452fe014a8759e06e4eec7ed258a09"

kind_is_pinned() {
    local version_output
    version_output=$(timeout 10s kind version 2>&1) || return 1
    printf '%s\n' "$version_output" | grep -Eq \
        '^kind[[:space:]]+v0\.20\.0([[:space:]]|$)'
}

kubectl_is_pinned() {
    local version_output
    version_output=$(timeout 10s kubectl version --client --output=yaml 2>&1) || return 1
    printf '%s\n' "$version_output" | grep -Eq \
        '^[[:space:]]*gitVersion:[[:space:]]*v1\.27\.3[[:space:]]*$'
}

# Install kind, replacing versions outside the Kubernetes pairing pin.
KIND_CURRENT_PATH=$(command -v kind 2>/dev/null || true)
KIND_NEEDS_INSTALL=1
if [ -n "$KIND_CURRENT_PATH" ]; then
    if kind_is_pinned; then
        echo "kind is already installed at: $KIND_CURRENT_PATH"
        KIND_NEEDS_INSTALL=0
    else
        echo "kind at $KIND_CURRENT_PATH is not ${KIND_VERSION}; replacing it."
    fi
fi

if [ "$KIND_NEEDS_INSTALL" -eq 1 ]; then
    echo "Installing kind..."
    KIND_TMP=$(mktemp "${TMPDIR:-/tmp}/kind.XXXXXX") || exit 1
    if ! curl -fL --retry 3 --retry-delay 1 --retry-max-time 300 \
        --connect-timeout 10 --max-time 120 -o "$KIND_TMP" \
        "https://kind.sigs.k8s.io/dl/${KIND_VERSION}/kind-linux-amd64"; then
        rm -f "$KIND_TMP"
        echo "Failed to download kind ${KIND_VERSION}." >&2
        exit 1
    fi
    if ! printf '%s  %s\n' "$KIND_SHA256" "$KIND_TMP" | sha256sum -c - >/dev/null; then
        rm -f "$KIND_TMP"
        echo "Checksum verification failed for kind ${KIND_VERSION}." >&2
        exit 1
    fi
    if ! $SUDO install -m 0755 "$KIND_TMP" "$INSTALL_KIND_PATH"; then
        rm -f "$KIND_TMP"
        echo "Failed to install kind to $INSTALL_KIND_PATH." >&2
        exit 1
    fi
    rm -f "$KIND_TMP"
    hash -r
    KIND_SELECTED_PATH=$(command -v kind 2>/dev/null || true)
    if [ -z "$KIND_SELECTED_PATH" ] || ! kind_is_pinned; then
        echo "kind ${KIND_VERSION} was installed to $INSTALL_KIND_PATH, but PATH selects ${KIND_SELECTED_PATH:-no kind command}." >&2
        echo "Add $INSTALL_BIN_DIR before older tool directories in PATH." >&2
        exit 1
    fi
    echo "kind has been installed to: $INSTALL_KIND_PATH (PATH: $KIND_SELECTED_PATH)"
fi

# Install kubectl, replacing versions outside the Kind v0.20.0 Kubernetes pin.
KUBECTL_CURRENT_PATH=$(command -v kubectl 2>/dev/null || true)
KUBECTL_NEEDS_INSTALL=1
if [ -n "$KUBECTL_CURRENT_PATH" ]; then
    if kubectl_is_pinned; then
        echo "kubectl is already installed at: $KUBECTL_CURRENT_PATH"
        KUBECTL_NEEDS_INSTALL=0
    else
        echo "kubectl at $KUBECTL_CURRENT_PATH is not ${KUBECTL_VERSION}; replacing it."
    fi
fi

if [ "$KUBECTL_NEEDS_INSTALL" -eq 1 ]; then
    echo "Installing kubectl..."
    KUBECTL_TMP=$(mktemp "${TMPDIR:-/tmp}/kubectl.XXXXXX") || exit 1
    if ! curl -fL --retry 3 --retry-delay 1 --retry-max-time 300 \
        --connect-timeout 10 --max-time 120 -o "$KUBECTL_TMP" \
        "https://dl.k8s.io/release/${KUBECTL_VERSION}/bin/linux/amd64/kubectl"; then
        rm -f "$KUBECTL_TMP"
        echo "Failed to download kubectl ${KUBECTL_VERSION}." >&2
        exit 1
    fi
    if ! printf '%s  %s\n' "$KUBECTL_SHA256" "$KUBECTL_TMP" | sha256sum -c - >/dev/null; then
        rm -f "$KUBECTL_TMP"
        echo "Checksum verification failed for kubectl ${KUBECTL_VERSION}." >&2
        exit 1
    fi
    if ! $SUDO install -m 0755 "$KUBECTL_TMP" "$INSTALL_KUBECTL_PATH"; then
        rm -f "$KUBECTL_TMP"
        echo "Failed to install kubectl to $INSTALL_KUBECTL_PATH." >&2
        exit 1
    fi
    rm -f "$KUBECTL_TMP"
    hash -r
    KUBECTL_SELECTED_PATH=$(command -v kubectl 2>/dev/null || true)
    if [ -z "$KUBECTL_SELECTED_PATH" ] || ! kubectl_is_pinned; then
        echo "kubectl ${KUBECTL_VERSION} was installed to $INSTALL_KUBECTL_PATH, but PATH selects ${KUBECTL_SELECTED_PATH:-no kubectl command}." >&2
        echo "Add $INSTALL_BIN_DIR before older tool directories in PATH." >&2
        exit 1
    fi
    echo "kubectl has been installed to: $INSTALL_KUBECTL_PATH (PATH: $KUBECTL_SELECTED_PATH)"
fi
