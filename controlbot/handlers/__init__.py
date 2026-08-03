"""استيراد وحدات المعالجات يُنفّذ تسجيلها عبر @route.

الترتيب مهم: controls يستورد من campaign، وlogs يستورد من controls.
"""

from controlbot.handlers import campaign, controls, logs, targets  # noqa: F401
from controlbot.handlers.base import Route, dispatch, parse_route, route

__all__ = [
    "Route",
    "dispatch",
    "parse_route",
    "route",
    "campaign",
    "controls",
    "logs",
    "targets",
]
