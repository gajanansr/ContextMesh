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

def main():
    proxy_process = None
    
    # Auto-start proxy if it's not running
    if not is_proxy_running():
        print("Starting ContextMesh token proxy on port 8099...")
        # Start proxy in background
        proxy_process = subprocess.Popen(
            ["contextmesh", "proxy"], 
            stdout=subprocess.DEVNULL, 
            stderr=subprocess.DEVNULL
        )
        # Give it a second to boot up
        time.sleep(1.5)
        
        if not is_proxy_running():
            print("Error: Failed to start the token proxy. Check `contextmesh proxy` logs.")
            if proxy_process:
                proxy_process.terminate()
            sys.exit(1)

    # Force Claude to route traffic through the ContextMesh token proxy
    os.environ["ANTHROPIC_BASE_URL"] = "http://127.0.0.1:8099"
    
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
        # Cleanup the proxy if we were the ones who started it
        if proxy_process:
            proxy_process.terminate()

if __name__ == "__main__":
    main()
