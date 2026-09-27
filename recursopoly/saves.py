"""Recursopoly game saves.

Each unfinished game is written to ``saves/<JOIN CODE>.json`` whenever it
changes, so games survive a server restart: on startup every save is
loaded back and players rejoin with the same join code (open pages rejoin
by themselves when the server is back).

A save holds the game (Game.snapshot), the chat, and a copy of the rule set
it was created with (values, boards and card decks). A game therefore
carries on with its own rules even if the rule set file is edited or
deleted in the meantime. Finished games are deleted: scores.csv keeps their
history.
"""

import dataclasses
import json
import logging
import os
import time

from game_engine import Card, Game, JOIN_CODE_ALPHABET
from rulesets import RuleSet

log = logging.getLogger("recursopoly.saves")

SAVE_VERSION = 1


def _save_path(saves_dir, code):
    code = str(code or "").upper()
    if not code or any(ch not in JOIN_CODE_ALPHABET for ch in code):
        raise ValueError(f"Not a join code: {code!r}")
    return os.path.join(saves_dir, code + ".json")


def ruleset_snapshot(ruleset):
    """The parts of a rule set a game needs, as JSON-safe data."""
    return {
        "id": ruleset.id,
        "name": ruleset.name,
        "description": ruleset.description,
        "values": dict(ruleset.values),
        "boards": ruleset.boards,
        "decks": {name: [dataclasses.asdict(c) for c in cards] for name, cards in ruleset.decks.items()},
        "sections": ruleset.sections,
        "board_paths": list(ruleset.board_paths),
        "card_paths": dict(ruleset.card_paths),
        "passcode": ruleset.passcode,
    }


def ruleset_from_snapshot(data):
    return RuleSet(
        id=data["id"],
        name=data["name"],
        description=data.get("description", ""),
        values=dict(data["values"]),
        boards=data["boards"],
        decks={name: [Card(**c) for c in cards] for name, cards in data.get("decks", {}).items()},
        sections=data.get("sections", {}),
        board_paths=list(data.get("board_paths", [])),
        card_paths=dict(data.get("card_paths", {})),
        passcode=data.get("passcode", ""),
    )


def save_game(saves_dir, game, chat=None):
    """Write one game's save file (atomically, so a crash never leaves half
    a file)."""
    os.makedirs(saves_dir, exist_ok=True)
    path = _save_path(saves_dir, game.join_code)
    data = {
        "version": SAVE_VERSION,
        "saved_at": time.time(),
        "ruleset": ruleset_snapshot(game.ruleset),
        "game": game.snapshot(),
        "chat": list(chat or []),
    }
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(data, fh, ensure_ascii=False, separators=(",", ":"))
    os.replace(tmp, path)
    return path


def delete_save(saves_dir, code):
    """Remove a game's save file, if there is one."""
    try:
        os.remove(_save_path(saves_dir, code))
    except (FileNotFoundError, ValueError):
        pass


def load_game(path, now=None):
    """Load one save file. Returns (game, chat)."""
    with open(path, encoding="utf-8") as fh:
        data = json.load(fh)
    if data.get("version") != SAVE_VERSION:
        raise ValueError(f"unsupported save version {data.get('version')!r}")
    game = Game.restore(data["game"], ruleset_from_snapshot(data["ruleset"]), now=now)
    return game, data.get("chat", [])


def load_games(saves_dir, keep_days=0, now=None):
    """Load every save in ``saves_dir``. Returns {code: (game, chat)}.
    Saves untouched for ``keep_days`` (0 = no limit) are deleted. A broken
    save is renamed to .broken and skipped, so it can't stop the server
    starting."""
    loaded = {}
    if not os.path.isdir(saves_dir):
        return loaded
    now = time.time() if now is None else now
    for filename in sorted(os.listdir(saves_dir)):
        if not filename.endswith(".json"):
            continue
        path = os.path.join(saves_dir, filename)
        if keep_days and now - os.path.getmtime(path) > keep_days * 86400:
            log.info("Recursopoly save %s is over %d days old; deleted", filename, keep_days)
            os.remove(path)
            continue
        try:
            game, chat = load_game(path, now=now)
        except (OSError, ValueError, KeyError, TypeError) as err:
            log.warning("Recursopoly save %s could not be loaded (%s); renamed to .broken", filename, err)
            try:
                os.replace(path, path + ".broken")
            except OSError:
                pass
            continue
        loaded[game.join_code] = (game, chat)
    return loaded
