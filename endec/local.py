"""Local area (the counties you serve) and alert filters (what an alert is called and what happens to it)."""
from . import defs
from .same import STATE_ABBR


def all_counties():
    """[(SSCCC, 'Kanawha, WV')] for every county DeerDec knows, sorted by state then name."""
    out = []
    for code, name in defs.US_SAME_CODE.items():
        if len(code) == 5 and not code.endswith("000"):
            out.append((code, f"{name}, {STATE_ABBR.get(code[:2], code[:2])}"))
    return sorted(out, key=lambda x: (x[0][:2], x[1]))


def covers(codes, fips):
    """True if any of these 6-digit SAME codes covers part of the chosen counties (SSCCC set); a whole state or
    the whole US counts."""
    mine = set(codes or [])
    if not mine:
        return False
    states = {c[:2] for c in mine}
    for f in fips or []:
        if f == "000000":
            return True
        ss, ccc = f[1:3], f[3:]
        if ccc == "000":
            if ss in states:
                return True
        elif f[1:] in mine:
            return True
    return False


def is_local(cfg, fips):
    """True if the alert touches the local area."""
    return covers(getattr(cfg, "local_fips", []), fips)


def area_name(cfg):
    """What to call the local area out loud: the name you gave it, else the counties."""
    n = (getattr(cfg, "local_area_name", "") or "").strip()
    if n:
        return n
    names = [defs.US_SAME_CODE.get(c, c) for c in (getattr(cfg, "local_fips", []) or [])]
    return ", ".join(names[:6]) or "your local"


def station_name(cfg):
    return (getattr(cfg, "station_name", "") or getattr(cfg, "callsign", "") or "this station").strip()


def _csv(s):
    return {x.strip().upper() for x in str(s or "").replace(";", ",").split(",") if x.strip()}


def match_filters(cfg, eee, org, fips):
    """-> (label, action, [matched filter names], local).  Filters are tried from the top of the list down and the
    FIRST one that matches is used (its label and its action).  With no match, 'LOCAL ALERT' is the label for an
    alert that covers the local area."""
    local = is_local(cfg, fips)
    for f in getattr(cfg, "alert_filters", []) or []:
        if not f.get("enabled", True):
            continue
        ev, og = _csv(f.get("events")), _csv(f.get("orgs"))
        if ev and eee.upper() not in ev:
            continue
        if og and org.upper() not in og:
            continue
        where = f.get("where", "any")
        if (where == "local" and not local) or (where == "not local" and local):
            continue
        if where == "counties" and not covers(f.get("counties"), fips):
            continue
        label = f.get("label", "") or ("LOCAL ALERT" if local and getattr(cfg, "local_label", True) else "")
        return label, f.get("action", "normal"), [f.get("name", "") or "filter"], local
    return ("LOCAL ALERT" if local and getattr(cfg, "local_label", True) else ""), "normal", [], local
