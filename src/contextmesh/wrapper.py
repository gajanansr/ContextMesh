"""Wrapper script to run Claude Code with the ContextMesh proxy enabled."""

import os
import subprocess
import sys
import socket
import time

def is_proxy_running(port=8099):
    """Check if the proxy is already running."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        return s.connect_ex(('127.0.0.1', port)) == 0

def detect_auth_mode() -> str:
    """
    Detect which authentication mode the user is using.
    Returns: 'api_key', 'subscription', 'bedrock', or 'vertex'
    """
    # Bedrock/Vertex are enterprise cloud deployments - check first
    if os.environ.get("CLAUDE_CODE_USE_BEDROCK") == "1" or os.environ.get("AWS_BEDROCK_RUNTIME_ENDPOINT"):
        return "bedrock"
    if os.environ.get("CLAUDE_CODE_USE_VERTEX") == "1" or os.environ.get("VERTEX_AI_PROJECT"):
        return "vertex"
    # API key mode
    if os.environ.get("ANTHROPIC_API_KEY"):
        return "api_key"
    # If ANTHROPIC_BASE_URL is already set by the user, respect it
    if os.environ.get("ANTHROPIC_BASE_URL"):
        return "api_key"  # Treat as API-like, proxy will forward correctly
    # Otherwise the user is running via claude.ai OAuth subscription (Max/Pro)
    return "subscription"

def start_proxy() -> subprocess.Popen | None:
    """Start the ContextMesh proxy and wait for it to be ready."""
    if is_proxy_running():
        return None  # Already running, nothing to do

    print("Starting ContextMesh token proxy on port 8099...")
    log_file = os.path.expanduser("~/.contextmesh/proxy.log")
    os.makedirs(os.path.dirname(log_file), exist_ok=True)

    with open(log_file, "a") as f:
        proxy_process = subprocess.Popen(
            ["contextmesh", "proxy"],
            stdout=f,
            stderr=f
        )

    # Poll until proxy is up, up to 5 seconds
    for _ in range(20):
        if is_proxy_running():
            return proxy_process
        time.sleep(0.25)

    print("Error: Failed to start the token proxy. Check `contextmesh proxy` logs.")
    proxy_process.terminate()
    sys.exit(1)

def main():
    proxy_process = None
    auth_mode = detect_auth_mode()

    if auth_mode == "api_key":
        # Route through the ContextMesh proxy for token interception + compression
        proxy_process = start_proxy()
        os.environ["ANTHROPIC_BASE_URL"] = "http://127.0.0.1:8099"
        print("✓ ContextMesh RTK Proxy active (API Key mode)")

    elif auth_mode == "bedrock":
        # Bedrock uses AWS SDK auth — proxy works differently, skip for now
        # The daemon and MCP server still work for context management
        print("✓ ContextMesh running in AWS Bedrock mode (proxy disabled)")

    elif auth_mode == "vertex":
        # Vertex uses GCP auth — same as Bedrock
        print("✓ ContextMesh running in Google Vertex mode (proxy disabled)")

    else:
        # Max/Pro subscription via OAuth — proxy still works!
        # Claude Code sends OAuth requests to api.anthropic.com regardless.
        # We can still intercept them for compression, we just can't track costs.
        proxy_process = start_proxy()
        os.environ["ANTHROPIC_BASE_URL"] = "http://127.0.0.1:8099"
        print("✓ ContextMesh RTK Proxy active (Subscription mode — compression only, no cost tracking)")

    try:
        subprocess.run(["claude"] + sys.argv[1:], check=True)
    except FileNotFoundError:
        print("Error: 'claude' command not found. Is Claude Code installed?")
        print("Try: npm install -g @anthropic-ai/claude-code")
    except KeyboardInterrupt:
        pass
    except subprocess.CalledProcessError as e:
        sys.exit(e.returncode)
    finally:
        if proxy_process:
            proxy_process.terminate()

if __name__ == "__main__":
    main()
