"""Minimal S-expression parser for Specctra DSN/SES files.

Returns nested lists of str tokens. Numeric conversion is done by consumers.
"""

from __future__ import annotations


def tokenize(text: str):
    """Yield tokens: '(', ')', or atom strings (quotes stripped)."""
    i = 0
    n = len(text)
    prev_atom = None
    while i < n:
        c = text[i]
        if c in " \t\r\n":
            i += 1
        elif c == "(":
            yield "("
            i += 1
        elif c == ")":
            yield ")"
            i += 1
        elif c == '"':
            # Special case: `(string_quote ")` declares the quote char itself.
            if prev_atom == "string_quote":
                yield '"'
                prev_atom = '"'
                i += 1
            else:
                j = text.index('"', i + 1)
                prev_atom = text[i + 1 : j]
                yield prev_atom
                i = j + 1
        else:
            j = i
            while j < n and text[j] not in ' \t\r\n()"':
                j += 1
            prev_atom = text[i:j]
            yield prev_atom
            i = j


def parse(text: str):
    """Parse text into a single nested-list S-expression."""
    stack = [[]]
    for tok in tokenize(text):
        if tok == "(":
            new = []
            stack[-1].append(new)
            stack.append(new)
        elif tok == ")":
            stack.pop()
            if not stack:
                raise ValueError("unbalanced parens: extra ')'")
        else:
            stack[-1].append(tok)
    if len(stack) != 1:
        raise ValueError(f"unbalanced parens: {len(stack) - 1} unclosed '('")
    root = stack[0]
    if len(root) != 1:
        raise ValueError(f"expected single toplevel form, got {len(root)}")
    return root[0]


def find(node: list, key: str):
    """First child list whose head is `key`, or None."""
    for child in node:
        if isinstance(child, list) and child and child[0] == key:
            return child
    return None


def find_all(node: list, key: str):
    """All child lists whose head is `key`."""
    return [c for c in node if isinstance(c, list) and c and c[0] == key]
