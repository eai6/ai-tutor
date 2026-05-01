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

    # FIRST: handle sequential-equals chains: a op1 b = c op2 d = e
    # e.g., "75 × 3 + 15 = 225 + 15 = 240" or "12 × 5 = 60 + 3 = 63"
    # This catches two bugs the simple binary pattern misses:
    #   1. The simple pattern greedy-consumes "= 15" from the first step,
    #      hiding "15 + 15 = 240" from the second-step check.
    #   2. An intermediate like "75 × 3 = 15" is wrong but only visible
    #      in context (the student expected 225).
    double_eq_pattern = (
        r'(\d+(?:\.\d+)?)\s*([×x*÷/+\-−])\s*(\d+(?:\.\d+)?)'
        r'\s*=\s*(\d+(?:\.\d+)?)'
        r'\s*([×x*÷/+\-−])\s*(\d+(?:\.\d+)?)'
        r'\s*=\s*(\d+(?:\.\d+)?)'
    )

    def replace_double_eq(match):
        a, op1, b, c_claimed, op2, d, e_claimed = match.groups()
        step1 = calc(a, op1, b)
        if step1 is None:
            return match.group(0)
        c_correct = step1
        # For step 2, use the student-shown intermediate (c_claimed) so we
        # don't mask a bad intermediate — we'll flag that separately.
        step2 = calc(c_claimed, op2, d)
        if step2 is None:
            return match.group(0)
        e_correct = step2

        c_is_wrong = abs(c_correct - float(c_claimed)) >= 0.01
        e_is_wrong = abs(e_correct - float(e_claimed)) >= 0.01

        if not c_is_wrong and not e_is_wrong:
            return match.group(0)

        # Replace with corrected arithmetic. If the intermediate was wrong,
        # rebuild the final using the CORRECT intermediate so the tutor's
        # narrative stays internally consistent.
        c_out = f"{c_correct:g}" if c_is_wrong else c_claimed
        final_for_e = calc(str(c_correct), op2, d) if c_is_wrong else e_correct
        e_out = f"{final_for_e:g}" if (c_is_wrong or e_is_wrong) else e_claimed

        if c_is_wrong:
            corrections.append({
                'expression': f"{a} {op1} {b}",
                'claimed': c_claimed,
                'correct': f"{c_correct:g}",
            })
            logger.warning(f"[MathCheck] Fixed step1: {a} {op1} {b} = {c_claimed} → {c_correct:g}")
        if e_is_wrong or c_is_wrong:
            corrections.append({
                'expression': f"{c_out} {op2} {d}",
                'claimed': e_claimed,
                'correct': e_out,
            })
            logger.warning(f"[MathCheck] Fixed step2: {c_out} {op2} {d} = {e_claimed} → {e_out}")
        return f"{a} {op1} {b} = {c_out} {op2} {d} = {e_out}"

    text = re.sub(double_eq_pattern, replace_double_eq, text)

    # 1.5: pure additive N-term sums with N >= 4. Catches the worked-
    # example "Step 5: Check" lines like
    #   "60 + 80 + 75 + 70 + 75 = 220 ✓"
    # which the 3-term chain pattern below misses (it only allows two
    # operators). Restricted to + and − so we don't have to model
    # operator precedence — those are left-associative.
    nterm_pattern = re.compile(
        r'(\d+(?:\.\d+)?)'
        r'((?:\s*[+\-−]\s*\d+(?:\.\d+)?){3,})'
        r'\s*=\s*(\d+(?:\.\d+)?)'
    )

    def replace_nterm(match):
        first_str, rest_str, claimed_str = match.groups()
        # Tokenise the trailing "(op num)+" sequence.
        tokens = re.findall(r'([+\-−])\s*(\d+(?:\.\d+)?)', rest_str)
        try:
            total = float(first_str)
            for op, n in tokens:
                v = float(n)
                if op == '+':
                    total += v
                else:  # '-' or '−'
                    total -= v
            claimed = float(claimed_str)
        except ValueError:
            return match.group(0)
        if abs(total - claimed) < 0.01:
            return match.group(0)
        correct_str = f"{total:g}"
        corrections.append({
            'expression': f"{first_str}{rest_str}",
            'claimed': claimed_str,
            'correct': correct_str,
        })
        logger.warning(
            "[MathCheck] Fixed N-term sum: %s%s = %s → %s",
            first_str, rest_str, claimed_str, correct_str,
        )
        return f"{first_str}{rest_str} = {correct_str}"

    text = nterm_pattern.sub(replace_nterm, text)

    # Track N-term spans in the corrected text so the chain and
    # simple-binary passes don't re-match a subsequence of an already-
    # handled long sum. Without this guard, a fixed `60 + 80 + 75 + 70
    # + 75 = 360` becomes a target for the simple-binary pass to
    # mis-flag `70 + 75 = 360` as wrong arithmetic.
    nterm_spans = [m.span() for m in nterm_pattern.finditer(text)]

    def _inside(start: int, end: int, spans) -> bool:
        for s, e in spans:
            if start >= s and end <= e:
                return True
        return False

    # SECOND: handle chained expressions: a op b op c = result
    # e.g., "8 × 2.5 + 15 = 35" or "20 - 3 + 5 = 22"
    # Must run BEFORE simple pattern to avoid partial matches
    chain_pattern = r'(\d+(?:\.\d+)?)\s*([×x*÷/+\-−])\s*(\d+(?:\.\d+)?)\s*([+\-−])\s*(\d+(?:\.\d+)?)\s*=\s*(\d+(?:\.\d+)?)'

    def replace_chain(match):
        if _inside(match.start(), match.end(), nterm_spans):
            return match.group(0)
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

    # Recompute spans in the corrected text — substitution shifts
    # offsets, and downstream passes need accurate ranges to skip.
    nterm_spans_corrected = [m.span() for m in nterm_pattern.finditer(corrected)]

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
        # Skip if this match is a sub-expression of a chain or N-term match
        if _inside(match.start(), match.end(), nterm_spans_corrected):
            return match.group(0)
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
