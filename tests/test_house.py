"""House tests: mock seats only, no network, no CLIs. Run: python -m pytest tests -q"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import parley  # noqa: E402
from parley import MockSeat, MockVerifier, RULES, SeatRefused, run_parley  # noqa: E402


def events(out: Path) -> list[dict]:
    return [json.loads(l) for l in (out / "transcript.jsonl").read_text(encoding="utf-8").splitlines()]


def kinds(out: Path) -> list[str]:
    return [e["event"] for e in events(out)]


def reply(message="m", position="p", accept=False, resolved=None) -> dict:
    return {"message": message, "position": position, "accept": accept, "resolved": resolved}


class FailingSeat(MockSeat):
    def reply(self, prompt, turn_no=0):
        raise RuntimeError("boom")


class RefusingSeat(MockSeat):
    def admit(self):
        raise SeatRefused("not today")


class RecordingSeat(MockSeat):
    def __init__(self, name, script=None, stateful=False):
        super().__init__(name, script)
        self.stateful = stateful
        self.prompts = []

    def reply(self, prompt, turn_no=0):
        self.prompts.append(prompt)
        return super().reply(prompt, turn_no)


def house(tmp_path, seats, order=None, **kw):
    kw.setdefault("retry_delay", 0)
    kw.setdefault("quiet", True)
    out = tmp_path / "run"
    return run_parley("Which plan?", seats, order or list(seats), out, **kw), out


# ---------------------------------------------------------------- resolution rule
def test_resolves_when_both_commit_to_same_text(tmp_path):
    result, out = house(tmp_path, {"a": MockSeat("a"), "b": MockSeat("b")})
    assert result["status"] == "resolved"
    assert result["resolution"] == "Plan A with a review step."
    assert result["turns"] == 4
    assert result["outcome"] == {"status": "pending", "note": None, "recorded": None}


def test_turn_started_precedes_every_turn(tmp_path):
    _, out = house(tmp_path, {"a": MockSeat("a"), "b": MockSeat("b")})
    k = kinds(out)
    assert k[0] == "started" and k[-1] == "finished"
    turns = [i for i, e in enumerate(k) if e == "turn"]
    assert turns and all(k[i - 1] == "turn_started" for i in turns)
    assert k.count("turn_started") == k.count("turn") == 4


def test_accept_adopts_latest_proposal_from_another_party(tmp_path):
    a = MockSeat("a", [reply(resolved="Go left."), reply(resolved="Go left.")])
    b = MockSeat("b", [reply(accept=True)])
    result, out = house(tmp_path, {"a": a, "b": b})
    assert result["status"] == "resolved" and result["resolution"] == "Go left."
    assert [e for e in events(out) if e["event"] == "turn"][1]["resolved"] == "Go left."


def test_normalised_match_ignores_case_space_and_trailing_period(tmp_path):
    a = MockSeat("a", [reply(resolved="Go  Left.")])
    b = MockSeat("b", [reply(resolved="go left")])
    result, _ = house(tmp_path, {"a": a, "b": b})
    assert result["status"] == "resolved" and result["turns"] == 2


def test_three_parties_all_must_commit(tmp_path):
    a = MockSeat("a", [reply(resolved="X")])
    b = MockSeat("b", [reply(resolved="X")])
    c = MockSeat("c", [reply(), reply(resolved="X")])
    result, _ = house(tmp_path, {"a": a, "b": b, "c": c})
    assert result["status"] == "resolved" and result["turns"] == 6


def test_unresolved_at_cap(tmp_path):
    a = MockSeat("a", [reply(resolved="X")])
    b = MockSeat("b", [reply(resolved="Y")])
    result, _ = house(tmp_path, {"a": a, "b": b}, max_turns=4)
    assert result["status"] == "unresolved after 4 turns" and result["resolution"] is None


# ---------------------------------------------------------------- failure paths
def test_retry_then_abort_is_on_the_ledger(tmp_path):
    result, out = house(tmp_path, {"a": FailingSeat("a"), "b": MockSeat("b")}, retries=2)
    assert result["status"] == "aborted: a failed on turn 1"
    k = kinds(out)
    assert k.count("turn_started") == 3 and k.count("turn_retry") == 2 and k.count("turn_failed") == 1


def test_admission_refuses_before_run_dir_exists(tmp_path):
    out = tmp_path / "run"
    with pytest.raises(SeatRefused, match="seat b: not today"):
        run_parley("q", {"a": MockSeat("a"), "b": RefusingSeat("b")}, ["a", "b"], out, quiet=True)
    assert not out.exists()


def test_bad_order_refused(tmp_path):
    with pytest.raises(SeatRefused):
        run_parley("q", {"a": MockSeat("a")}, ["a", "zz"], tmp_path / "run", quiet=True)


# ---------------------------------------------------------------- resume
def test_resume_continues_from_transcript(tmp_path):
    out = tmp_path / "run"
    first = run_parley("Which plan?", {"a": MockSeat("a"), "b": MockSeat("b")}, ["a", "b"], out,
                       max_turns=2, retry_delay=0, quiet=True)
    assert first["status"].startswith("unresolved") and first["turns"] == 2
    # Fresh seat objects (as a real resume would have); scripts positioned past the first two replies.
    a = MockSeat("a", [reply(resolved="Plan A with a review step.")])
    b = MockSeat("b", [reply(accept=True)])
    second = run_parley("Which plan?", {"a": a, "b": b}, ["a", "b"], out, max_turns=6, resume=True,
                        retry_delay=0, quiet=True)
    assert second["status"] == "resolved" and second["turns"] == 4
    k = kinds(out)
    assert k.count("started") == 1 and k.count("resumed") == 1 and k.count("finished") == 2
    assert [e["turn"] for e in events(out) if e["event"] == "turn"] == [1, 2, 3, 4]


# ---------------------------------------------------------------- verifier
def test_verified_resolution_records_falsifier(tmp_path):
    result, out = house(tmp_path, {"a": MockSeat("a"), "b": MockSeat("b")}, verifier=MockVerifier([True]))
    assert result["status"] == "resolved"
    assert result["falsifier"] == "mock falsifier"
    assert result["verifier"] == {"name": "mock", "answers": True, "reason": "mock verdict", "falsifier": "mock falsifier"}
    k = kinds(out)
    assert "resolution_proposed" in k and "verified" in k and "vetoed" not in k


def test_veto_blocks_then_parties_resolve_again(tmp_path):
    a = MockSeat("a", [reply(resolved="Plan A"), reply(resolved="Plan B")])
    b = MockSeat("b", [reply(resolved="Plan A"), reply(resolved="Plan B")])
    a_rec = RecordingSeat("a", a.script)
    result, out = house(tmp_path, {"a": a_rec, "b": b}, verifier=MockVerifier([False, True]), max_turns=8)
    assert result["status"] == "resolved" and result["resolution"] == "Plan B"
    assert result["turns"] == 4
    k = kinds(out)
    assert k.count("resolution_proposed") == 2 and k.count("vetoed") == 1 and k.count("verified") == 1
    veto = next(e for e in events(out) if e["event"] == "vetoed")
    assert veto["after_turn"] == 2 and veto["resolution"] == "Plan A"
    # The veto reached the parties as a HOUSE note in the next prompt.
    assert "HOUSE (after turn 2)" in a_rec.prompts[1] and "blocked" in a_rec.prompts[1]
    assert "HOUSE" in (out / "parley.md").read_text(encoding="utf-8")


def test_veto_does_not_let_accept_adopt_the_vetoed_text(tmp_path):
    # After a veto, accept=true must not silently re-adopt the text that was just blocked.
    a = MockSeat("a", [reply(resolved="Plan A"), reply(accept=True)])
    b = MockSeat("b", [reply(resolved="Plan A"), reply(accept=True)])
    result, _ = house(tmp_path, {"a": a, "b": b}, verifier=MockVerifier([False, True]), max_turns=4)
    assert result["status"].startswith("unresolved")


def test_verifier_failure_fails_closed(tmp_path):
    result, out = house(tmp_path, {"a": MockSeat("a"), "b": MockSeat("b")}, verifier=MockVerifier(fail=True))
    assert result["status"] == "resolved-unverified"
    assert result["resolution"] == "Plan A with a review step."
    assert result["falsifier"] is None and result["verifier"]["answers"] is None
    assert "unverified" in kinds(out) and "verified" not in kinds(out)


def test_resume_after_veto_keeps_the_veto(tmp_path):
    out = tmp_path / "run"
    a = MockSeat("a", [reply(resolved="Plan A")])
    b = MockSeat("b", [reply(resolved="Plan A")])
    first = run_parley("q", {"a": a, "b": b}, ["a", "b"], out, verifier=MockVerifier([False]), max_turns=2,
                       retry_delay=0, quiet=True)
    assert first["status"].startswith("unresolved") and "vetoed" in kinds(out)
    a2 = MockSeat("a", [reply(accept=True)])
    b2 = MockSeat("b", [reply(accept=True)])
    second = run_parley("q", {"a": a2, "b": b2}, ["a", "b"], out, verifier=MockVerifier([True]), max_turns=4,
                        resume=True, retry_delay=0, quiet=True)
    # accept=true cannot adopt the vetoed "Plan A" from before the veto: still unresolved.
    assert second["status"].startswith("unresolved")


# ---------------------------------------------------------------- delta prompts
def test_stateful_seat_gets_delta_after_first_turn(tmp_path):
    a = RecordingSeat("a", [reply(), reply(resolved="X")], stateful=True)
    b = RecordingSeat("b", [reply(), reply(resolved="X")], stateful=False)
    result, out = house(tmp_path, {"a": a, "b": b})
    assert result["status"] == "resolved"
    assert RULES in a.prompts[0] and "QUESTION TO RESOLVE" in a.prompts[0]
    assert RULES not in a.prompts[1] and "NEW TURNS SINCE YOUR LAST REPLY (turn 1)" in a.prompts[1]
    assert "Turn 2 - B" in a.prompts[1] and "Turn 1 - A" not in a.prompts[1]
    assert all(RULES in p for p in b.prompts)
    starts = [e for e in events(out) if e["event"] == "turn_started"]
    assert [e["delta"] for e in starts] == [False, False, True, False]


def test_stateful_seat_gets_full_prompt_on_first_turn_of_a_resumed_run(tmp_path):
    out = tmp_path / "run"
    run_parley("q", {"a": MockSeat("a"), "b": MockSeat("b")}, ["a", "b"], out, max_turns=2, retry_delay=0, quiet=True)
    a = RecordingSeat("a", [reply(resolved="X")], stateful=True)
    b = RecordingSeat("b", [reply(resolved="X")], stateful=True)
    run_parley("q", {"a": a, "b": b}, ["a", "b"], out, resume=True, retry_delay=0, quiet=True)
    assert RULES in a.prompts[0] and "Turn 1 - A" in a.prompts[0]


# ---------------------------------------------------------------- outcome
def test_outcome_subcommand_updates_result_and_ledger(tmp_path):
    _, out = house(tmp_path, {"a": MockSeat("a"), "b": MockSeat("b")}, verifier=MockVerifier([True]))
    code = parley.main(["outcome", str(out), "--status", "failed", "--note", "falsifier observed, see X"])
    assert code == 0
    result = json.loads((out / "result.json").read_text(encoding="utf-8"))
    assert result["outcome"]["status"] == "failed" and result["outcome"]["recorded"]
    last = events(out)[-1]
    assert last["event"] == "outcome" and last["falsifier"] == "mock falsifier"


def test_outcome_on_missing_run_is_refused(tmp_path):
    assert parley.main(["outcome", str(tmp_path / "nope"), "--status", "held", "--note", "x"]) == 2


# ---------------------------------------------------------------- parsing
def test_extract_json_from_fenced_text():
    text = 'Sure:\n```json\n{"message": "hi", "position": "p", "accept": false, "resolved": null}\n```'
    assert parley.validate(parley.extract_json(text))["message"] == "hi"


def test_validate_verdict_requires_bool_and_reason():
    with pytest.raises(ValueError):
        parley.validate_verdict({"answers": "yes", "reason": "r"})
    v = parley.validate_verdict({"answers": False, "reason": " r ", "falsifier": ""})
    assert v == {"answers": False, "reason": "r", "falsifier": None}


# ---------------------------------------------------------------- process tree kill
def _pid_alive(pid: int) -> bool:
    if os.name == "nt":
        out = subprocess.run(["tasklist", "/FI", f"PID eq {pid}", "/NH"], capture_output=True, text=True).stdout
        return str(pid) in out
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def test_run_cli_timeout_kills_grandchild(tmp_path):
    marker = tmp_path / "child.pid"
    child = ("import subprocess,sys,time,pathlib\n"
             "p=subprocess.Popen([sys.executable,'-c','import time;time.sleep(120)'])\n"
             f"pathlib.Path({str(marker)!r}).write_text(str(p.pid))\n"
             "time.sleep(120)\n")
    with pytest.raises(subprocess.TimeoutExpired):
        parley.run_cli([sys.executable, "-c", child], input_text="", timeout=3)
    assert marker.is_file(), "parent never started the grandchild"
    pid = int(marker.read_text())
    deadline = time.monotonic() + 5
    while _pid_alive(pid) and time.monotonic() < deadline:
        time.sleep(0.1)
    assert not _pid_alive(pid), "grandchild survived the timeout kill"


# ---------------------------------------------------------------- names
def test_parse_names():
    assert parley.parse_names("codex=Astra, claude=Fable the Second") == {"codex": "Astra", "claude": "Fable the Second"}
    assert parley.parse_names("") == {}
    with pytest.raises(SeatRefused):
        parley.parse_names("codex")


def test_names_reach_the_prompt_and_config(tmp_path):
    a = RecordingSeat("codex", [reply(resolved="X")])
    b = RecordingSeat("claude", [reply(resolved="X")])
    _, out = house(tmp_path, {"codex": a, "claude": b}, names={"codex": "Astra"})
    assert "You are codex = Astra" in a.prompts[0]
    assert "claude = Claude (Anthropic, via Claude Code)" in a.prompts[0]
    assert json.loads((out / "config.json").read_text(encoding="utf-8"))["names"] == {"codex": "Astra"}


def test_default_display_names_are_generic():
    assert all(name not in parley.DISPLAY[k] for k in parley.DISPLAY for name in ("Astra", "Fable", "4.7"))


# ---------------------------------------------------------------- codex stream parsing
def test_parse_codex_events_surfaces_the_cli_error():
    stream = ('{"type":"thread.started","thread_id":"abc"}\n{"type":"turn.started"}\n'
              '{"type":"error","message":"{\\"type\\":\\"error\\",\\"status\\":400,\\"error\\":{\\"type\\":\\"invalid_request_error\\",'
              '\\"message\\":\\"The model is not supported with this account.\\"}}"}\n')
    with pytest.raises(RuntimeError, match="codex: The model is not supported with this account."):
        parley.parse_codex_events(stream)


def test_parse_codex_events_reads_text_reasoning_and_thread():
    stream = ('{"type":"thread.started","thread_id":"abc"}\n'
              '{"type":"item.completed","item":{"type":"reasoning","text":"thinking"}}\n'
              '{"type":"item.completed","item":{"type":"agent_message","text":"{\\"message\\":\\"hi\\"}"}}\n'
              '{"type":"turn.completed"}\n')
    assert parley.parse_codex_events(stream) == ('{"message":"hi"}', "thinking", "abc")
