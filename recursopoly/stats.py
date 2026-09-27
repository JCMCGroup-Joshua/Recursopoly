"""Recursopoly statistics, worked out from scores.csv.

Pure functions over the rows ScoreLogger.read_rows() returns (dicts keyed by
the CSV columns), so they can be tested without files or Flask. Used by the
leaderboard and game history pages.

Players are matched across games by name, ignoring case.
"""


def parse_details(text):
    """Turn a details column like "net_worth=1500; position=1" into a dict."""
    out = {}
    for part in (text or "").split(";"):
        key, sep, value = part.strip().partition("=")
        if sep:
            out[key.strip()] = value.strip()
    return out


def _int(value, default=0):
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _final_rows(rows):
    """game_ended rows with their details parsed, in file order."""
    for row in rows:
        if row.get("event_type") == "game_ended":
            yield row, parse_details(row.get("details"))


def _is_win(details):
    if "result" in details:
        return details["result"] == "winner"
    return details.get("rank") == "1"  # rows written before Phase 3


def _net_worth(details, row):
    # Phase 1-2 rows logged the final money as "final_score".
    if "net_worth" in details:
        return _int(details["net_worth"])
    return _int(details.get("final_score"), _int(row.get("money")))


def leaderboard(rows):
    """All-time stats per player, best first.

    Each entry: name, games, wins, best_net_worth, most_properties, journeys.
    """
    players = {}

    def entry(name):
        key = name.lower()
        if key not in players:
            players[key] = {"name": name, "games": set(), "wins": 0, "best_net_worth": None,
                            "most_properties": 0, "journeys": 0}
        players[key]["name"] = name  # latest spelling wins
        return players[key]

    purchases = {}  # (code, player) -> purchases, for rows without a properties count
    for row in rows:
        name = row.get("player_name") or ""
        if not name:
            continue
        kind = row.get("event_type")
        code = row.get("join_code")
        if kind == "game_started":
            entry(name)["games"].add(code)
        elif kind == "ticket_purchased":
            entry(name)["journeys"] += 1
        elif kind == "purchase":
            purchases[(code, name.lower())] = purchases.get((code, name.lower()), 0) + 1

    for row, details in _final_rows(rows):
        name = row.get("player_name") or ""
        if not name:
            continue
        e = entry(name)
        e["games"].add(row.get("join_code"))
        if _is_win(details):
            e["wins"] += 1
        worth = _net_worth(details, row)
        if e["best_net_worth"] is None or worth > e["best_net_worth"]:
            e["best_net_worth"] = worth
        owned = _int(details.get("properties"), purchases.get((row.get("join_code"), name.lower()), 0))
        e["most_properties"] = max(e["most_properties"], owned)

    table = []
    for e in players.values():
        table.append({**e, "games": len(e["games"]), "best_net_worth": e["best_net_worth"] or 0})
    table.sort(key=lambda e: (-e["wins"], -e["best_net_worth"], -e["games"], e["name"].lower()))
    return table


def top_by(table, key):
    """The leaderboard entry with the highest ``key`` (None if all zero)."""
    best = max(table, key=lambda e: e[key], default=None)
    return best if best and best[key] else None


def games(rows):
    """Every game in the log, newest first.

    Each game: join_code, started, ended, ruleset, players, winner, finished,
    standings [{name, position, net_worth, result}] and event_count.
    """
    found = {}
    order = []
    for row in rows:
        code = row.get("join_code")
        if not code:
            continue
        if code not in found:
            found[code] = {"join_code": code, "started": row.get("timestamp"), "ended": None,
                           "ruleset": None, "players": [], "winner": None, "finished": False,
                           "standings": [], "event_count": 0}
            order.append(code)
        g = found[code]
        g["event_count"] += 1
        details = parse_details(row.get("details"))
        name = row.get("player_name") or ""
        if row.get("event_type") == "game_started":
            if name and name not in g["players"]:
                g["players"].append(name)
            g["ruleset"] = g["ruleset"] or details.get("ruleset")
        elif row.get("event_type") == "game_ended":
            g["finished"] = True
            g["ended"] = row.get("timestamp")
            g["ruleset"] = g["ruleset"] or details.get("ruleset")
            position = _int(details.get("position") or details.get("rank"), len(g["standings"]) + 1)
            g["standings"].append({"name": name, "position": position,
                                   "net_worth": _net_worth(details, row),
                                   "result": details.get("result") or ("winner" if position == 1 else "finished")})
            if _is_win(details):
                g["winner"] = name
    for g in found.values():
        g["standings"].sort(key=lambda s: s["position"])
        g["ruleset"] = g["ruleset"] or "classic"
    return [found[code] for code in reversed(order)]


def game_detail(rows, join_code):
    """One game's summary plus its full event list, or None if unknown."""
    code = (join_code or "").strip().upper()
    summary = next((g for g in games(rows) if g["join_code"] == code), None)
    if summary is None:
        return None
    events = [row for row in rows if row.get("join_code") == code]
    return {**summary, "events": events}
