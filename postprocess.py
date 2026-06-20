"""
Compatibility shim — postprocess.py has moved to heather/postprocess.py.

This file re-exports everything so existing imports continue to work.
Will be removed after Phase 6 migration is complete.
"""
from heather.postprocess import *  # noqa: F401, F403
from heather.postprocess import (  # explicit re-exports for IDE support
    is_incomplete_sentence,
    salvage_truncated_response,
    contains_gender_violation,
    postprocess_response,
    strip_phantom_photo_claims,
    strip_obvious_phantom_claims,
    strip_quote_wrapping,
    strip_thinking_tags,
    strip_bracketed_metadata,
    strip_ai_denial_claims,
    strip_unprompted_ai_self_id,
    strip_human_life_claims,
    strip_asterisk_actions,
    fix_glm_sorta_artifact,
    add_human_imperfections,
)
