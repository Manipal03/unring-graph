"""End-to-end demo: an agent ships a release, then someone wants to undo step one.

    python demo.py

1. Probe: each tool runs once in a sandbox. Its undo is applied and the
   observable state is diffed, so every verdict comes from a measurement.
2. Run: a scripted "release agent" makes 7 tool calls through unring, which
   writes them to a hash-chained ledger (data/ledger.sqlite).
3. Graph: ledger_to_graph.py turns that ledger into a Relate store (data/unring.rl).
4. Ask: blast_radius.py answers "if I undo call #1, what else is now wrong?"

The tools act on an in-memory fake world (files, tickets, Slack, email, a
docs site), so the demo needs no credentials. The unring and Relate parts are
the real libraries.
"""

from __future__ import annotations

import copy
from pathlib import Path

from unring import Runtime
from unring.exceptions import IrreversibleEffectError
from unring.ledger import Ledger

import blast_radius
from ledger_to_graph import build_graph

DATA = Path(__file__).parent / "data"

# Which resources each tool reads and writes, as URI templates over its arguments.
TOUCHES = {
    "files.write":   {"writes": ["file:{path}"], "reads": ["file:{source}"]},
    "tickets.close": {"writes": ["ticket:{ticket_id}"]},
    "slack.post":    {"writes": ["slack:{channel}"], "reads": ["file:{source}"]},
    "email.send":    {"writes": ["email:{to}"], "reads": ["file:{source}"]},
    "docs.publish":  {"writes": ["site:{page}"], "reads": ["file:{source}"]},
    "crm.note":      {"writes": ["crm:{account}"], "reads": ["slack:{source}"]},
}

# Tools the probe can't test. A real CRM sandbox isn't available here, so
# crm.note stays "unknown" instead of being guessed.
NOT_PROBED = {"crm.note"}


def new_world() -> dict:
    return {
        "files": {"notes.md": "", "changelog.md": "# Changelog\n"},
        "tickets": {"4412": {"status": "open", "history": []}},
        "slack": {"#releases": []},
        "email": [],
        "site": {"/changelog": ""},
        "crm": {"acme": []},
    }


def make_tools(rt: Runtime, world: dict) -> dict:
    """Register the agent's tools on `rt`, all acting on `world`."""

    @rt.effect(name="files.write", reversal="snapshot")
    def files_write(path: str, text: str, source: str = "") -> None:
        world["files"][path] = text

    @files_write.capture
    def _(args):
        return world["files"].get(args["path"])

    @files_write.restore
    def _(before, args):
        world["files"][args["path"]] = before

    @rt.effect(name="tickets.close", reversal="inverse")
    def tickets_close(ticket_id: str) -> None:
        world["tickets"][ticket_id]["status"] = "closed"

    @tickets_close.inverse
    def _(result, args):  # tickets.reopen
        world["tickets"][args["ticket_id"]]["status"] = "open"

    @rt.effect(name="slack.post", reversal="inverse")
    def slack_post(channel: str, source: str) -> int:
        world["slack"][channel].append({"text": world["files"][source], "deleted": False})
        return len(world["slack"][channel]) - 1

    @slack_post.inverse
    def _(msg_index, args):  # slack.delete: Slack leaves a "message deleted" tombstone
        world["slack"][args["channel"]][msg_index] = {"text": "", "deleted": True}

    @rt.effect(name="email.send", reversal="irreversible", risk="high")
    def email_send(to: str, source: str) -> None:
        world["email"].append({"to": to, "body": world["files"][source]})

    @rt.effect(name="docs.publish", reversal="snapshot")
    def docs_publish(page: str, source: str) -> None:
        world["site"][page] = world["files"][source]

    @docs_publish.capture
    def _(args):
        return world["site"].get(args["page"])

    @docs_publish.restore
    def _(before, args):
        world["site"][args["page"]] = before

    @rt.effect(name="crm.note", reversal="inverse")
    def crm_note(account: str, source: str) -> None:
        world["crm"][account].append(f"announced: {world['slack'][source][-1]['text'][:40]}")

    @crm_note.inverse
    def _(result, args):
        world["crm"][args["account"]].pop()

    return {
        "files.write": files_write, "tickets.close": tickets_close, "slack.post": slack_post,
        "email.send": email_send, "docs.publish": docs_publish, "crm.note": crm_note,
    }


# --- 1. probe: measure each tool's undo -------------------------------------

INVERSE_NAMES = {"files.write": "snapshot restore", "tickets.close": "tickets.reopen",
                 "slack.post": "slack.delete", "docs.publish": "snapshot restore", "crm.note": "crm.delete_note"}
PROBE_CALLS = {
    "files.write": {"path": "notes.md", "text": "probe", "source": ""},
    "tickets.close": {"ticket_id": "4412"},
    "slack.post": {"channel": "#releases", "source": "notes.md"},
    "email.send": {"to": "customers", "source": "notes.md"},
    "docs.publish": {"page": "/changelog", "source": "notes.md"},
}


def _diff(before, after, path=""):
    """Flat list of differences between two nested values."""
    if isinstance(before, dict) and isinstance(after, dict):
        out = []
        for k in sorted(set(before) | set(after)):
            out += _diff(before.get(k), after.get(k), f"{path}.{k}" if path else k)
        return out
    if isinstance(before, list) and isinstance(after, list) and len(before) == len(after):
        out = []
        for i, (b, a) in enumerate(zip(before, after)):
            out += _diff(b, a, f"{path}[{i}]")
        return out
    return [] if before == after else [{"field": path, "before": before, "after": after}]


def probe() -> dict:
    verdicts = {}
    for tool, args in PROBE_CALLS.items():
        world = new_world()
        world["files"]["notes.md"] = "seed"
        rt = Runtime()
        tools = make_tools(rt, world)
        before = copy.deepcopy(world)
        with rt.transaction() as tx:
            tools[tool](**args)
        try:
            tx.rollback()
        except IrreversibleEffectError:
            verdicts[tool] = {"verdict": "irreversible", "inverse": None, "readers": [],
                              "residual_diff": _diff(before, world)}
            continue
        residue = _diff(before, world)
        verdicts[tool] = {"verdict": "partially_reversible" if residue else "reversible",
                          "inverse": INVERSE_NAMES[tool], "readers": ["world snapshot"],
                          "residual_diff": residue}
    return verdicts


# --- 2. run: the release agent ------------------------------------------------

def run_agent(ledger: Ledger) -> None:
    world = new_world()
    rt = Runtime(ledger)
    t = make_tools(rt, world)
    notes = "v2.3: faster sync, fixed login loop (#4412)"
    with rt.transaction():
        t["files.write"](path="notes.md", text=notes)                                        # 1
        t["tickets.close"](ticket_id="4412")                                                 # 2
        t["files.write"](path="changelog.md", text="# Changelog\n" + notes, source="notes.md")  # 3
        t["slack.post"](channel="#releases", source="notes.md")                              # 4
        t["email.send"](to="customers", source="notes.md")                                   # 5
        t["docs.publish"](page="/changelog", source="changelog.md")                          # 6
        t["crm.note"](account="acme", source="#releases")                                    # 7


def main() -> None:
    DATA.mkdir(exist_ok=True)
    ledger_path = DATA / "ledger.sqlite"
    for p in (ledger_path, Path(f"{ledger_path}-wal"), Path(f"{ledger_path}-shm")):
        p.unlink(missing_ok=True)  # generated output, rebuilt every run

    print("1. Probing each tool's undo in a sandbox...")
    verdicts = probe()
    for tool, v in verdicts.items():
        print(f"   {tool:<14} {v['verdict']:<21} residue: {v['residual_diff'] or 'none'}")
    for tool in NOT_PROBED:
        print(f"   {tool:<14} {'unknown':<21} (no sandbox available: not tested)")

    print("\n2. Running the release agent through unring...")
    ledger = Ledger(ledger_path)
    run_agent(ledger)
    ledger.verify_chain()
    print(f"   {len(ledger.entries())} ledger entries, hash chain verified -> {ledger_path.name}")

    print("\n3. Building the Relate graph...")
    db = build_graph(ledger, TOUCHES, DATA / "unring.rl", verdicts)
    ledger.close()
    counts = {r["label"]: r["n"] for r in db.execute(
        "MATCH (n) UNWIND labels(n) AS label RETURN label, count(*) AS n")}
    deps = list(db.execute("MATCH ()-[d:DEPENDS_ON]->() RETURN count(d) AS n"))[0]["n"]
    print(f"   {counts} + {deps} DEPENDS_ON edges -> data/unring.rl")

    print("\n4. The human asks: 'the release notes were wrong, undo call #1'")
    blast_radius.print_report(db, 1)
    db.close()


if __name__ == "__main__":
    main()
