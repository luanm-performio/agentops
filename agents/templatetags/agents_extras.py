from django import template
from django.utils.safestring import mark_safe
import markdown as md_lib

register = template.Library()


@register.filter
def markdown(text: str) -> str:
    md = md_lib.Markdown(extensions=["fenced_code", "tables", "nl2br"])
    return mark_safe(md.convert(text or ""))
