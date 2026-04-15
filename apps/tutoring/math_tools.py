"""Math calculation tools for the AI tutor.

Provides a calculator that verifies and corrects arithmetic in tutor responses.
This prevents the LLM from making calculation errors (e.g., 8 × 2.5 = 21).
"""
import re
import logging

logger = logging.getLogger(__name__)


def verify_calculations(text: str) -> tuple[str, list[dict]]:
    """Find arithmetic expressions in tutor text and verify them.

    Looks for patterns like:
    - "= 35" after an expression
    - "8 × 2.5 = 20"
    - "3 + 4 = 7"

    Returns (corrected_text, list of corrections made).
    """
    corrections = []

    def calc(a, op, b):
        """Evaluate a simple binary operation."""
        a, b = float(a), float(b)
        if op in ('×', 'x', '*'):
            return a * b
        elif op in ('÷', '/'):
            return a / b if b != 0 else None
        elif op == '+':
            return a + b
        elif op in ('-', '−'):
            return a - b
        return None

    # First handle chained expressions: a op b op c = result
    # e.g., "8 × 2.5 + 15 = 35" or "20 - 3 + 5 = 22"
    # Must run BEFORE simple pattern to avoid partial matches
    chain_pattern = r'(\d+(?:\.\d+)?)\s*([×x*÷/+\-−])\s*(\d+(?:\.\d+)?)\s*([+\-−])\s*(\d+(?:\.\d+)?)\s*=\s*(\d+(?:\.\d+)?)'

    def replace_chain(match):
        a_str, op1, b_str, op2, c_str, claimed_str = match.groups()

        # Calculate step by step following BIDMAS
        # If first op is × or ÷, do it first (BIDMAS)
        if op1 in ('×', 'x', '*', '÷', '/'):
            step1 = calc(a_str, op1, b_str)
            if step1 is None:
                return match.group(0)
            correct = calc(str(step1), op2, c_str)
        else:
            # Left to right for same precedence
            step1 = calc(a_str, op1, b_str)
            if step1 is None:
                return match.group(0)
            correct = calc(str(step1), op2, c_str)

        if correct is None:
            return match.group(0)

        claimed = float(claimed_str)
        if abs(correct - claimed) < 0.01:
            return match.group(0)

        correct_str = f"{correct:g}"
        corrections.append({
            'expression': f"{a_str} {op1} {b_str} {op2} {c_str}",
            'claimed': claimed_str,
            'correct': correct_str,
        })
        logger.warning(f"[MathCheck] Fixed chain: {a_str} {op1} {b_str} {op2} {c_str} = {claimed_str} → {correct_str}")
        return f"{a_str} {op1} {b_str} {op2} {c_str} = {correct_str}"

    # Track spans covered by chain matches so simple pattern skips them
    chain_spans = []
    for m in re.finditer(chain_pattern, text):
        chain_spans.append((m.start(), m.end()))

    corrected = re.sub(chain_pattern, replace_chain, text)

    # Pattern: number op number = result (with Unicode math operators)
    # Matches: 8 × 2.5 = 20, 3 + 4 = 7, 45 ÷ 5 = 9, 20 - 3 = 17
    # Skip matches that overlap with already-handled chain expressions.
    pattern = r'(\d+(?:\.\d+)?)\s*([×x*÷/+\-−])\s*(\d+(?:\.\d+)?)\s*=\s*(\d+(?:\.\d+)?)'

    # Build a set of positions in the ORIGINAL text that were part of chain matches.
    # After chain substitution, positions shift, so we instead check against the
    # corrected text by finding chain expressions there.
    chain_pattern_corrected_spans = []
    for m in re.finditer(chain_pattern, corrected):
        chain_pattern_corrected_spans.append((m.start(), m.end()))

    def replace_match(match):
        # Skip if this match is a sub-expression of a chain match
        for cs, ce in chain_pattern_corrected_spans:
            if match.start() >= cs and match.end() <= ce:
                return match.group(0)

        a_str, op, b_str, claimed_str = match.groups()
        correct = calc(a_str, op, b_str)
        if correct is None:
            return match.group(0)

        claimed = float(claimed_str)
        # Check if they match (allow small float precision diff)
        if abs(correct - claimed) < 0.01:
            return match.group(0)  # Correct, no change

        # Wrong! Fix it
        correct_str = f"{correct:g}"  # Remove trailing zeros
        corrections.append({
            'expression': f"{a_str} {op} {b_str}",
            'claimed': claimed_str,
            'correct': correct_str,
        })
        logger.warning(f"[MathCheck] Fixed: {a_str} {op} {b_str} = {claimed_str} → {correct_str}")
        return f"{a_str} {op} {b_str} = {correct_str}"

    corrected = re.sub(pattern, replace_match, corrected)

    return corrected, corrections


def safe_eval_expression(expr: str) -> float | None:
    """Safely evaluate a math expression string.

    Only allows basic arithmetic operations.
    Returns None if expression is invalid or dangerous.
    """
    # Clean the expression
    expr = expr.replace('×', '*').replace('÷', '/').replace('−', '-')
    expr = expr.replace('^', '**')

    # Only allow digits, operators, parentheses, dots, spaces
    if not re.match(r'^[\d\s\+\-\*\/\.\(\)\^]+$', expr):
        return None

    try:
        result = eval(expr, {"__builtins__": {}}, {})
        return float(result)
    except Exception:
        return None
