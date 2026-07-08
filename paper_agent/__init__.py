import logging
import os

log = logging.getLogger(__name__)

__version__ = "1.9.11"
__author__ = "Byaidu"
__all__ = ["translate", "translate_stream"]


def sanitize_no_proxy_env() -> None:
    for name in ("NO_PROXY", "no_proxy"):
        value = os.environ.get(name)
        if not value:
            continue
        parts = []
        changed = False
        for item in value.split(","):
            stripped = item.strip()
            if stripped in {"::1", "::1/128", "[::1]"}:
                changed = True
            else:
                parts.append(stripped)
        if changed:
            os.environ[name] = ",".join(part for part in parts if part)


sanitize_no_proxy_env()


def __getattr__(name):
    if name in {"translate", "translate_stream"}:
        from paper_agent.high_level import translate, translate_stream

        return {"translate": translate, "translate_stream": translate_stream}[name]
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
