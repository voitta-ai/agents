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
        {"id": "p1", "created": "...", "action": "publish-post",
         "payload": {"title": "...", "body_md": "..."},
         "status": "done", "decided_by": "U0123456789", "decided_ts": "...",
         "resolved": "...", "results": [{"step": 1, "type": "slack-post",
         "status": "ok", "result": {}}]},
        {"id": "p2", ..., "status": "pending"}
      ]
    }

Ids are short and monotonic per workspace: a base-36 counter with a `p`
prefix, so `do p1` reads as a reply to a proposal and not as a number. Nothing
is ever deleted; a resolved proposal stays as the audit trail of what was
posted where, which is the record an operator asks for when a published post
needs explaining.

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
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

STATE_DIRNAME = "state"
PROPOSALS_FILENAME = "proposals.json"

STATUS_PENDING = "pending"
STATUS_DONE = "done"
STATUS_SKIPPED = "skipped"
STATUS_FAILED = "failed"

_ID_PREFIX = "p"
_DIGITS = "0123456789abcdefghijklmnopqrstuvwxyz"


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
        with self._locked():
            data = self._read()
            proposal = {
                "id": f"{_ID_PREFIX}{_base36(int(data['next_id']))}",
                "created": _now(),
                "action": action,
                "payload": payload,
                "status": STATUS_PENDING,
            }
            data["next_id"] = int(data["next_id"]) + 1
            data["proposals"].append(proposal)
            self._write(data)
        retval = proposal
        return retval

    def pending(self) -> list[dict[str, Any]]:
        with self._locked():
            data = self._read()
        retval = [p for p in data["proposals"] if p.get("status") == STATUS_PENDING]
        return retval

    def resolve(self, proposal_id: str, status: str, **fields: Any) -> dict[str, Any]:
        """Flip a pending proposal to a terminal status, recording `fields`.

        Re-reads under the lock so a proposal recorded by a concurrent run in
        the meantime is not lost, and refuses to touch a proposal that is no
        longer pending: a second approvals pass racing the first must not
        re-execute anything.
        """
        with self._locked():
            data = self._read()
            for proposal in data["proposals"]:
                if proposal.get("id") == proposal_id:
                    break
            else:
                raise KeyError(f"no proposal {proposal_id!r} in {self.path}")
            if proposal.get("status") != STATUS_PENDING:
                raise ValueError(
                    f"proposal {proposal_id!r} is already {proposal.get('status')!r}"
                )
            proposal["status"] = status
            proposal["resolved"] = _now()
            proposal.update(fields)
            self._write(data)
        retval = proposal
        return retval
