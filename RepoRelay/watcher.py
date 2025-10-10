#!/usr/bin/env python3
"""
RepoRelay GitHub Conversation Watcher (Multi-Repo)
-------------------------------------------------
Watches *multiple* GitHub repositories found under a local root folder.
For each repo, it polls issue/PR comments since the last watermark and looks
for a regex trigger in the comment text. When matched, it collects the full
context, optionally includes a "parent" issue, runs an external command
(e.g., "codex exec -") with the context on stdin **in that repo's working directory**, and
posts the result back to the conversation as a comment.

Environment is controlled exclusively via `REPORELAY_*` variables.
"""

import datetime as _dt
import json
import logging
import os
import re
import signal
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Tuple

import requests

ISO8601 = "%Y-%m-%dT%H:%M:%SZ"


def _env(key: str, default: str = "") -> str:
    env_key = f"REPORELAY_{key}"
    return os.environ.get(env_key, default)


def _env_flag(key: str, default: bool) -> bool:
    raw = _env(key, "1" if default else "0").strip().lower()
    return raw in {"1", "true", "yes", "on"}


def _load_env_file(path: Path) -> None:
    """Populate os.environ with values from a simple ``.env`` file if present."""
    try:
        data = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return
    for line in data.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        if not key or key in os.environ:
            continue
        os.environ[key] = value.strip()


def _strip_ansi(text: str) -> str:
    if not text:
        return text
    return _ANSI_RE.sub("", text)


def _compute_new_since(previous: str, batches: Iterable[Iterable[dict]]) -> str:
    """Return the latest timestamp seen across batches, falling back to previous."""
    candidates = [previous]
    for batch in batches:
        for item in batch:
            for key in ("updated_at", "created_at"):
                ts = item.get(key)
                if ts:
                    candidates.append(ts)
    try:
        return max(candidates)
    except ValueError:
        return previous


def _now_utc() -> str:
    return _dt.datetime.utcnow().strftime(ISO8601)


def _iso(dt: _dt.datetime) -> str:
    return dt.strftime(ISO8601)


def _parse_iso(s: str) -> _dt.datetime:
    return _dt.datetime.strptime(s, ISO8601)


def _build_subprocess_env(cwd: Path) -> Dict[str, str]:
    env = dict(os.environ)
    if env.get("REPORELAY_FORWARD_GITHUB_TOKEN") != "1":
        env.pop("GITHUB_TOKEN", None)
    env["PWD"] = str(cwd)
    return env

def discover_git_remote(path: Path) -> Optional[str]:
    """Return the remote.origin.url for a git repo at 'path', else None."""
    try:
        url = subprocess.check_output(
            ["git", "-C", str(path), "config", "--get", "remote.origin.url"],
            text=True,
        ).strip()
        return url or None
    except Exception:
        return None

def parse_github_owner_repo(remote_url: str) -> Optional[str]:
    """
    Accept 'git@github.com:owner/repo.git' or 'https://github.com/owner/repo.git'
    and return 'owner/repo' or None.
    """
    m = re.search(r"github\.com[:/](?P<owner>[^/]+)/(?P<repo>[^/.]+)(?:\.git)?$", remote_url or "", re.I)
    if not m:
        return None
    return f"{m.group('owner')}/{m.group('repo')}"

def discover_local_repos(root: Path, recursive: bool, require_marker: bool, exclude_dirs: List[str]) -> Dict[str, Path]:
    """Scan ``root`` for git repositories and return ``{owner/repo: path}``."""
    repos: Dict[str, Path] = {}

    def consider_dir(d: Path):
        if not (d / ".git").exists():
            return
        # optional marker gate
        if require_marker and not (d / ".reporelay-enabled").exists():
            return
        remote = discover_git_remote(d)
        if not remote:
            return
        or_name = parse_github_owner_repo(remote)
        if not or_name:
            return
        if or_name not in repos:
            repos[or_name] = d

    if not recursive:
        for child in root.iterdir():
            if not child.is_dir():
                continue
            if child.name in exclude_dirs:
                continue
            consider_dir(child)
    else:
        for dirpath, dirnames, filenames in os.walk(root):
            # prune excluded directory names in-place
            dirnames[:] = [n for n in dirnames if n not in exclude_dirs]
            d = Path(dirpath)
            if (d / ".git").exists():
                consider_dir(d)
                # do not descend into subdirectories of a found repo
                dirnames[:] = []
                continue

    return repos

@dataclass
class Config:
    token: str
    root: Path
    recursive: bool = field(default_factory=lambda: _env_flag("RECURSIVE", False))
    regex: str = field(default_factory=lambda: _env("REGEX", r"codexe"))
    match_target: str = field(default_factory=lambda: _env("MATCH_TARGET", "comments"))
    poll_seconds: int = field(default_factory=lambda: int(_env("POLL_SECONDS", "20")))
    per_repo_pause: float = field(default_factory=lambda: float(_env("PER_REPO_PAUSE", "0.3")))
    state_path: Path = field(default=None)
    codex_cmd: str = field(default_factory=lambda: os.getenv("CODEX_CMD", "codex"))
    codex_args: List[str] = field(default_factory=lambda: os.getenv("CODEX_ARGS", "exec -").split())
    codex_resume_args: List[str] = field(default_factory=lambda: os.getenv("CODEX_RESUME_ARGS", "resume").split())
    codex_timeout: int = field(default_factory=lambda: int(os.getenv("CODEX_TIMEOUT", "3600")))
    lockfile: Path = field(default=None)
    require_marker: bool = field(default_factory=lambda: _env_flag("REQUIRE_MARKER", False))
    exclude_dirs: List[str] = field(default_factory=lambda: [s for s in _env("EXCLUDE_DIRS", "").split(",") if s])
    ignore_self: bool = field(default_factory=lambda: _env_flag("IGNORE_SELF", False))
    default_resume: bool = field(default_factory=lambda: _env_flag("DEFAULT_RESUME", True))
    resume_send_context: bool = field(default_factory=lambda: _env_flag("RESUME_SEND_CONTEXT", False))

    def __post_init__(self):
        if self.state_path is None:
            self.state_path = self.root / ".reporelay_state.json"
        if self.lockfile is None:
            self.lockfile = self.root / ".reporelay.lock"
        self.match_target = self.match_target.lower()
        if self.match_target not in {"comments", "issue_or_comments"}:
            sys.exit("REPORELAY_MATCH_TARGET must be 'comments' or 'issue_or_comments'.")
        if self.per_repo_pause < 0:
            self.per_repo_pause = 0.0

    @staticmethod
    def from_env() -> "Config":
        cwd = Path(os.getcwd()).resolve()
        _load_env_file(cwd / ".env")
        token = os.getenv("GITHUB_TOKEN", "").strip()
        if not token:
            sys.exit("GITHUB_TOKEN is required in environment.")
        default_root = cwd.parent if cwd.parent != cwd else cwd
        root = Path(_env("ROOT", str(default_root))).resolve()
        return Config(token=token, root=root)

class GitHub:
    def __init__(self, token: str):
        self.session = requests.Session()
        self.session.headers.update({
            "Authorization": f"token {token}",
            "Accept": "application/vnd.github+json",
            "User-Agent": "reporelay/1.0",
        })
        self.api = "https://api.github.com"
        self._me = None

    def me_login(self) -> str:
        if self._me is None:
            r = self.session.get(f"{self.api}/user", timeout=30)
            r.raise_for_status()
            self._me = r.json()["login"]
        return self._me

    def list_issue_comments_since(self, repo: str, since_iso: str, per_page: int = 100) -> List[dict]:
        """List comments across all issues for a single repo since ISO time."""
        comments: List[dict] = []
        url = f"{self.api}/repos/{repo}/issues/comments"
        params = {"since": since_iso, "per_page": per_page, "page": 1}
        while True:
            r = self.session.get(url, params=params, timeout=60)
            if r.status_code == 304:
                break
            r.raise_for_status()
            batch = r.json()
            if not isinstance(batch, list):
                break
            comments.extend(batch)
            link = r.headers.get("Link", "")
            if 'rel="next"' not in link:
                break
            m = re.search(r'<([^>]+)>;\s*rel="next"', link)
            if not m:
                break
            next_url = m.group(1)
            url, params = next_url, {}
        return comments

    def list_review_comments_since(self, repo: str, since_iso: str, per_page: int = 100) -> List[dict]:
        """List pull request review comments across a repo since ISO time."""
        comments: List[dict] = []
        url = f"{self.api}/repos/{repo}/pulls/comments"
        params = {"since": since_iso, "per_page": per_page, "page": 1}
        while True:
            r = self.session.get(url, params=params, timeout=60)
            if r.status_code == 304:
                break
            r.raise_for_status()
            batch = r.json()
            if not isinstance(batch, list):
                break
            comments.extend(batch)
            link = r.headers.get("Link", "")
            if 'rel="next"' not in link:
                break
            m = re.search(r'<([^>]+)>;\s*rel="next"', link)
            if not m:
                break
            next_url = m.group(1)
            url, params = next_url, {}
        return comments

    def get_issue(self, repo: str, number: int) -> dict:
        r = self.session.get(f"{self.api}/repos/{repo}/issues/{number}", timeout=30)
        r.raise_for_status()
        return r.json()

    def list_issue_comments(self, repo: str, number: int) -> List[dict]:
        url = f"{self.api}/repos/{repo}/issues/{number}/comments"
        out: List[dict] = []
        params = {"per_page": 100, "page": 1}
        while True:
            r = self.session.get(url, params=params, timeout=60)
            r.raise_for_status()
            batch = r.json()
            out.extend(batch)
            link = r.headers.get("Link", "")
            if 'rel="next"' not in link:
                break
            m = re.search(r'<([^>]+)>;\s*rel="next"', link)
            if not m:
                break
            next_url = m.group(1)
            url, params = next_url, {}
        return out

    def list_issues_since(self, repo: str, since_iso: str, per_page: int = 100) -> List[dict]:
        """List issues (excluding PRs) updated since ISO time."""
        issues: List[dict] = []
        url = f"{self.api}/repos/{repo}/issues"
        params = {"since": since_iso, "per_page": per_page, "page": 1, "state": "all"}
        while True:
            r = self.session.get(url, params=params, timeout=60)
            if r.status_code == 304:
                break
            r.raise_for_status()
            batch = r.json()
            if not isinstance(batch, list):
                break
            for item in batch:
                if "pull_request" in item:
                    continue
                issues.append(item)
            link = r.headers.get("Link", "")
            if 'rel="next"' not in link:
                break
            m = re.search(r'<([^>]+)>;\s*rel="next"', link)
            if not m:
                break
            next_url = m.group(1)
            url, params = next_url, {}
        return issues

    def post_issue_comment(self, repo: str, number: int, body: str) -> dict:
        r = self.session.post(
            f"{self.api}/repos/{repo}/issues/{number}/comments",
            json={"body": body},
            timeout=60,
        )
        r.raise_for_status()
        return r.json()

    def add_reaction_to_comment(self, repo: str, comment_id: int, content: str) -> bool:
        url = f"{self.api}/repos/{repo}/issues/comments/{comment_id}/reactions"
        headers = {
            "Accept": "application/vnd.github+json, application/vnd.github.squirrel-girl-preview+json",
            "Content-Type": "application/json",
        }
        r = self.session.post(url, json={"content": content}, headers=headers, timeout=30)
        log = logging.getLogger("reporelay")
        log.info("Reaction response for %s comment %s: %s", repo, comment_id, r.status_code)
        if r.status_code in (200, 201):
            return True
        if r.status_code in (204, 409):
            return False
        r.raise_for_status()
        return True

    def add_reaction_to_review_comment(self, repo: str, comment_id: int, content: str) -> bool:
        url = f"{self.api}/repos/{repo}/pulls/comments/{comment_id}/reactions"
        headers = {
            "Accept": "application/vnd.github+json, application/vnd.github.squirrel-girl-preview+json",
            "Content-Type": "application/json",
        }
        r = self.session.post(url, json={"content": content}, headers=headers, timeout=30)
        log = logging.getLogger("reporelay")
        log.info("Reaction response for %s review comment %s: %s", repo, comment_id, r.status_code)
        if r.status_code in (200, 201):
            return True
        if r.status_code in (204, 409):
            return False
        r.raise_for_status()
        return True

class State:
    def __init__(self, path: Path):
        self.path = str(path)
        self.data = {
            "repos": {
                # "owner/repo": {
                #     "path": "/abs/local/path",
                #     "last_since": ISO8601,
                #     "processed_comment_ids": [],
                #     "runs": {}  # issue_number -> dict(...)
                # }
            }
        }
        self._load()

    def _load(self):
        try:
            with open(self.path, "r") as f:
                loaded = json.load(f)
            if isinstance(loaded, dict):
                self.data.update(loaded)
        except FileNotFoundError:
            pass

    def ensure_repo(self, repo: str, path: Path):
        if repo not in self.data["repos"]:
            self.data["repos"][repo] = {
                "path": str(path),
                "last_since": _iso(_dt.datetime.utcnow() - _dt.timedelta(days=7)),
                "pr_review_last_since": _iso(_dt.datetime.utcnow() - _dt.timedelta(days=7)),
                "processed_comment_ids": [],
                "processed_review_comment_ids": [],
                "runs": {},
                "issue_runs": {},
                "pr_runs": {},
            }
        else:
            # keep path up to date if it changed
            self.data["repos"][repo]["path"] = str(path)
            self.data["repos"][repo].setdefault("processed_comment_ids", [])
            self.data["repos"][repo].setdefault("processed_review_comment_ids", [])
            self.data["repos"][repo].setdefault("runs", {})
            self.data["repos"][repo].setdefault("issue_runs", {})
            self.data["repos"][repo].setdefault("pr_runs", {})
            if "last_since" not in self.data["repos"][repo]:
                self.data["repos"][repo]["last_since"] = _iso(_dt.datetime.utcnow() - _dt.timedelta(days=7))
            self.data["repos"][repo].setdefault(
                "pr_review_last_since",
                self.data["repos"][repo].get("last_since", _iso(_dt.datetime.utcnow() - _dt.timedelta(days=7))),
            )

    def save(self):
        tmp = self.path + ".tmp"
        with open(tmp, "w") as f:
            json.dump(self.data, f, indent=2)
        os.replace(tmp, self.path)

class SingleInstanceLock:
    def __init__(self, path: Path):
        self.path = str(path)
        self.fd = None

    def acquire(self):
        import fcntl
        self.fd = open(self.path, "w")
        try:
            fcntl.flock(self.fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            self.fd.write(str(os.getpid()))
            self.fd.flush()
        except BlockingIOError:
            sys.exit(f"Another RepoRelay watcher instance is running (lock: {self.path}).")

    def release(self):
        if not self.fd:
            return
        import fcntl
        try:
            fcntl.flock(self.fd, fcntl.LOCK_UN)
        finally:
            self.fd.close()

def find_parent_issue_number(issue_body: str) -> Optional[int]:
    if not issue_body:
        return None
    m = re.search(r"(?im)^\s*parent\s*[:\-]?\s*#(\d+)\s*$", issue_body)
    if m:
        return int(m.group(1))
    for line in issue_body.splitlines():
        if re.search(r"(?i)\bparent\b", line):
            m2 = re.search(r"#(\d+)", line)
            if m2:
                return int(m2.group(1))
    return None

def build_job_input(
    repo: str,
    issue: dict,
    comments: List[dict],
    parent: Optional[dict],
    trigger_comment: dict,
    resume: bool,
    conversation_type: str = "issue",
) -> str:
    conversation_type = conversation_type or "issue"
    label = "PR" if conversation_type == "pr" else "ISSUE"
    body_label = f"{label} BODY"
    comments_label = f"{label} COMMENTS (chronological)"
    header = [
        f"REPO: {repo}",
        f"{label}: #{issue['number']} - {issue.get('title','').strip()}",
        f"TRIGGERED_BY: comment_id={trigger_comment.get('id')} by @{trigger_comment.get('user',{}).get('login','')} at {trigger_comment.get('created_at')}",
        f"MODE: {'RESUME' if resume else 'NEW'}",
        "",
        "=== INITIAL INSTRUCTIONS (SYSTEM) ===",
        "1) Read the conversation body, any linked parent issue body, and the collected comments.",
        "2) Follow the instructions contained in the conversation (and parent if relevant).",
        "3) Perform the requested work, and provide your output.",
        "",
        f"=== {body_label} ===",
        issue.get("body") or "(no body)",
        "",
    ]

    if parent is not None:
        header.extend([
            "=== PARENT ISSUE BODY ===",
            f"(Parent issue #{parent.get('number')}: {parent.get('title','').strip()})",
            parent.get("body") or "(no body)",
            "",
        ])

    trigger_body = trigger_comment.get("body") or ""
    if trigger_body:
        header.extend([
            "=== TRIGGER COMMENT BODY ===",
            trigger_body,
            "",
        ])

    header.append(f"=== {comments_label} ===")
    for c in sorted(comments, key=lambda x: x.get("created_at","")):
        who = c.get("user", {}).get("login", "unknown")
        when = c.get("created_at", "?")
        body = c.get("body","").rstrip()
        header.append(f"[{when}] @{who}:")
        header.append(body)
        header.append("")

    return "\n".join(header)

def run_external(codex_cmd: str, codex_args: List[str], payload: Optional[str], timeout: int, cwd: Path) -> Tuple[int, str, str]:
    try:
        proc = subprocess.run(
            [codex_cmd] + codex_args,
            input=(payload.encode("utf-8") if payload is not None else None),
            capture_output=True,
            timeout=timeout,
            cwd=str(cwd),
            env=_build_subprocess_env(cwd),
        )
        out = proc.stdout.decode("utf-8", errors="replace")
        err = proc.stderr.decode("utf-8", errors="replace")
        max_chars = 60000
        if len(out) > max_chars:
            out = out[:max_chars] + "\n\n[output truncated]"
        if len(err) > max_chars:
            err = err[:max_chars] + "\n\n[stderr truncated]"
        return proc.returncode, out, err
    except subprocess.TimeoutExpired:
        return 124, "", f"Process timed out after {timeout}s."
    except FileNotFoundError:
        return 127, "", f"Command not found or not executable: {codex_cmd}"
    except Exception as e:
        return 125, "", f"Unexpected error: {e!r}"


def postprocess_stdout(out: str, codex_cmd: str) -> str:
    """Trim Codex CLI chatter so comments only contain the final response."""
    if not out:
        return out
    try:
        cmd_name = Path(codex_cmd).name.lower()
    except Exception:
        cmd_name = codex_cmd.lower()

    if cmd_name != "codex":
        return out

    lower = out.lower()
    tokens_idx = lower.rfind("tokens used")
    if tokens_idx == -1:
        return out.strip()

    prefix = out[:tokens_idx].rstrip()

    marker_idx = prefix.lower().rfind("\ncodex\n")
    if marker_idx == -1:
        marker_idx = prefix.lower().rfind("\nthinking\n")
    if marker_idx == -1:
        marker_idx = 0
    else:
        marker_idx = marker_idx + prefix[marker_idx:].find("\n") + 1

    trimmed = prefix[marker_idx:].strip()

    lines = trimmed.splitlines()
    if lines and lines[0].strip().lower() == "codex":
        lines = lines[1:]

    return "\n".join(lines).strip() or out.strip()


_RUN_ID_PATTERNS = [
    re.compile(r"(?im)\b(?:run[ _-]?id|session)\s*[:=]\s*([A-Za-z0-9._-]{6,})"),
    re.compile(r"(?im)\bresume\s+with:?\s*codex\s+resume\s+([A-Za-z0-9._-]{6,})"),
    re.compile(r"(?im)\"id\"\s*:\s*\"([A-Za-z0-9._-]{6,})\""),
]


def extract_codex_run_id(text: str) -> Optional[str]:
    """Return a Codex run identifier found in mixed stdout/stderr text, if any."""
    blob = text or ""
    for pat in _RUN_ID_PATTERNS:
        match = pat.search(blob)
        if match:
            return match.group(1)
    return None


def extract_intent(text: str) -> Tuple[str, Optional[str]]:
    """Infer trigger intent from comment text."""
    snippet = text or ""
    if re.search(r"(?i)\bcodexe\b.*\bnew\b", snippet):
        return "new", None
    resume_match = re.search(
        r"(?i)\bcodexe\b.*\bresume\b(?:\s+([A-Za-z0-9._-]{6,}))?",
        snippet,
    )
    if resume_match:
        return "resume", resume_match.group(1)
    return "default", None


def decide_codex_invocation(
    cfg: Config,
    intent: str,
    requested_id: Optional[str],
    stored_id: Optional[str],
) -> Tuple[List[str], bool, bool, Optional[str]]:
    """Determine CLI arguments, stdin usage, and resume flag."""
    intent = intent or "default"

    if intent == "new":
        return cfg.codex_args, True, False, None

    if intent == "resume":
        target_id = requested_id or stored_id
        if target_id:
            return cfg.codex_resume_args + [target_id], cfg.resume_send_context, True, target_id
        return cfg.codex_args, True, False, None

    if stored_id and cfg.default_resume:
        return cfg.codex_resume_args + [stored_id], cfg.resume_send_context, True, stored_id

    return cfg.codex_args, True, False, None


def format_result_comment(ok: bool, run_id: str, returncode: int, out: str, err: str) -> str:
    out = (out or "").strip()
    if out:
        return out

    err = (err or "").strip()
    if err:
        return f"```\n{err}\n```"

    return f"(run {run_id} exited with code {returncode} without producing output)"

def extract_resume_flag(text: str) -> bool:
    return bool(re.search(r"(?i)\bresume\b", text or ""))

def main():
    cfg = Config.from_env()

    # Logging
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )
    log = logging.getLogger("reporelay")

    # Single-instance lock
    lock = SingleInstanceLock(cfg.lockfile)
    lock.acquire()

    # Graceful shutdown
    stop = {"flag": False}
    def _sig(*_a):
        log.info("Signal received, shutting down...")
        stop["flag"] = True
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, _sig)

    # Discover repos from local filesystem
    log.info("Scanning for git repos under %s (recursive=%s)", cfg.root, cfg.recursive)
    repos = discover_local_repos(cfg.root, cfg.recursive, cfg.require_marker, cfg.exclude_dirs)
    if not repos:
        log.warning("No repos found. Create a git repo under %s or adjust REPORELAY_ROOT/REPORELAY_ROOT.", cfg.root)

    # Compile regex trigger (case-insensitive)
    try:
        trigger_re = re.compile(cfg.regex, re.I)
    except re.error as e:
        lock.release()
        sys.exit(f"Invalid REPORELAY_REGEX '{cfg.regex}': {e}")

    gh = GitHub(cfg.token)
    me = gh.me_login()
    log.info(
        "Authenticated as @%s, watching %d repos, regex='%s', poll=%ss, match_target=%s, per_repo_pause=%.2fs",
        me,
        len(repos),
        cfg.regex,
        cfg.poll_seconds,
        cfg.match_target,
        cfg.per_repo_pause,
    )

    # Load (and create) per-repo state
    st = State(cfg.state_path)
    for repo, path in repos.items():
        st.ensure_repo(repo, path)
    st.save()

    # Poll loop
    while not stop["flag"]:
        try:
            # Re-discover repos periodically in case new ones are added
            # (cheap: re-scan every loop; cost is small compared to API calls)
            repos = discover_local_repos(cfg.root, cfg.recursive, cfg.require_marker, cfg.exclude_dirs)
            for repo, path in repos.items():
                st.ensure_repo(repo, path)

            for repo, meta in list(st.data["repos"].items()):
                local_path = Path(meta["path"])
                if repo not in repos:
                    # Repo disappeared locally: skip but keep state
                    continue

                since = meta.get("last_since") or _iso(_dt.datetime.utcnow() - _dt.timedelta(days=7))
                processed = set(meta.get("processed_comment_ids", []))

                comments = gh.list_issue_comments_since(repo, since)
                # Update watermark to now (double-guarded by processed_comment_ids)
                meta["last_since"] = _now_utc()

                processed_comments = meta.setdefault("processed_comment_ids", [])
                processed = set(processed_comments)
                issue_runs = meta.setdefault("issue_runs", {})
                pr_runs = meta.setdefault("pr_runs", {})

                for c in sorted(comments, key=lambda x: x.get("created_at", "")):
                    cid = c.get("id")
                    if cid in processed:
                        continue
                    body = (c.get("body") or "")

                    author = c.get("user", {}).get("login", "")
                    if cfg.ignore_self and author == me:
                        processed_comments.append(cid)
                        processed.add(cid)
                        continue

                    if not trigger_re.search(body):
                        processed_comments.append(cid)
                        processed.add(cid)
                        continue

                    issue_url = c.get("issue_url", "")
                    m = re.search(r"/issues/(\d+)$", issue_url)
                    if not m:
                        processed_comments.append(cid)
                        processed.add(cid)
                        continue
                    number = int(m.group(1))

                    issue = gh.get_issue(repo, number)
                    is_pr = "pull_request" in issue

                    conversation_type = "pr" if is_pr else "issue"
                    runs_store = pr_runs if is_pr else issue_runs

                    issue_comments = gh.list_issue_comments(repo, number)
                    parent_issue = None
                    pnum = find_parent_issue_number(issue.get("body", "") or "")
                    if pnum:
                        try:
                            parent_issue = gh.get_issue(repo, pnum)
                        except Exception as e:
                            logging.warning("Could not fetch parent issue #%s in %s: %r", pnum, repo, e)

                    intent, requested_id = extract_intent(body)
                    conversation_state = runs_store.get(str(number), {})
                    stored_id = conversation_state.get("codex_run_id")
                    args, send_payload, resume_flag, resume_target = decide_codex_invocation(
                        cfg, intent, requested_id, stored_id
                    )

                    payload = build_job_input(
                        repo,
                        issue,
                        issue_comments,
                        parent_issue,
                        c,
                        resume=resume_flag,
                        conversation_type=conversation_type,
                    )
                    payload_to_send = payload if send_payload else None

                    run_id = f"{repo.replace('/', '_')}-{number}-{cid}-{int(time.time())}"
                    log.info(
                        "Trigger from @%s on %s#%d (%s comment %s); intent=%s; resume=%s; run_id=%s; cwd=%s",
                        author,
                        repo,
                        number,
                        conversation_type.upper(),
                        cid,
                        intent,
                        resume_flag,
                        run_id,
                        local_path,
                    )

                    rc, out, err = run_external(
                        cfg.codex_cmd,
                        args,
                        payload_to_send,
                        cfg.codex_timeout,
                        cwd=local_path,
                    )
                    processed_out = postprocess_stdout(out, cfg.codex_cmd)

                    ok = (rc == 0) and bool(processed_out.strip())
                    comment_body = format_result_comment(ok, run_id, rc, processed_out, err)
                    combined = "\n".join(part for part in (out, err) if part)
                    codex_id = extract_codex_run_id(combined) or (resume_target if resume_flag else None)
                    issue_updated_at = issue.get("updated_at") or issue.get("created_at") or _now_utc()

                    if ok and cid is not None:
                        try:
                            reacted = gh.add_reaction_to_comment(repo, cid, "eyes")
                            if reacted:
                                log.info("Added 👀 reaction to %s %s comment %s", repo, conversation_type, cid)
                        except Exception as e:
                            log.warning("Failed to add reaction to %s %s comment %s: %r", repo, conversation_type, cid, e)

                    try:
                        gh.post_issue_comment(repo, number, comment_body)
                    except requests.HTTPError as e:
                        log.error("Failed to post comment to %s#%d: %s", repo, number, e)
                    except Exception as e:
                        log.error("Unexpected error posting comment to %s#%d: %r", repo, number, e)

                    meta.setdefault("runs", {})[str(number)] = {
                        "status": "ok" if ok else "error",
                        "last_run_at": _now_utc(),
                        "last_comment_id": cid,
                        "run_id": run_id,
                        "resume": resume_flag,
                        "returncode": rc,
                        "source": f"{conversation_type}_comment",
                        "codex_run_id": codex_id,
                    }

                    updated_field = "last_pr_updated" if is_pr else "last_issue_updated"
                    new_state = dict(conversation_state)
                    new_state.update(
                        {
                            updated_field: issue_updated_at,
                            "run_id": run_id,
                            "returncode": rc,
                            "status": "ok" if ok else "error",
                        }
                    )
                    if codex_id:
                        new_state["codex_run_id"] = codex_id
                    runs_store[str(number)] = new_state

                    processed_comments.append(cid)
                    processed.add(cid)

                    st.save()

                review_processed_list = meta.setdefault("processed_review_comment_ids", [])
                review_processed = set(review_processed_list)
                review_since = meta.get("pr_review_last_since") or since
                review_comments = gh.list_review_comments_since(repo, review_since)
                meta["pr_review_last_since"] = _now_utc()

                for rc in sorted(review_comments, key=lambda x: x.get("created_at", "")):
                    rcid = rc.get("id")
                    if rcid in review_processed:
                        continue

                    body = (rc.get("body") or "")
                    author = rc.get("user", {}).get("login", "")
                    if cfg.ignore_self and author == me:
                        review_processed_list.append(rcid)
                        review_processed.add(rcid)
                        continue

                    if not trigger_re.search(body):
                        review_processed_list.append(rcid)
                        review_processed.add(rcid)
                        continue

                    pr_url = rc.get("pull_request_url") or ""
                    pr_match = re.search(r"/pulls/(\d+)$", pr_url)
                    if not pr_match:
                        review_processed_list.append(rcid)
                        review_processed.add(rcid)
                        continue
                    number = int(pr_match.group(1))

                    issue = gh.get_issue(repo, number)
                    if "pull_request" not in issue:
                        review_processed_list.append(rcid)
                        review_processed.add(rcid)
                        continue

                    issue_comments = gh.list_issue_comments(repo, number)
                    parent_issue = None
                    pnum = find_parent_issue_number(issue.get("body", "") or "")
                    if pnum:
                        try:
                            parent_issue = gh.get_issue(repo, pnum)
                        except Exception as e:
                            logging.warning(
                                "Could not fetch parent issue #%s in %s (review trigger): %r",
                                pnum,
                                repo,
                                e,
                            )

                    runs_store = pr_runs
                    intent, requested_id = extract_intent(body)
                    conversation_state = runs_store.get(str(number), {})
                    stored_id = conversation_state.get("codex_run_id")
                    args, send_payload, resume_flag, resume_target = decide_codex_invocation(
                        cfg,
                        intent,
                        requested_id,
                        stored_id,
                    )

                    location_bits: List[str] = []
                    if rc.get("path"):
                        location_bits.append(f"path={rc['path']}")
                    if rc.get("line"):
                        location_bits.append(f"line={rc['line']}")
                    elif rc.get("original_line"):
                        location_bits.append(f"original_line={rc['original_line']}")
                    if rc.get("side"):
                        location_bits.append(f"side={rc['side']}")
                    review_context = ", ".join(location_bits)
                    review_body = body
                    if review_context:
                        review_body = f"{review_body}\n\n[Review context: {review_context}]"
                    if rc.get("html_url"):
                        review_body = f"{review_body}\n\nLink: {rc['html_url']}"

                    trigger_comment = {
                        "id": rcid,
                        "user": rc.get("user") or {},
                        "created_at": rc.get("created_at") or rc.get("updated_at") or _now_utc(),
                        "body": review_body,
                    }

                    payload = build_job_input(
                        repo,
                        issue,
                        issue_comments,
                        parent_issue,
                        trigger_comment,
                        resume=resume_flag,
                        conversation_type="pr",
                    )
                    payload_to_send = payload if send_payload else None

                    run_id = f"{repo.replace('/', '_')}-{number}-review-{rcid}-{int(time.time())}"
                    log.info(
                        "Trigger from review comment by @%s on %s#%d (comment %s); intent=%s; resume=%s; run_id=%s; cwd=%s",
                        author,
                        repo,
                        number,
                        rcid,
                        intent,
                        resume_flag,
                        run_id,
                        local_path,
                    )

                    rc_status, out, err = run_external(
                        cfg.codex_cmd,
                        args,
                        payload_to_send,
                        cfg.codex_timeout,
                        cwd=local_path,
                    )
                    processed_out = postprocess_stdout(out, cfg.codex_cmd)

                    ok = (rc_status == 0) and bool(processed_out.strip())
                    comment_body = format_result_comment(ok, run_id, rc_status, processed_out, err)
                    if rc.get("html_url"):
                        comment_body = f"Triggered from review comment {rc['html_url']} by @{author}\n\n" + comment_body
                    combined = "\n".join(part for part in (out, err) if part)
                    codex_id = extract_codex_run_id(combined) or (resume_target if resume_flag else None)
                    issue_updated_at = issue.get("updated_at") or issue.get("created_at") or _now_utc()

                    if ok and rcid is not None:
                        try:
                            reacted = gh.add_reaction_to_review_comment(repo, rcid, "eyes")
                            if reacted:
                                log.info("Added 👀 reaction to %s review comment %s", repo, rcid)
                        except Exception as e:
                            log.warning("Failed to add reaction to %s review comment %s: %r", repo, rcid, e)

                    try:
                        gh.post_issue_comment(repo, number, comment_body)
                    except requests.HTTPError as e:
                        log.error(
                            "Failed to post comment to %s#%d (review trigger): %s",
                            repo,
                            number,
                            e,
                        )
                    except Exception as e:
                        log.error(
                            "Unexpected error posting comment to %s#%d (review trigger): %r",
                            repo,
                            number,
                            e,
                        )

                    meta.setdefault("runs", {})[str(number)] = {
                        "status": "ok" if ok else "error",
                        "last_run_at": _now_utc(),
                        "last_comment_id": rcid,
                        "run_id": run_id,
                        "resume": resume_flag,
                        "returncode": rc_status,
                        "source": "pr_review_comment",
                        "codex_run_id": codex_id,
                    }

                    conversation_record = dict(conversation_state)
                    conversation_record.update(
                        {
                            "last_pr_updated": issue_updated_at,
                            "run_id": run_id,
                            "returncode": rc_status,
                            "status": "ok" if ok else "error",
                        }
                    )
                    if codex_id:
                        conversation_record["codex_run_id"] = codex_id
                    runs_store[str(number)] = conversation_record

                    review_processed_list.append(rcid)
                    review_processed.add(rcid)

                    st.save()
                if cfg.match_target == "issue_or_comments":
                    issue_runs = meta.setdefault("issue_runs", {})
                    pr_runs = meta.setdefault("pr_runs", {})
                    issues = gh.list_issues_since(repo, since)
                    for issue in issues:
                        is_pr = "pull_request" in issue

                        title_text = issue.get("title", "") or ""
                        body_text = issue.get("body", "") or ""
                        if not (trigger_re.search(title_text) or trigger_re.search(body_text)):
                            continue

                        number = issue.get("number")
                        if number is None:
                            continue

                        issue_updated_at = issue.get("updated_at") or issue.get("created_at") or _now_utc()
                        runs_store = pr_runs if is_pr else issue_runs
                        updated_field = "last_pr_updated" if is_pr else "last_issue_updated"
                        last_processed_at = runs_store.get(str(number), {}).get(updated_field)
                        if last_processed_at == issue_updated_at:
                            continue

                        issue_comments = gh.list_issue_comments(repo, number)
                        parent_issue = None
                        pnum = find_parent_issue_number(body_text)
                        if pnum:
                            try:
                                parent_issue = gh.get_issue(repo, pnum)
                            except Exception as e:
                                logging.warning(
                                    "Could not fetch parent issue #%s in %s (issue trigger): %r",
                                    pnum,
                                    repo,
                                    e,
                                )

                        trigger_id = issue.get("id") or f"issue-{number}"
                        trigger_comment = {
                            "id": trigger_id,
                            "user": issue.get("user") or {},
                            "created_at": issue.get("created_at") or issue_updated_at,
                            "body": body_text or title_text,
                        }

                        intent, requested_id = extract_intent(trigger_comment["body"])
                        conversation_state = runs_store.get(str(number), {})
                        stored_id = conversation_state.get("codex_run_id")
                        args, send_payload, resume_flag, resume_target = decide_codex_invocation(
                            cfg, intent, requested_id, stored_id
                        )

                        payload = build_job_input(
                            repo,
                            issue,
                            issue_comments,
                            parent_issue,
                            trigger_comment,
                            resume=resume_flag,
                            conversation_type="pr" if is_pr else "issue",
                        )
                        payload_to_send = payload if send_payload else None

                        run_id = f"{repo.replace('/', '_')}-{number}-{trigger_id}-{int(time.time())}"
                        log.info(
                            "Trigger from %s body/title on %s#%d; intent=%s; resume=%s; run_id=%s; cwd=%s",
                            "PR" if is_pr else "issue",
                            repo,
                            number,
                            intent,
                            resume_flag,
                            run_id,
                            local_path,
                        )

                        rc, out, err = run_external(
                            cfg.codex_cmd,
                            args,
                            payload_to_send,
                            cfg.codex_timeout,
                            cwd=local_path,
                        )
                        processed_out = postprocess_stdout(out, cfg.codex_cmd)

                        ok = (rc == 0) and bool(processed_out.strip())
                        comment_body = format_result_comment(ok, run_id, rc, processed_out, err)
                        combined = "\n".join(part for part in (out, err) if part)
                        codex_id = extract_codex_run_id(combined) or (resume_target if resume_flag else None)

                        try:
                            gh.post_issue_comment(repo, number, comment_body)
                        except requests.HTTPError as e:
                            log.error(
                                "Failed to post comment to %s#%d (%s trigger): %s",
                                repo,
                                number,
                                "pr" if is_pr else "issue",
                                e,
                            )
                        except Exception as e:
                            log.error(
                                "Unexpected error posting comment to %s#%d (%s trigger): %r",
                                repo,
                                number,
                                "pr" if is_pr else "issue",
                                e,
                            )

                        meta.setdefault("runs", {})[str(number)] = {
                            "status": "ok" if ok else "error",
                            "last_run_at": _now_utc(),
                            "last_comment_id": trigger_id,
                            "run_id": run_id,
                            "resume": resume_flag,
                            "returncode": rc,
                            "source": "pr_issue" if is_pr else "issue",
                            "codex_run_id": codex_id,
                        }

                        conversation_record = dict(conversation_state)
                        conversation_record.update(
                            {
                                updated_field: issue_updated_at,
                                "run_id": run_id,
                                "returncode": rc,
                                "status": "ok" if ok else "error",
                            }
                        )
                        if codex_id:
                            conversation_record["codex_run_id"] = codex_id

                        runs_store[str(number)] = conversation_record

                        st.save()

                # Trim processed list per repo
                if len(meta.get("processed_comment_ids", [])) > 5000:
                    meta["processed_comment_ids"] = meta["processed_comment_ids"][-2000:]
                    st.save()
                if len(meta.get("processed_review_comment_ids", [])) > 5000:
                    meta["processed_review_comment_ids"] = meta["processed_review_comment_ids"][-2000:]
                    st.save()

                if cfg.per_repo_pause > 0:
                    time.sleep(cfg.per_repo_pause)

            # End per-loop save
            st.save()

        except requests.HTTPError as e:
            resp = getattr(e, "response", None)
            retry_after = None
            if resp is not None:
                retry_after = resp.headers.get("Retry-After")
                remaining = resp.headers.get("X-RateLimit-Remaining")
                reset = resp.headers.get("X-RateLimit-Reset")
                logging.warning("HTTPError %s; remaining=%s; reset=%s; retry_after=%s",
                                e, remaining, reset, retry_after)
            sleep_s = int(retry_after) if (retry_after and retry_after.isdigit()) else cfg.poll_seconds * 3
            time.sleep(sleep_s)
        except Exception as e:
            logging.exception("Unexpected error in poll loop: %r", e)
            time.sleep(cfg.poll_seconds)
        finally:
            if stop["flag"]:
                break
            time.sleep(cfg.poll_seconds)

    lock.release()
    log.info("Stopped.")

if __name__ == "__main__":
    main()
