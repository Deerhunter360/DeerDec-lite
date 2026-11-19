import textwrap
from typing import List


def wrap_paras(paras: List[str], cols: int) -> List[str]:
    """Each paragraph wrapped to `cols` characters (monospace), kept as separate line groups."""
    lines = []
    for p in paras:
        lines += textwrap.wrap(" ".join(p.split()), cols, break_long_words=False) or [""]
    return lines


def paginate(groups: List[List[str]], cols: int = 35, per_page: int = 11) -> List[List[str]]:
    """groups = [main paragraphs, extra paragraphs...]; each group starts on a new page."""
    pages = []
    for g in groups:
        if not g:
            continue
        lines = wrap_paras(g, cols)
        pages += [lines[i:i + per_page] for i in range(0, len(lines), per_page)]
    return pages or [[""]]
