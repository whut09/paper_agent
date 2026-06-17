"""MCP server application facade."""

__all__ = ["create_mcp_app", "create_starlette_app"]


def __getattr__(name: str):
    if name not in __all__:
        raise AttributeError(name)
    from paper_agent import mcp_server

    return getattr(mcp_server, name)
