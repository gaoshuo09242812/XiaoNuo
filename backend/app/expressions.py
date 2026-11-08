"""Display names from existing text presentation rules, never acoustic emotion."""
EXPRESSIONS = frozenset(('neutral','happy','gentle','sleepy','thinking','quiet'))


def safe_expression(value):
    return value if isinstance(value,str) and value in EXPRESSIONS else 'neutral'
