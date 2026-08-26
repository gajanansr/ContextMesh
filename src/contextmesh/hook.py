import sys
import json
import base64

def main():
    """
    ContextMesh Hook Engine.
    Intercepts Claude Code's PreToolUse, checks if it's a Bash command,
    and silently rewrites the command to route through the ContextMesh Executor
    (`contextmesh run`). This allows us to compress the output locally and inject
    the AST RepoMap without any network proxies.
    """
    try:
        raw_data = sys.stdin.read()
        if not raw_data:
            sys.exit(0)
            
        data = json.loads(raw_data)
        
        # We intercept Bash (which Claude uses for almost all terminal tasks)
        if data.get("tool_name") == "Bash":
            command = data.get("tool_input", {}).get("command", "")
            session_id = data.get("session_id", "unknown")
            
            # Base64 encode the command to avoid any quote-escaping hell in the shell
            cmd_b64 = base64.b64encode(command.encode("utf-8")).decode("utf-8")
            new_command = f"contextmesh run {cmd_b64} --session {session_id}"
            
            # This specific JSON format is required by Claude Code to mutate tool input
            payload = {
                "hookSpecificOutput": {
                    "hookEventName": "PreToolUse",
                    "permissionDecision": "allow",
                    "updatedInput": {
                        "command": new_command
                    }
                }
            }
            
            print(json.dumps(payload))
            sys.exit(0)
            
    except Exception:
        pass
    
    # If not Bash, or if an error occurred, just exit 0 to allow normal execution
    sys.exit(0)

if __name__ == "__main__":
    main()
