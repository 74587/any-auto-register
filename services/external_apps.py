"""外部插件（CLIProxyAPI）的安装 / 启停管理"""

from __future__ import annotations

import os
import re
import shutil
import stat
import subprocess
import threading
import time
from pathlib import Path
from typing import Any

import requests

_ROOT = Path(__file__).resolve().parents[2]
_EXT_ROOT = _ROOT / "_ext_targets"
_LOG_ROOT = Path(__file__).resolve().parent / "external_logs"
_LOG_ROOT.mkdir(parents=True, exist_ok=True)

_SEMVER_TAG_PATTERN = re.compile(r"^v?\d+\.\d+\.\d+(?:-[0-9A-Za-z.-]+)?(?:\+[0-9A-Za-z.-]+)?$")
_HEALTH_TIMEOUT_SECONDS = 2
_START_TIMEOUT_SECONDS = 90
_STOP_TIMEOUT_SECONDS = 10

_SERVICE_META: dict[str, dict[str, Any]] = {
    "cliproxyapi": {
        "label": "CLIProxyAPI",
        "repo_name": "CLIProxyAPI",
        "remote": "https://github.com/router-for-me/CLIProxyAPI.git",
        "url": "http://127.0.0.1:8317",
        "health": "http://127.0.0.1:8317/",
        "management_url": "http://127.0.0.1:8317/management.html",
        "management_key_setting": "cliproxyapi_management_key",
        "port": 8317,
    },
}

_PROCS: dict[str, subprocess.Popen] = {}
_LOG_FILES: dict[str, Any] = {}
_LAST_ERROR: dict[str, str] = {}
_LOCK = threading.Lock()


def _meta(name: str) -> dict[str, Any]:
    try:
        return _SERVICE_META[name]
    except KeyError:
        raise KeyError(f"未知的外部插件: {name}") from None


def _get_setting(key: str, default: str = "") -> str:
    try:
        from core.config_store import config_store

        return str(config_store.get(key, "") or "").strip() or default
    except Exception:
        return default


def _creationflags() -> int:
    return getattr(subprocess, "CREATE_NO_WINDOW", 0)


def _run_git(repo: Path | None, *args: str, check: bool = True) -> subprocess.CompletedProcess:
    command = ["git"]
    if repo is not None:
        command += ["-C", str(repo)]
    return subprocess.run(
        command + list(args),
        check=check,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        creationflags=_creationflags(),
    )


def _git_output(repo: Path, *args: str) -> str:
    try:
        return subprocess.check_output(
            ["git", "-C", str(repo), *args],
            text=True,
            creationflags=_creationflags(),
        ).strip()
    except Exception:
        return ""


def _repo_path(name: str) -> Path:
    return _EXT_ROOT / _meta(name)["repo_name"]


def _log_path(name: str) -> Path:
    return _LOG_ROOT / f"{name}.log"


def _close_log(name: str) -> None:
    handle = _LOG_FILES.pop(name, None)
    if handle is None:
        return
    try:
        handle.close()
    except Exception:
        pass


def _open_log(name: str):
    _close_log(name)
    handle = open(_log_path(name), "a", encoding="utf-8")
    _LOG_FILES[name] = handle
    return handle


def _make_tree_writable(path: Path) -> None:
    if not path.exists():
        return
    for root, dirnames, filenames in os.walk(path):
        for entry in (*dirnames, *filenames):
            target = Path(root) / entry
            try:
                target.chmod(target.stat().st_mode | stat.S_IWRITE)
            except Exception:
                pass
    try:
        path.chmod(path.stat().st_mode | stat.S_IWRITE)
    except Exception:
        pass


def _kill_processes_touching_path(path: Path) -> None:
    if os.name != "nt":
        return
    try:
        subprocess.run(
            [
                "powershell",
                "-NoProfile",
                "-Command",
                "$p=$args[0]; "
                "Get-CimInstance Win32_Process | "
                "Where-Object { (($_.CommandLine -like ('*' + $p + '*')) -or ($_.ExecutablePath -like ('*' + $p + '*'))) } | "
                "ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }",
                str(path),
            ],
            check=False,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=_creationflags(),
        )
    except Exception:
        pass


def _update_mode() -> str:
    mode = _get_setting("external_apps_update_mode", "tag").lower()
    return "branch" if mode == "branch" else "tag"


def _has_remote_branch(repo: Path, branch: str) -> bool:
    if not branch:
        return False
    return _run_git(repo, "show-ref", "--verify", "--quiet", f"refs/remotes/origin/{branch}", check=False).returncode == 0


def _origin_default_branch(repo: Path) -> str:
    head = _git_output(repo, "symbolic-ref", "--short", "refs/remotes/origin/HEAD")
    if head.startswith("origin/"):
        branch = head.split("/", 1)[1].strip()
        if branch:
            return branch
    return next((name for name in ("main", "master") if _has_remote_branch(repo, name)), "main")


def _current_local_branch(repo: Path) -> str:
    branch = _git_output(repo, "rev-parse", "--abbrev-ref", "HEAD")
    return branch if branch and branch != "HEAD" else ""


def _branch_candidates(repo: Path, preferred: str) -> list[str]:
    ordered = [
        str(preferred or "").strip(),
        _current_local_branch(repo),
        _origin_default_branch(repo),
        "main",
        "master",
    ]
    seen: list[str] = []
    for branch in ordered:
        if branch and branch not in seen:
            seen.append(branch)
    return seen


def _sync_repo_to_branch_head(repo: Path, preferred_branch: str = "") -> None:
    for branch in _branch_candidates(repo, preferred_branch):
        if not _has_remote_branch(repo, branch):
            continue
        _run_git(repo, "checkout", "-B", branch, f"origin/{branch}")
        _run_git(repo, "reset", "--hard", f"origin/{branch}")
        _run_git(repo, "clean", "-fd")
        return
    raise RuntimeError(f"未找到可用远端分支（repo={repo}）")


def _latest_semver_tag(repo: Path) -> str:
    listing = _git_output(
        repo,
        "for-each-ref",
        "refs/tags",
        "--sort=-version:refname",
        "--format=%(refname:strip=2)",
    )
    return next((tag for line in listing.splitlines() if _SEMVER_TAG_PATTERN.fullmatch(tag := line.strip())), "")


def _sync_repo_to_latest_semver_tag(repo: Path) -> bool:
    tag = _latest_semver_tag(repo)
    if not tag:
        return False
    _run_git(repo, "checkout", "--force", tag)
    _run_git(repo, "reset", "--hard", tag)
    _run_git(repo, "clean", "-fd")
    return True


def _sync_repo_to_latest(name: str) -> None:
    repo = _repo_path(name)
    repo.parent.mkdir(parents=True, exist_ok=True)
    if not repo.exists():
        _run_git(None, "clone", _meta(name)["remote"], str(repo))

    _run_git(repo, "fetch", "--all", "--tags", "--prune")
    if _update_mode() == "branch" or not _sync_repo_to_latest_semver_tag(repo):
        _sync_repo_to_branch_head(repo)


def _health_ok(name: str) -> bool:
    url = _meta(name).get("health")
    if not url:
        return False
    try:
        return requests.get(url, timeout=_HEALTH_TIMEOUT_SECONDS).status_code < 500
    except Exception:
        return False


def _find_pid_by_port(port: int) -> int | None:
    if not port:
        return None
    try:
        listing = subprocess.check_output(
            ["netstat", "-ano", "-p", "tcp"],
            text=True,
            creationflags=_creationflags(),
        )
    except Exception:
        return None
    for line in listing.splitlines():
        parts = line.split()
        if len(parts) < 5 or parts[0].upper() != "TCP":
            continue
        local, state, pid = parts[1], parts[3].upper(), parts[4]
        if local.endswith(f":{port}") and state == "LISTENING":
            try:
                return int(pid)
            except ValueError:
                return None
    return None


def _running_proc(name: str) -> subprocess.Popen | None:
    proc = _PROCS.get(name)
    return proc if proc and proc.poll() is None else None


def _status_one(name: str) -> dict[str, Any]:
    meta = _meta(name)
    repo = _repo_path(name)
    proc = _running_proc(name)
    running = _health_ok(name)
    pid = proc.pid if proc else None
    if running:
        pid = _find_pid_by_port(int(meta.get("port") or 0)) or pid
    return {
        "name": name,
        "label": meta["label"],
        "repo_path": str(repo),
        "repo_exists": repo.exists(),
        "url": meta.get("url", ""),
        "management_url": meta.get("management_url", ""),
        "management_key": _get_setting(meta.get("management_key_setting", ""), name),
        "running": running,
        "pid": pid,
        "log_path": str(_log_path(name)),
        "last_error": _LAST_ERROR.get(name, ""),
        "kind": "web",
    }


def list_status() -> list[dict[str, Any]]:
    return [_status_one(name) for name in _SERVICE_META]


def _find_go() -> str | None:
    candidates = [shutil.which("go"), r"C:\Program Files\Go\bin\go.exe", "/usr/local/go/bin/go"]
    return next((item for item in candidates if item and Path(item).exists()), None)


def _ensure_cliproxyapi_runtime_config(repo: Path) -> Path:
    config_path = repo / "config.local.yaml"
    if not config_path.exists():
        shutil.copyfile(repo / "config.example.yaml", config_path)
    secret = _get_setting("cliproxyapi_management_key", "cliproxyapi")

    lines = config_path.read_text(encoding="utf-8").splitlines()
    replaced = False
    for index, line in enumerate(lines):
        if line.lstrip().startswith("secret-key:"):
            indent = line[: len(line) - len(line.lstrip())]
            lines[index] = f'{indent}secret-key: "{secret}"'
            replaced = True
    if not replaced:
        lines.append(f'  secret-key: "{secret}"')
    config_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return config_path


def _build_command(name: str) -> tuple[list[str], Path]:
    repo = _repo_path(name)
    if name != "cliproxyapi":
        raise KeyError(f"未知的外部插件: {name}")
    go_exe = _find_go()
    if not go_exe:
        raise RuntimeError("未找到 go，可在设置中先安装 Go 或将 go 加入 PATH")
    config_path = _ensure_cliproxyapi_runtime_config(repo)
    return [go_exe, "run", "./cmd/server", "-config", str(config_path)], repo


def install(name: str) -> dict[str, Any]:
    with _LOCK:
        _meta(name)
        _sync_repo_to_latest(name)
    return _status_one(name)


def uninstall(name: str) -> dict[str, Any]:
    _meta(name)
    try:
        stop(name)
    except Exception:
        pass

    with _LOCK:
        repo = _repo_path(name)
        if repo.exists():
            _remove_repo_tree(name, repo)
        _PROCS.pop(name, None)
        _LAST_ERROR.pop(name, None)
        _close_log(name)
    return _status_one(name)


def _remove_repo_tree(name: str, repo: Path) -> None:
    _kill_processes_touching_path(repo)
    last_error: Exception | None = None
    for _ in range(12):
        try:
            _make_tree_writable(repo)
            shutil.rmtree(repo)
            return
        except Exception as exc:
            last_error = exc
            _kill_processes_touching_path(repo)
            time.sleep(0.5)
    _LAST_ERROR[name] = f"卸载失败：目录仍存在 {repo}" + (f"，原因：{last_error}" if last_error else "")
    raise RuntimeError(_LAST_ERROR[name])


def start(name: str) -> dict[str, Any]:
    with _LOCK:
        meta = _meta(name)
        repo = _repo_path(name)
        if not repo.exists():
            raise RuntimeError(f"{meta['label']} 未安装，请先在插件页点击“安装”")
        if _status_one(name)["running"]:
            return _status_one(name)

        log_file = _open_log(name)
        try:
            command, cwd = _build_command(name)
            _PROCS[name] = subprocess.Popen(
                command,
                cwd=str(cwd),
                stdout=log_file,
                stderr=subprocess.STDOUT,
                creationflags=_creationflags(),
            )
            _LAST_ERROR[name] = ""
        except Exception as exc:
            _LAST_ERROR[name] = str(exc)
            _close_log(name)
            raise

    for _ in range(_START_TIMEOUT_SECONDS):
        time.sleep(1)
        if _health_ok(name):
            return _status_one(name)
        proc = _PROCS.get(name)
        if proc and proc.poll() is not None:
            _LAST_ERROR[name] = f"启动失败，退出码={proc.returncode}"
            return _status_one(name)
    _LAST_ERROR[name] = "启动超时"
    return _status_one(name)


def _terminate_pid(pid: int) -> None:
    if os.name == "nt":
        subprocess.run(
            ["taskkill", "/PID", str(pid), "/T", "/F"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=_creationflags(),
        )
        return
    try:
        os.kill(pid, 15)
    except OSError:
        pass


def stop(name: str) -> dict[str, Any]:
    with _LOCK:
        meta = _meta(name)
        proc = _PROCS.get(name)
        port_pid = _find_pid_by_port(int(meta.get("port") or 0))
        if proc and proc.poll() is None:
            if os.name == "nt":
                _terminate_pid(proc.pid)
            else:
                proc.terminate()
                try:
                    proc.wait(timeout=8)
                except Exception:
                    proc.kill()
        if port_pid and (not proc or port_pid != proc.pid):
            _terminate_pid(port_pid)
        _PROCS.pop(name, None)
        _close_log(name)

    for _ in range(_STOP_TIMEOUT_SECONDS):
        if not _health_ok(name):
            break
        time.sleep(1)
    return _status_one(name)


def start_all() -> list[dict[str, Any]]:
    results = []
    for name in _SERVICE_META:
        if not _repo_path(name).exists():
            item = _status_one(name)
            item["last_error"] = "未安装；如需使用请先手动安装"
            results.append(item)
            continue
        try:
            results.append(start(name))
        except Exception:
            results.append(_status_one(name))
    return results


def stop_all() -> list[dict[str, Any]]:
    return [stop(name) for name in _SERVICE_META]
