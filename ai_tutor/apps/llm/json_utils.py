"""
Robust JSON parsing for LLM output.

LLMs frequently produce malformed JSON:
- Truncated output (stop_reason='max_tokens') with unclosed brackets/strings
- Markdown code fences around JSON
- Trailing commas
- Unescaped quotes in strings

This module provides `parse_llm_json` which handles all of these.
"""

import json
import re
import logging
from typing import Union, List, Dict, Optional

logger = logging.getLogger(__name__)


def parse_llm_json(
    raw: str,
    expect_array: bool = False,
) -> Union[Dict, List, None]:
    """
    Parse JSON from LLM output with automatic repair.

    Args:
        raw: Raw LLM response text (may include markdown fences, truncation, etc.)
        expect_array: If True, expect a JSON array (e.g. list of questions).

    Returns:
        Parsed dict/list, or None if all repair attempts fail.
    """
    if not raw or not raw.strip():
        return None

    text = _strip_markdown_fences(raw.strip())

    # Attempt 1: Direct parse
    try:
        result = json.loads(text)
        return result
    except json.JSONDecodeError:
        pass

    # Attempt 2: Repair truncated JSON
    repaired = _repair_truncated_json(text)
    if repaired is not None:
        logger.info("JSON parsed after truncation repair")
        return repaired

    # Attempt 3: Fix trailing commas
    fixed = re.sub(r',(\s*[}\]])', r'\1', text)
    try:
        return json.loads(fixed)
    except json.JSONDecodeError:
        pass

    # Attempt 4: If expecting array, try to extract it
    if expect_array:
        extracted = _extract_json_array(text)
        if extracted is not None:
            return extracted

    # Attempt 5: ast.literal_eval as last resort
    try:
        import ast
        return ast.literal_eval(text)
    except Exception:
        pass

    logger.warning(f"All JSON repair attempts failed. First 200 chars: {text[:200]}")
    return None


def _strip_markdown_fences(content: str) -> str:
    """Remove markdown code fences (```json ... ```) from LLM output."""
    if '```' not in content:
        return content

    # Try to extract content between ```json ... ``` or ``` ... ```
    if '```json' in content:
        parts = content.split('```json', 1)
        if len(parts) > 1:
            inner = parts[1]
            if '```' in inner:
                inner = inner.split('```', 1)[0]
            return inner.strip()

    parts = content.split('```')
    if len(parts) >= 3:
        # Take content between first and second fence
        candidate = parts[1].strip()
        # Remove optional language tag on first line
        if candidate and not candidate[0] in '{[':
            candidate = candidate.split('\n', 1)[-1].strip()
        return candidate

    return content


def _repair_truncated_json(content: str) -> Optional[Union[Dict, List]]:
    """
    Repair JSON truncated mid-stream (e.g. stop_reason='max_tokens').

    Strategy:
    1. Fix unclosed strings (odd quote count)
    2. Strip trailing partial key-value pairs
    3. Close open brackets in correct order
    4. Remove trailing commas before closing brackets
    """
    text = content.rstrip()

    # Fix unclosed string (odd number of unescaped quotes)
    quote_count = 0
    for i, ch in enumerate(text):
        if ch == '"' and (i == 0 or text[i - 1] != '\\'):
            quote_count += 1
    if quote_count % 2 != 0:
        # Strip back to last unescaped quote
        for pos in range(len(text) - 1, 0, -1):
            if text[pos] == '"' and (pos == 0 or text[pos - 1] != '\\'):
                text = text[:pos]
                break

    # Remove trailing comma, colon, or partial key-value
    text = re.sub(r'[,:]\s*$', '', text.rstrip())
    # Strip dangling key without value
    text = re.sub(r'([{,\[])\s*"[^"]*"\s*$', r'\1', text.rstrip())
    text = re.sub(r'[{,]\s*$', '', text.rstrip())

    # Build bracket stack to determine closing order
    stack = []
    in_string = False
    escape_next = False
    for ch in text:
        if escape_next:
            escape_next = False
            continue
        if ch == '\\' and in_string:
            escape_next = True
            continue
        if ch == '"':
            in_string = not in_string
            continue
        if in_string:
            continue
        if ch == '{':
            stack.append('}')
        elif ch == '[':
            stack.append(']')
        elif ch in ('}', ']') and stack and stack[-1] == ch:
            stack.pop()

    if not stack:
        # Brackets are balanced — try parse directly with trailing comma fix
        fixed = re.sub(r',(\s*[}\]])', r'\1', text)
        try:
            return json.loads(fixed)
        except json.JSONDecodeError:
            return None

    # Close open brackets
    text += ''.join(reversed(stack))

    # Clean trailing commas before closing brackets
    text = re.sub(r',(\s*[}\]])', r'\1', text)

    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return None


def _extract_json_array(content: str) -> Optional[List]:
    """Try to extract a JSON array from content that may have extra text."""
    # Find the outermost [ ... ]
    start = content.find('[')
    if start == -1:
        return None

    end = content.rfind(']')
    if end > start:
        candidate = content[start:end + 1]
        # Try direct parse
        try:
            return json.loads(candidate)
        except json.JSONDecodeError:
            pass

        # Try repair on the extracted array
        repaired = _repair_truncated_json(candidate)
        if isinstance(repaired, list):
            return repaired

    # Array is truncated (no closing ]) — repair from start
    candidate = content[start:]
    repaired = _repair_truncated_json(candidate)
    if isinstance(repaired, list):
        return repaired

    return None
