#!/usr/bin/env python3
"""Cross-platform installation and video generation for the Loom Skill."""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import urllib.error
import urllib.request
import uuid
import zipfile
from datetime import datetime, timezone
from pathlib import Path


PROJECT_ARCHIVE_URL = (
    "https://github.com/iammm0/video-loom/archive/refs/heads/main.zip"
)
PROJECT_NAME = "video-loom"
DEFAULT_VOICE_NAME = "zh-CN-XiaoxiaoNeural-Female"
NEEDS_INPUT_EXIT_CODE = 10
SEEDANCE_API_KEY_URL = "https://console.volcengine.com/ark"
SKILL_USER_AGENT = "VideoLoom-Agent-Skill"

# Keep the recommended list focused on commonly used providers. When an LLM
# key is missing, the helper emits all choices at once to avoid extra turns.
RECOMMENDED_LLM_PROVIDERS = {
    "moonshot": (
        "Kimi / Moonshot AI",
        "https://platform.kimi.com/console/api-keys",
    ),
    "openai": ("OpenAI", "https://platform.openai.com/api-keys"),
    "gemini": ("Google Gemini", "https://aistudio.google.com/app/apikey"),
    "deepseek": ("DeepSeek", "https://platform.deepseek.com/api_keys"),
    "volcengine": (
        "ByteDance VolcEngine Ark / Doubao",
        "https://console.volcengine.com/ark",
    ),
    "minimax": ("MiniMax", "https://platform.minimax.io/"),
    "mimo": (
        "Xiaomi MiMo",
        "https://platform.xiaomimimo.com/docs/zh-CN/quick-start/first-api-call",
    ),
}
KEYLESS_LLM_PROVIDERS = {"ollama", "litellm"}
CUSTOM_OPENAI_PROVIDER = "oneapi"

# Hidden providers such as Qwen, Azure, and Grok remain usable when already
# selected, but are not automatic fallback candidates. A fully configured
# generic OpenAI-compatible endpoint can be reused safely.
ADDITIONAL_REUSABLE_PROVIDERS = (CUSTOM_OPENAI_PROVIDER,)


class SkillError(RuntimeError):
    """An actionable Skill error that can be reported without a traceback."""


def log(message: str) -> None:
    """Flush concise progress so the agent knows the long-running job started."""
    print(f"[video-loom] {message}", flush=True)


def is_project_root(root: Path) -> bool:
    """Return whether the directory looks like a video-loom checkout."""
    if not (root / "cli.py").is_file() or not (root / "config.example.toml").is_file():
        return False
    pyproject = root / "pyproject.toml"
    if not pyproject.is_file():
        return True
    name_match = re.search(
        r"(?m)^name\s*=\s*[\"']([^\"']+)[\"']",
        pyproject.read_text(encoding="utf-8"),
    )
    return name_match is None or name_match.group(1) == PROJECT_NAME


def infer_default_root() -> Path:
    """Prefer the checkout that contains this skill, otherwise ~/video-loom."""
    skill_dir = Path(__file__).resolve().parent
    repo_root = skill_dir.parent.parent
    if is_project_root(repo_root):
        return repo_root
    return Path.home() / PROJECT_NAME


DEFAULT_ROOT = infer_default_root()


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Install video-loom and generate a final video from a topic."
    )
    parser.add_argument("--subject", required=True, help="video topic")
    parser.add_argument(
        "--root",
        type=Path,
        default=DEFAULT_ROOT,
        help=f"video-loom installation directory (default: {DEFAULT_ROOT})",
    )
    parser.add_argument(
        "cli_args",
        nargs=argparse.REMAINDER,
        help="additional video-loom CLI arguments placed after --",
    )
    args = parser.parse_args(argv)
    args.subject = args.subject.strip()
    if not args.subject:
        parser.error("--subject cannot be empty")
    if args.cli_args and args.cli_args[0] == "--":
        args.cli_args = args.cli_args[1:]
    return args


def _safe_extract(archive: zipfile.ZipFile, destination: Path) -> None:
    """Reject ZIP entries that would escape the temporary extraction directory."""
    destination = destination.resolve()
    for member in archive.infolist():
        target = (destination / member.filename).resolve()
        if target != destination and destination not in target.parents:
            raise SkillError(f"project archive contains an unsafe path: {member.filename}")
    archive.extractall(destination)


def ensure_project(root: Path) -> None:
    """Reuse an existing checkout or install it from the official GitHub archive."""
    root = root.expanduser().resolve()
    if is_project_root(root):
        log(f"using existing project: {root}")
        return
    if root.exists() and any(root.iterdir()):
        raise SkillError(f"installation directory exists but is not a valid project: {root}")

    root.parent.mkdir(parents=True, exist_ok=True)
    log(f"first-time installation: downloading the official project to {root}")
    with tempfile.TemporaryDirectory(prefix="loom-install-") as temp_dir_value:
        temp_dir = Path(temp_dir_value)
        archive_path = temp_dir / f"{PROJECT_NAME}.zip"
        request = urllib.request.Request(
            PROJECT_ARCHIVE_URL,
            headers={"User-Agent": SKILL_USER_AGENT},
        )
        with urllib.request.urlopen(request, timeout=120) as response:
            # Stream the archive to avoid holding a second full copy in memory.
            with archive_path.open("wb") as archive_file:
                shutil.copyfileobj(response, archive_file)
        with zipfile.ZipFile(archive_path) as archive:
            _safe_extract(archive, temp_dir)

        candidates = [
            path
            for path in temp_dir.iterdir()
            if path.is_dir() and is_project_root(path)
        ]
        if len(candidates) != 1:
            raise SkillError("download completed but no valid video-loom project was found")
        if root.exists():
            root.rmdir()
        shutil.move(str(candidates[0]), str(root))
    log("project download completed")


def ensure_config(root: Path) -> Path:
    """Create the initial configuration without overwriting an existing file."""
    config_path = root / "config.toml"
    if not config_path.exists():
        shutil.copy2(root / "config.example.toml", config_path)
        log(f"created configuration file: {config_path}")
    return config_path


def _plain_config_value(text: str, key: str) -> str:
    """Read a simple top-level TOML value without printing its contents."""
    match = re.search(rf"(?m)^{re.escape(key)}\s*=\s*(.*)$", text)
    if not match:
        return ""
    value = match.group(1).split("#", 1)[0].strip()
    if value.startswith('"') and value.endswith('"'):
        return value[1:-1]
    return value


def _table_body(text: str, table: str) -> tuple[int, int, str] | None:
    """Return the body span of a TOML table, excluding the header line."""
    header = re.search(rf"(?m)^\[{re.escape(table)}\]\s*$", text)
    if not header:
        return None
    body_start = header.end()
    next_header = re.search(r"(?m)^\[", text[body_start:])
    body_end = body_start + next_header.start() if next_header else len(text)
    return body_start, body_end, text[body_start:body_end]


def _plain_table_value(text: str, table: str, key: str) -> str:
    """Read a key from a named TOML table without printing its contents."""
    block = _table_body(text, table)
    if not block:
        return ""
    return _plain_config_value(block[2], key)


def _replace_config_value(text: str, key: str, value: object) -> str:
    """Replace one active field while preserving the configuration layout."""
    pattern = re.compile(rf"(?m)^({re.escape(key)}\s*=\s*).*$")
    if not pattern.search(text):
        raise SkillError(f"configuration field not found in config.toml: {key}")
    encoded = json.dumps(value, ensure_ascii=False)
    return pattern.sub(lambda match: f"{match.group(1)}{encoded}", text, count=1)


def _replace_table_value(text: str, table: str, key: str, value: object) -> str:
    """Replace one field inside a named TOML table."""
    block = _table_body(text, table)
    if not block:
        raise SkillError(f"configuration table not found in config.toml: [{table}]")
    body_start, body_end, body = block
    new_body = _replace_config_value(body, key, value)
    return text[:body_start] + new_body + text[body_end:]


def _has_configured_value(value: str) -> bool:
    """Treat empty strings and whitespace-only key arrays as unconfigured."""
    if not value:
        return False
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError:
        return bool(value.strip())
    if isinstance(parsed, list):
        return any(str(item).strip() for item in parsed)
    return bool(str(parsed).strip())


def apply_environment_config(config_path: Path) -> None:
    """Write supplied credentials while logging field names only."""
    provider = os.environ.get("LOOM_LLM_PROVIDER", "").strip().lower()
    if provider == "openai_compatible":
        provider = CUSTOM_OPENAI_PROVIDER
    llm_key = os.environ.get("LOOM_LLM_API_KEY", "").strip()
    base_url = os.environ.get("LOOM_LLM_BASE_URL", "").strip()
    model_name = os.environ.get("LOOM_LLM_MODEL_NAME", "").strip()
    seedance_key = os.environ.get("LOOM_SEEDANCE_API_KEY", "").strip()
    if not any((provider, llm_key, base_url, model_name, seedance_key)):
        return

    text = config_path.read_text(encoding="utf-8")
    current_provider = _plain_config_value(text, "llm_provider") or "moonshot"
    provider = provider or current_provider
    changes: list[str] = []
    if os.environ.get("LOOM_LLM_PROVIDER", "").strip():
        text = _replace_config_value(text, "llm_provider", provider)
        changes.append("llm_provider")
    if llm_key:
        text = _replace_config_value(text, f"{provider}_api_key", llm_key)
        changes.append(f"{provider}_api_key")
    if base_url:
        text = _replace_config_value(text, f"{provider}_base_url", base_url)
        changes.append(f"{provider}_base_url")
    if model_name:
        text = _replace_config_value(text, f"{provider}_model_name", model_name)
        changes.append(f"{provider}_model_name")
    if seedance_key:
        text = _replace_table_value(text, "seedance", "api_key", seedance_key)
        changes.append("seedance.api_key")
    config_path.write_text(text, encoding="utf-8")
    log("updated configuration fields: " + ", ".join(changes))


def _provider_is_ready(text: str, provider: str) -> bool:
    """Return whether a provider has enough configuration to generate."""
    if provider in KEYLESS_LLM_PROVIDERS:
        return True
    if not _has_configured_value(
        _plain_config_value(text, f"{provider}_api_key")
    ):
        return False
    if provider == CUSTOM_OPENAI_PROVIDER:
        return all(
            _has_configured_value(_plain_config_value(text, f"{provider}_{suffix}"))
            for suffix in ("base_url", "model_name")
        )
    return True


def reuse_existing_llm_provider(config_path: Path) -> str:
    """
    Reuse existing LLM credentials before asking the user for another key.

    Keep the current provider when it is ready. Otherwise, scan configured
    recommended providers in UI order and update ``llm_provider``. Credential
    values are inspected in memory and are never logged.
    """
    text = config_path.read_text(encoding="utf-8")
    current_provider = _plain_config_value(text, "llm_provider") or "moonshot"
    if _provider_is_ready(text, current_provider):
        return current_provider

    reusable_providers = (
        *RECOMMENDED_LLM_PROVIDERS,
        *ADDITIONAL_REUSABLE_PROVIDERS,
    )
    for provider in reusable_providers:
        if _provider_is_ready(text, provider):
            text = _replace_config_value(text, "llm_provider", provider)
            config_path.write_text(text, encoding="utf-8")
            log(f"reusing configured LLM provider: {provider}")
            return provider
    return current_provider


def has_cli_option(cli_args: list[str], option: str) -> bool:
    """Return whether forwarded arguments explicitly set a CLI option."""
    return any(item == option or item.startswith(f"{option}=") for item in cli_args)


def _has_seedance_key(text: str) -> bool:
    """Return whether Seedance can run from config or process environment."""
    if any(
        os.environ.get(name, "").strip()
        for name in ("SEEDANCE_API_KEY", "ARK_API_KEY", "VOLCENGINE_API_KEY")
    ):
        return True
    if _has_configured_value(_plain_table_value(text, "seedance", "api_key")):
        return True
    return _has_configured_value(_plain_config_value(text, "volcengine_api_key"))


def missing_config(config_path: Path, cli_args: list[str]) -> tuple[str, list[str]]:
    """Return the active provider and only the fields required by this run."""
    del cli_args
    text = config_path.read_text(encoding="utf-8")
    provider = _plain_config_value(text, "llm_provider") or "moonshot"
    missing: list[str] = []
    if provider not in KEYLESS_LLM_PROVIDERS and not _has_configured_value(
        _plain_config_value(text, f"{provider}_api_key")
    ):
        missing.append(f"{provider}_api_key")
    if provider == CUSTOM_OPENAI_PROVIDER:
        for suffix in ("base_url", "model_name"):
            field = f"{provider}_{suffix}"
            if not _has_configured_value(_plain_config_value(text, field)):
                missing.append(field)
    if not _has_seedance_key(text):
        missing.append("seedance.api_key")
    return provider, missing


def report_missing_config(provider: str, missing: list[str]) -> int:
    """Tell the agent exactly which credentials must be requested."""
    print("LOOM_NEEDS_INPUT")
    print(f"LLM_PROVIDER={provider}")
    for field in missing:
        print(f"MISSING={field}")
    if any(field.endswith("_api_key") and not field.startswith("seedance.") for field in missing):
        print("LLM_PROVIDER_OPTIONS_BEGIN")
        for provider_id, (label, url) in RECOMMENDED_LLM_PROVIDERS.items():
            print(f"LLM_PROVIDER_OPTION={provider_id}|{label}|{url}")
        print(
            "LLM_PROVIDER_OPTION=oneapi|Other OpenAI-compatible provider|"
            "requires an API key, API base URL, and model name"
        )
        print("LLM_PROVIDER_OPTIONS_END")
    if any(field.startswith(f"{CUSTOM_OPENAI_PROVIDER}_") for field in missing):
        print(
            "OPENAI_COMPATIBLE_REQUIRED="
            "API key, API base URL, model name"
        )
    if "seedance.api_key" in missing:
        print(f"SEEDANCE_API_KEY_URL={SEEDANCE_API_KEY_URL}")
    print("Request only the listed values, set the environment variables, and rerun the same command.")
    return NEEDS_INPUT_EXIT_CODE


def result_manifest_path(root: Path) -> Path:
    return root / ".agent-logs" / PROJECT_NAME / "latest-result.json"


def write_result_manifest(root: Path, payload: dict[str, object]) -> Path:
    """
    Atomically write the stable result file for agents that cannot wait.

    The file contains task status and result paths only, never configuration
    contents, credentials, or full logs.
    """
    result_path = result_manifest_path(root)
    result_path.parent.mkdir(parents=True, exist_ok=True)
    data = {
        "updated_at": datetime.now(timezone.utc).isoformat(),
        **payload,
    }
    unique_suffix = str(uuid.uuid4()).replace("-", "")
    temp_path = result_path.with_name(
        f".{result_path.name}.{os.getpid()}.{unique_suffix}.tmp"
    )
    temp_path.write_text(
        json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    temp_path.replace(result_path)
    return result_path.resolve()


def run_checked(command: list[str], *, cwd: Path) -> None:
    """Run dependency sync quietly and show only the last 30 lines on failure."""
    log("installing or verifying project dependencies with uv")
    result = subprocess.run(
        command,
        cwd=cwd,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        errors="replace",
        check=False,
    )
    if result.returncode != 0:
        output_tail = (result.stdout or "").splitlines()[-30:]
        if output_tail:
            print("\n".join(output_tail), file=sys.stderr)
        raise SkillError(f"dependency installation failed with exit code {result.returncode}")


def generate_video(
    root: Path,
    subject: str,
    cli_args: list[str],
) -> tuple[list[Path], Path, Path, Path]:
    """Run one traceable CLI task and return only its final video files."""
    uv = shutil.which("uv")
    if not uv:
        raise SkillError("uv was not found; reopen the terminal or add uv to PATH")
    run_checked([uv, "sync", "--frozen"], cwd=root)

    task_id = str(uuid.uuid4())
    task_dir = root / "storage" / "tasks" / task_id
    log_dir = root / ".agent-logs" / PROJECT_NAME
    log_dir.mkdir(parents=True, exist_ok=True)
    log_path = log_dir / f"run-{task_id}.log"
    write_result_manifest(
        root,
        {
            "status": "running",
            "subject": subject,
            "task_id": task_id,
            "task_dir": str(task_dir.resolve()),
            "log_file": str(log_path.resolve()),
            "video_files": [],
        },
    )
    voice_args = (
        []
        if has_cli_option(cli_args, "--voice-name")
        else ["--voice-name", DEFAULT_VOICE_NAME]
    )
    command = [
        uv,
        "run",
        "python",
        "cli.py",
        *cli_args,
        "--video-subject",
        subject,
        "--task-id",
        task_id,
        # Older CLI versions leave voice_name empty and fail during Edge TTS
        # with ``Invalid voice ''``. Supply a stable Chinese voice unless the
        # user has explicitly selected another voice.
        *voice_args,
        # A Skill request must produce a finished video. Force the final stage
        # so forwarded options cannot stop at script, audio, or materials.
        "--stop-at",
        "video",
    ]
    log(f"starting video generation, task ID: {task_id}")
    log(f"full generation log: {log_path}")
    with log_path.open("w", encoding="utf-8") as log_file:
        result = subprocess.run(
            command,
            cwd=root,
            stdout=log_file,
            stderr=subprocess.STDOUT,
            text=True,
            check=False,
        )
    if result.returncode != 0:
        tail = log_path.read_text(encoding="utf-8", errors="replace").splitlines()[-30:]
        if tail:
            print("\n".join(tail), file=sys.stderr)
        error = (
            f"video generation failed with exit code {result.returncode}; "
            f"log: {log_path}"
        )
        write_result_manifest(
            root,
            {
                "status": "failed",
                "subject": subject,
                "task_id": task_id,
                "task_dir": str(task_dir.resolve()),
                "log_file": str(log_path.resolve()),
                "video_files": [],
                "error": error,
            },
        )
        raise SkillError(error)

    videos = sorted(
        path.resolve()
        for path in task_dir.glob("final-*.mp4")
        if path.is_file() and path.stat().st_size > 0
    )
    if not videos:
        error = f"generation completed without a valid final MP4; log: {log_path}"
        write_result_manifest(
            root,
            {
                "status": "failed",
                "subject": subject,
                "task_id": task_id,
                "task_dir": str(task_dir.resolve()),
                "log_file": str(log_path.resolve()),
                "video_files": [],
                "error": error,
            },
        )
        raise SkillError(error)
    result_path = write_result_manifest(
        root,
        {
            "status": "completed",
            "subject": subject,
            "task_id": task_id,
            "task_dir": str(task_dir.resolve()),
            "log_file": str(log_path.resolve()),
            "video_files": [str(video) for video in videos],
        },
    )
    return videos, task_dir.resolve(), log_path.resolve(), result_path


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    root = args.root.expanduser().resolve()
    try:
        ensure_project(root)
        config_path = ensure_config(root)
        apply_environment_config(config_path)
        reuse_existing_llm_provider(config_path)
        provider, missing = missing_config(config_path, args.cli_args)
        if missing:
            write_result_manifest(
                root,
                {
                    "status": "needs_input",
                    "subject": args.subject,
                    "missing": missing,
                },
            )
            return report_missing_config(provider, missing)
        videos, task_dir, log_path, result_path = generate_video(
            root, args.subject, args.cli_args
        )
    except (OSError, SkillError, urllib.error.URLError, zipfile.BadZipFile) as exc:
        print(f"LOOM_ERROR={exc}", file=sys.stderr)
        return 1

    print("LOOM_RESULT")
    for video in videos:
        print(f"VIDEO_FILE={video}")
    print(f"TASK_DIR={task_dir}")
    print(f"LOG_FILE={log_path}")
    print(f"RESULT_FILE={result_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
