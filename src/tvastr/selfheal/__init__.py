"""Self-healing loop: tvastr diagnosing and fixing its own recurring failures.

Task 1 lays the foundation — config flags plus :mod:`tvastr.selfheal.selflog`,
the continuous self-log capture layer every later stage (mining, triage, fix
generation) reads from.
"""

from __future__ import annotations
