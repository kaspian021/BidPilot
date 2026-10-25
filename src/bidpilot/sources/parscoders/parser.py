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
    extract_js_cookie_assignments,
    extract_json_notices,
    extract_response_notices,
    fallback_id,
    find_bid_form,
    find_proposal_cta_url,
    javascript_requirement_snippet,
    map_bid_fields,
    parse_list_page,
    parse_project_card,
    parse_published,
    requires_javascript,
    score_bid_form,
    serialize_form,
)

__all__ = [
    "DEFAULT_ID_REGEX",
    "detect_success",
    "detect_existing_bid",
    "enrich_from_detail",
    "extract_external_id",
    "fallback_id",
    "extract_js_cookie_assignments",
    "extract_json_notices",
    "extract_response_notices",
    "find_bid_form",
    "find_proposal_cta_url",
    "javascript_requirement_snippet",
    "map_bid_fields",
    "score_bid_form",
    "serialize_form",
    "parse_list_page",
    "parse_project_card",
    "parse_published",
    "requires_javascript",
]
