"""Normalise team names so sources that spell them differently can be joined.

The FIA documents use full entry names that change with sponsors ('Oracle Red Bull Racing.',
'*SCUDERIA FERRARI HP*', 'Visa Cash App RB F1 Team'), while FastF1 uses short names
('Red Bull Racing', 'Ferrari', 'RB'). Both map to the same key here.
"""

import re

# Checked in order: the Red Bull junior team must be tested before 'RED BULL'.
TEAM_PATTERNS = [
    ("Racing Bulls", r"RACING BULLS|CASH APP RB|^RB( |$)|ALPHATAURI|TORO ROSSO"),
    ("Red Bull Racing", r"RED BULL"),
    ("Ferrari", r"FERRARI"),
    ("Mercedes", r"MERCEDES"),
    ("McLaren", r"MCLAREN"),
    ("Aston Martin", r"ASTON MARTIN"),
    ("Alpine", r"ALPINE"),
    ("Williams", r"WILLIAMS"),
    ("Haas", r"HAAS"),
    ("Audi", r"AUDI"),
    ("Kick Sauber", r"SAUBER|\bKICK\b"),
    ("Cadillac", r"CADILLAC"),
]


def team_key(name):
    """Canonical team name for any spelling, or None if it isn't a known team."""
    if not isinstance(name, str):
        return None
    upper = re.sub(r"[*.]", "", name).strip().upper()
    for key, pattern in TEAM_PATTERNS:
        if re.search(pattern, upper):
            return key
    return None
