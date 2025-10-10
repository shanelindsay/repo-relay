# RepoRelay Test Plan

_Scope: multi-repo watcher handling GitHub issues & PR comments, trigger parsing, external command execution, resume behaviour._

## A. Pre-flight

1. **Create/choose two test repos on GitHub** (e.g., `owner/repoA`, `owner/repoB`).
2. **Clone both under a shared folder**, e.g. `~/work/reporelay-root/repoA` and `~/work/reporelay-root/repoB`.
3. **Personal access token**: create a token with issues read/write. Export `GITHUB_TOKEN` in the shell that will launch RepoRelay.
4. **Install** Python 3.9+ and run `pip install -r requirements.txt` in the repo root (or create a venv and install there).
5. Optional: prepare a minimal external command to prove `cwd` (e.g., a shell script `codex` that writes `$(pwd)/RELAY_PROOF.txt` and prints `## Result`).
6. If the same account will trigger comments, export `REPORELAY_IGNORE_SELF=0` before launching.

## B. Happy-path verification

1. **Codex new run**  
   - Set `REPORELAY_ROOT` to the parent folder.  
   - Start RepoRelay (`./RepoRelay/run-reporelay.sh`).  
   - Comment `codexe do something` on `repoA`.  
   - Expect: RepoRelay posts a result comment; artefacts appear only in `repoA`.

2. **Resume command**  
   - On the same thread, comment `codexe resume`.  
   - Output should show the run resumed (stdout contains resume acknowledgement) and RepoRelay adds an 👀 reaction.

3. **Explicit new run**  
   - Comment `codexe new full rebuild`.  
   - Expect: RepoRelay runs with `CODEX_ARGS` (not resume) and posts a fresh comment.

4. **Resume specific id**  
   - Capture a run id from Codex output (`Run ID:` line).  
   - Comment `codexe resume <id>`.  
   - Expect: RepoRelay calls `CODEX_RESUME_ARGS <id>` and posts the new output.

## C. Context & parent handling

1. **Parent issue inclusion**  
   - Create a parent issue `#2`.  
   - In child issue `#1` body, add `Parent: #2`.  
   - Trigger RepoRelay in `#1`.  
   - Payload seen by Codex should include a `PARENT ISSUE BODY` section.

2. **PR coverage**  
   - Open a pull request and comment `codexe check`.  
   - Expect: RepoRelay ignores PRs (current behaviour).  
   - Optional future work: enable PR support.

## D. Error paths

1. **Long output truncation**  
   - Force external command to print >70k chars.  
   - Expect: posted comment ends with `[output truncated]`.

2. **Timeout**  
   - Set `CODEX_TIMEOUT=2` and run a command that sleeps 5s.  
   - Expect: failure comment with timeout message, 👀 reaction omitted.

3. **Bad regex**  
   - Launch with `REPORELAY_REGEX='[bad'`.  
   - Expect: RepoRelay exits with a helpful error.

## E. Operational checks

- Logs stream to stdout/stderr; verify tmux launcher captures them.
- State file (`.reporelay_state.json`) contains processed IDs, run metadata, and Codex run ids.
- Ensure reaction logic: successful runs add 👀 to the triggering comment; failures do not.
- Confirm `REPORELAY_FORWARD_GITHUB_TOKEN=1` forwards the token to the subprocess, while `0` scrubs it.

