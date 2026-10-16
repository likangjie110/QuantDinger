"""Guard: every call site that validates a mining expression states its own market's field set.

``normalize_expression`` defaults to the price-volume group, so a call site that omits
``allowed_fields`` validates against five fields whatever the run's market is: a cross-market
field is then refused at L1 with ``unknownField``, and nothing in the record says why. The failure
mode is invisible to every behavioural test -- the path still runs, it just refuses the candidate.

The call-site set grows with the code, which is why this is a scan and not a convention: the plan
for the field-group work counted five call sites, the audit that accompanied it found twenty, and
four of those lived in modules written after the count was taken. A bare call now fails here, with
its file and line. There are no exceptions: a whitelist entry would be the one place the failure
mode could hide, so a site that genuinely needs the default has to say so in this file first.
"""

import ast
import pathlib
from collections import Counter

_PACKAGE = pathlib.Path(__file__).resolve().parents[1] / "app" / "services" / "factor_mining"
_APP = _PACKAGE.parents[1]

# Call sites that validate a candidate expression, and why each one is on the list.
_WHY = {
    "normalize_expression": "it is where unknownField is raised",
    "ExpressionEvaluator": "it validates every subtree it memoizes",
    "expand_candidates": "it fingerprints each template grid point",
    "evolve": "it fingerprints each child and draws its fresh leaves",
    "mutate": "it validates the rewritten child",
    "crossover": "it validates the grafted child",
    "pool_panels": "it rebuilds stored pool members",
}
# Generic helper names are tracked inside the package only -- a same-named function elsewhere is a
# different function. ``normalize_expression`` exists only in this DSL, so it is tracked app-wide
# and a call from a future module cannot slip past.
_ELSEWHERE = frozenset({"normalize_expression"})

# What the scan reached when this guard was written: call sites per ``name@module``. Falling below
# one of these means the scan lost a site it used to cover (a rename, a moved module, an import
# that stopped resolving), which would let the check above pass by covering less. Lower a number
# only in the commit that really removes the call site.
_REACHED_AT_LEAST = {
    "normalize_expression@engine.py": 4,
    "ExpressionEvaluator@engine.py": 3,
    "expand_candidates@engine.py": 2,
    "evolve@engine.py": 1,
    "pool_panels@engine.py": 1,
    "normalize_expression@evaluator.py": 1,
    "normalize_expression@evolution.py": 3,
    "mutate@evolution.py": 1,
    "crossover@evolution.py": 1,
    "ExpressionEvaluator@pool.py": 1,
    "expand_candidates@service.py": 1,
    "normalize_expression@templates.py": 1,
}


def _calls_in(source: str, label: str, tracked: frozenset[str]) -> list[tuple[str, int, str, bool]]:
    """``(label, line, name, passes allowed_fields)`` for every tracked call in ``source``."""
    found = []
    for node in ast.walk(ast.parse(source, filename=label)):
        if not isinstance(node, ast.Call):
            continue
        if isinstance(node.func, ast.Name):
            name = node.func.id
        else:
            name = getattr(node.func, "attr", None)
        if name in tracked:
            found.append((label, node.lineno, name,
                          any(keyword.arg == "allowed_fields" for keyword in node.keywords)))
    return found


def _scan() -> list[tuple[str, int, str, bool]]:
    """Every tracked call site in ``app``, labelled relative to it."""
    found: list[tuple[str, int, str, bool]] = []
    for path in sorted(_APP.rglob("*.py")):
        tracked = frozenset(_WHY) if _PACKAGE in path.parents else _ELSEWHERE
        source = path.read_text(encoding="utf-8")
        if not any(name in source for name in tracked):
            # A substring check only decides whether to parse: it can admit a file with no call in
            # it, never skip one.
            continue
        found.extend(_calls_in(source, str(path.relative_to(_APP)), tracked))
    return found


def test_every_call_site_states_the_field_set_of_its_own_market():
    offenders = [
        f"{label}:{line}: {name}() -- {_WHY[name]}"
        for label, line, name, states_the_set in _scan() if not states_the_set
    ]
    assert not offenders, (
        "these call sites validate a candidate expression against the default price-volume group, "
        "so a cross-market field is refused as unknownField here with nothing to explain it.\n"
        "Pass allowed_fields=fields_for(market), or the enclosing function's allowed_fields "
        "parameter:\n  " + "\n  ".join(offenders)
    )


def test_the_scan_still_reaches_the_call_sites_it_was_written_for():
    """A scan that reaches nothing satisfies the check above vacuously, so it is counted."""
    reached = Counter(f"{name}@{pathlib.Path(label).name}"
                      for label, _line, name, _states in _scan())
    lost = {key: (reached.get(key, 0), least)
            for key, least in _REACHED_AT_LEAST.items() if reached.get(key, 0) < least}
    assert not lost, (
        "the scan no longer reaches every call site it was written for, so it is covering less "
        "than it looks like: " + ", ".join(f"{key}: {found} of {least}" for key, (found, least) in lost.items())
    )


def test_the_scan_flags_a_bare_call():
    """The detector itself, since a detector that never fires would also never fail this file."""
    source = (
        "from app.services.factor_mining.dsl import normalize_expression\n"
        "\n"
        "def bare(node):\n"
        "    return normalize_expression(node)\n"
        "\n"
        "def stated(node, allowed):\n"
        "    return normalize_expression(node, allowed_fields=allowed)\n"
    )
    assert _calls_in(source, "probe.py", frozenset({"normalize_expression"})) == [
        ("probe.py", 4, "normalize_expression", False),
        ("probe.py", 7, "normalize_expression", True),
    ]
