"""Backend application facade.

This module keeps backend dependencies lazy so importing ``paper_agent.app`` does
not require Flask/Celery unless the backend app is actually requested.
"""

__all__ = ["celery_app", "flask_app"]


def __getattr__(name: str):
    if name not in __all__:
        raise AttributeError(name)
    from paper_agent import backend

    return getattr(backend, name)
