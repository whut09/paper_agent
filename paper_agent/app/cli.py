"""CLI application facade."""

__all__ = ["main"]


def __getattr__(name: str):
    if name != "main":
        raise AttributeError(name)
    from paper_agent.paper_agent import main

    return main
