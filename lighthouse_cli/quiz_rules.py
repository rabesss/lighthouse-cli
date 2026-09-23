"""Quiz navigation rules shared by display and future attempt drivers."""

from __future__ import annotations

from typing import Any

PAGING_LABELS = {
    0: "All questions on one page",
    1: "One question per page",
    2: "Page breaks after each section",
    3: "Five questions per page",
    4: "Ten questions per page",
}


def navigation_rules(quiz: dict[str, Any]) -> dict[str, Any]:
    """Unknown server values remain unknown, never permission to go back.

    PagingTypeId can be null for classic paging; only the attempt page can
    establish the actual layout in that case. Quiz settings alone do not
    establish which pages a running attempt can currently visit.
    """
    paging = quiz.get("PagingTypeId")
    if type(paging) is not int or paging not in PAGING_LABELS:
        paging = None
    prevent_back = quiz.get("PreventMovingBackwards")
    if type(prevent_back) is not bool:
        prevent_back = None
    return {
        "paging_type_id": paging,
        "layout": (
            "Unknown or classic paging"
            if paging is None
            else PAGING_LABELS.get(paging, "Unknown or classic paging")
        ),
        "prevent_moving_backwards": prevent_back,
        "save_before_advancing": True,
        "can_revisit_previous_pages": (
            None if prevent_back is None else not prevent_back
        ),
    }
