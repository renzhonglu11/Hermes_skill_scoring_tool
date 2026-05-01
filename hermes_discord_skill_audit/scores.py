from __future__ import annotations

from enum import IntEnum


class UserReviewScore(IntEnum):
    GOOD = 0
    NOT_GOOD = 1
    OKAY = 2


REACTION_SCORE_MAP: dict[str, UserReviewScore] = {
    "✅": UserReviewScore.GOOD,
    "❌": UserReviewScore.NOT_GOOD,
    "👌": UserReviewScore.OKAY,
}


def review_score_to_string(score: int | UserReviewScore | None) -> str:
    if score is None:
        return "unknown"
    try:
        return UserReviewScore(int(score)).name.lower()
    except Exception:
        return f"unknown:{score}"
