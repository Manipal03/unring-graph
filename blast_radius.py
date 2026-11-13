"""Questions you ask the graph before undoing an agent's action.

    python blast_radius.py data/unring.rl            # list every call
    python blast_radius.py data/unring.rl --call 1   # what breaks if call 1 is undone

Every function here is one Cypher query over the store `ledger_to_graph.py` wrote.
"""

from __future__ import annotations

import argparse

import relate

# Verdicts after which a call's consequences can't be cleaned up.
UNSAFE = ["irreversible", "partially_reversible", "unknown"]


def calls(db) -> list[dict]:
    return list(db.execute(
        "MATCH (c:Call)-[:OF]->(t:Tool) RETURN c.id AS id, c.tool AS tool, c.args AS args, "
        "c.status AS status, t.verdict AS verdict ORDER BY id"
    ))


def blast_radius(db, call_id: int) -> list[dict]:
    """Every call that, directly or transitively, consumed what `call_id` produced."""
    return list(db.execute(
        """
        MATCH p = (d:Call)-[:DEPENDS_ON*1..]->(c:Call {id: $id})
        MATCH (d)-[:OF]->(t:Tool)
        WITH d, t, min(length(p)) AS hops,
             collect(DISTINCT [r IN relationships(p) | r.via][0]) AS via
        RETURN d.id AS id, d.tool AS tool, hops, t.verdict AS verdict, via
        ORDER BY hops, id
        """,
        {"id": call_id},
    ))


def undo_order(db, call_id: int) -> list[dict]:
    """Safe order to undo `call_id`: newest dependents first, the call itself last."""
    return list(db.execute(
        """
        MATCH (c:Call {id: $id})
        OPTIONAL MATCH (d:Call)-[:DEPENDS_ON*1..]->(c)
        WITH c, collect(DISTINCT d) AS dependents
        UNWIND dependents + [c] AS x
        MATCH (x)-[:OF]->(t:Tool)
        RETURN x.id AS id, x.tool AS tool, t.verdict AS verdict, t.inverse AS inverse
        ORDER BY id DESC
        """,
        {"id": call_id},
    ))


def stranded(db, call_id: int) -> list[dict]:
    """Dependents whose effects can't be cleanly undone: undoing `call_id`
    leaves them pointing at state that no longer exists."""
    return list(db.execute(
        """
        MATCH (d:Call)-[:DEPENDS_ON*1..]->(:Call {id: $id})
        MATCH (d)-[:OF]->(t:Tool) WHERE t.verdict IN $unsafe
        RETURN DISTINCT d.id AS id, d.tool AS tool, t.verdict AS verdict, t.residual_diff AS residual_diff
        ORDER BY id
        """,
        {"id": call_id, "unsafe": UNSAFE},
    ))


def hotspots(db, limit: int = 5) -> list[dict]:
    """Resources with the most downstream readers: the riskiest things for an agent to rewrite."""
    return list(db.execute(
        """
        MATCH (w:Call)-[:TOUCHED {op: 'write'}]->(r:Resource)<-[:TOUCHED {op: 'read'}]-(rd:Call)
        RETURN r.uri AS resource, count(DISTINCT rd) AS readers, collect(DISTINCT rd.tool) AS read_by
        ORDER BY readers DESC LIMIT $limit
        """,
        {"limit": limit},
    ))


def print_report(db, call_id: int) -> None:
    target = [c for c in calls(db) if c["id"] == call_id]
    if not target:
        raise SystemExit(f"no call with id {call_id}; run without --call to list them")
    print(f"\nUndo call {call_id}: {target[0]['tool']} {target[0]['args']}")

    print("\nBlast radius (calls that consumed its output):")
    radius = blast_radius(db, call_id)
    for r in radius:
        print(f"  hop {r['hops']}: #{r['id']} {r['tool']:<14} [{r['verdict']}] read {', '.join(r['via'])}")
    if not radius:
        print("  none: safe to undo on its own")

    print("\nSafe undo order (newest dependent first):")
    for i, r in enumerate(undo_order(db, call_id), 1):
        print(f"  {i}. #{r['id']} {r['tool']:<16} inverse={r['inverse'] or '-':<16} [{r['verdict']}]")

    bad = stranded(db, call_id)
    print("\nCannot be cleanly cleaned up:")
    for r in bad:
        residue = r["residual_diff"] or "never tested, so nothing can be claimed"
        print(f"  #{r['id']} {r['tool']} [{r['verdict']}] residue: {residue}")
    if not bad:
        print("  nothing: every dependent has a measured clean inverse")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("store", help="path to the Relate store, e.g. data/unring.rl")
    ap.add_argument("--call", type=int, help="ledger id of the call you want to undo")
    a = ap.parse_args()
    db = relate.connect(a.store)
    if a.call is None:
        for c in calls(db):
            print(f"#{c['id']:<3} {c['tool']:<16} [{c['verdict']}] {c['args']}")
        print("\nHotspots:")
        for h in hotspots(db):
            print(f"  {h['resource']:<22} {h['readers']} readers: {', '.join(h['read_by'])}")
    else:
        print_report(db, a.call)


if __name__ == "__main__":
    main()
