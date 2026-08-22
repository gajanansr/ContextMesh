"""Wrapper script to run Claude Code with the ContextMesh proxy enabled."""

import os
import subprocess
import sys

def main():
    # Force Claude to route traffic through the ContextMesh token proxy
    os.environ["ANTHROPIC_BASE_URL"] = "http://localhost:8099"
    
    # We pass all command line arguments down to claude
    try:
        # Check if claude is installed globally
        subprocess.run(["claude"] + sys.argv[1:], check=True)
    except FileNotFoundError:
        print("Error: 'claude' command not found. Is Claude Code installed?")
        print("Try: npm install -g @anthropic-ai/claude-code")
        sys.exit(1)
    except KeyboardInterrupt:
        pass
    except subprocess.CalledProcessError as e:
        sys.exit(e.returncode)

if __name__ == "__main__":
    main()
