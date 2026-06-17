"""Gradio GUI application facade."""

__all__ = ["setup_gui"]


def __getattr__(name: str):
    if name != "setup_gui":
        raise AttributeError(name)
    from paper_agent.gui import setup_gui

    return setup_gui
