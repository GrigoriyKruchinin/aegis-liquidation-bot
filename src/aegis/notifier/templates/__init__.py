"""Telegram HTML templates."""

from __future__ import annotations

from aegis.notifier.templates.opportunity import render_opportunity
from aegis.notifier.templates.system import render_startup, render_status

__all__ = ["render_opportunity", "render_startup", "render_status"]
