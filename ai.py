#!/usr/bin/env python3
import os
import sys
import socket
import getpass
import re
import argparse
import json
from datetime import datetime, timezone
from typing import Any, Dict, Optional, List

import yaml
from openai import OpenAI, AzureOpenAI


APP_NAME = "AI Assistant CLI"
APP_VERSION = "1.1.0"
COPYRIGHT = "© 2025-2026 led-mirage"
CONFIG_FILE = "config.yaml"

# Chat History
HISTORY_DIR = "history"
HISTORY_FILE = os.path.join(HISTORY_DIR, "default.json")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog=APP_NAME,
        description=(
            f"{APP_NAME} - Simple AI assistant for your terminal.\n"
            "Reads prompts from CLI or config.yaml and calls the OpenAI API."
        ),
        epilog=COPYRIGHT,
    )
    parser.add_argument(
        "-v",
        "--version",
        action="version",
        version=f"%(prog)s {APP_VERSION} {COPYRIGHT}",
    )
    parser.add_argument(
        "-c",
        "--config",
        default=CONFIG_FILE,
        help="Path to config YAML file (default: config.yaml)",
    )
    parser.add_argument(
        "-s",
        "--system-prompt",
        help="Override system prompt from CLI (meta vars like <<date>> are expanded).",
    )
    parser.add_argument(
        "-p",
        "--user-prompt",
        help="Override user prompt from CLI (meta vars like <<date>> are expanded).",
    )
    parser.add_argument(
        "-m",
        "--model",
        help="Override model name (e.g., gpt-4.1-mini).",
    )
    parser.add_argument(
        "-1",
        "--oneshot",
        action="store_true",
        help="Do not use chat history. Send a single request and do not save history.",
    )
    parser.add_argument(
        "--clear-history",
        action="store_true",
        help="Clear chat history and exit.",
    )
    parser.add_argument(
        "--debug",
        action="store_true",
        help="Show detailed error messages."
    )
    parser.add_argument(
        "rest",
        nargs="*",
        help="prompt words when -p is omitted"
    )
    return parser.parse_args()


def load_config(path: str) -> Dict[str, Any]:
    if not os.path.exists(path):
        return {}
    with open(path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def choose_system_prompt(config: Dict[str, Any], cli_prompt: Optional[str]) -> str:
    if cli_prompt:
        base = cli_prompt
    else:
        base = config.get("system_prompt", "")
    base = base.strip()
    return expand_metavariables(base)


def choose_user_prompt(config: Dict[str, Any], cli_prompt: Optional[str], args_rest: List[str]) -> str:
    if cli_prompt:
        base = cli_prompt
    elif args_rest:
        base = " ".join(args_rest)
    else:
        base = config.get("user_prompt", "")
    base = base.strip()
    if not base:
        base = "Generate a short message."
    return expand_metavariables(base)


def expand_metavariables(text: str) -> str:
    """Expand meta variables like <<datetime>> in the given text."""
    now = datetime.now()
    now_utc = datetime.now(timezone.utc)

    replacements = {
        "datetime": now.strftime("%Y-%m-%d %H:%M:%S"),
        "date": now.strftime("%Y-%m-%d"),
        "time": now.strftime("%H:%M:%S"),
        "iso-datetime": now_utc.isoformat(),
        "weekday": now.strftime("%A"),
        "hostname": socket.gethostname(),
        "user": getpass.getuser(),
    }

    pattern = re.compile(r"<<([a-zA-Z0-9_-]+)>>")

    def replacer(match: re.Match) -> str:
        key = match.group(1)
        return replacements.get(key, match.group(0))

    return pattern.sub(replacer, text)


def create_client(config: Dict[str, Any]):
    api = config.get("api", "openai")
    if api == "openai":
        api_key_envvar = config.get("api_key_envvar", "OPENAI_API_KEY")
        api_key = os.environ.get(api_key_envvar)
        if not api_key:
            raise RuntimeError(f"{api_key_envvar} is not set")        
        return OpenAI(api_key=api_key)
    elif api == "azure":
        api_key_envvar = config.get("api_key_envvar", "AZURE_OPENAI_API_KEY")
        api_key = os.environ.get(api_key_envvar)
        if not api_key:
            raise RuntimeError(f"{api_key_envvar} is not set")        

        azure_endpoint_envvar = config.get("azure_endpoint_envvar", "AZURE_OPENAI_ENDPOINT")
        azure_endpoint = os.environ.get(azure_endpoint_envvar)
        if not azure_endpoint:
            raise RuntimeError(f"{azure_endpoint_envvar} is not set")

        return AzureOpenAI(
            api_key=api_key,
            azure_endpoint=azure_endpoint,
            api_version="2025-04-01-preview"
        )
    else:
        raise ValueError(f"Unsupported API provider: {api!r}. Expected 'openai' or 'azure'.")


def generate_oneshot_message(config: Dict[str, Any], model: str, system_prompt: str, user_prompt: str) -> str:
    client = create_client(config)
    response = client.chat.completions.create(
        model=model,
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
    )
    content = response.choices[0].message.content
    return content.strip() if content else ""


def generate_chat_message(
    config: Dict[str, Any],
    model: str,
    system_prompt: str,
    user_prompt: str,
    history_path: str,
    history_expire_seconds: int,
    max_turns: int,
    debug: bool = False,
) -> str:
    """
    Generate a message in chat mode.
    - Read/write history/default.json
    - Reset history if older than history_expire_seconds
    - Max number of turns in history is max_turns
    """
    client = create_client(config)

    # Check if the history has expired
    if history_is_expired(history_path, history_expire_seconds):
        clear_history(history_path)

    # Load history
    data = load_history(history_path)
    messages: List[Dict[str, str]] = data.get("messages", [])

    # If history is empty, add a new system message at the beginning
    if not any(m.get("role") == "system" for m in messages):
        if system_prompt.strip():
            messages.insert(0, {"role": "system", "content": system_prompt})

    # Add the current user message
    # (meta-variable expansion is done on the caller side)
    messages.append({"role": "user", "content": user_prompt})

    # Trim history
    messages = trim_messages(messages, max_turns)

    if debug:
        print("=== Chat messages to send ===", file=sys.stderr)
        for m in messages:
            print(f"{m['role']}: {m['content']}", file=sys.stderr)
        print("=== end ===", file=sys.stderr)

    # Call the API
    response = client.chat.completions.create(
        model=model,
        messages=messages,
    )
    content = response.choices[0].message.content or ""
    content = content.strip()

    # Add assistant message to history
    messages.append({"role": "assistant", "content": content})

    # Save history
    save_history(history_path, model, messages)

    return content


def trim_messages(messages: List[Dict[str, str]], max_turns: int) -> List[Dict[str, str]]:
    """
    Limit the history to a maximum of MAX_TURNS turns.
    - Assumes only one system message is kept at the beginning
    - Counts one user + one assistant pair as a single turn
    """
    if not messages:
        return messages

    # Separate the system message at the beginning
    system_msgs = [m for m in messages if m["role"] == "system"]
    other_msgs = [m for m in messages if m["role"] != "system"]

    system_msg = system_msgs[0] if system_msgs else None

    # Count other_msgs from the head as user/assistant pairs
    # Simply treat "2 messages as 1 turn" and trim
    # Max number of messages = system(1) + 2 * MAX_TURNS
    max_len = 2 * max_turns
    if len(other_msgs) > max_len:
        other_msgs = other_msgs[-max_len:]

    new_messages: List[Dict[str, str]] = []
    if system_msg:
        new_messages.append(system_msg)
    new_messages.extend(other_msgs)
    return new_messages


def ensure_history_dir() -> None:
    os.makedirs(HISTORY_DIR, exist_ok=True)


def clear_history(path: str) -> None:
    try:
        os.remove(path)
    except FileNotFoundError:
        pass
    except OSError:
        pass


def history_is_expired(path: str, history_expire_seconds: int) -> bool:
    if not os.path.exists(path):
        return False
    try:
        mtime = os.path.getmtime(path)
    except OSError:
        return True
    now = datetime.now().timestamp()
    return (now - mtime) > history_expire_seconds


def load_history(path: str) -> Dict[str, Any]:
    if not os.path.exists(path):
        return {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f) or {}
    except Exception:
        # If the file is corrupted or unreadable, start fresh
        return {}


def save_history(path: str, model: str, messages: List[Dict[str, str]]) -> None:
    ensure_history_dir()
    data = {
        "model": model,
        "messages": messages,
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def main() -> int:
    args = parse_args()
    config = load_config(args.config)

    # Handle --clear-history
    if args.clear_history:
        clear_history(HISTORY_FILE)
        if args.debug:
            print(f"Cleared history: {HISTORY_FILE}", file=sys.stderr)
        return 0

    model = args.model or config.get("model", "gpt-4.1-mini")
    system_prompt = choose_system_prompt(config, args.system_prompt)
    user_prompt = choose_user_prompt(config, args.user_prompt, args.rest)
    history_expire_seconds = config.get("history_expire_seconds", 600)
    max_turns = config.get("max_turns", 20)
    oneshot_mode = args.oneshot or (args.user_prompt is None and not args.rest)

    if args.debug:
        print(f"System prompt: {system_prompt}", file=sys.stderr)
        print(f"User prompt: {user_prompt}", file=sys.stderr)
        print(f"api: {config.get('api', 'openai')}", file=sys.stderr)
        print(f"Model: {model}", file=sys.stderr)
        print(f"Oneshot: {oneshot_mode}", file=sys.stderr)

    try:
        if oneshot_mode:
            # oneshot mode
            message = generate_oneshot_message(
                config=config,
                model=model,
                system_prompt=system_prompt,
                user_prompt=user_prompt,
            )
        else:
            # chat mode
            message = generate_chat_message(
                config=config,
                model=model,
                system_prompt=system_prompt,
                user_prompt=user_prompt,
                history_path=HISTORY_FILE,
                history_expire_seconds=history_expire_seconds,
                max_turns=max_turns,
                debug=args.debug,
            )
    except Exception as e:
        if args.debug:
            print(f"Error: {e}", file=sys.stderr)
        return 1

    print(message)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
