"""The proposals store: `<workspace>/state/proposals.json` (issue #21).

A proposal is the unit of approval-gated work. The `approval` destination
records one at the end of an `agent run`, and the `agent approvals` pass later
flips it to `done`, `skipped` or `failed` after a trusted human has replied in
Slack. Both are run-and-exit processes on independent launchd clocks, so the
file is the only thing they share.

Shape on disk:

    {
      "next_id": 3,
      "proposals": [
        {"id": "p1-k3f", "created": "...", "created_ts": 1757700000.0,
         "action": "publish-post",
         "payload": {"title": "...", "body_md": "..."},
         "status": "done", "decided_by": "U0123456789", "decided_ts": "...",
         "claimed_at": "...", "claimed_pid": 4242, "resolved": "...",
         "results": [{"step": 1, "type": "slack-post", "status": "ok", "result": {}}]},
        {"id": "p2-x9q", ..., "status": "pending"}
      ]
    }

Ids are short, monotonic per workspace, and not guessable in advance: a
base-36 counter with a `p` prefix, then a dash and three random base-36
characters. The counter keeps them readable in order; the suffix means nobody
can type `do p10` into the channel today and have it approve whatever becomes
p10 next week. A reply has to quote the whole id. Nothing is ever deleted; a
resolved proposal stays as the audit trail of what was posted where, which is
the record an operator asks for when a published post needs explaining.

Statuses: `pending` -> `running` -> `done` | `failed`, or `pending` ->
`skipped`. `running` exists so that executing a proposal is an atomic claim:
`transition()` compares the current status under the lock and refuses to
change a proposal that is not in the expected state, so two approvals passes
that both read the same pending proposal cannot both run its steps. The one
whose claim fails sees None and moves on. A proposal still `running` when a
pass starts means an earlier pass died mid-steps; it is reported, never
re-run automatically (see `framework/approvals.py`).

Two processes can touch the file within the same second (a run recording a
proposal while the approvals pass flips another one), so every read-modify-
write holds an `fcntl` lock on a sibling `.lock` file and the write is a
rename of a temp file. Without that, whichever process saved last would
silently discard the other's change: a proposal that was never posted about,
or a `done` that reverted to `pending` and executed twice. Cheap insurance
against the worst kind of failure this store can have.
"""
from __future__ import annotations

import fcntl
import json
import os
import secrets
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

STATE_DIRNAME = "state"
PROPOSALS_FILENAME = "proposals.json"

STATUS_PENDING = "pending"
STATUS_RUNNING = "running"
STATUS_DONE = "done"
STATUS_SKIPPED = "skipped"
STATUS_FAILED = "failed"

_ID_PREFIX = "p"
_DIGITS = "0123456789abcdefghijklmnopqrstuvwxyz"
_SUFFIX_LEN = 3
_TERMINAL = (STATUS_DONE, STATUS_SKIPPED, STATUS_FAILED)


def _base36(n: int) -> str:
    if n == 0:
        retval = "0"
        return retval
    out = []
    while n:
        n, rem = divmod(n, 36)
        out.append(_DIGITS[rem])
    retval = "".join(reversed(out))
    return retval


def _now() -> str:
    retval = datetime.now(timezone.utc).isoformat(timespec="seconds")
    return retval


def _suffix() -> str:
    retval = "".join(secrets.choice(_DIGITS) for _ in range(_SUFFIX_LEN))
    return retval


def created_ts(proposal: dict[str, Any]) -> float:
    """Epoch seconds the proposal was recorded, for comparing with Slack `ts`."""
    raw = proposal.get("created_ts")
    if raw is None:
        retval = datetime.fromisoformat(proposal["created"]).timestamp()
    else:
        retval = float(raw)
    return retval


def proposals_path(workspace: str | os.PathLike) -> Path:
    retval = Path(workspace) / STATE_DIRNAME / PROPOSALS_FILENAME
    return retval


class ProposalStore:
    def __init__(self, path: Path):
        self.path = Path(path)
        self._lock_path = self.path.with_suffix(self.path.suffix + ".lock")

    @contextmanager
    def _locked(self) -> Iterator[None]:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with open(self._lock_path, "w") as lock_fh:
            fcntl.flock(lock_fh, fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(lock_fh, fcntl.LOCK_UN)

    def _read(self) -> dict[str, Any]:
        if not self.path.exists():
            retval = {"next_id": 1, "proposals": []}
            return retval
        raw = json.loads(self.path.read_text())
        if not isinstance(raw, dict) or not isinstance(raw.get("proposals"), list):
            raise ValueError(f"{self.path}: not a proposals store")
        retval = raw
        return retval

    def _write(self, data: dict[str, Any]) -> None:
        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        tmp.write_text(json.dumps(data, indent=2, sort_keys=True))
        os.replace(tmp, self.path)

    def record(self, action: str, payload: dict[str, Any]) -> dict[str, Any]:
        """Append a pending proposal and return it (with its new id)."""
        now = datetime.now(timezone.utc)
        with self._locked():
            data = self._read()
            proposal = {
                "id": f"{_ID_PREFIX}{_base36(int(data['next_id']))}-{_suffix()}",
                "created": now.isoformat(timespec="seconds"),
                "created_ts": now.timestamp(),
                "action": action,
                "payload": payload,
                "status": STATUS_PENDING,
            }
            data["next_id"] = int(data["next_id"]) + 1
            data["proposals"].append(proposal)
            self._write(data)
        retval = proposal
        return retval

    def with_status(self, status: str) -> list[dict[str, Any]]:
        with self._locked():
            data = self._read()
        retval = [p for p in data["proposals"] if p.get("status") == status]
        return retval

    def get(self, proposal_id: str) -> dict[str, Any] | None:
        """One proposal by id, or None. A read, so no claim and no lock held."""
        with self._locked():
            data = self._read()
        retval = None
        for proposal in data["proposals"]:
            if proposal.get("id") == proposal_id:
                retval = proposal
                break
        return retval

    def pending(self) -> list[dict[str, Any]]:
        retval = self.with_status(STATUS_PENDING)
        return retval

    def running(self) -> list[dict[str, Any]]:
        retval = self.with_status(STATUS_RUNNING)
        return retval

    def transition(
        self, proposal_id: str, expect: str, status: str, **fields: Any
    ) -> dict[str, Any] | None:
        """Compare-and-set a proposal's status under the lock.

        Returns the updated proposal, or None when its current status is not
        `expect` -- which is how a second approvals pass learns that the first
        one already claimed (or resolved) it and must not touch it. `expect ==
        status` is allowed and just records `fields` (per-step progress while
        running). A terminal status also stamps `resolved`. An unknown id is
        an error: that is a corrupted store, not a race.
        """
        with self._locked():
            data = self._read()
            for proposal in data["proposals"]:
                if proposal.get("id") == proposal_id:
                    break
            else:
                raise KeyError(f"no proposal {proposal_id!r} in {self.path}")
            if proposal.get("status") != expect:
                retval = None
                return retval
            proposal["status"] = status
            if status in _TERMINAL:
                proposal["resolved"] = _now()
            proposal.update(fields)
            self._write(data)
        retval = proposal
        return retval
