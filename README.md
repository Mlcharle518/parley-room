# Parley room

A neutral space where two or more agents negotiate to a resolution. The house is `parley.py`, a plain
stdlib Python script with no model and no opinion: it holds the shared transcript, rotates turns,
delivers only what the other parties wrote, checks the resolution rule, and stops.

What it adds beyond "send one prompt to several models":

- **A reply contract.** Every turn is one JSON object with a message, a position, an accept flag and an
  exact resolution text. The house validates it; a malformed reply is retried, then the parley aborts.
- **A resolution rule.** The parley ends only when every party commits to the same text.
- **A verifier that may block, never write.** A fresh, tool-less, read-only model is asked one question
  after the parties agree: does this text answer the question as posed? A veto goes into the transcript
  as a HOUSE note and the parties must resolve again. If the verifier itself fails, the resolution is
  recorded as *unverified*, never as verified.
- **A ledger written before the request.** `turn_started` is recorded before each seat is called, so a
  crash or timeout mid-turn still shows the request was made and usage spent.
- **Admission before turn 0.** Every seat is probed before the run directory exists: missing CLI, unknown
  Codex thread, missing Claude session, stale mailbox file. A seat that cannot prove it will work refuses
  the parley instead of failing on turn 7.
- **An outcome field.** A resolution is a claim. `result.json` carries the verifier's falsifier and a
  pending outcome that `python parley.py outcome` fills in later, so a parley can be scored against what
  actually happened.

## Seats

| Flag | Seat | What it is | How it's driven |
|---|---|---|---|
| `--codex-seat thread` | Codex thread | An existing Codex CLI thread, with all its context (stateful) | `codex exec resume <thread-id>` each turn, read-only sandbox. Admission requires the thread's session file under `$CODEX_HOME/sessions`. Fails with "already has an active writer" while that thread is open interactively; use the mailbox seat then |
| `--codex-seat fresh` | Codex fresh | A fresh Codex model each turn, no prior context | `codex exec` read-only in an empty directory |
| `--codex-seat mailbox` | Codex mailbox | A live interactive Codex session | The house writes `mailbox/turn_NN_to_codex.md` and waits for `turn_NN_from_codex.json`; the session watches `PENDING_codex` |
| `--claude-seat mailbox` | Claude mailbox | A live Claude Code session, with all its context | Same files with `claude` in the name; the session watches `PENDING_claude` |
| `--claude-seat cli` | Claude fresh | A fresh Claude with no prior context | `claude -p` each turn |
| `--claude-seat resume` | Claude resume | One Claude Code session for the whole parley (stateful) | `claude -p --session-id <uuid>` on its first turn, `--resume <uuid>` after; unanswered permission prompts are denied (`--permission-prompts none`). The id is written to `runs/<stamp>/claude_session.txt`; pass it back with `--claude-session` when resuming a run |
| `--grok-seat api` | Grok | xAI Grok through the public API (`XAI_API_KEY`), stateless | streamed chat completion each turn |
| `--*-seat mock` | Mock | Scripted | For testing the house |

Stateful seats keep their own context, so after their first turn they receive only the turns since they
last spoke (plus any HOUSE note). Stateless seats get the rules, the question and the whole transcript
every turn. On a resumed run every seat gets the full prompt on its first turn in that process.

## Verifier

```
--verifier claude        fresh Claude, --tools "" --strict-mcp-config --permission-mode dontAsk,
                         no hooks, no setting sources, no session persistence (default model haiku)
--verifier codex         fresh Codex, --ephemeral --ignore-user-config --sandbox read-only, shell tool off
--verifier mock          scripted, for tests
```

The verifier sees only the question and the agreed text, not the transcript, so it cannot be argued into
a verdict. Admission checks that the CLI actually exposes the flags that deny tools; if it does not, the
verifier is refused rather than run with unknown powers. It answers `{"answers": bool, "reason": ...,
"falsifier": "<the later observation that would show this resolution was wrong>"}`.

## Run

```powershell
cd parley-room
python parley.py run --question "Which is the better default: fail open or fail closed?" --codex-seat fresh --claude-seat cli --verifier claude
```

`--question` takes text or `@file`. `--first` says who opens; `--order codex,claude,grok` sets a full
rotation. The mailbox seat waits up to `--mailbox-timeout` seconds (default one hour) for each reply,
so that session must be watching the mailbox (see below). Watch the parley live with:

```powershell
Get-Content runs\<stamp>\parley.md -Wait
```

Resume an interrupted run with `--resume runs\<stamp>` (add `--claude-session <id>` for the resume seat).
Exit code 0 = resolved and verified; 1 = unresolved, aborted or unverified; 2 = refused at admission.

## Reply contract

Every seat answers every turn with exactly one JSON object:

```json
{"message": "<to the other parties>", "position": "<current position>", "accept": false, "resolved": null}
```

- `resolved` is the exact resolution text a party commits to.
- `accept: true` with `resolved: null` adopts the most recently proposed `resolved` text from another party.
  Text proposed before a veto cannot be adopted this way.
- The parley ends when the latest turn from every party (all after any veto) carries matching `resolved`
  text and the verifier, if any, does not block it; or at `--max-turns` as unresolved. A failed turn is
  retried (`--retries`, default 2) and then aborts the parley.

## Records

`runs/<stamp>/` holds:

- `config.json` — question, order, seats, verifier.
- `transcript.jsonl` — the ledger: `started`/`resumed`, `turn_started` (before each request, with attempt
  number and whether the prompt was a delta), `turn`, `turn_retry`/`turn_failed`, `resolution_proposed`,
  `verified`/`vetoed`/`unverified`, `finished`, and later `outcome`.
- `parley.md` — readable transcript, updated each turn, with HOUSE notes for vetoes.
- `result.json` — status, resolution, verifier verdict, falsifier, outcome.
- `mailbox/` for live seats; `claude_session.txt` for the resume seat.

## Recording the outcome

```powershell
python parley.py outcome runs\<stamp> --status held --note "shipped as agreed; see <evidence path>"
python parley.py outcome runs\<stamp> --status failed --note "falsifier observed: <what happened>"
```

Statuses: `held`, `failed`, `mixed`, `moot`. The ledger gets an `outcome` event next to the resolution and
its falsifier.

## Driving the mailbox seat from a Claude Code session

In the Claude session: watch `runs/<stamp>/mailbox/PENDING_claude`; when it names turn N, read
`turn_NN_to_claude.md`, write the reply JSON to `turn_NN_from_claude.json`. A live Codex session
does the same with `PENDING_codex` / `turn_NN_to_codex.md` / `turn_NN_from_codex.json`. Paste the
session this instruction: "Watch <mailbox dir>. Whenever PENDING_<you> exists, read the turn file it
names, reply with exactly the JSON the file asks for into the from-file, then keep watching until
result.json appears in the run directory." A leftover `PENDING_*` file from an earlier run is refused at
admission; delete it once that session has stopped watching.

## Tests and offline check

```powershell
python -m pytest tests -q
python parley.py run --question "test" --codex-seat mock --claude-seat mock --verifier mock --out runs\mock
```

The tests use mock seats only: resolution rule, accept adoption, three parties, retry and abort, resume,
veto and re-resolution, fail-closed verifier, delta prompts, the outcome subcommand, and the process-tree
kill on timeout (the CLIs are `.cmd` wrappers around node on Windows; killing the wrapper alone leaves
the model call running).

## Limits

A verified resolution is still only an agreement between models that answers the question as posed. It is
not evidence that the resolution is right; the outcome field exists because that is decided later. The
Grok seat and both CLIs ride the owner's accounts and limits.
