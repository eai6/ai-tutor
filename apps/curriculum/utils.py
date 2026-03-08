"""
Grade-level helpers for multi-grade curriculum support.

Grade levels follow the Seychelles format: S1, S2, S3, S4, S5.
They are stored as comma-separated strings (e.g. "S1,S2,S3").
"""

import re
from typing import List


def _expand_grade_range(token: str) -> List[str]:
    """Expand a dash range like 'S1-S3' into ['S1', 'S2', 'S3'].

    Falls through to literal if the pattern doesn't match.
    """
    m = re.match(r'^([A-Za-z]*)(\d+)-\1(\d+)$', token)
    if m:
        prefix, start, end = m.group(1), int(m.group(2)), int(m.group(3))
        return [f"{prefix}{n}" for n in range(start, end + 1)]
    return [token]


def parse_grade_level_string(csv: str) -> List[str]:
    """Parse a comma-separated grade string into a list, expanding dash ranges.

    >>> parse_grade_level_string("S1,S2,S3")
    ['S1', 'S2', 'S3']
    >>> parse_grade_level_string("")
    []
    >>> parse_grade_level_string("S2")
    ['S2']
    >>> parse_grade_level_string("S1-S3")
    ['S1', 'S2', 'S3']
    >>> parse_grade_level_string("S1-S3,S5")
    ['S1', 'S2', 'S3', 'S5']
    """
    if not csv or not csv.strip():
        return []
    result = []
    for token in csv.split(','):
        token = token.strip()
        if token:
            result.extend(_expand_grade_range(token))
    return result


def format_grade_display(csv: str) -> str:
    """Format a grade CSV for human-readable display.

    >>> format_grade_display("")
    'All Levels'
    >>> format_grade_display("S1")
    'S1'
    >>> format_grade_display("S1,S2,S3")
    'S1-S3'
    >>> format_grade_display("S1,S3")
    'S1, S3'
    """
    grades = parse_grade_level_string(csv)
    if not grades:
        return "All Levels"
    if len(grades) == 1:
        return grades[0]

    # Check if contiguous by extracting numeric parts
    nums = []
    for g in grades:
        m = re.search(r'\d+', g)
        if m:
            nums.append(int(m.group()))
        else:
            # Non-standard grade — just join with commas
            return ', '.join(grades)

    if nums == list(range(min(nums), max(nums) + 1)):
        # Contiguous range
        prefix = re.match(r'[A-Za-z]*', grades[0]).group()
        return f"{prefix}{min(nums)}-{prefix}{max(nums)}"

    return ', '.join(grades)


def determine_cycles(csv: str) -> List[str]:
    """Determine which Seychelles cycles a grade-level string covers.

    S1/S2 → Cycle 4, S3+ → Cycle 5, empty → both.

    >>> determine_cycles("")
    ['4', '5']
    >>> determine_cycles("S1")
    ['4']
    >>> determine_cycles("S3")
    ['5']
    >>> determine_cycles("S1,S2,S3")
    ['4', '5']
    """
    grades = parse_grade_level_string(csv)
    if not grades:
        return ['4', '5']

    cycles = set()
    for g in grades:
        m = re.search(r'\d+', g)
        if m:
            num = int(m.group())
            if num <= 2:
                cycles.add('4')
            else:
                cycles.add('5')
        else:
            cycles.add('4')
            cycles.add('5')

    return sorted(cycles)
