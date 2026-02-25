"""
Grade-level helpers for multi-grade curriculum support.

Grade levels follow the Seychelles format: S1, S2, S3, S4, S5.
They are stored as comma-separated strings (e.g. "S1,S2,S3").
"""

import re
from typing import List


def parse_grade_level_string(csv: str) -> List[str]:
    """Parse a comma-separated grade string into a list.

    >>> parse_grade_level_string("S1,S2,S3")
    ['S1', 'S2', 'S3']
    >>> parse_grade_level_string("")
    []
    >>> parse_grade_level_string("S2")
    ['S2']
    """
    if not csv or not csv.strip():
        return []
    return [g.strip() for g in csv.split(',') if g.strip()]


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
