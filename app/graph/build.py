"""Graph assembly.

Nodes are thin wrappers over tested modules. Routing returns enumerated outcomes, never free
text. Every branch has an explicit terminal path.
"""

from __future__ import annotations

from pathlib import Path

from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.graph import END, START, StateGraph

from app.graph.nodes import core
from app.graph.state import GraphState, RequestContext


def _decision(state: GraphState) -> str:
    return state.get("decision", "blocked")


def _route(state: GraphState) -> str:
    return state.get("route", "unsupported")


def build_graph(checkpointer=None):
    g = StateGraph(GraphState, context_schema=RequestContext)

    g.add_node("admit", core.admit)
    g.add_node("understand", core.understand_and_route)
    g.add_node("retrieve", core.retrieve_candidates)
    g.add_node("expand", core.expand_retrieval)
    g.add_node("propose", core.propose_plan)
    g.add_node("clarify", core.clarify)
    g.add_node("repair", core.repair)
    g.add_node("bind_compile", core.bind_validate_compile)
    g.add_node("execute", core.execute)
    g.add_node("definition", core.definition_answer)
    g.add_node("metadata", core.metadata_answer)
    g.add_node("stop", core.stop)

    g.add_edge(START, "admit")
    g.add_conditional_edges("admit", _decision, {"allowed": "understand", "denied": "stop"})
    g.add_conditional_edges(
        "understand",
        _route,
        {
            "sql": "retrieve",
            "definition": "definition",
            "metadata": "metadata",
            "clarify": "retrieve",
            "unsupported": "stop",
            "denied": "stop",
        },
    )
    g.add_conditional_edges("retrieve", _decision, {"ready": "propose", "empty": "propose"})
    g.add_conditional_edges(
        "propose",
        _decision,
        {
            "ready": "bind_compile",
            "expand": "expand",
            "clarify": "clarify",
            "blocked": "stop",
        },
    )
    # Resume re-enters admit so authorization is re-derived; a permission may have been
    # revoked while the request was waiting for the user.
    g.add_conditional_edges(
        "clarify",
        _decision,
        {
            "resumed": "admit",
            "blocked": "stop",
        },
    )
    g.add_conditional_edges(
        "repair",
        _decision,
        {
            "ready": "bind_compile",
            "blocked": "stop",
        },
    )
    g.add_conditional_edges("expand", _decision, {"ready": "retrieve", "blocked": "stop"})
    g.add_conditional_edges(
        "bind_compile",
        _decision,
        {
            "ready": "execute",
            "repairable": "repair",
            "blocked": "stop",
        },
    )
    g.add_conditional_edges("execute", _decision, {"answered": END, "blocked": "stop"})
    g.add_conditional_edges("definition", _decision, {"answered": END, "blocked": "stop"})
    g.add_conditional_edges("metadata", _decision, {"answered": END, "blocked": "stop"})
    g.add_edge("stop", END)

    return g.compile(checkpointer=checkpointer)


def sqlite_checkpointer(path: str | Path = "checkpoints.sqlite"):
    """Durable checkpointer. Local stand-in for the AsyncPostgresSaver a deployment uses.

    Checkpoints are a durable, separately-accessed store: they hold the state of a suspended
    request, so they need the same access control and retention policy as any other artifact
    store. Nothing sensitive is placed in state for exactly this reason.
    """
    import sqlite3

    conn = sqlite3.connect(str(path), check_same_thread=False)
    saver = SqliteSaver(conn)
    saver.setup()
    return saver


def memory_checkpointer():
    """Non-durable checkpointer. A checkpointer is mandatory, not optional: without one an
    interrupt cannot suspend and clarification silently stops working."""
    from langgraph.checkpoint.memory import InMemorySaver

    return InMemorySaver()
