"""Validation and safe rendering shared by API handlers."""
import re
import nh3

EMAIL_PATTERN = re.compile(r"[^\s@<>;,]+@[^\s@<>;,]+\.[^\s@<>;,]+")

def valid_email(value):
    return isinstance(value, str) and len(value) <= 254 and bool(EMAIL_PATTERN.fullmatch(value.strip()))

def sanitize_vacancy_html(value):
    return nh3.clean(
        value or "",
        tags={"p", "br", "ul", "ol", "li", "strong", "b", "em", "i", "h1", "h2", "h3", "h4", "a", "div", "span"},
        attributes={"a": {"href", "title"}},
        clean_content_tags={"script", "style", "iframe", "object", "svg", "math", "template"},
        url_schemes={"https", "http", "mailto"},
        link_rel="noopener noreferrer",
    )

