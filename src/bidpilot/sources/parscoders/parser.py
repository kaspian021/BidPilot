"""Backwards-compatible re-export of the shared HTML parser.

The implementation moved to :mod:`bidpilot.sources.html_parser` when other
marketplaces (Ponisha, Karlancer, …) started reusing it.
"""

from ..html_parser import (  # noqa: F401
    DEFAULT_ID_REGEX,
    detect_existing_bid,
    detect_success,
    enrich_from_detail,
    extract_external_id,
    fallback_id,
    find_bid_form,
    javascript_requirement_snippet,
    map_bid_fields,
    parse_list_page,
    parse_project_card,
    parse_published,
    requires_javascript,
)

__all__ = [
    "DEFAULT_ID_REGEX",
    "detect_success",
    "detect_existing_bid",
    "enrich_from_detail",
    "extract_external_id",
    "fallback_id",
    "find_bid_form",
    "javascript_requirement_snippet",
    "map_bid_fields",
    "parse_list_page",
    "parse_project_card",
    "parse_published",
    "requires_javascript",
]
