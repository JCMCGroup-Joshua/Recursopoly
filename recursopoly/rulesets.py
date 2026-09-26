"""Recursopoly rule sets.

A rule set is one JSON file in rulesets/ bundling every tunable game value,
grouped into sections:

    {
      "name": "AMST",
      "description": "...",
      "board": "boards/amst_board.json",        # the property set / board
      "cards": {"chance": "cards/...txt", ...},  # one deck per card square type
      "players":     {"min_players": 2, ...},
      "economy":     {"starting_money": 2000, ...},
      "building":    {"max_hotels_per_property": 3, ...},
      "house_rules": {"must_lap_before_buying": true, ...},
      "pool":        {"pool_receives": ["taxes", "fines"], ...}
    }

rulesets/classic.json is the base: any section or value another rule set
leaves out is taken from classic. Adding a variant means writing a new JSON
file (and optionally a board and card decks); no code changes.

Paths inside a rule set are relative to the project folder. This module
reads files; game_engine stays free of file access.
"""

import copy
import json
import os
from dataclasses import dataclass, field

from game_engine import parse_board_data, parse_cards_text

BASE_RULESET = "classic"
SECTIONS = ("players", "economy", "building", "house_rules", "pool")

# Every value the game engine reads. classic.json must define them all.
REQUIRED_VALUES = (
    "min_players", "max_players",
    "starting_money", "go_salary", "jail_fine", "full_group_rent_multiplier",
    "house_sell_percent", "mortgage_percent", "unmortgage_interest_percent",
    "max_houses_per_property", "houses_before_hotel", "max_hotels_per_property",
    "doubles_before_jail", "max_jail_turns", "must_lap_before_buying",
    "pool_receives", "pool_payout_trigger", "pool_payout_split",
)

POOL_CATEGORIES = {"taxes", "fines", "fees"}
POOL_TRIGGERS = {"on_landing", "on_stakeholder_landing"}
POOL_SPLITS = {"by_stake", "equal"}


@dataclass
class RuleSet:
    id: str
    name: str
    description: str
    values: dict            # flat {"starting_money": 1500, ...}
    board: dict             # parsed board JSON
    decks: dict             # {deck name: [Card, ...]}
    sections: dict = field(default_factory=dict)  # merged sections, for display
    path: str = ""

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


def _merge(base, data):
    """Section-by-section merge of ``data`` over ``base``."""
    merged = {
        "name": data.get("name", base.get("name")),
        "description": data.get("description", ""),
        "board": data.get("board", base.get("board")),
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
            if key in flat:
                raise ValueError(f"value '{key}' appears in more than one section")
            flat[key] = value
    return flat


def _validate(flat, board, required, where):
    missing = sorted(set(required) - set(flat))
    if missing:
        raise ValueError(f"{where}: missing values {', '.join(missing)}")
    for key in required:
        if key == "must_lap_before_buying":
            if not isinstance(flat[key], bool):
                raise ValueError(f"{where}: {key} must be true or false")
        elif key == "pool_receives":
            if not isinstance(flat[key], list) or not set(flat[key]) <= POOL_CATEGORIES:
                raise ValueError(f"{where}: pool_receives must be a list drawn from "
                                 f"{sorted(POOL_CATEGORIES)}")
        elif key == "pool_payout_trigger":
            if flat[key] not in POOL_TRIGGERS:
                raise ValueError(f"{where}: pool_payout_trigger must be one of {sorted(POOL_TRIGGERS)}")
        elif key == "pool_payout_split":
            if flat[key] not in POOL_SPLITS:
                raise ValueError(f"{where}: pool_payout_split must be one of {sorted(POOL_SPLITS)}")
        elif not isinstance(flat[key], int) or isinstance(flat[key], bool) or flat[key] < 0:
            raise ValueError(f"{where}: {key} must be a whole number of 0 or more")
    if flat["min_players"] < 1 or flat["max_players"] < flat["min_players"]:
        raise ValueError(f"{where}: need 1 <= min_players <= max_players")
    if flat["houses_before_hotel"] > flat["max_houses_per_property"]:
        raise ValueError(f"{where}: houses_before_hotel can't exceed max_houses_per_property")
    if flat["doubles_before_jail"] < 1 or flat["max_jail_turns"] < 1:
        raise ValueError(f"{where}: doubles_before_jail and max_jail_turns must be at least 1")
    # Parsing the board checks its structure (indexes, pooled squares, ...).
    parse_board_data(board, 0)


def load_ruleset(path, root, base=None):
    """Load one rule set file. ``base`` is the parsed classic rule set JSON
    (None when loading classic itself)."""
    rid = os.path.splitext(os.path.basename(path))[0]
    data = _read_json(path)
    if not isinstance(data, dict):
        raise ValueError(f"{path}: a rule set must be a JSON object")
    merged = _merge(base or {}, data)
    if not merged["board"]:
        raise ValueError(f"{path}: no 'board' given")
    flat = _flatten(merged)
    board = _read_json(os.path.join(root, merged["board"]))
    _validate(flat, board, REQUIRED_VALUES, path)
    return RuleSet(
        id=rid,
        name=merged["name"] or rid,
        description=merged["description"],
        values=flat,
        board=board,
        decks=_load_decks(merged["cards"], root),
        sections={s: dict(merged[s]) for s in SECTIONS},
        path=path,
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
