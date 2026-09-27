"""Recursopoly rule sets.

A rule set is one JSON file in rulesets/ bundling every tunable game value,
grouped into sections:

    {
      "name": "AMST",
      "description": "...",
      "board": "boards/amst_board.json",        # the property set / board, or
      "boards": ["boards/outer.json", ...],     # several nested boards, outer first
      "cards": {"chance": "cards/...txt", ...},  # one deck per card square type
      "players":     {"min_players": 2, ...},
      "economy":     {"starting_money": 2000, ...},
      "building":    {"max_hotels_per_property": 3, ...},
      "house_rules": {"must_lap_before_buying": true, ...},
      "pooled_squares": {"receives": ["taxes", "fines"], "payout_trigger": ...},
      "travel":      {"ticket_prices": [50, 150, 300], "choose_destination": true},
      "passcode": "..."                         # optional: needed to host a game
    }

rulesets/classic.json is the base: any section or value another rule set
leaves out is taken from classic. Adding a variant means writing a new JSON
file (and optionally a board and card decks); no code changes.

"passcode" is never taken from classic: each file locks only itself. A
game can't change its passcode; only the admin settings page rewrites it.

Paths inside a rule set are relative to the project folder. This module
reads files; game_engine stays free of file access.
"""

import copy
import hmac
import json
import os
import re
from dataclasses import dataclass, field

from game_engine import parse_board_data, parse_cards_text

BASE_RULESET = "classic"
SECTIONS = ("players", "economy", "building", "house_rules", "pooled_squares", "travel")
TOP_LEVEL_KEYS = {"name", "description", "board", "boards", "cards", "passcode", *SECTIONS}
MAX_PASSCODE_LENGTH = 100
# Keys in these sections get a prefix when flattened, so the engine reads
# e.g. pooled_squares.receives as "pool_receives".
KEY_PREFIX = {"pooled_squares": "pool_"}

# Every value the game engine reads. classic.json must define them all.
REQUIRED_VALUES = (
    "min_players", "max_players",
    "starting_money", "go_salary", "jail_fine", "full_group_rent_multiplier",
    "house_sell_percent", "mortgage_percent", "unmortgage_interest_percent",
    "max_houses_per_property", "houses_before_hotel", "max_hotels_per_property",
    "doubles_before_jail", "max_jail_turns", "must_lap_before_buying",
    "pool_receives", "pool_payout_trigger", "pool_payout_split", "pool_sell_back_percent",
    "ticket_prices", "choose_destination",
)

# Every editable value, for the web forms (lobby rule editor and settings
# page): flat key, section and key in the file, label, type and options.
FIELDS = (
    {"key": "min_players", "section": "players", "label": "Minimum players", "type": "int", "min": 1},
    {"key": "max_players", "section": "players", "label": "Maximum players", "type": "int", "min": 1},
    {"key": "starting_money", "section": "economy", "label": "Starting money (\u00a3)", "type": "int"},
    {"key": "go_salary", "section": "economy", "label": "Go salary (\u00a3)", "type": "int"},
    {"key": "jail_fine", "section": "economy", "label": "Jail fine (\u00a3)", "type": "int"},
    {"key": "full_group_rent_multiplier", "section": "economy",
     "label": "Rent multiplier for a full colour group", "type": "int"},
    {"key": "house_sell_percent", "section": "economy", "label": "Building sell-back (%)", "type": "int"},
    {"key": "mortgage_percent", "section": "economy", "label": "Mortgage value (% of price)", "type": "int"},
    {"key": "unmortgage_interest_percent", "section": "economy", "label": "Unmortgage interest (%)", "type": "int"},
    {"key": "max_houses_per_property", "section": "building", "label": "Most houses per property", "type": "int"},
    {"key": "houses_before_hotel", "section": "building", "label": "Houses needed before a hotel", "type": "int"},
    {"key": "max_hotels_per_property", "section": "building", "label": "Most hotels per property", "type": "int"},
    {"key": "doubles_before_jail", "section": "house_rules", "label": "Doubles in a row before jail",
     "type": "int", "min": 1},
    {"key": "max_jail_turns", "section": "house_rules", "label": "Tries at doubles before the fine",
     "type": "int", "min": 1},
    {"key": "must_lap_before_buying", "section": "house_rules", "label": "Must pass Go once before buying",
     "type": "bool"},
    {"key": "pool_receives", "section": "pooled_squares", "name": "receives",
     "label": "Pooled squares collect", "type": "multi", "options": ["taxes", "fines", "fees"]},
    {"key": "pool_payout_trigger", "section": "pooled_squares", "name": "payout_trigger",
     "label": "Pot pays out", "type": "choice", "options": ["on_landing", "on_stakeholder_landing"]},
    {"key": "pool_payout_split", "section": "pooled_squares", "name": "payout_split",
     "label": "Pot is split", "type": "choice", "options": ["by_stake", "equal"]},
    {"key": "pool_sell_back_percent", "section": "pooled_squares", "name": "sell_back_percent",
     "label": "Stake sell-back (% of buy-in)", "type": "int"},
    {"key": "ticket_prices", "section": "travel", "label": "Train fares by board (\u00a3)", "type": "intlist"},
    {"key": "choose_destination", "section": "travel", "label": "Players choose their train destination",
     "type": "bool"},
)
FIELD_BY_KEY = {f["key"]: f for f in FIELDS}
RULESET_ID = re.compile(r"^[a-z0-9_-]{1,40}$")

POOL_CATEGORIES = {"taxes", "fines", "fees"}
POOL_TRIGGERS = {"on_landing", "on_stakeholder_landing"}
POOL_SPLITS = {"by_stake", "equal"}


@dataclass
class RuleSet:
    id: str
    name: str
    description: str
    values: dict            # flat {"starting_money": 1500, ...}
    boards: list            # parsed board JSON, outermost first (board_id = position)
    decks: dict             # {deck name: [Card, ...]}
    sections: dict = field(default_factory=dict)  # merged sections, for display
    path: str = ""
    board_paths: list = field(default_factory=list)  # as written in the file
    card_paths: dict = field(default_factory=dict)
    passcode: str = ""      # needed to host a game with this rule set; "" = open

    @property
    def locked(self):
        return bool(self.passcode)

    def check_passcode(self, given):
        """True if the rule set is open or ``given`` is its passcode."""
        return not self.passcode or hmac.compare_digest(str(given or ""), self.passcode)

    @property
    def board(self):
        """The outer board (the only one in single-board rule sets)."""
        return self.boards[0]

    def with_values(self, **overrides):
        """A copy with some values replaced (handy for tests and experiments)."""
        clone = copy.copy(self)
        clone.values = {**self.values, **overrides}
        return clone


def _read_json(path):
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except json.JSONDecodeError as err:
        raise ValueError(f"{path}: invalid JSON ({err})") from None


def _load_decks(card_paths, root):
    decks = {}
    for deck, rel in (card_paths or {}).items():
        path = os.path.join(root, rel)
        with open(path, encoding="utf-8") as fh:
            decks[deck] = parse_cards_text(fh.read(), deck)
    return decks


def _board_paths(data):
    """The board file(s) a rule set names: "boards" (a list, outer first)
    or a single "board"."""
    if "boards" in data:
        boards = data["boards"]
        if not isinstance(boards, list) or not boards or not all(isinstance(b, str) for b in boards):
            raise ValueError("'boards' must be a non-empty list of board file paths")
        return list(boards)
    if "board" in data:
        return [data["board"]]
    return []


def _merge(base, data):
    """Section-by-section merge of ``data`` over ``base``."""
    merged = {
        "name": data.get("name", base.get("name")),
        "description": data.get("description", ""),
        "boards": _board_paths(data) or _board_paths(base),
        "cards": data.get("cards", base.get("cards", {})),
    }
    for section in SECTIONS:
        values = dict(base.get(section, {}))
        extra = data.get(section, {})
        if not isinstance(extra, dict):
            raise ValueError(f"'{section}' must be an object")
        values.update(extra)
        merged[section] = values
    return merged


def _flatten(merged):
    flat = {}
    for section in SECTIONS:
        for key, value in merged[section].items():
            key = KEY_PREFIX.get(section, "") + key
            if key in flat:
                raise ValueError(f"value '{key}' appears in more than one section")
            flat[key] = value
    return flat


def _label(flat_key):
    """How a flattened key is written in the rule set file."""
    for section, prefix in KEY_PREFIX.items():
        if flat_key.startswith(prefix):
            return f"{section}.{flat_key[len(prefix):]}"
    return flat_key


def _validate(flat, boards, required, where):
    validate_values(flat, len(boards), where, required)
    # Parsing the boards checks their structure (indexes, pooled squares, ...).
    for board_id, board in enumerate(boards):
        parse_board_data(board, board_id)


def validate_values(flat, board_count, where="rule set", required=REQUIRED_VALUES):
    """Check a complete set of flat rule values. Raises ValueError."""
    missing = sorted(set(required) - set(flat))
    if missing:
        raise ValueError(f"{where}: missing values {', '.join(_label(k) for k in missing)}")
    for key in required:
        if key in ("must_lap_before_buying", "choose_destination"):
            if not isinstance(flat[key], bool):
                raise ValueError(f"{where}: {key} must be true or false")
        elif key == "pool_receives":
            if not isinstance(flat[key], list) or not set(flat[key]) <= POOL_CATEGORIES:
                raise ValueError(f"{where}: pooled_squares.receives must be a list drawn from "
                                 f"{sorted(POOL_CATEGORIES)}")
        elif key == "pool_payout_trigger":
            if flat[key] not in POOL_TRIGGERS:
                raise ValueError(f"{where}: pooled_squares.payout_trigger must be one of "
                                 f"{sorted(POOL_TRIGGERS)}")
        elif key == "ticket_prices":
            prices = flat[key]
            if not isinstance(prices, list) or not all(
                    isinstance(v, int) and not isinstance(v, bool) and v >= 0 for v in prices):
                raise ValueError(f"{where}: travel.ticket_prices must be a list of whole numbers")
        elif key == "pool_payout_split":
            if flat[key] not in POOL_SPLITS:
                raise ValueError(f"{where}: pooled_squares.payout_split must be one of "
                                 f"{sorted(POOL_SPLITS)}")
        elif not isinstance(flat[key], int) or isinstance(flat[key], bool) or flat[key] < 0:
            raise ValueError(f"{where}: {_label(key)} must be a whole number of 0 or more")
    if flat["min_players"] < 1 or flat["max_players"] < flat["min_players"]:
        raise ValueError(f"{where}: need 1 <= min_players <= max_players")
    if flat["houses_before_hotel"] > flat["max_houses_per_property"]:
        raise ValueError(f"{where}: houses_before_hotel can't exceed max_houses_per_property")
    if flat["doubles_before_jail"] < 1 or flat["max_jail_turns"] < 1:
        raise ValueError(f"{where}: doubles_before_jail and max_jail_turns must be at least 1")
    if board_count > 1 and len(flat["ticket_prices"]) < board_count:
        raise ValueError(f"{where}: travel.ticket_prices needs a price for each of the "
                         f"{board_count} boards (the fare to reach a station on that board)")


def coerce_values(changes):
    """Convert submitted form values to the types the rule set uses.
    Unknown keys are rejected. Raises ValueError with a readable message."""
    out = {}
    for key, value in (changes or {}).items():
        spec = FIELD_BY_KEY.get(key)
        if spec is None:
            raise ValueError(f"'{key}' is not a rule that can be changed")
        kind = spec["type"]
        try:
            if kind == "int":
                out[key] = int(str(value).strip())
            elif kind == "bool":
                out[key] = value if isinstance(value, bool) else str(value).lower() in ("1", "true", "yes", "on")
            elif kind == "choice":
                if value not in spec["options"]:
                    raise ValueError
                out[key] = value
            elif kind == "multi":
                items = value if isinstance(value, list) else [v for v in str(value).split(",") if v.strip()]
                items = [str(v).strip() for v in items]
                if not set(items) <= set(spec["options"]):
                    raise ValueError
                out[key] = [o for o in spec["options"] if o in items]
            elif kind == "intlist":
                items = value if isinstance(value, list) else [v for v in str(value).split(",") if v.strip()]
                out[key] = [int(str(v).strip()) for v in items]
        except (TypeError, ValueError):
            raise ValueError(f"{spec['label']}: invalid value {value!r}") from None
        if kind == "int" and out[key] < spec.get("min", 0):
            raise ValueError(f"{spec['label']} must be at least {spec.get('min', 0)}")
    return out


def check_passcode_text(passcode):
    """Tidy a new passcode ("" removes it). Raises ValueError."""
    passcode = str(passcode or "").strip()
    if len(passcode) > MAX_PASSCODE_LENGTH:
        raise ValueError(f"A passcode can be at most {MAX_PASSCODE_LENGTH} characters.")
    return passcode


def ruleset_file_data(name, description, board_paths, card_paths, values, base_values=None,
                      passcode=""):
    """The JSON to write for a rule set. With ``base_values`` (classic's),
    only values that differ are written, so the file keeps falling back to
    classic for everything else."""
    data = {"name": name, "description": description}
    if passcode:
        data["passcode"] = passcode
    if len(board_paths) == 1:
        data["board"] = board_paths[0]
    else:
        data["boards"] = list(board_paths)
    if card_paths:
        data["cards"] = dict(card_paths)
    for spec in FIELDS:
        key = spec["key"]
        if base_values is not None and base_values.get(key) == values[key]:
            continue
        data.setdefault(spec["section"], {})[spec.get("name", key)] = values[key]
    return data


def save_ruleset(rulesets_dir, rid, data):
    """Write a rule set file. ``rid`` must be a simple id (letters, digits,
    _ and -), so the file always lands in the rule sets folder."""
    if not RULESET_ID.match(rid or ""):
        raise ValueError("A rule set id may only use lower-case letters, digits, _ and - (up to 40).")
    path = os.path.join(rulesets_dir, rid + ".json")
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=2, ensure_ascii=False)
        fh.write("\n")
    os.replace(tmp, path)
    return path


def load_ruleset(path, root, base=None):
    """Load one rule set file. ``base`` is the parsed classic rule set JSON
    (None when loading classic itself)."""
    rid = os.path.splitext(os.path.basename(path))[0]
    data = _read_json(path)
    if not isinstance(data, dict):
        raise ValueError(f"{path}: a rule set must be a JSON object")
    unknown = sorted(set(data) - TOP_LEVEL_KEYS)
    if unknown:
        raise ValueError(f"{path}: unknown key(s) {', '.join(unknown)}; "
                         f"expected {', '.join(sorted(TOP_LEVEL_KEYS))}")
    try:
        merged = _merge(base or {}, data)
    except ValueError as err:
        raise ValueError(f"{path}: {err}") from None
    if not merged["boards"]:
        raise ValueError(f"{path}: no 'board' given")
    passcode = data.get("passcode", "")
    if not isinstance(passcode, str) or len(passcode.strip()) > MAX_PASSCODE_LENGTH:
        raise ValueError(f"{path}: 'passcode' must be text of up to {MAX_PASSCODE_LENGTH} characters")
    flat = _flatten(merged)
    boards = [_read_json(os.path.join(root, rel)) for rel in merged["boards"]]
    _validate(flat, boards, REQUIRED_VALUES, path)
    return RuleSet(
        id=rid,
        name=merged["name"] or rid,
        description=merged["description"],
        values=flat,
        boards=boards,
        decks=_load_decks(merged["cards"], root),
        sections={s: dict(merged[s]) for s in SECTIONS},
        path=path,
        board_paths=list(merged["boards"]),
        card_paths=dict(merged["cards"] or {}),
        passcode=passcode.strip(),
    )


def load_rulesets(rulesets_dir, root):
    """Load every rulesets/*.json, classic first. Returns {id: RuleSet}."""
    base_path = os.path.join(rulesets_dir, BASE_RULESET + ".json")
    if not os.path.exists(base_path):
        raise FileNotFoundError(f"Recursopoly needs {base_path}")
    base = _read_json(base_path)
    rulesets = {BASE_RULESET: load_ruleset(base_path, root)}
    for filename in sorted(os.listdir(rulesets_dir)):
        if filename.endswith(".json") and filename != BASE_RULESET + ".json":
            ruleset = load_ruleset(os.path.join(rulesets_dir, filename), root, base=base)
            rulesets[ruleset.id] = ruleset
    return rulesets
