"""Turn an unring effect ledger into a Relate graph.

unring records *what* an agent did as a flat, hash-chained log. This file
rewrites that log as a graph, so the question "if I undo this call, what else
is now wrong?" becomes a traversal instead of a recursive SQL join.

Graph model:

    (:Run {id})-[:MADE]->(:Call {id, seq, tool, reversal, risk, args, status})
    (:Call)-[:OF]->(:Tool {name, verdict, inverse, residual_diff, readers})
    (:Call)-[:TOUCHED {op: 'read' | 'write'}]->(:Resource {uri})
    (:Call)-[:DEPENDS_ON {via}]->(:Call)   # derived: B read what A wrote earlier

The ledger does not know which resources a call touched, so the caller
passes a `touches` map, one entry per tool, with URI templates filled from
the call's arguments:

    {"slack.post": {"reads": ["file:{source}"], "writes": ["slack:{channel}"]}}
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import relate
from unring.ledger import Ledger

Touches = dict[str, dict[str, list[str]]]


def _fill(templates: list[str], args: dict) -> list[str]:
    """Fill each template from the call's args, skipping any whose args are missing or empty."""
    return [t.format(**args) for t in templates
            if all(args.get(name) for name in re.findall(r"{(\w+)}", t))]


def build_graph(ledger: Ledger, touches: Touches, store_path: str | Path, verdicts: dict | None = None):
    """Write `ledger` into a fresh Relate store at `store_path` and return the connection.

    `verdicts` maps tool name -> {"verdict", "inverse", "residual_diff", "readers"},
    i.e. measured reversibility results. Tools without one are stored as
    verdict "unknown": an untested tool is never assumed to be reversible.
    """
    store_path = Path(store_path)
    store_path.parent.mkdir(parents=True, exist_ok=True)
    if store_path.exists():
        store_path.unlink()  # this file is generated output; rebuild it from the ledger
    db = relate.connect(str(store_path))
    verdicts = verdicts or {}

    for entry in ledger.entries():
        p = entry.payload
        if entry.kind == "effect_call":
            db.execute(
                """
                MERGE (r:Run {id: $tx})
                MERGE (t:Tool {name: $tool})
                CREATE (c:Call {id: $id, seq: $seq, ts: $ts, tool: $tool, reversal: $reversal,
                                risk: $risk, args: $args, status: 'done'})
                CREATE (r)-[:MADE]->(c)
                CREATE (c)-[:OF]->(t)
                """,
                {"tx": entry.tx_id, "tool": p["name"], "id": entry.id, "seq": entry.seq, "ts": entry.ts,
                 "reversal": p["reversal"], "risk": p["risk"], "args": json.dumps(p["args"], default=str)},
            )
            spec = touches.get(p["name"], {})
            for op, key in (("read", "reads"), ("write", "writes")):
                for uri in _fill(spec.get(key, []), p["args"]):
                    db.execute(
                        "MATCH (c:Call {id: $id}) MERGE (res:Resource {uri: $uri}) "
                        "CREATE (c)-[:TOUCHED {op: $op}]->(res)",
                        {"id": entry.id, "uri": uri, "op": op},
                    )
        elif entry.kind == "compensation":
            db.execute(
                "MATCH (c:Call {id: $id}) SET c.status = 'undone', c.undo_verified = $verified",
                {"id": p["ledger_id"], "verified": p.get("verified")},
            )
        elif entry.kind == "compensation_skipped_irreversible":
            db.execute("MATCH (c:Call {id: $id}) SET c.status = 'undo_impossible'", {"id": p["ledger_id"]})

    # Read-after-write: a later call that read a resource depends on every
    # earlier call that wrote it.
    db.execute(
        """
        MATCH (a:Call)-[:TOUCHED {op: 'write'}]->(res:Resource)<-[:TOUCHED {op: 'read'}]-(b:Call)
        WHERE a.id < b.id
        MERGE (b)-[:DEPENDS_ON {via: res.uri}]->(a)
        """
    )

    db.execute("MATCH (t:Tool) SET t.verdict = 'unknown'")
    for tool, v in verdicts.items():
        db.execute(
            "MATCH (t:Tool {name: $tool}) SET t.verdict = $verdict, t.inverse = $inverse, "
            "t.residual_diff = $diff, t.readers = $readers",
            {"tool": tool, "verdict": v["verdict"], "inverse": v.get("inverse"),
             "diff": json.dumps(v.get("residual_diff", [])), "readers": v.get("readers", [])},
        )
    db.flush()
    return db
