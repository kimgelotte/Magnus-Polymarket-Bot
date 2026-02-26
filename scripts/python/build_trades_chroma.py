#!/usr/bin/env python3
"""Build ChromaDB from trades in DB and run basic analytics."""
import sys
import os
from pathlib import Path
from collections import defaultdict

root = Path(__file__).resolve().parents[2]
if str(root) not in sys.path:
    sys.path.insert(0, str(root))

from agents.db_manager import DatabaseManager

CHROMA_DIR = root / "data" / "magnus_trades_chroma"

def build_doc(t: dict) -> str:
    q = (t.get("question") or "").strip()
    cat = t.get("category") or "Unknown"
    status = t.get("status") or "OPEN"
    notes = (t.get("notes") or "").strip()[:200]
    return f"Question: {q} | Category: {cat} | Buy: {t.get('buy_price')} | Status: {status} | Notes: {notes}"

def build_analysis_doc(a: dict) -> str:
    q = (a.get("question") or "").strip()
    cat = a.get("category") or "Unknown"
    action = a.get("action") or "REJECT"
    reason = (a.get("reason") or "").strip()[:300]
    return f"Question: {q} | Category: {cat} | Decision: {action} | Reason: {reason} | Price: {a.get('current_price')} | Hype: {a.get('hype_score', 0)}"

def run_analytics(trades: list[dict]) -> None:
    """Print trade statistics from DB (no Chroma needed)."""
    if not trades:
        print("No trades in DB.")
        return
    by_status = defaultdict(int)
    by_category = defaultdict(lambda: {"total": 0, "CLOSED_PROFIT": 0, "CLOSED_LOSS": 0, "OPEN": 0})
    for t in trades:
        s = t.get("status") or "OPEN"
        by_status[s] += 1
        cat = t.get("category") or "Unknown"
        by_category[cat]["total"] += 1
        by_category[cat][s] = by_category[cat].get(s, 0) + 1

    print("\n--- STATISTIK (alla trades i DB) ---")
    print("Status:", dict(by_status))
    closed_win = by_status.get("CLOSED_PROFIT", 0)
    closed_loss = by_status.get("CLOSED_LOSS", 0)
    open_count = by_status.get("OPEN", 0)
    if closed_win + closed_loss > 0:
        win_rate = closed_win / (closed_win + closed_loss) * 100
        print(f"Closed: {closed_win} wins / {closed_loss} losses → win rate {win_rate:.1f}%")
    print(f"Open: {open_count}")

    print("\n--- PER KATEGORI ---")
    for cat, counts in sorted(by_category.items(), key=lambda x: -x[1]["total"]):
        total = counts["total"]
        w = counts.get("CLOSED_PROFIT", 0)
        l = counts.get("CLOSED_LOSS", 0)
        o = counts.get("OPEN", 0)
        wr = f"win rate {w/(w+l)*100:.0f}%" if (w + l) > 0 else "-"
        print(f"  {cat}: n={total} (W={w} L={l} OPEN={o}) {wr}")

def build_chroma(trades: list[dict], limit: int | None) -> None:
    """Populate ChromaDB with trades (requires OPENAI_API_KEY)."""
    try:
        from langchain_openai import OpenAIEmbeddings
    except ImportError:
        print("⚠️ langchain-openai missing. Install for Chroma embedding.")
        return
    if not os.getenv("OPENAI_API_KEY"):
        print("⚠️ OPENAI_API_KEY missing. Analytics only (no Chroma).")
        return

    import chromadb
    from chromadb.utils import embedding_functions

    CHROMA_DIR.mkdir(parents=True, exist_ok=True)
    docs = [build_doc(t) for t in trades]
    ids = [f"trade_{t.get('id', i)}" for i, t in enumerate(trades)]
    metadatas = [
        {
            "status": t.get("status") or "OPEN",
            "category": (t.get("category") or "Unknown")[:50],
            "buy_price": float(t.get("buy_price") or 0),
            "amount_usdc": float(t.get("amount_usdc") or 0),
        }
        for t in trades
    ]

    ef = OpenAIEmbeddings(model="text-embedding-3-small")
    def embed(texts: list[str]) -> list[list[float]]:
        return ef.embed_documents(texts)

    client = chromadb.PersistentClient(path=str(CHROMA_DIR))
    coll = client.get_or_create_collection("magnus_trades", metadata={"description": "Trades from DB"})
    print(f"Embedding {len(docs)} trades...")
    embeddings = embed(docs)
    coll.add(ids=ids, documents=docs, metadatas=metadatas, embeddings=embeddings)
    print(f"✅ Chroma uppdaterad: {CHROMA_DIR} ({len(docs)} dokument)")

def query_similar(trades: list[dict], query: str) -> None:
    """Find similar trades (requires Chroma to be built first)."""
    if not query or not trades:
        return
    try:
        from langchain_openai import OpenAIEmbeddings
        import chromadb
    except ImportError:
        print("⚠️ Chroma/OpenAI missing for similarity query.")
        return
    if not os.getenv("OPENAI_API_KEY"):
        return
    if not CHROMA_DIR.exists() or not list(CHROMA_DIR.glob("*.sqlite3")):
        print("Run without --query first to build Chroma.")
        return

    ef = OpenAIEmbeddings(model="text-embedding-3-small")
    client = chromadb.PersistentClient(path=str(CHROMA_DIR))
    coll = client.get_collection("magnus_trades")
    q_embed = ef.embed_query(query)
    results = coll.query(query_embeddings=[q_embed], n_results=min(5, len(trades)), include=["documents", "metadatas"])
    print(f"\n--- SIMILAR TRADES FOR: «{query[:60]}» ---")
    for doc, meta in zip(results["documents"][0], results["metadatas"][0]):
        print(f"  Status: {meta.get('status')} | Category: {meta.get('category')} | Buy: {meta.get('buy_price')}")
        print(f"  {doc[:120]}...")

def run_analytics_analyses(analyses: list[dict]) -> None:
    """Statistics over AI analyses (BUY vs REJECT)."""
    if not analyses:
        print("\n--- ANALYSES: none in DB yet. Run Magnus to populate.")
        return
    from collections import defaultdict
    by_action = defaultdict(int)
    by_cat = defaultdict(lambda: {"BUY": 0, "REJECT": 0})
    for a in analyses:
        act = a.get("action") or "REJECT"
        by_action[act] += 1
        cat = a.get("category") or "Unknown"
        by_cat[cat][act] = by_cat[cat].get(act, 0) + 1
    print(f"\n--- ANALYSER (senaste) ---")
    print("Beslut:", dict(by_action))
    print("Per kategori (BUY/REJECT):")
    for cat, counts in sorted(by_cat.items(), key=lambda x: -(x[1]["BUY"] + x[1]["REJECT"])):
        print(f"  {cat}: BUY={counts['BUY']} REJECT={counts['REJECT']}")

def build_analyses_chroma(analyses: list[dict], limit: int | None) -> None:
    """Populate ChromaDB with analyses (collection: magnus_analyses)."""
    if not analyses:
        return
    try:
        from langchain_openai import OpenAIEmbeddings
    except ImportError:
        return
    if not os.getenv("OPENAI_API_KEY"):
        return
    import chromadb
    CHROMA_DIR.mkdir(parents=True, exist_ok=True)
    docs = [build_analysis_doc(a) for a in analyses]
    ids = [f"analysis_{a.get('id', i)}" for i, a in enumerate(analyses)]
    metadatas = [
        {
            "action": (a.get("action") or "REJECT")[:20],
            "category": (a.get("category") or "Unknown")[:50],
            "hype_score": int(a.get("hype_score") or 0),
            "current_price": float(a.get("current_price") or 0),
        }
        for a in analyses
    ]
    ef = OpenAIEmbeddings(model="text-embedding-3-small")
    def embed(texts): return ef.embed_documents(texts)
    client = chromadb.PersistentClient(path=str(CHROMA_DIR))
    try:
        client.delete_collection("magnus_analyses")
    except Exception:
        pass
    coll = client.get_or_create_collection("magnus_analyses", metadata={"description": "All AI analyses (BUY/REJECT)"})
    print(f"Embedding {len(docs)} analyses...")
    embeddings = embed(docs)
    coll.add(ids=ids, documents=docs, metadatas=metadatas, embeddings=embeddings)
    print(f"✅ Chroma analyser uppdaterad: magnus_analyses ({len(docs)} dokument)")


def get_similar_analyses_context(question: str, k: int = 3) -> str:
    """Query ChromaDB for similar past analyses. Returns formatted string for agent prompts."""
    if not os.getenv("OPENAI_API_KEY"):
        return ""
    try:
        from langchain_openai import OpenAIEmbeddings
        import chromadb
    except ImportError:
        return ""
    if not CHROMA_DIR.exists():
        return ""
    try:
        client = chromadb.PersistentClient(path=str(CHROMA_DIR))
        coll = client.get_collection("magnus_analyses")
    except Exception:
        return ""
    ef = OpenAIEmbeddings(model="text-embedding-3-small")
    emb = ef.embed_query(question)
    res = coll.query(query_embeddings=[emb], n_results=min(k, coll.count()))
    if not res or not res.get("documents") or not res["documents"][0]:
        return ""
    lines = []
    for doc, meta in zip(res["documents"][0], (res.get("metadatas") or [[]])[0] or []):
        action = (meta or {}).get("action", "?")
        lines.append(f"  • {doc} (→ {action})")
    return "\n".join(lines) if lines else ""


def main():
    import argparse
    p = argparse.ArgumentParser(description="Build ChromaDB from trades + analyses")
    p.add_argument("--limit", type=int, default=None, help="Max trades (newest first)")
    p.add_argument("--query", type=str, default=None, help="Query for similar trades")
    args = p.parse_args()

    db = DatabaseManager()
    trades = db.get_all_trades(limit=args.limit)
    analyses = db.get_all_analyses(limit=args.limit)
    run_analytics(trades)
    run_analytics_analyses(analyses)

    if not args.query:
        build_chroma(trades, args.limit)
        build_analyses_chroma(analyses, args.limit)
    else:
        build_chroma(trades, args.limit)
        build_analyses_chroma(analyses, args.limit)
        query_similar(trades, args.query)

if __name__ == "__main__":
    main()
