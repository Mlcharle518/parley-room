r"""Parley room: a neutral house that lets two or more agents negotiate to a resolution.

The house is a plain script. It holds one shared transcript, gives each seat a turn in strict
rotation, delivers only what the other parties wrote, checks the resolution rule, and stops at
resolution or at the turn cap. It has no opinion and never edits a message.

Seats (--codex-seat / --claude-seat / --grok-seat):
  thread   an existing Codex CLI thread, resumed each turn (`codex exec resume <id>`), read-only sandbox
  fresh    a fresh Codex model each turn, read-only in an empty directory
  mailbox  a live Claude Code or Codex session: the house writes the turn to a file and waits for the reply
  cli      a fresh Claude via `claude -p` (no prior context)
  resume   one Claude Code session for the whole parley (`--session-id` first, `--resume` after)
  api      xAI Grok through the public API (XAI_API_KEY)
  mock     scripted, for testing the house

Admission: every seat (and the verifier) is probed BEFORE the run directory exists. A seat that cannot
prove it will work (missing CLI, unknown Codex thread, missing session, stale mailbox) refuses the
parley at turn 0 rather than failing mid-run.

Stateful seats (thread, resume) keep their own context, so after their first turn they receive only
the turns since they last spoke; stateless seats get the full transcript every turn.

Verifier (--verifier claude|codex|mock): a fresh, tool-less, read-only model asked ONE question after
the parties agree: does this resolution answer the question as posed? It may block a resolution
(the parley continues with the veto on the record); it never writes one. If the verifier itself
fails, the resolution is recorded as unverified, not as verified.

Ledger: transcript.jsonl records `turn_started` BEFORE each seat request (so a crash mid-turn still
shows the request was made), `turn` after a valid reply, `resolution_proposed`, `verified`/`vetoed`/
`unverified`, and `outcome` when one is recorded later with `python parley.py outcome <run> ...`.

Usage:
  python parley.py run --question "..."                       # fresh Codex vs fresh Claude, Claude verifier
  python parley.py run --question @question.md --codex-seat thread --thread <id> --claude-seat mailbox
  python parley.py run --question "..." --grok-seat api --names codex=Astra,claude=Fable
  python parley.py run --question "test" --codex-seat mock --claude-seat mock      # offline check
  python parley.py outcome runs/<stamp> --status held|failed|mixed|moot --note "what happened"

Reply contract (all seats, exactly one JSON object):
  {"message": "<what you say to the other parties>",
   "position": "<your current position, one or two sentences>",
   "accept": true|false,          # do you accept the most recent proposal as the resolution
   "resolved": "<exact text of the resolution you are committing to>" | null}

Resolution rule: the parley ends when the most recent turns (one per seat, all after any veto) carry a
non-null `resolved` whose normalised text matches. A turn with accept=true and resolved=null adopts
the most recently proposed `resolved` text from another party. Otherwise it ends at --max-turns as
UNRESOLVED. Stdlib only.
"""
from __future__ import annotations

import argparse
import glob
import sys
import json
import os
import re
import shutil
import signal
import subprocess
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent

if sys.version_info < (3, 10):
    raise SystemExit("parley.py needs Python 3.10 or later")


def utc() -> str:
    return datetime.now(timezone.utc).isoformat()


def norm(text: str | None) -> str:
    return re.sub(r"\s+", " ", (text or "").strip().lower().rstrip("."))


def extract_json(text: str) -> dict:
    text = text.strip()
    try:
        value = json.loads(text)
    except json.JSONDecodeError:
        start, end = text.find("{"), text.rfind("}")
        if start < 0 or end <= start:
            raise ValueError("no JSON object in reply")
        value = json.loads(text[start:end + 1])
    if not isinstance(value, dict):
        raise ValueError("reply is not a JSON object")
    return value


def validate(value: dict) -> dict:
    for key in ("message", "position"):
        if not isinstance(value.get(key), str) or not value[key].strip():
            raise ValueError(f"reply needs a non-empty string '{key}'")
    if not isinstance(value.get("accept"), bool):
        raise ValueError("reply needs boolean 'accept'")
    resolved = value.get("resolved")
    if resolved is not None and (not isinstance(resolved, str) or not resolved.strip()):
        raise ValueError("'resolved' must be null or non-empty text")
    return {"message": value["message"].strip(), "position": value["position"].strip(),
            "accept": value["accept"], "resolved": resolved.strip() if resolved else None}


class SeatRefused(Exception):
    """A seat or verifier failed admission; raised before the run directory exists."""


# ---------------------------------------------------------------- processes
def kill_tree(pid: int) -> None:
    """Kill a process and everything it spawned. On Windows the CLIs are .cmd wrappers around node:
    killing the wrapper alone leaves the node child running and spending usage."""
    if os.name == "nt":
        subprocess.run(["taskkill", "/T", "/F", "/PID", str(pid)], capture_output=True)
    else:
        try:
            os.killpg(os.getpgid(pid), signal.SIGKILL)
        except ProcessLookupError:
            pass


def run_cli(cmd: list[str], *, input_text: str, timeout: float, cwd: Path | None = None,
            env: dict | None = None) -> subprocess.CompletedProcess:
    """subprocess.run with a whole-tree kill on timeout."""
    proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            text=True, encoding="utf-8", errors="replace", cwd=cwd, env=env,
                            start_new_session=(os.name != "nt"))
    try:
        out, err = proc.communicate(input_text, timeout=timeout)
    except subprocess.TimeoutExpired:
        kill_tree(proc.pid)
        try:
            proc.communicate(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
        raise
    return subprocess.CompletedProcess(cmd, proc.returncode, out, err)


def probe(cmd: list[str], env: dict | None = None, timeout: float = 30) -> str:
    """Run a cheap read-only command for admission; returns combined output or raises SeatRefused."""
    try:
        proc = run_cli(cmd, input_text="", timeout=timeout, env=env)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise SeatRefused(f"{cmd[0]} did not answer `{' '.join(cmd[1:])}`: {type(exc).__name__}") from None
    if proc.returncode != 0:
        raise SeatRefused(f"{cmd[0]} `{' '.join(cmd[1:])}` exited {proc.returncode}: {proc.stderr.strip()[-200:]}")
    return proc.stdout + proc.stderr


def codex_home() -> Path:
    return Path(os.environ.get("CODEX_HOME") or (Path.home() / ".codex"))


def claude_env() -> dict:
    env = dict(os.environ)
    env.pop("CLAUDECODE", None)  # a seat launched from inside a Claude session must not inherit its marker
    return env


# ---------------------------------------------------------------- prompts
RULES = (
    "You are one of the parties in a parley. A neutral house script relays turns between the parties; the "
    "house has no opinion, edits nothing, and every party sees the same transcript of messages (not each "
    "other's private reasoning). Parties speak in a fixed rotation. The parley ends when ALL parties commit "
    "to the same resolution text, or at the turn cap, in which case it is recorded as unresolved. A verifier "
    "may veto an agreed resolution that does not answer the question; a veto appears in the transcript as a "
    "HOUSE note and the parties must resolve again. Argue for what you think is right; concede when another "
    "party is right; do not agree merely to be agreeable, and do not stall. When you are ready to commit, "
    "put the exact resolution text in resolved; to adopt the most recently proposed resolution verbatim, set "
    "accept to true. "
    "Reply with exactly one JSON object and nothing else, shaped "
    '{"message": "<to the other parties>", "position": "<your current position>", '
    '"accept": true or false, "resolved": "<exact resolution text>" or null}.'
)
REMINDER = ('Reply with exactly one JSON object and nothing else, shaped {"message": "...", "position": "...", '
            '"accept": true or false, "resolved": "<exact resolution text>" or null}.')

DISPLAY = {"codex": "Codex (OpenAI, via the Codex CLI)", "claude": "Claude (Anthropic, via Claude Code)",
           "grok": "Grok (xAI, via the API)"}


def parse_names(spec: str) -> dict[str, str]:
    """`codex=Astra,claude=Fable` -> {"codex": "Astra", "claude": "Fable"}; names may contain spaces."""
    names = {}
    for part in (spec or "").split(","):
        if not part.strip():
            continue
        if "=" not in part:
            raise SeatRefused(f"--names entries look like seat=Name; got {part!r}")
        seat, name = part.split("=", 1)
        if not seat.strip() or not name.strip():
            raise SeatRefused(f"--names entries look like seat=Name; got {part!r}")
        names[seat.strip()] = name.strip()
    return names


def render_transcript(turns: list[dict], notes: list[dict] | None = None) -> str:
    if not turns:
        return "(no turns yet; you speak first)"
    by_turn = {}
    for n in notes or []:
        by_turn.setdefault(n["after_turn"], []).append(n["text"])
    lines = []
    for t in turns:
        flag = f" [resolved: {t['resolved']}]" if t["resolved"] else ""
        lines.append(f"Turn {t['turn']} - {t['speaker'].upper()}{flag}:\n{t['message']}\n(position: {t['position']})")
        for text in by_turn.get(t["turn"], []):
            lines.append(f"HOUSE (after turn {t['turn']}): {text}")
    return "\n\n".join(lines)


def build_prompt(name: str, others: list[str], question: str, turns: list[dict], turn_no: int, max_turns: int,
                 notes: list[dict] | None = None, since: int | None = None, names: dict | None = None) -> str:
    """Full prompt (rules + question + whole transcript), or, for a stateful seat that already has the
    earlier turns in its own context, only the turns after `since` (its last turn)."""
    shown = {**DISPLAY, **(names or {})}
    me = f"{name} = {shown.get(name, name)}"
    them = "; ".join(f"{o} = {shown.get(o, o)}" for o in others)
    if since is None:
        return (f"{RULES}\n\nYou are {me}. The other parties: {them}. This is turn {turn_no} of at most {max_turns}.\n\n"
                f"QUESTION TO RESOLVE:\n{question}\n\nTRANSCRIPT SO FAR:\n{render_transcript(turns, notes)}\n\n"
                f"Reply now as {name}.")
    new = [t for t in turns if t["turn"] > since]
    new_notes = [n for n in (notes or []) if n["after_turn"] >= since]
    return (f"Parley continues. You are {name}. This is turn {turn_no} of at most {max_turns}. "
            f"The question and earlier turns are already in your context.\n\n"
            f"NEW TURNS SINCE YOUR LAST REPLY (turn {since}):\n{render_transcript(new, new_notes) if new else '(none)'}\n\n"
            f"{REMINDER}\n\nReply now as {name}.")


VERIFY_RULES = (
    "You are the verifier for a parley between AI parties. You will see the question that was put to them and the "
    "resolution text they all committed to. You may BLOCK a resolution; you never write or improve one. Judge only "
    "whether the resolution text answers the question as posed: it must be a committed answer to that question "
    "(not a restatement, a plan to answer later, a different question, or empty agreement). Do not judge whether "
    "it is the best answer. Reply with exactly one JSON object and nothing else, shaped "
    '{"answers": true or false, "reason": "<one or two sentences>", '
    '"falsifier": "<one sentence: the later observation that would show this resolution was wrong>"}.'
)


def build_verify_prompt(question: str, resolution: str) -> str:
    return f"{VERIFY_RULES}\n\nQUESTION PUT TO THE PARTIES:\n{question}\n\nAGREED RESOLUTION:\n{resolution}\n\nReply now."


def validate_verdict(value: dict) -> dict:
    if not isinstance(value.get("answers"), bool):
        raise ValueError("verdict needs boolean 'answers'")
    reason = value.get("reason")
    if not isinstance(reason, str) or not reason.strip():
        raise ValueError("verdict needs a non-empty 'reason'")
    falsifier = value.get("falsifier")
    return {"answers": value["answers"], "reason": reason.strip(),
            "falsifier": falsifier.strip() if isinstance(falsifier, str) and falsifier.strip() else None}


# ---------------------------------------------------------------- seats
def parse_codex_events(stdout: str) -> tuple[str, str, str | None]:
    """(agent text, joined reasoning, thread id) from `codex exec --json` output.
    Raises RuntimeError with Codex's own message when the stream carries an error or failed turn,
    so the ledger records why (wrong model for the account, quota, auth) instead of "no reply"."""
    reasoning, text, thread = [], "", None
    for line in stdout.splitlines():
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if event.get("type") == "thread.started":
            thread = event.get("thread_id")
        if event.get("type") in ("error", "turn.failed"):
            message = event.get("message") or (event.get("error") or {}).get("message") or "unknown error"
            try:  # Codex often nests a JSON error document in the message string
                inner = json.loads(message)
                message = (inner.get("error") or {}).get("message") or inner.get("message") or message
            except (json.JSONDecodeError, AttributeError):
                pass
            raise RuntimeError(f"codex: {str(message)[:300]}")
        item = event.get("item") or {}
        if item.get("type") == "reasoning":
            reasoning.append(item.get("text", ""))
        elif item.get("type") == "agent_message":
            text = item.get("text", "")
    return text, "\n".join(reasoning), thread


class CodexThreadSeat:
    """Resumes an existing Codex thread each turn; it keeps its own context and memory."""
    stateful = True

    def __init__(self, thread_id: str, timeout: float):
        self.thread_id, self.timeout = thread_id, timeout
        self.exe = shutil.which("codex.cmd") or shutil.which("codex")

    def admit(self) -> None:
        if not self.exe:
            raise SeatRefused("codex CLI not found on PATH")
        probe([self.exe, "--version"])
        if not glob.glob(str(codex_home() / "sessions" / "**" / f"rollout-*-{self.thread_id}.jsonl"), recursive=True):
            raise SeatRefused(f"no Codex session file for thread {self.thread_id} under {codex_home() / 'sessions'}")

    def reply(self, prompt: str, turn_no: int = 0) -> tuple[dict, str]:
        # exec-level options must precede the `resume` subcommand; resume takes only -c/-i/--last.
        cmd = [self.exe, "exec", "--json", "--skip-git-repo-check", "-s", "read-only",
               "-c", 'model_reasoning_summary="detailed"', "resume", self.thread_id, "-"]
        proc = run_cli(cmd, input_text=prompt, timeout=self.timeout)
        text, reasoning, _ = parse_codex_events(proc.stdout)
        if proc.returncode != 0 or not text:
            tail = proc.stderr.strip().splitlines()[-1] if proc.stderr.strip() else ""
            raise RuntimeError(f"codex returned no reply (exit {proc.returncode}) {tail}")
        return validate(extract_json(text)), reasoning


class CodexFreshSeat:
    """A fresh Codex model each turn (no prior thread); the house-supplied transcript is its only memory.
    Runs read-only in an empty directory so no repo context leaks in."""
    stateful = False

    def __init__(self, model: str, timeout: float, workdir: Path):
        self.model, self.timeout, self.workdir = model, timeout, workdir
        self.exe = shutil.which("codex.cmd") or shutil.which("codex")

    def admit(self) -> None:
        if not self.exe:
            raise SeatRefused("codex CLI not found on PATH")
        probe([self.exe, "--version"])

    def reply(self, prompt: str, turn_no: int = 0) -> tuple[dict, str]:
        self.workdir.mkdir(parents=True, exist_ok=True)
        cmd = [self.exe, "exec", "--json", "--skip-git-repo-check", "-s", "read-only", "-C", str(self.workdir)]
        if self.model:
            cmd += ["-m", self.model]
        cmd += ["-c", 'model_reasoning_summary="detailed"', "-c", 'model_reasoning_effort="high"', "-"]
        proc = run_cli(cmd, input_text=prompt, timeout=self.timeout)
        text, reasoning, _ = parse_codex_events(proc.stdout)
        if proc.returncode != 0 or not text:
            tail = proc.stderr.strip().splitlines()[-1] if proc.stderr.strip() else ""
            raise RuntimeError(f"codex returned no reply (exit {proc.returncode}) {tail}")
        return validate(extract_json(text)), reasoning


class GrokApiSeat:
    """xAI Grok through the public API (XAI_API_KEY); stateless per turn, transcript from the house."""
    stateful = False

    def __init__(self, model: str, timeout: float):
        self.model, self.timeout = model, timeout
        self.key = os.environ.get("XAI_API_KEY", "").strip()

    def admit(self) -> None:
        if not self.key:
            raise SeatRefused("XAI_API_KEY is not set")

    def reply(self, prompt: str, turn_no: int = 0) -> tuple[dict, str]:
        import urllib.error
        import urllib.request
        # Streamed: a non-streamed call on a long reasoning turn gets cut at ~60 s by an idle timeout
        # on the path; SSE chunks keep the connection alive. Content and reasoning deltas are joined.
        body = json.dumps({"model": self.model, "max_tokens": 8000, "stream": True,
                           "messages": [{"role": "user", "content": prompt}]}).encode("utf-8")
        req = urllib.request.Request("https://api.x.ai/v1/chat/completions", body,
                                     {"Authorization": f"Bearer {self.key}", "Content-Type": "application/json",
                                      "Accept": "text/event-stream"})
        content, reasoning = [], []
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as r:
                for raw in r:
                    line = raw.decode("utf-8", "replace").strip()
                    if not line.startswith("data:"):
                        continue
                    data = line[5:].strip()
                    if data == "[DONE]":
                        break
                    try:
                        event = json.loads(data)
                    except json.JSONDecodeError:
                        continue
                    for choice in event.get("choices", []):
                        delta = choice.get("delta") or {}
                        if delta.get("content"):
                            content.append(delta["content"])
                        if delta.get("reasoning_content"):
                            reasoning.append(delta["reasoning_content"])
        except urllib.error.HTTPError as exc:
            raise RuntimeError(f"xAI HTTP {exc.code}") from None
        except (urllib.error.URLError, OSError) as exc:
            raise RuntimeError(f"xAI transport failed ({type(exc).__name__})") from None
        text = "".join(content)
        if not text:
            raise RuntimeError("xAI stream produced no content")
        return validate(extract_json(text)), "".join(reasoning)


class ClaudeCliSeat:
    """A fresh Claude per turn via `claude -p`; no memory beyond the transcript the house sends."""
    stateful = False

    def __init__(self, model: str, timeout: float, workdir: Path):
        self.model, self.timeout, self.workdir = model, timeout, workdir
        self.exe = shutil.which("claude.cmd") or shutil.which("claude")

    def admit(self) -> None:
        if not self.exe:
            raise SeatRefused("claude CLI not found on PATH")
        probe([self.exe, "--version"], env=claude_env())

    def reply(self, prompt: str, turn_no: int = 0) -> tuple[dict, str]:
        self.workdir.mkdir(parents=True, exist_ok=True)
        cmd = [self.exe, "-p", "--output-format", "json"] + (["--model", self.model] if self.model else []) + [prompt]
        proc = run_cli(cmd, input_text="", timeout=self.timeout, cwd=self.workdir, env=claude_env())
        if proc.returncode != 0:
            raise RuntimeError(f"claude returned exit {proc.returncode}: {proc.stderr.strip()[-300:]}")
        result = json.loads(proc.stdout)
        return validate(extract_json(result.get("result", ""))), ""


class ClaudeResumeSeat:
    """One Claude Code session for the whole parley: `--session-id` on its first turn, `--resume` after.
    Symmetric with CodexThreadSeat: the model keeps its own context, so after its first turn the house
    sends only the new turns. Unanswered permission prompts are denied, never hung
    (`--permission-prompts none`, Claude Code >= 2.1.259; detected from --help at admission)."""
    stateful = True

    def __init__(self, model: str, timeout: float, workdir: Path, session_id: str | None = None):
        self.model, self.timeout, self.workdir = model, timeout, workdir
        self.exe = shutil.which("claude.cmd") or shutil.which("claude")
        self.session_id = session_id or str(uuid.uuid4())
        self.started = session_id is not None
        self.deny_prompts = False

    def admit(self) -> None:
        if not self.exe:
            raise SeatRefused("claude CLI not found on PATH")
        help_text = probe([self.exe, "--help"], env=claude_env())
        for flag in ("--session-id", "--resume"):
            if flag not in help_text:
                raise SeatRefused(f"this claude CLI has no {flag}; the resume seat needs it")
        self.deny_prompts = "--permission-prompts" in help_text
        if self.started and not glob.glob(str(Path.home() / ".claude" / "projects" / "*" / f"{self.session_id}.jsonl")):
            raise SeatRefused(f"no Claude session file found for {self.session_id}")

    def reply(self, prompt: str, turn_no: int = 0) -> tuple[dict, str]:
        self.workdir.mkdir(parents=True, exist_ok=True)
        cmd = [self.exe, "-p", "--output-format", "json"] + (["--model", self.model] if self.model else [])
        if self.deny_prompts:
            cmd += ["--permission-prompts", "none"]
        cmd += (["--resume", self.session_id] if self.started else ["--session-id", self.session_id]) + [prompt]
        proc = run_cli(cmd, input_text="", timeout=self.timeout, cwd=self.workdir, env=claude_env())
        if proc.returncode != 0:
            raise RuntimeError(f"claude returned exit {proc.returncode}: {proc.stderr.strip()[-300:]}")
        result = json.loads(proc.stdout)
        if result.get("is_error"):
            raise RuntimeError(f"claude reported an error: {str(result.get('result', ''))[:300]}")
        got = str(result.get("session_id", "")).lower()
        if got != self.session_id.lower():
            raise RuntimeError(f"claude answered in session {got or '?'}, expected {self.session_id}")
        self.started = True
        return validate(extract_json(result.get("result", ""))), ""


class MailboxSeat:
    """A live agent that reads its turn from a file and writes its reply to a file.

    Files: turn_NN_to_<name>.md (the house's delivery), turn_NN_from_<name>.json (the reply),
    PENDING_<name> (present while a reply is awaited; contains the turn number)."""
    stateful = False  # the live session has context, but it is the owner's session: send everything

    def __init__(self, mailbox: Path, timeout: float, name: str = "claude"):
        self.mailbox, self.timeout, self.name = mailbox, timeout, name

    def admit(self) -> None:
        pending = self.mailbox / f"PENDING_{self.name}"
        if pending.exists():
            raise SeatRefused(f"stale {pending} from an earlier run; delete it when that session is no longer watching")

    def reply(self, prompt: str, turn_no: int = 0) -> tuple[dict, str]:
        self.mailbox.mkdir(parents=True, exist_ok=True)
        to = self.mailbox / f"turn_{turn_no:02d}_to_{self.name}.md"
        back = self.mailbox / f"turn_{turn_no:02d}_from_{self.name}.json"
        pending = self.mailbox / f"PENDING_{self.name}"
        to.write_text(prompt + f"\n\nWrite your reply JSON to: {back.name}\n", encoding="utf-8")
        pending.write_text(f"{turn_no}\n", encoding="utf-8")
        print(f"  [mailbox:{self.name}] waiting for {back.name} (up to {self.timeout:.0f}s)", flush=True)
        deadline = time.monotonic() + self.timeout
        while time.monotonic() < deadline:
            if back.is_file():
                text = back.read_text(encoding="utf-8").strip()
                if text:
                    pending.unlink(missing_ok=True)
                    return validate(extract_json(text)), ""
            time.sleep(2)
        raise RuntimeError(f"mailbox reply for turn {turn_no} did not arrive in time")


class MockSeat:
    """Scripted seat. Default script: open with plan A, then commit to 'Plan A with a review step.',
    then accept whatever was proposed. Pass `script` (list of reply dicts) to drive tests; the last
    entry repeats."""
    stateful = False

    def __init__(self, name: str, script: list[dict] | None = None):
        self.name, self.n = name, 0
        self.script = script or [
            {"message": f"{name} opens: propose plan A.", "position": "plan A", "accept": False, "resolved": None},
            {"message": f"{name}: fine, plan A with a review step.", "position": "plan A + review",
             "accept": False, "resolved": "Plan A with a review step."},
            {"message": f"{name} accepts.", "position": "agree", "accept": True, "resolved": None},
        ]

    def admit(self) -> None:
        pass

    def reply(self, prompt: str, turn_no: int = 0) -> tuple[dict, str]:
        entry = self.script[min(self.n, len(self.script) - 1)]
        self.n += 1
        return validate(dict(entry)), "mock"


# ---------------------------------------------------------------- verifiers
class ClaudeVerifier:
    """Fresh Claude with every tool, MCP server, hook, skill and setting source disabled: it can read the
    question and the resolution and answer; it cannot act."""
    name = "claude"

    def __init__(self, model: str, timeout: float, workdir: Path):
        self.model, self.timeout, self.workdir = model, timeout, workdir
        self.exe = shutil.which("claude.cmd") or shutil.which("claude")

    def admit(self) -> None:
        if not self.exe:
            raise SeatRefused("verifier: claude CLI not found on PATH")
        help_text = probe([self.exe, "--help"], env=claude_env())
        for flag in ("--tools", "--strict-mcp-config", "--permission-mode", "--setting-sources", "--settings"):
            if flag not in help_text:
                raise SeatRefused(f"verifier: this claude CLI has no {flag}; cannot prove tools are denied")

    def verdict(self, question: str, resolution: str) -> dict:
        self.workdir.mkdir(parents=True, exist_ok=True)
        cmd = [self.exe, "-p", "--output-format", "json"] + (["--model", self.model] if self.model else []) + [
               "--tools", "", "--strict-mcp-config",
               "--permission-mode", "dontAsk", "--disable-slash-commands", "--setting-sources", "",
               "--settings", '{"disableAllHooks":true}', "--no-session-persistence",
               build_verify_prompt(question, resolution)]
        proc = run_cli(cmd, input_text="", timeout=self.timeout, cwd=self.workdir, env=claude_env())
        if proc.returncode != 0:
            raise RuntimeError(f"verifier claude exit {proc.returncode}: {proc.stderr.strip()[-300:]}")
        result = json.loads(proc.stdout)
        if result.get("is_error"):
            raise RuntimeError(f"verifier claude error: {str(result.get('result', ''))[:300]}")
        return validate_verdict(extract_json(result.get("result", "")))


class CodexVerifier:
    """Fresh Codex, ephemeral, user config ignored, read-only sandbox, shell tool off, no approvals."""
    name = "codex"

    def __init__(self, model: str, timeout: float, workdir: Path):
        self.model, self.timeout, self.workdir = model, timeout, workdir
        self.exe = shutil.which("codex.cmd") or shutil.which("codex")

    def admit(self) -> None:
        if not self.exe:
            raise SeatRefused("verifier: codex CLI not found on PATH")
        help_text = probe([self.exe, "exec", "--help"])
        for flag in ("--ephemeral", "--ignore-user-config", "--sandbox"):
            if flag not in help_text:
                raise SeatRefused(f"verifier: this codex CLI has no {flag}; cannot prove tools are denied")

    def verdict(self, question: str, resolution: str) -> dict:
        self.workdir.mkdir(parents=True, exist_ok=True)
        cmd = [self.exe, "exec", "--json", "--skip-git-repo-check", "--ephemeral", "--ignore-user-config",
               "--sandbox", "read-only", "-c", 'approval_policy="never"', "-c", "features.shell_tool=false",
               "-C", str(self.workdir)] + (["-m", self.model] if self.model else []) + ["-"]
        proc = run_cli(cmd, input_text=build_verify_prompt(question, resolution), timeout=self.timeout)
        text, _, _ = parse_codex_events(proc.stdout)
        if proc.returncode != 0 or not text:
            raise RuntimeError(f"verifier codex returned no verdict (exit {proc.returncode})")
        return validate_verdict(extract_json(text))


class MockVerifier:
    name = "mock"

    def __init__(self, verdicts: list[bool] | None = None, fail: bool = False):
        self.verdicts, self.fail, self.n = verdicts or [True], fail, 0

    def admit(self) -> None:
        pass

    def verdict(self, question: str, resolution: str) -> dict:
        if self.fail:
            raise RuntimeError("mock verifier failure")
        answers = self.verdicts[min(self.n, len(self.verdicts) - 1)]
        self.n += 1
        return {"answers": answers, "reason": "mock verdict", "falsifier": "mock falsifier"}


# ---------------------------------------------------------------- house
def admit_all(seats: dict, verifier=None) -> None:
    """Refuse the parley at turn 0 if any seat (or the verifier) cannot prove it will work."""
    for name, seat in seats.items():
        try:
            seat.admit()
        except SeatRefused as exc:
            raise SeatRefused(f"seat {name}: {exc}") from None
    if verifier is not None:
        try:
            verifier.admit()
        except SeatRefused as exc:
            raise SeatRefused(f"verifier: {exc}") from None


def load_run(out: Path) -> tuple[list[dict], list[dict]]:
    """(prior turns, house notes) from an existing run's ledger."""
    turns, notes = [], []
    for line in (out / "transcript.jsonl").read_text(encoding="utf-8").splitlines():
        rec = json.loads(line)
        if rec.get("event") == "turn":
            turns.append({k: rec[k] for k in ("turn", "speaker", "message", "position", "accept",
                                               "resolved", "reasoning", "time")})
        elif rec.get("event") == "vetoed":
            notes.append({"after_turn": rec["after_turn"], "text": rec["note"]})
    return turns, notes


def run_parley(question: str, seats: dict, order: list[str], out: Path, *, max_turns: int = 12, retries: int = 2,
               retry_delay: float = 10.0, verifier=None, resume: bool = False, config: dict | None = None,
               quiet: bool = False, names: dict | None = None) -> dict:
    """The house. Returns the result dict that is also written to result.json. Admission runs first;
    the run directory is only created once every seat is admitted."""
    if not question.strip():
        raise SeatRefused("a question is required")
    unknown = [s for s in order if s not in seats]
    if unknown or len(set(order)) != len(order) or not order:
        raise SeatRefused(f"order must list each configured seat once; got {order}, seats {list(seats)}")
    admit_all(seats, verifier)
    say = (lambda *a, **k: None) if quiet else (lambda *a, **k: print(*a, **k, flush=True))

    if resume:
        if not (out / "transcript.jsonl").is_file():
            raise SeatRefused(f"no transcript.jsonl in {out}")
        prior, notes = load_run(out)
        (out / "result.json").unlink(missing_ok=True)
    else:
        out.mkdir(parents=True, exist_ok=False)
        prior, notes = [], []
    stamp = out.name
    n_parties = len(order)
    ledger = (out / "transcript.jsonl").open("a", encoding="utf-8")
    if not resume:
        (out / "config.json").write_text(json.dumps({"question": question, "order": order, "max_turns": max_turns,
                                                     "verifier": getattr(verifier, "name", None), "names": names or {},
                                                     **(config or {}),
                                                     "started": utc()}, indent=2), encoding="utf-8")

    def emit(kind: str, **data):
        rec = {"time": utc(), "event": kind, **data}
        ledger.write(json.dumps(rec, ensure_ascii=False) + "\n")
        ledger.flush()

    def write_md(turns: list[dict], status: str, resolution: str | None):
        md = [f"# Parley - {stamp}", "", f"**Question:** {question}", "", f"**Status:** {status}"]
        if resolution:
            md += ["", f"**Resolution:** {resolution}"]
        md += [""]
        by_turn = {}
        for n in notes:
            by_turn.setdefault(n["after_turn"], []).append(n["text"])
        for t in turns:
            md += [f"## Turn {t['turn']} - {t['speaker']}", "", t["message"], "",
                   f"*Position:* {t['position']}  ", f"*Accept:* {t['accept']}  ",
                   f"*Resolved:* {t['resolved'] or '-'}", ""]
            for text in by_turn.get(t["turn"], []):
                md += [f"> **HOUSE:** {text}", ""]
        (out / "parley.md").write_text("\n".join(md), encoding="utf-8")

    say(f"Parley room: {out}\nQuestion: {question[:200]}\nSeats: {', '.join(f'{k}={type(v).__name__}' for k, v in seats.items())}; "
        f"order {order}; cap {max_turns} turns; verifier {getattr(verifier, 'name', 'none')}")
    turns: list[dict] = list(prior)
    emit("resumed" if resume else "started", question=question, order=order, prior_turns=len(turns))
    if prior:
        say(f"  resumed with {len(prior)} prior turn(s); next is turn {len(prior) + 1}")
    status, resolution, verdict, falsifier = "unresolved", None, None, None
    veto_after = max((n["after_turn"] for n in notes), default=0)
    replies_this_process = {name: 0 for name in seats}  # delta prompts only once a stateful seat has context
    last_spoke: dict[str, int] = {t["speaker"]: t["turn"] for t in turns}
    try:
        for turn_no in range(len(turns) + 1, max_turns + 1):
            speaker = order[(turn_no - 1) % n_parties]
            seat = seats[speaker]
            others = [s for s in order if s != speaker]
            since = last_spoke.get(speaker) if getattr(seat, "stateful", False) and replies_this_process[speaker] else None
            prompt = build_prompt(speaker, others, question, turns, turn_no, max_turns, notes, since, names)
            reply = None
            for attempt in range(1, retries + 2):
                # Recorded BEFORE the request: a crash or timeout mid-turn must still show that a
                # request was made and usage spent (the same rule as a payment written before it is sent).
                emit("turn_started", turn=turn_no, speaker=speaker, attempt=attempt, delta=since is not None)
                try:
                    reply, reasoning = seat.reply(prompt, turn_no)
                    break
                except (RuntimeError, ValueError, subprocess.TimeoutExpired, json.JSONDecodeError, OSError) as exc:
                    emit("turn_retry" if attempt <= retries else "turn_failed", turn=turn_no,
                         speaker=speaker, attempt=attempt, error=str(exc)[:500])
                    say(f"  turn {turn_no} ({speaker}) attempt {attempt} failed: {str(exc)[:160]}")
                    if attempt <= retries and retry_delay:
                        time.sleep(retry_delay * attempt)
            if reply is None:
                status = f"aborted: {speaker} failed on turn {turn_no}"
                break
            replies_this_process[speaker] += 1
            last_spoke[speaker] = turn_no
            if reply["accept"] and reply["resolved"] is None:
                reply["resolved"] = next((t["resolved"] for t in reversed(turns)
                                          if t["speaker"] != speaker and t["resolved"] and t["turn"] > veto_after), None)
            record = {"turn": turn_no, "speaker": speaker, **reply, "reasoning": reasoning, "time": utc()}
            turns.append(record)
            emit("turn", **record)
            flag = f"  -> resolved: {reply['resolved']}" if reply["resolved"] else ""
            say(f"  turn {turn_no} {speaker}: {reply['message'][:160]}{flag}")
            write_md(turns, "in progress", None)
            recent = [t for t in turns[-n_parties:] if t["turn"] > veto_after]
            if len(recent) == n_parties and all(t["resolved"] for t in recent) and \
                    len({norm(t["resolved"]) for t in recent}) == 1:
                candidate = turns[-1]["resolved"]
                emit("resolution_proposed", after_turn=turn_no, resolution=candidate)
                if verifier is None:
                    status, resolution = "resolved", candidate
                    break
                try:
                    verdict = verifier.verdict(question, candidate)
                except (RuntimeError, ValueError, subprocess.TimeoutExpired, json.JSONDecodeError, OSError) as exc:
                    # Fail closed: a verifier that cannot answer does not make the resolution verified.
                    verdict = {"answers": None, "reason": f"verifier failed: {str(exc)[:300]}", "falsifier": None}
                    emit("unverified", after_turn=turn_no, resolution=candidate, error=str(exc)[:500])
                    status, resolution = "resolved-unverified", candidate
                    say(f"  verifier failed; resolution recorded as UNVERIFIED: {str(exc)[:160]}")
                    break
                if verdict["answers"]:
                    emit("verified", after_turn=turn_no, resolution=candidate, reason=verdict["reason"],
                         falsifier=verdict["falsifier"])
                    status, resolution, falsifier = "resolved", candidate, verdict["falsifier"]
                    say(f"  verifier: answers the question. {verdict['reason'][:160]}")
                    break
                note = (f"The verifier blocked the agreed resolution \"{candidate}\": {verdict['reason']} "
                        f"Resolve again with text that answers the question as posed.")
                notes.append({"after_turn": turn_no, "text": note})
                veto_after = turn_no
                emit("vetoed", after_turn=turn_no, resolution=candidate, reason=verdict["reason"], note=note)
                say(f"  verifier VETO: {verdict['reason'][:160]}")
                write_md(turns, "in progress", None)
        else:
            status = f"unresolved after {max_turns} turns"
    except KeyboardInterrupt:
        status = "interrupted"
    write_md(turns, status, resolution)
    result = {"status": status, "resolution": resolution, "turns": len(turns), "finished": utc(),
              "verifier": None if verifier is None else {"name": verifier.name, **(verdict or {"answers": None, "reason": None, "falsifier": None})},
              "falsifier": falsifier,
              "outcome": {"status": "pending", "note": None, "recorded": None}}
    (out / "result.json").write_text(json.dumps(result, indent=2, ensure_ascii=False), encoding="utf-8")
    emit("finished", status=status, resolution=resolution)
    ledger.close()
    say(f"\n{status.upper()}" + (f": {resolution}" if resolution else ""))
    return result


def record_outcome(out: Path, status: str, note: str) -> dict:
    """Fill in what happened after the parley; appends an `outcome` event to the ledger."""
    path = out / "result.json"
    if not path.is_file():
        raise SeatRefused(f"no result.json in {out}")
    result = json.loads(path.read_text(encoding="utf-8"))
    result["outcome"] = {"status": status, "note": note, "recorded": utc()}
    path.write_text(json.dumps(result, indent=2, ensure_ascii=False), encoding="utf-8")
    with (out / "transcript.jsonl").open("a", encoding="utf-8") as ledger:
        ledger.write(json.dumps({"time": utc(), "event": "outcome", "status": status, "note": note,
                                 "resolution": result.get("resolution"), "falsifier": result.get("falsifier")},
                                ensure_ascii=False) + "\n")
    return result


# ---------------------------------------------------------------- cli
def build_from_args(args: argparse.Namespace) -> tuple[dict, list[str], object, Path, bool]:
    if args.resume:
        out = Path(args.resume).resolve()
    else:
        out = (args.out / datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")).resolve()
    mailbox = out / "mailbox"
    seats = {}
    if args.codex_seat == "thread":
        if not args.thread:
            raise SeatRefused("--thread is required for the codex thread seat")
        seats["codex"] = CodexThreadSeat(args.thread, args.turn_timeout)
    elif args.codex_seat == "mailbox":
        seats["codex"] = MailboxSeat(mailbox, args.mailbox_timeout, "codex")
    elif args.codex_seat == "fresh":
        seats["codex"] = CodexFreshSeat(args.codex_model, args.turn_timeout, out / ".codex-cwd")
    else:
        seats["codex"] = MockSeat("codex")
    if args.claude_seat == "mailbox":
        seats["claude"] = MailboxSeat(mailbox, args.mailbox_timeout, "claude")
    elif args.claude_seat == "cli":
        seats["claude"] = ClaudeCliSeat(args.claude_model, args.turn_timeout, out / ".claude-cwd")
    elif args.claude_seat == "resume":
        seats["claude"] = ClaudeResumeSeat(args.claude_model, args.turn_timeout, out / ".claude-cwd",
                                           args.claude_session or None)
    else:
        seats["claude"] = MockSeat("claude")
    if args.grok_seat == "api":
        seats["grok"] = GrokApiSeat(args.grok_model, args.turn_timeout)
    elif args.grok_seat == "mock":
        seats["grok"] = MockSeat("grok")
    order = [s.strip() for s in args.order.split(",") if s.strip()] if args.order else \
        (["codex", "claude"] if args.first == "codex" else ["claude", "codex"])
    if "grok" in seats and "grok" not in order:
        order.append("grok")
    verifier = None
    if args.verifier == "claude":
        verifier = ClaudeVerifier(args.verifier_model or "haiku", args.turn_timeout, out / ".verifier-cwd")
    elif args.verifier == "codex":
        verifier = CodexVerifier(args.verifier_model or args.codex_model, args.turn_timeout, out / ".verifier-cwd")
    elif args.verifier == "mock":
        verifier = MockVerifier()
    return seats, order, verifier, out, bool(args.resume)


def run(args: argparse.Namespace) -> int:
    question = args.question
    if question.startswith("@"):
        question = Path(question[1:]).read_text(encoding="utf-8").strip()
    seats, order, verifier, out, resume = build_from_args(args)
    config = {"thread": args.thread, "first": args.first, "codex_seat": args.codex_seat, "claude_seat": args.claude_seat,
              "grok_seat": args.grok_seat}
    result = run_parley(question, seats, order, out, max_turns=args.max_turns, retries=args.retries,
                        verifier=verifier, resume=resume, config=config, names=parse_names(args.names))
    if isinstance(seats.get("claude"), ClaudeResumeSeat):
        (out / "claude_session.txt").write_text(seats["claude"].session_id + "\n", encoding="utf-8")
    return 0 if result["status"] == "resolved" else 1


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run")
    r.add_argument("--question", required=True, help="Text, or @path to a file")
    r.add_argument("--thread", help="Codex thread id to resume for the codex seat")
    r.add_argument("--first", choices=["codex", "claude"], default="codex")
    r.add_argument("--max-turns", type=int, default=12)
    r.add_argument("--codex-seat", choices=["thread", "mailbox", "fresh", "mock"], default="fresh")
    r.add_argument("--codex-model", default="", help="Model for the fresh codex seat (default: the CLI configured model)")
    r.add_argument("--grok-seat", choices=["none", "api", "mock"], default="none",
                   help="api = xAI Grok via the public API (XAI_API_KEY), stateless per turn")
    r.add_argument("--grok-model", default="grok-4.7")
    r.add_argument("--order", default="", help="Comma-separated speaking order, e.g. codex,claude,grok")
    r.add_argument("--claude-seat", choices=["mailbox", "cli", "resume", "mock"], default="cli",
                   help="cli = fresh Claude per turn; resume = one Claude Code session for the whole parley")
    r.add_argument("--claude-model", default="", help="Model for the Claude seats (default: the CLI configured model)")
    r.add_argument("--names", default="", help="Display names the parties use for each other, e.g. codex=Astra,claude=Fable")
    r.add_argument("--claude-session", default="", help="Session id to resume for the resume seat (with --resume)")
    r.add_argument("--verifier", choices=["none", "claude", "codex", "mock"], default="claude",
                   help="Tool-less read-only model that may veto an agreed resolution; never writes one")
    r.add_argument("--verifier-model", default="", help="Model for the verifier (default haiku / the codex model)")
    r.add_argument("--turn-timeout", type=float, default=600.0, help="Seconds for a CLI seat to answer")
    r.add_argument("--mailbox-timeout", type=float, default=3600.0, help="Seconds to wait for the mailbox seat")
    r.add_argument("--out", type=Path, default=HERE / "runs")
    r.add_argument("--resume", default="", help="Existing run directory to continue from its transcript")
    r.add_argument("--retries", type=int, default=2, help="Retries per failed turn before aborting")
    o = sub.add_parser("outcome", help="Record what happened after a parley resolved")
    o.add_argument("run_dir", type=Path)
    o.add_argument("--status", required=True, choices=["held", "failed", "mixed", "moot"],
                   help="held = the resolution survived contact with reality; failed = the falsifier was observed")
    o.add_argument("--note", required=True, help="What was observed, with a pointer to the evidence")
    args = ap.parse_args(argv)
    try:
        if args.cmd == "outcome":
            result = record_outcome(args.run_dir.resolve(), args.status, args.note)
            print(json.dumps(result["outcome"], indent=2))
            return 0
        return run(args)
    except SeatRefused as exc:
        print(f"REFUSED: {exc}", flush=True)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
