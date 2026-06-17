"""Figure, table, and formula asset extraction facade."""

from paper_agent.paper_summary import (
    _capture_captioned_figures,
    _capture_captioned_tables,
    _capture_formula_blocks_from_doc,
    _capture_image_blocks,
    _capture_tables,
    _extract_text_and_assets,
)

__all__ = [
    "_capture_captioned_figures",
    "_capture_captioned_tables",
    "_capture_formula_blocks_from_doc",
    "_capture_image_blocks",
    "_capture_tables",
    "_extract_text_and_assets",
]

