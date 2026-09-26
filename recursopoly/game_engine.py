"""Recursopoly game engine.

Pure Python game rules with no Flask (or any web) imports and no file access,
so everything here can be unit-tested on its own. The web layer (app.py)
calls into this module for every rule decision and simply broadcasts the
resulting state; rulesets.py reads the JSON files a game is built from.

Key design points:

* Every tunable number (money, building limits, jail, house rules, pooled
  square behaviour) comes from the game's rule set via ``Game._rule``. The
  engine never hard-codes a value a rule set could define.
* Boards come from JSON property sets. Every :class:`Board` has a
  ``board_id``; a :class:`Game` holds its boards in a dict keyed by that id.
* A player's position is a :class:`Position` of ``(board_id, index)``, so
  Phase 5 train travel can move tokens between boards.
* A :class:`Square` has a ``type`` plus a free-form ``attributes`` dict.
  Ownership is a list of stakes ``[{"player", "percent"}]``: a normal
  property is one stake of 100%, a pooled square has several.
* Turns are a small state machine (:class:`TurnState`). ``AWAITING_DECISION``
  pauses the turn until the active player answers ``Game.pending_decision``.
* Payments go through ``Game._pay``. A player who can't pay runs up a debt
  and must raise money or declare bankruptcy. Money paid to the bank may be
  diverted into a pooled square's pot, as the rule set says.
* The engine never writes files. Anything worth logging is queued as a
  :class:`GameEvent` which the caller drains with :meth:`Game.drain_events`.
"""

import copy
import random
import secrets
import time
from dataclasses import dataclass, field

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Square types with built-in behaviour. Boards may use any other type name
# too (e.g. "strip_club"); unknown types are labels unless the square is
# pooled ("stakeholder": true) or has a card deck of the same name.
KNOWN_SQUARE_TYPES = (
    "go",
    "property",
    "station",
    "utility",
    "tax",
    "chance",
    "community_chest",
    "jail",
    "free_parking",
    "go_to_jail",
)

# Square types that can be bought outright and charge rent.
OWNABLE_TYPES = ("property", "station", "utility")

# Card effects understood by Game._apply_card (see cards/chance.txt).
CARD_EFFECTS = (
    "move_to",
    "move_by",
    "move_to_nearest",
    "collect",
    "pay",
    "collect_from_each",
    "pay_each",
    "repairs",
    "go_to_jail",
    "get_out_of_jail_free",
)

# Join codes avoid characters that are easily confused (O/0, I/1).
JOIN_CODE_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"

# Token colours, assigned in join order (cycled if max_players is larger).
PLAYER_COLOURS = (
    "#e6194b",  # red
    "#3cb44b",  # green
    "#4363d8",  # blue
    "#f58231",  # orange
    "#911eb4",  # purple
    "#42d4f4",  # cyan
    "#f032e6",  # magenta
    "#9a6324",  # brown
)

MAX_NAME_LENGTH = 20
MAX_LOG_LINES = 200
# How many card-triggered moves can chain in one turn (e.g. "go back three
# spaces" onto another card square).
MAX_CARD_CHAIN = 3


class GameStatus:
    LOBBY = "lobby"
    IN_PROGRESS = "in_progress"
    ENDED = "ended"


class TurnState:
    """States of the active player's turn."""

    WAITING_TO_ROLL = "waiting_to_roll"
    # The active player must answer Game.pending_decision (buy or decline,
    # buy a stake, pay a debt or go bankrupt, ...) before the turn continues.
    AWAITING_DECISION = "awaiting_decision"
    TURN_OVER = "turn_over"


class GameError(Exception):
    """A rule was broken. ``code`` is machine-readable, str(err) is for humans."""

    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


def money(amount):
    return f"£{amount}"


def _same(a, b):
    return (a or "").lower() == (b or "").lower()


# ---------------------------------------------------------------------------
# Board
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Position:
    """Where a token is: which board, and which square on that board."""

    board_id: int
    index: int

    def to_dict(self):
        return {"board_id": self.board_id, "index": self.index}


@dataclass
class Square:
    index: int
    name: str
    type: str
    # Everything else from the board file (group, price, rent, house_rents,
    # hotel_rents, stakeholder, max_stakes, buy_in, ...) plus game state:
    # stakes, houses, hotels, mortgaged, pot.
    attributes: dict = field(default_factory=dict)

    # -- ownership -----------------------------------------------------------

    @property
    def stakes(self):
        """[{"player": name, "percent": n, ...}]. Empty when nobody owns it."""
        return self.attributes.get("stakes", [])

    @property
    def owner(self):
        """The sole owner (one stake of 100%), or None."""
        stakes = self.stakes
        if len(stakes) == 1 and stakes[0]["percent"] >= 100:
            return stakes[0]["player"]
        return None

    def set_owner(self, name):
        if name:
            self.attributes["stakes"] = [{"player": name, "percent": 100}]
        else:
            self.attributes.pop("stakes", None)

    def stake_of(self, name):
        """The stake entry held by ``name``, or None."""
        for stake in self.stakes:
            if _same(stake["player"], name):
                return stake
        return None

    @property
    def pooled(self):
        """A stakeholder square: collects a pot shared by its stakeholders."""
        return bool(self.attributes.get("stakeholder"))

    @property
    def pot(self):
        return self.attributes.get("pot", 0)

    # -- buildings -----------------------------------------------------------

    @property
    def houses(self):
        return self.attributes.get("houses", 0)

    @property
    def hotels(self):
        return self.attributes.get("hotels", 0)

    @property
    def mortgaged(self):
        return bool(self.attributes.get("mortgaged"))

    def int_list(self, key):
        """A list of whole numbers from the board file (a list or one number)."""
        raw = self.attributes.get(key)
        if raw is None:
            return []
        if isinstance(raw, list):
            return [int(v) for v in raw]
        return [int(raw)]

    def to_dict(self):
        attrs = copy.deepcopy(self.attributes)
        attrs["owner"] = self.owner  # convenience for the page
        return {"index": self.index, "name": self.name, "type": self.type, "attributes": attrs}


class Board:
    """One looping track of squares."""

    def __init__(self, board_id, squares, name=None, groups=None, attributes=None):
        if not squares:
            raise ValueError("A Recursopoly board needs at least one square")
        self.board_id = board_id
        self.name = name or f"Board {board_id}"
        self.squares = list(squares)
        # Colour groups: {"brown": {"name": "Brown", "colour": "#8b4a2b"}}.
        self.groups = dict(groups or {})
        # Other board-level keys from the board file (Phase 5: go_salary, ...).
        self.attributes = dict(attributes or {})

    def __len__(self):
        return len(self.squares)

    @property
    def size(self):
        return len(self.squares)

    def square(self, index):
        return self.squares[index % self.size]

    def find_first(self, square_type):
        """Index of the first square of ``square_type``, or None."""
        for sq in self.squares:
            if sq.type == square_type:
                return sq.index
        return None

    def find_by_name(self, name):
        """Index of the first square called ``name`` (any case), or None."""
        for sq in self.squares:
            if _same(sq.name, name):
                return sq.index
        return None

    def group(self, group):
        """All property squares in colour group ``group``."""
        return [sq for sq in self.squares
                if sq.type == "property" and sq.attributes.get("group") == group]

    def pools(self):
        return [sq for sq in self.squares if sq.pooled]

    @property
    def go_index(self):
        found = self.find_first("go")
        return 0 if found is None else found

    @property
    def jail_index(self):
        found = self.find_first("jail")
        # Fall back to the classic "first corner" if the board has no jail.
        return self.size // 4 if found is None else found

    def to_dict(self):
        return {
            "board_id": self.board_id,
            "name": self.name,
            "size": self.size,
            "groups": copy.deepcopy(self.groups),
            "attributes": dict(self.attributes),
            "squares": [sq.to_dict() for sq in self.squares],
        }


def _whole(value, where):
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise ValueError(f"{where} must be a whole number of 0 or more")
    return value


def parse_board_data(data, board_id):
    """Build a :class:`Board` from parsed board JSON.

    Format (see boards/classic_board.json)::

        {"name": "...", "groups": {"brown": {"name": "Brown", "colour": "#8b4a2b"}},
         "squares": [{"index": 0, "name": "GO", "type": "go"},
                     {"index": 1, "name": "Old Kent Road", "type": "property",
                      "group": "brown", "price": 60, "rent": 2, ...}, ...]}

    The data is copied, so each game gets its own squares to mutate.
    """
    if not isinstance(data, dict) or not isinstance(data.get("squares"), list) or not data["squares"]:
        raise ValueError(f"board {board_id}: needs a non-empty 'squares' list")
    squares = []
    for pos, raw in enumerate(data["squares"]):
        where = f"board {board_id} square {pos}"
        if not isinstance(raw, dict):
            raise ValueError(f"{where}: each square must be an object")
        if raw.get("index", pos) != pos:
            raise ValueError(f"{where}: index must be {pos} (squares are listed in order)")
        name, sq_type = raw.get("name"), raw.get("type")
        if not isinstance(name, str) or not name.strip() or not isinstance(sq_type, str) or not sq_type:
            raise ValueError(f"{where}: needs a 'name' and a 'type'")
        attrs = copy.deepcopy({k: v for k, v in raw.items() if k not in ("index", "name", "type")})
        for key in ("price", "rent", "amount", "house_cost", "hotel_cost", "buy_in"):
            if key in attrs:
                _whole(attrs[key], f"{where} ({name}) {key}")
        for key in ("house_rents", "hotel_rents", "rents", "dice_multipliers"):
            if key in attrs:
                values = attrs[key] if isinstance(attrs[key], list) else [attrs[key]]
                for v in values:
                    _whole(v, f"{where} ({name}) {key}")
        if attrs.get("stakeholder"):
            if _whole(attrs.get("max_stakes"), f"{where} ({name}) max_stakes") < 1:
                raise ValueError(f"{where} ({name}): max_stakes must be at least 1")
            _whole(attrs.get("buy_in"), f"{where} ({name}) buy_in")
        # Game state never comes from the file.
        for key in ("stakes", "houses", "hotels", "mortgaged", "pot", "owner"):
            attrs.pop(key, None)
        squares.append(Square(pos, name.strip(), sq_type.strip().lower(), attrs))
    meta = {k: v for k, v in data.items() if k not in ("name", "groups", "squares")}
    return Board(board_id, squares, name=data.get("name"), groups=data.get("groups"), attributes=meta)


# ---------------------------------------------------------------------------
# Cards
# ---------------------------------------------------------------------------


def _parse_value(text):
    """Turn attribute text into an int where possible, else keep the string."""
    text = text.strip()
    try:
        return int(text)
    except ValueError:
        return text


def parse_attributes(text):
    """Parse ``key=value; key=value`` into a dict."""
    attrs = {}
    for part in text.split(";"):
        part = part.strip()
        if not part:
            continue
        if "=" in part:
            key, value = part.split("=", 1)
            attrs[key.strip()] = _parse_value(value)
        else:
            attrs[part] = True  # bare flag
    return attrs


@dataclass
class Card:
    deck: str
    text: str
    effect: str
    params: dict = field(default_factory=dict)


def parse_cards_text(text, deck):
    """Parse a deck file: one ``text | effect | key=value; ...`` card per line."""
    cards = []
    for line_no, raw in enumerate(text.splitlines(), start=1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        parts = [p.strip() for p in line.split("|")]
        if len(parts) < 2 or not parts[0]:
            raise ValueError(f"{deck} card line {line_no}: expected 'text | effect'")
        effect = parts[1].lower()
        if effect not in CARD_EFFECTS:
            raise ValueError(f"{deck} card line {line_no}: unknown effect {effect!r}")
        params = parse_attributes(parts[2]) if len(parts) > 2 else {}
        cards.append(Card(deck, parts[0], effect, params))
    return cards


class Deck:
    """A shuffled pile. Drawn cards go to the bottom, except Get Out of Jail
    Free cards, which the player keeps until they are used."""

    def __init__(self, name, cards, rng):
        self.name = name
        self.cards = list(cards)
        rng.shuffle(self.cards)

    def draw(self):
        if not self.cards:
            return None
        card = self.cards.pop(0)
        if card.effect != "get_out_of_jail_free":
            self.cards.append(card)
        return card

    def put_back(self, card):
        self.cards.append(card)


# ---------------------------------------------------------------------------
# Players
# ---------------------------------------------------------------------------


@dataclass
class Player:
    name: str
    colour: str
    money: int
    position: Position
    join_order: int
    # Secret handed to the player's browser so it can reclaim this seat after
    # a page change or reconnect.
    token: str = field(default_factory=lambda: secrets.token_urlsafe(16))
    connected: bool = True
    disconnected_since: float = None
    left: bool = False  # left voluntarily; never gets another turn
    bankrupt: bool = False
    doubles_in_a_row: int = 0
    laps: int = 0  # times the player has passed or landed on Go
    in_jail: bool = False
    jail_turns: int = 0  # failed attempts to roll doubles while in jail
    jail_cards: list = field(default_factory=list)  # held Get Out of Jail Free Cards
    # Unpaid amounts: [{"creditor": name or None, "amount", "reason", "category"}]
    debts: list = field(default_factory=list)
    # Extensible per-player state for later phases (journeys, ...).
    attributes: dict = field(default_factory=dict)

    @property
    def in_game(self):
        """Still playing (not bankrupt and hasn't left)."""
        return not self.left and not self.bankrupt

    @property
    def active(self):
        """Can this player take a turn right now?"""
        return self.connected and self.in_game

    @property
    def debt_total(self):
        return sum(d["amount"] for d in self.debts)

    def to_dict(self):
        # The token is deliberately NOT included: state is broadcast to all.
        return {
            "name": self.name,
            "colour": self.colour,
            "money": self.money,
            "position": self.position.to_dict(),
            "join_order": self.join_order,
            "connected": self.connected,
            "left": self.left,
            "bankrupt": self.bankrupt,
            "laps": self.laps,
            "in_jail": self.in_jail,
            "jail_turns": self.jail_turns,
            "jail_cards": len(self.jail_cards),
            "debt": self.debt_total,
            "attributes": dict(self.attributes),
        }


@dataclass
class GameEvent:
    """Something worth recording in scores.csv."""

    event_type: str
    player_name: str = ""
    board_id: int = None
    position: int = None
    money: int = None
    details: str = ""


# ---------------------------------------------------------------------------
# Join codes
# ---------------------------------------------------------------------------


def generate_join_code(length, existing=(), rng=None):
    """Return a join code of ``length`` characters not present in ``existing``."""
    rng = rng or secrets.SystemRandom()
    for _ in range(10000):
        code = "".join(rng.choice(JOIN_CODE_ALPHABET) for _ in range(length))
        if code not in existing:
            return code
    raise RuntimeError("Could not generate a unique Recursopoly join code")


def normalise_join_code(code):
    return (code or "").strip().upper()


def clean_name(name):
    """Validate and tidy a display name."""
    name = " ".join((name or "").split())
    if not name:
        raise GameError("bad_name", "Please enter a display name.")
    if len(name) > MAX_NAME_LENGTH:
        raise GameError("bad_name", f"Names can be at most {MAX_NAME_LENGTH} characters.")
    return name


# ---------------------------------------------------------------------------
# Game
# ---------------------------------------------------------------------------


class Game:
    """One Recursopoly game session, governed by one rule set."""

    def __init__(self, join_code, ruleset, rng=None):
        """
        ``ruleset``: a rulesets.RuleSet (anything with ``id``, ``name``,
        ``description``, ``values``, ``board`` and ``decks``).
        ``rng``: a random.Random, injectable so tests can fix dice and decks.
        """
        self.join_code = join_code
        self.ruleset = ruleset
        self.rules = dict(ruleset.values)
        self.rng = rng or random.SystemRandom()
        self.boards = {0: parse_board_data(ruleset.board, 0)}
        self.decks = {name: Deck(name, cards, self.rng) for name, cards in ruleset.decks.items()}

        self.status = GameStatus.LOBBY
        self.players = []  # join order
        self.host_name = None
        self.current_index = None  # index into self.players
        self.turn_state = None
        self.turn_number = 0
        self.last_roll = None
        self.last_card = None  # {"player", "deck", "label", "text"} of the latest card
        # What the active player must choose before the turn can continue
        # (only set while turn_state is AWAITING_DECISION): a "buy",
        # "buy_stake" or "debt" decision.
        self.pending_decision = None
        self.trades = []  # open trade offers
        self._next_trade_id = 1
        self.eliminated = []  # names, in the order players went bankrupt or left
        self.winner = None
        self.created_at = time.time()

        self.log = []  # human-readable lines for the event log panel
        self._events = []  # GameEvent queue for scores.csv

    # -- helpers -----------------------------------------------------------

    def _rule(self, key):
        """A value from this game's rule set (always present: rule sets fall
        back to classic for anything they leave out)."""
        return self.rules[key]

    @property
    def start_board_id(self):
        return min(self.boards)

    def board_for(self, player):
        return self.boards[player.position.board_id]

    def square_at(self, position):
        return self.boards[position.board_id].square(position.index)

    def go_salary_for(self, board):
        # Phase 5: nested boards can override the Go salary in their file.
        return board.attributes.get("go_salary", self._rule("go_salary"))

    def get_player(self, name):
        for p in self.players:
            if _same(p.name, name):
                return p
        return None

    def _require_player(self, name):
        player = self.get_player(name or "")
        if player is None:
            raise GameError("unknown_player", "You are not a player in this game.")
        return player

    def players_in_game(self):
        return [p for p in self.players if p.in_game]

    @property
    def current_player(self):
        if self.current_index is None or not self.players:
            return None
        return self.players[self.current_index]

    def _require_in_progress(self):
        if self.status != GameStatus.IN_PROGRESS:
            raise GameError("not_started", "The game is not in progress.")

    def _require_current(self, name):
        self._require_in_progress()
        player = self._require_player(name)
        if player is not self.current_player:
            raise GameError("not_your_turn", "It's not your turn.")
        return player

    def _say(self, message):
        self.log.append({"time": time.time(), "message": message})
        del self.log[:-MAX_LOG_LINES]

    def _event(self, event_type, player=None, details=""):
        self._events.append(
            GameEvent(
                event_type=event_type,
                player_name=player.name if player else "",
                board_id=player.position.board_id if player else None,
                position=player.position.index if player else None,
                money=player.money if player else None,
                details=details,
            )
        )

    def drain_events(self):
        """Return and clear queued GameEvents (for the score logger)."""
        events, self._events = self._events, []
        return events

    # -- lobby -------------------------------------------------------------

    def add_player(self, name, token=None):
        """Join the game, or reclaim a seat.

        Returns ``(player, rejoined)``. A player can reclaim an existing seat
        if they supply its token, or if that seat is currently disconnected
        (same name + code rejoin).
        """
        name = clean_name(name)
        existing = self.get_player(name)

        if existing is not None:
            if existing.left:
                raise GameError("left", f"{existing.name} has left this game.")
            if (token and secrets.compare_digest(token, existing.token)) or not existing.connected:
                self._reconnect(existing)
                return existing, True
            raise GameError("duplicate_name", f"The name '{name}' is already taken in this game.")

        if self.status == GameStatus.IN_PROGRESS:
            raise GameError("started", "That game has already started.")
        if self.status == GameStatus.ENDED:
            raise GameError("ended", "That game has ended.")
        if len(self.players) >= self._rule("max_players"):
            raise GameError("full", "That game is full.")

        order = len(self.players)
        player = Player(
            name=name,
            colour=PLAYER_COLOURS[order % len(PLAYER_COLOURS)],
            money=self._rule("starting_money"),
            position=Position(self.start_board_id, self.boards[self.start_board_id].go_index),
            join_order=order,
        )
        self.players.append(player)
        if self.host_name is None:
            self.host_name = player.name
        self._say(f"{player.name} joined the game.")
        return player, False

    def _reconnect(self, player):
        was_connected = player.connected
        player.connected = True
        player.disconnected_since = None
        # Only announce the return if the absence was announced; quick page
        # changes (lobby -> game) should not clutter the event log.
        if not was_connected and player.attributes.pop("announced_offline", False):
            self._say(f"{player.name} reconnected.")

    def mark_disconnected(self, name, now=None):
        """Flag a player as disconnected. Their turn is skipped later by
        :meth:`skip_turn_if_disconnected` once the grace period is over."""
        player = self.get_player(name)
        if player is None or not player.connected:
            return
        player.connected = False
        player.disconnected_since = time.time() if now is None else now

    def check_disconnect(self, name, now=None, grace=0):
        """Called once a disconnected player's grace period is over.

        If they are still gone, announce it in the log and skip their turn if
        it is theirs. Returns True if anything changed.
        """
        player = self.get_player(name)
        if player is None or player.connected or player.disconnected_since is None:
            return False
        now = time.time() if now is None else now
        if now - player.disconnected_since < grace:
            return False
        changed = False
        if player.in_game and not player.attributes.get("announced_offline"):
            player.attributes["announced_offline"] = True
            self._say(f"{player.name} disconnected.")
            changed = True
        return self.skip_turn_if_disconnected(now=now, grace=grace) or changed

    def remove_player(self, name):
        """A player leaves voluntarily.

        In the lobby their seat is freed (and host passes on if needed). Once
        the game has started they are out: their properties and stakes go
        back to the bank and they never play again.
        """
        player = self._require_player(name)
        if self.status == GameStatus.LOBBY:
            self.players.remove(player)
            for i, p in enumerate(self.players):
                p.join_order = i
                p.colour = PLAYER_COLOURS[i % len(PLAYER_COLOURS)]
            if self.host_name == player.name:
                self.host_name = self.players[0].name if self.players else None
                if self.host_name:
                    self._say(f"{self.host_name} is now the host.")
            self._say(f"{player.name} left the game.")
            return

        was_current = player is self.current_player
        was_in_game = player.in_game
        player.left = True
        player.connected = False
        self._say(f"{player.name} left the game.")
        if self.status != GameStatus.IN_PROGRESS or not was_in_game:
            return
        self._release_assets_to_bank(player)
        player.debts = []
        self.eliminated.append(player.name)
        self._cancel_trades_with(player)
        if self._check_for_winner():
            return
        if was_current:
            self._advance_turn()

    def is_host(self, name):
        return self.host_name is not None and _same(name, self.host_name)

    def start(self, requested_by):
        if not self.is_host(requested_by):
            raise GameError("not_host", "Only the host can start the game.")
        if self.status != GameStatus.LOBBY:
            raise GameError("started", "The game has already started.")
        playing = [p for p in self.players if not p.left]
        if len(playing) < self._rule("min_players"):
            raise GameError(
                "not_enough_players",
                f"At least {self._rule('min_players')} players are needed to start.",
            )
        if not any(p.active for p in playing):
            raise GameError("not_enough_players", "No connected players to start with.")

        self.status = GameStatus.IN_PROGRESS
        self.turn_number = 1
        self.current_index = None
        self._say(f"The game of Recursopoly has started, playing {self.ruleset.name} rules!")
        for p in self.players:
            self._event("game_started", p, details=f"players={len(self.players)}; ruleset={self.ruleset.id}")
        self._begin_turn(self._next_active_index(-1))

    # -- turns -------------------------------------------------------------

    def _next_active_index(self, from_index):
        """Index of the next connected, in-game player after ``from_index``
        (wrapping), or None if nobody can play."""
        n = len(self.players)
        for step in range(1, n + 1):
            idx = (from_index + step) % n
            if self.players[idx].active:
                return idx
        return None

    def _begin_turn(self, index):
        if index is None:
            # Nobody is connected. Keep the turn where it is; it will be
            # handed on when someone reconnects.
            self.turn_state = TurnState.WAITING_TO_ROLL
            return
        self.current_index = index
        self.pending_decision = None
        player = self.players[index]
        player.doubles_in_a_row = 0
        self.turn_state = TurnState.WAITING_TO_ROLL
        self._say(f"It's {player.name}'s turn." + (" They are in jail." if player.in_jail else ""))
        if player.debts:
            # Debts run up on someone else's turn are settled before rolling.
            self._open_debt_decision(player, turn_start=True)

    def _advance_turn(self):
        if self.status != GameStatus.IN_PROGRESS:
            return
        self.turn_state = TurnState.TURN_OVER
        self.pending_decision = None
        start = -1 if self.current_index is None else self.current_index
        nxt = self._next_active_index(start)
        if nxt is not None and nxt <= start:
            self.turn_number += 1
        self._begin_turn(nxt)

    def skip_turn_if_disconnected(self, now=None, grace=0):
        """Skip the current turn if its player has been gone for ``grace`` s.

        Also hands the turn on if the current player is inactive and nobody
        held the turn (e.g. everyone had disconnected). Returns True if the
        turn moved.
        """
        if self.status != GameStatus.IN_PROGRESS:
            return False
        player = self.current_player
        if player is None:
            idx = self._next_active_index(-1)
            if idx is not None:
                self._begin_turn(idx)
                return True
            return False
        if player.active:
            return False
        if player.in_game and player.disconnected_since is not None:
            now = time.time() if now is None else now
            if now - player.disconnected_since < grace:
                return False
        before = self.current_index
        if player.in_game:
            self._say(f"{player.name} is not connected; skipping their turn.")
        decision = self.pending_decision
        if decision and decision["type"] in ("buy", "buy_stake"):
            self._say(f"{player.name} did not buy {decision['square']}.")
        # An unpaid debt stays with the player and is settled on their next turn.
        self._advance_turn()
        return self.current_index != before

    def roll_dice(self):
        return self.rng.randint(1, 6), self.rng.randint(1, 6)

    def roll(self, name, dice=None):
        """The named player rolls the dice and moves.

        ``dice`` may be supplied by tests; the web layer never passes it, so
        all rolling happens here on the server.
        Returns a dict describing what happened.
        """
        player = self._require_current(name)
        if self.turn_state != TurnState.WAITING_TO_ROLL:
            raise GameError("bad_state", "You can't roll right now.")

        d1, d2 = dice if dice is not None else self.roll_dice()
        total = d1 + d2
        doubles = d1 == d2
        self.last_roll = {"player": player.name, "dice": [d1, d2], "total": total, "doubles": doubles}
        result = {"dice": [d1, d2], "total": total, "doubles": doubles,
                  "passed_go": 0, "jailed": False, "roll_again": False}
        roll_details = f"dice={d1}+{d2}; total={total}; doubles={'yes' if doubles else 'no'}"

        if player.in_jail:
            return self._roll_in_jail(player, d1, d2, result, roll_details)

        if doubles:
            player.doubles_in_a_row += 1
        else:
            player.doubles_in_a_row = 0

        limit = self._rule("doubles_before_jail")
        if doubles and player.doubles_in_a_row >= limit:
            self._say(f"{player.name} rolled {d1} + {d2}: {limit} doubles in a row!")
            self._event("roll", player, details=roll_details)
            self._send_to_jail(player, f"{limit} doubles in a row")
            result["jailed"] = True
            self._advance_turn()
            return result

        self._move_and_land(player, total, result, roll_details, f"rolled {d1} + {d2}")
        return self._after_move(player, doubles, result)

    def _move_and_land(self, player, steps, result, roll_details, verb):
        result["passed_go"] = self._move_forward(player, steps)
        square = self.square_at(player.position)
        self._say(f"{player.name} {verb} and moved to {square.name}.")
        self._event("roll", player, details=f"{roll_details}; square={square.name}")
        result["square"] = square.to_dict()
        self._resolve_landing(player, square)

    def _after_move(self, player, doubles, result):
        """Decide what happens once a move and its landing effects are done."""
        if self.status != GameStatus.IN_PROGRESS:
            return result
        if player.in_jail:
            # Sent to jail by the square or a card: the turn ends, even on doubles.
            result["jailed"] = True
            self._advance_turn()
            return result
        if player.debts:
            self._open_debt_decision(player, roll_again=doubles)
        elif self.turn_state == TurnState.AWAITING_DECISION:
            # A buy offer: the turn pauses until the player calls decide().
            self.pending_decision["roll_again"] = doubles
        else:
            result["roll_again"] = self._finish_move(player, doubles)
            return result
        result["decision"] = dict(self.pending_decision)
        return result

    def _finish_move(self, player, doubles):
        """End of a move: roll again on doubles, otherwise pass the turn.
        Returns True if the player rolls again."""
        if doubles:
            self._say(f"{player.name} rolled doubles and rolls again.")
            self.turn_state = TurnState.WAITING_TO_ROLL
            return True
        self._advance_turn()
        return False

    def decide(self, name, choice):
        """The active player answers the pending decision.

        * "buy" and "buy_stake" decisions: choice "buy" or "decline".
        * "debt" decisions: choice "pay" (once enough money is raised) or
          "bankrupt".
        Phase 5 adds train tickets here.
        """
        player = self._require_current(name)
        decision = self.pending_decision
        if self.turn_state != TurnState.AWAITING_DECISION or decision is None:
            raise GameError("bad_state", "There is nothing to decide right now.")

        if decision["type"] in ("buy", "buy_stake"):
            if choice not in ("buy", "decline"):
                raise GameError("bad_choice", "Choose to buy or decline.")
            square = self.square_at(Position(decision["board_id"], decision["index"]))
            if choice == "buy" and decision["type"] == "buy":
                self._buy(player, square)
            elif choice == "buy":
                self._buy_stake(player, square)
            else:
                self._say(f"{player.name} decided not to buy {square.name}.")
            self.pending_decision = None
            self._finish_move(player, decision.get("roll_again", False))
            return

        if decision["type"] == "debt":
            if choice == "pay":
                self._settle_debts(player)
                self.pending_decision = None
                if decision.get("turn_start"):
                    self.turn_state = TurnState.WAITING_TO_ROLL
                else:
                    self._finish_move(player, decision.get("roll_again", False))
            elif choice == "bankrupt":
                self._declare_bankrupt(player)
            else:
                raise GameError("bad_choice", "Choose to pay or declare bankruptcy.")
            return

        raise GameError("bad_choice", "Unknown decision.")  # pragma: no cover

    # -- moving ------------------------------------------------------------

    def _move_forward(self, player, steps):
        """Move ``steps`` squares along the player's current board, paying the
        Go salary for each pass of (or landing on) Go. Returns passes of Go."""
        board = self.board_for(player)
        start = player.position.index
        go = board.go_index
        # Number of squares k in (start, start+steps] that are Go.
        passes = sum(1 for k in range(start + 1, start + steps + 1) if k % board.size == go)
        player.position = Position(board.board_id, (start + steps) % board.size)
        salary = self.go_salary_for(board)
        for _ in range(passes):
            player.laps += 1
            player.money += salary
            self._say(f"{player.name} passed Go and collected {money(salary)}.")
            self._event("passed_go", player, details=f"salary={salary}")
        return passes

    def _move_to_index(self, player, index):
        """Advance (forwards, collecting Go) to square ``index``."""
        board = self.board_for(player)
        steps = (index - player.position.index) % board.size
        self._move_forward(player, steps)

    # -- jail --------------------------------------------------------------

    def _send_to_jail(self, player, reason):
        board = self.board_for(player)
        player.position = Position(board.board_id, board.jail_index)
        player.doubles_in_a_row = 0
        player.in_jail = True
        player.jail_turns = 0
        self._say(f"{player.name} goes to jail ({reason}).")
        self._event("jailed", player, details=reason)

    def _release_from_jail(self, player, how):
        player.in_jail = False
        player.jail_turns = 0
        self._say(f"{player.name} is out of jail ({how}).")
        self._event("released_from_jail", player, details=how)

    def _roll_in_jail(self, player, d1, d2, result, roll_details):
        """A jailed player's roll: doubles free them; otherwise they wait, and
        on the last allowed attempt they must pay the fine and move."""
        total = d1 + d2
        if d1 == d2:
            self._release_from_jail(player, "rolled doubles")
            # Leaving jail on doubles does not earn another roll.
            self._move_and_land(player, total, result, roll_details, f"rolled {d1} + {d2}")
            return self._after_move(player, False, result)

        player.jail_turns += 1
        max_turns = self._rule("max_jail_turns")
        if player.jail_turns < max_turns:
            self._say(f"{player.name} rolled {d1} + {d2} and stays in jail "
                      f"(attempt {player.jail_turns} of {max_turns}).")
            self._event("roll", player, details=f"{roll_details}; in_jail=yes")
            self._advance_turn()
            return result

        fine = self._rule("jail_fine")
        self._say(f"{player.name} rolled {d1} + {d2} on their last try and must pay the "
                  f"{money(fine)} fine.")
        self._pay(player, fine, None, "jail fine", category="fines")
        self._event("jail_fine_paid", player, details=f"amount={fine}; forced=yes")
        self._release_from_jail(player, "paid the fine")
        self._move_and_land(player, total, result, roll_details, "left jail")
        return self._after_move(player, False, result)

    def pay_jail_fine(self, name):
        """Pay the fine before rolling to leave jail; the player then rolls
        normally."""
        player = self._require_current(name)
        if not player.in_jail:
            raise GameError("not_in_jail", "You are not in jail.")
        if self.turn_state != TurnState.WAITING_TO_ROLL:
            raise GameError("bad_state", "You can only pay the fine before rolling.")
        fine = self._rule("jail_fine")
        if player.money < fine:
            raise GameError("cant_afford", f"You need {money(fine)} to pay the fine.")
        self._pay(player, fine, None, "jail fine", category="fines")
        self._event("jail_fine_paid", player, details=f"amount={fine}; forced=no")
        self._release_from_jail(player, f"paid the {money(fine)} fine")

    def use_jail_card(self, name):
        player = self._require_current(name)
        if not player.in_jail:
            raise GameError("not_in_jail", "You are not in jail.")
        if self.turn_state != TurnState.WAITING_TO_ROLL:
            raise GameError("bad_state", "You can only use a card before rolling.")
        if not player.jail_cards:
            raise GameError("no_card", "You don't have a Get Out of Jail Free card.")
        card = player.jail_cards.pop(0)
        self._return_card(card)
        self._event("jail_card_used", player, details=f"deck={card.deck}")
        self._release_from_jail(player, "used a Get Out of Jail Free card")

    def _return_card(self, card):
        deck = self.decks.get(card.deck)
        if deck is not None:
            deck.put_back(card)

    # -- landing -----------------------------------------------------------

    def _resolve_landing(self, player, square, depth=0, rent_multiplier=1):
        """Apply the effect of landing on ``square``.

        Pooled squares pay out and offer stakes; ownable squares offer a
        purchase (pausing the turn) or charge rent; tax is charged through
        _pay; card squares draw from their deck. Go's salary is handled while
        moving. Phase 5 adds station travel offers.
        """
        if square.pooled:
            self._land_on_pool(player, square)
        elif square.type in OWNABLE_TYPES:
            if not square.stakes:
                self._offer_purchase(player, square)
            elif square.owner and not _same(square.owner, player.name):
                self._charge_rent(player, square, rent_multiplier)
        elif square.type == "tax":
            amount = square.attributes.get("amount", 0)
            if amount and self._pay(player, amount, None, square.name, category="taxes"):
                self._say(f"{player.name} paid {money(amount)} {square.name}.")
                self._event("tax_paid", player, details=f"square={square.name}; amount={amount}")
        elif square.type == "go_to_jail":
            self._send_to_jail(player, f"landed on {square.name}")
        elif square.type in self.decks:
            self._draw_card(player, square, depth)

    def _may_buy(self, player, square, price):
        """Common checks before offering anything for sale. Returns True if
        the offer can be made."""
        if player.debts:
            return False  # no shopping while in debt
        if self._rule("must_lap_before_buying") and player.laps < 1:
            self._say(f"{player.name} must complete a lap of the board before buying.")
            return False
        if player.money < price:
            self._say(f"{player.name} can't afford {square.name} ({money(price)}).")
            return False
        return True

    def _offer_purchase(self, player, square):
        price = square.attributes.get("price")
        if price is None or not self._may_buy(player, square, price):
            return  # no price in the board file means not for sale
        self.pending_decision = {
            "type": "buy",
            "player": player.name,
            "board_id": player.position.board_id,
            "index": player.position.index,
            "square": square.name,
            "price": price,
        }
        self.turn_state = TurnState.AWAITING_DECISION
        self._say(f"{player.name} can buy {square.name} for {money(price)}.")

    def _buy(self, player, square):
        price = square.attributes["price"]
        if player.money < price:
            raise GameError("cant_afford", f"You can't afford {square.name}.")
        player.money -= price
        square.set_owner(player.name)
        self._say(f"{player.name} bought {square.name} for {money(price)}.")
        self._event("purchase", player, details=f"square={square.name}; price={price}")

    def _charge_rent(self, player, square, multiplier=1):
        owner = self.get_player(square.owner)
        if owner is None or not owner.in_game:
            return
        if square.mortgaged:
            self._say(f"{square.name} is mortgaged, so no rent is due.")
            return
        dice_total = self.last_roll["total"] if self.last_roll else 0
        rent = self.rent_for(player.position.board_id, square, dice_total) * multiplier
        if rent <= 0:
            return
        if self._pay(player, rent, owner, f"rent for {square.name}"):
            self._say(f"{player.name} paid {money(rent)} rent to {owner.name} for {square.name}.")
            self._event("rent_paid", player,
                        details=f"square={square.name}; owner={owner.name}; amount={rent}")

    # -- pooled (stakeholder) squares --------------------------------------

    def _pool_for(self, player):
        """The pooled square that collects money paid on the player's board."""
        pools = self.board_for(player).pools()
        return pools[0] if pools else None

    def _land_on_pool(self, player, square):
        trigger = self._rule("pool_payout_trigger")
        if trigger == "on_landing" or (trigger == "on_stakeholder_landing" and square.stake_of(player.name)):
            self._pay_out_pool(square, player)
        self._offer_stake(player, square)

    def _pay_out_pool(self, square, lander):
        """Share the pot among the square's stakeholders (still in the game),
        by stake or equally, as the rule set says. Unsold stakes' share stays
        in the pot."""
        pot = square.pot
        holders = [(self.get_player(s["player"]), s) for s in square.stakes]
        holders = [(p, s) for p, s in holders if p is not None and p.in_game]
        if not pot or not holders:
            return
        if self._rule("pool_payout_split") == "equal":
            payouts = [(p, pot // len(holders)) for p, _ in holders]
        else:  # by_stake
            max_stakes = square.attributes["max_stakes"]
            payouts = [(p, pot * s.get("shares", 0) // max_stakes) for p, s in holders]
        paid = 0
        parts = []
        for holder, amount in payouts:
            if amount <= 0:
                continue
            holder.money += amount
            paid += amount
            parts.append(f"{holder.name} {money(amount)}")
            self._event("pool_payout", holder,
                        details=f"square={square.name}; amount={amount}; triggered_by={lander.name}")
        square.attributes["pot"] = pot - paid
        if paid:
            left = f" ({money(pot - paid)} stays in the pot)" if pot - paid else ""
            self._say(f"{lander.name} landed on {square.name}: the pot pays out "
                      f"{', '.join(parts)}{left}.")

    def _offer_stake(self, player, square):
        max_stakes = square.attributes["max_stakes"]
        sold = sum(s.get("shares", 0) for s in square.stakes)
        if sold >= max_stakes:
            return
        buy_in = square.attributes["buy_in"]
        if not self._may_buy(player, square, buy_in):
            return
        percent = self._stake_percent(1, max_stakes)
        self.pending_decision = {
            "type": "buy_stake",
            "player": player.name,
            "board_id": player.position.board_id,
            "index": player.position.index,
            "square": square.name,
            "price": buy_in,
            "percent": percent,
            "stakes_left": max_stakes - sold,
        }
        self.turn_state = TurnState.AWAITING_DECISION
        self._say(f"{player.name} can buy a {percent}% stake in {square.name} for {money(buy_in)}.")

    @staticmethod
    def _stake_percent(shares, max_stakes):
        percent = shares * 100 / max_stakes
        return int(percent) if percent == int(percent) else round(percent, 2)

    def _buy_stake(self, player, square):
        buy_in = square.attributes["buy_in"]
        if player.money < buy_in:
            raise GameError("cant_afford", f"You can't afford a stake in {square.name}.")
        player.money -= buy_in
        self._add_shares(square, player.name, 1)
        stake = square.stake_of(player.name)
        self._say(f"{player.name} bought a stake in {square.name} for {money(buy_in)} "
                  f"and now holds {stake['percent']}%.")
        self._event("stake_purchased", player,
                    details=f"square={square.name}; price={buy_in}; percent={stake['percent']}")

    def _add_shares(self, square, name, shares):
        stakes = square.attributes.setdefault("stakes", [])
        stake = square.stake_of(name)
        if stake is None:
            stake = {"player": name, "shares": 0, "percent": 0}
            stakes.append(stake)
        stake["shares"] += shares
        stake["percent"] = self._stake_percent(stake["shares"], square.attributes["max_stakes"])

    def stakes_of(self, name):
        """[(board_id, square, stake)] for every pooled square ``name`` has a stake in."""
        found = []
        for bid, board in self.boards.items():
            for sq in board.pools():
                stake = sq.stake_of(name)
                if stake:
                    found.append((bid, sq, stake))
        return found

    # -- cards -------------------------------------------------------------

    def _draw_card(self, player, square, depth):
        deck_name = square.type
        card = self.decks[deck_name].draw()
        if card is None:
            return
        self.last_card = {"player": player.name, "deck": deck_name, "label": square.name, "text": card.text}
        self._say(f"{player.name} drew {square.name}: \"{card.text}\"")
        self._event("card_drawn", player, details=f"deck={deck_name}; card={card.text}")
        self._apply_card(player, card, depth)

    def _apply_card(self, player, card, depth):
        p = card.params
        board = self.board_for(player)
        effect = card.effect

        if effect in ("move_to", "move_by", "move_to_nearest"):
            multiplier = 1
            if effect == "move_to":
                target = board.find_by_name(str(p.get("square", "")))
                if target is None:
                    return  # the square isn't on this board
                self._move_to_index(player, target)
            elif effect == "move_by":
                steps = int(p.get("steps", 0))
                if steps >= 0:
                    self._move_forward(player, steps)
                else:  # moving backwards never passes Go
                    player.position = Position(board.board_id, (player.position.index + steps) % board.size)
            else:
                target = self._nearest(board, player.position.index, p.get("type", "station"))
                if target is None:
                    return
                self._move_to_index(player, target)
                multiplier = int(p.get("rent_multiplier", 1))
            square = self.square_at(player.position)
            self._say(f"{player.name} moved to {square.name}.")
            if depth < MAX_CARD_CHAIN:
                self._resolve_landing(player, square, depth + 1, rent_multiplier=multiplier)
        elif effect == "collect":
            player.money += int(p.get("amount", 0))
        elif effect == "pay":
            self._pay(player, int(p.get("amount", 0)), None, card.text, category="fees")
        elif effect == "collect_from_each":
            for other in self.players_in_game():
                if other is not player:
                    self._pay(other, int(p.get("amount", 0)), player, card.text)
        elif effect == "pay_each":
            for other in self.players_in_game():
                if other is not player:
                    self._pay(player, int(p.get("amount", 0)), other, card.text)
        elif effect == "repairs":
            houses, hotels = self.building_counts(player.name)
            cost = houses * int(p.get("house", 0)) + hotels * int(p.get("hotel", 0))
            if cost:
                self._say(f"{player.name} owes {money(cost)} for {houses} house(s) and {hotels} hotel(s).")
                self._pay(player, cost, None, card.text, category="fees")
        elif effect == "go_to_jail":
            self._send_to_jail(player, "sent by a card")
        elif effect == "get_out_of_jail_free":
            player.jail_cards.append(card)

    @staticmethod
    def _nearest(board, start, square_type):
        for step in range(1, board.size + 1):
            idx = (start + step) % board.size
            if board.squares[idx].type == square_type:
                return idx
        return None

    # -- money, debt and bankruptcy ------------------------------------------

    def _pay(self, payer, amount, creditor=None, reason="", category=None):
        """Move ``amount`` from ``payer`` to ``creditor`` (None = the bank).

        ``category`` ("taxes", "fines" or "fees") marks bank payments that a
        pooled square may collect, if the rule set's pool_receives lists it.
        If the payer can't cover it, nothing moves and the amount is recorded
        as a debt; the payer must raise money or declare bankruptcy. Returns
        True if the payment went through.
        """
        if amount <= 0:
            return True
        if payer.money >= amount and not payer.debts:
            payer.money -= amount
            if creditor is not None:
                creditor.money += amount
            else:
                self._bank_receives(payer, amount, category)
            return True
        payer.debts.append({
            "creditor": creditor.name if creditor else None,
            "amount": amount,
            "reason": reason,
            "category": category,
        })
        to = creditor.name if creditor else "the bank"
        self._say(f"{payer.name} can't pay {money(amount)} to {to} ({reason}) "
                  f"and must raise the money or go bankrupt.")
        return False

    def _bank_receives(self, payer, amount, category):
        """Money paid to the bank: diverted into the pooled square's pot when
        the rule set says this kind of payment feeds the pool."""
        if not category or category not in self._rule("pool_receives"):
            return
        pool = self._pool_for(payer)
        if pool is None:
            return
        pool.attributes["pot"] = pool.pot + amount
        self._say(f"{money(amount)} goes into the {pool.name} pot (now {money(pool.pot)}).")

    def _open_debt_decision(self, player, roll_again=False, turn_start=False):
        creditors = sorted({d["creditor"] or "the bank" for d in player.debts})
        self.pending_decision = {
            "type": "debt",
            "player": player.name,
            "amount": player.debt_total,
            "creditors": creditors,
            "roll_again": roll_again,
            "turn_start": turn_start,
        }
        self.turn_state = TurnState.AWAITING_DECISION

    def _settle_debts(self, player):
        total = player.debt_total
        if player.money < total:
            raise GameError("cant_afford",
                            f"You need {money(total)} but have {money(player.money)}. "
                            "Sell buildings, mortgage or trade to raise money.")
        debts, player.debts = player.debts, []
        for debt in debts:
            player.money -= debt["amount"]
            creditor = self.get_player(debt["creditor"]) if debt["creditor"] else None
            if creditor is not None and creditor.in_game:
                creditor.money += debt["amount"]
            else:
                self._bank_receives(player, debt["amount"], debt.get("category"))
            to = creditor.name if creditor else "the bank"
            self._say(f"{player.name} paid {money(debt['amount'])} to {to} ({debt['reason']}).")
            self._event("debt_paid", player,
                        details=f"creditor={to}; amount={debt['amount']}; reason={debt['reason']}")

    def _declare_bankrupt(self, player):
        creditor_names = {d["creditor"] for d in player.debts}
        creditor = None
        if len(creditor_names) == 1:
            creditor = self.get_player(next(iter(creditor_names)))
            if creditor is not None and not creditor.in_game:
                creditor = None
        self._cancel_trades_with(player)
        if creditor is None:
            self._release_assets_to_bank(player)
            to = "the bank"
        else:
            self._transfer_assets(player, creditor)
            to = creditor.name
        player.money = 0
        player.debts = []
        player.bankrupt = True
        player.in_jail = False
        self.pending_decision = None
        self.eliminated.append(player.name)
        self._say(f"{player.name} is bankrupt! Their assets go to {to}.")
        self._event("bankrupt", player, details=f"creditor={to}")
        if self._check_for_winner():
            return
        if player is self.current_player:
            self._advance_turn()

    def _transfer_assets(self, player, creditor):
        """Bankruptcy to another player: buildings are sold to the bank and
        the creditor gets all cash, properties (mortgages and all), stakes
        and jail cards."""
        for bid, sq in self.owned_squares(player.name):
            player.money += self._building_refund(sq)
            sq.attributes.pop("houses", None)
            sq.attributes.pop("hotels", None)
            sq.set_owner(creditor.name)
        for bid, sq, stake in self.stakes_of(player.name):
            sq.attributes["stakes"].remove(stake)
            self._add_shares(sq, creditor.name, stake["shares"])
        creditor.money += max(player.money, 0)
        creditor.jail_cards.extend(player.jail_cards)
        player.jail_cards = []

    def _release_assets_to_bank(self, player):
        """Return a player's properties (unmortgaged, unbuilt), stakes and jail
        cards to the bank."""
        for bid, sq in self.owned_squares(player.name):
            for key in ("stakes", "houses", "hotels", "mortgaged"):
                sq.attributes.pop(key, None)
        for bid, sq, stake in self.stakes_of(player.name):
            sq.attributes["stakes"].remove(stake)
        for card in player.jail_cards:
            self._return_card(card)
        player.jail_cards = []

    def _check_for_winner(self):
        """End the game if only one player is left standing."""
        remaining = self.players_in_game()
        if self.status == GameStatus.IN_PROGRESS and len(remaining) <= 1:
            self._finish(remaining[0] if remaining else None, "last player standing")
            return True
        return False

    # -- ownership, rent and net worth ---------------------------------------

    def owned_squares(self, owner_name, board_id=None, square_type=None):
        """All (board_id, Square) pairs wholly owned by ``owner_name``
        (pooled squares are held as stakes; see stakes_of)."""
        found = []
        for bid, board in self.boards.items():
            if board_id is not None and bid != board_id:
                continue
            for sq in board.squares:
                if sq.pooled or not _same(sq.owner, owner_name) or sq.owner is None:
                    continue
                if square_type is None or sq.type == square_type:
                    found.append((bid, sq))
        return found

    def owns_full_group(self, owner_name, board_id, group):
        members = self.boards[board_id].group(group)
        return bool(members) and all(_same(sq.owner, owner_name) for sq in members)

    def building_counts(self, owner_name):
        """(houses, hotels) owned by ``owner_name`` across all boards."""
        houses = hotels = 0
        for _, sq in self.owned_squares(owner_name, square_type="property"):
            houses += sq.houses
            hotels += sq.hotels
        return houses, hotels

    def rent_for(self, board_id, square, dice_total):
        """Rent due for landing on a wholly owned, unmortgaged ``square``."""
        owner = square.owner
        if not owner or square.mortgaged:
            return 0
        attrs = square.attributes
        if square.type == "property":
            if square.hotels:
                rents = square.int_list("hotel_rents")
                if rents:
                    return rents[min(square.hotels, len(rents)) - 1]
            if square.houses:
                rents = square.int_list("house_rents")
                if rents:
                    return rents[min(square.houses, len(rents)) - 1]
            rent = attrs.get("rent", 0)
            group = attrs.get("group")
            if group and self.owns_full_group(owner, board_id, group):
                rent *= self._rule("full_group_rent_multiplier")
            return rent
        count = len(self.owned_squares(owner, board_id, square.type))
        if square.type == "station":
            rents = square.int_list("rents") or square.int_list("rent")
            return rents[min(max(count, 1), len(rents)) - 1] if rents else 0
        if square.type == "utility":
            multipliers = square.int_list("dice_multipliers")
            return dice_total * multipliers[min(max(count, 1), len(multipliers)) - 1] if multipliers else 0
        return 0

    def mortgage_value(self, square):
        return square.attributes.get("price", 0) * self._rule("mortgage_percent") // 100

    def unmortgage_cost(self, square):
        value = self.mortgage_value(square)
        return value + (value * self._rule("unmortgage_interest_percent") + 99) // 100

    @staticmethod
    def hotel_cost(square):
        return square.attributes.get("hotel_cost", square.attributes.get("house_cost", 0))

    def sell_value(self, square, hotel=False):
        """Refund for selling one house (or hotel) back to the bank."""
        cost = self.hotel_cost(square) if hotel else square.attributes.get("house_cost", 0)
        return cost * self._rule("house_sell_percent") // 100

    def _building_refund(self, square):
        return square.houses * self.sell_value(square) + square.hotels * self.sell_value(square, hotel=True)

    def net_worth(self, player):
        """Money plus property value (mortgaged squares count for what is left
        after the mortgage), buildings at cost and stakes at their buy-in,
        minus unpaid debts."""
        worth = player.money - player.debt_total
        for _, sq in self.owned_squares(player.name):
            price = sq.attributes.get("price", 0)
            worth += price - self.mortgage_value(sq) if sq.mortgaged else price
            worth += sq.houses * sq.attributes.get("house_cost", 0) + sq.hotels * self.hotel_cost(sq)
        for _, sq, stake in self.stakes_of(player.name):
            worth += stake["shares"] * sq.attributes.get("buy_in", 0)
        return worth

    # -- buildings and mortgages ---------------------------------------------

    def _level(self, square):
        """Building level for even-building checks: houses, then each hotel
        counts one level above the most houses allowed."""
        if square.hotels:
            return self._rule("max_houses_per_property") + square.hotels
        return square.houses

    def _managed_square(self, player, board_id, index):
        board = self.boards.get(board_id)
        if board is None or not 0 <= index < board.size:
            raise GameError("bad_square", "No such square.")
        square = board.square(index)
        if square.type not in OWNABLE_TYPES or square.pooled or not _same(square.owner, player.name):
            raise GameError("not_owner", f"You don't own {square.name}.")
        return board, square

    def _manage_problem(self, player):
        """Why ``player`` can't manage property right now, or None."""
        if self.status != GameStatus.IN_PROGRESS:
            return "The game is not in progress."
        if not player.in_game:
            return "You are out of the game."
        if player is not self.current_player:
            return "You can only manage property on your turn."
        return None

    def _group_build_problem(self, player, board, square):
        """Checks shared by houses and hotels."""
        if square.type != "property":
            return "You can only build on properties."
        group = square.attributes.get("group")
        if not group or not self.owns_full_group(player.name, board.board_id, group):
            return "You need the whole colour group to build."
        if any(sq.mortgaged for sq in board.group(group)):
            return "Unmortgage the colour group before building."
        if self._level(square) > min(self._level(sq) for sq in board.group(group)):
            return "Build evenly: add to the other properties in the group first."
        if player.debts:
            return "Pay your debts before building."
        return None

    def _build_problem(self, player, board, square):
        problem = self._group_build_problem(player, board, square)
        if problem:
            return problem
        if square.hotels:
            return f"{square.name} has a hotel; build hotels instead."
        max_houses = self._rule("max_houses_per_property")
        if square.houses >= max_houses:
            return f"{square.name} already has the most houses allowed ({max_houses})."
        cost = square.attributes.get("house_cost", 0)
        if not cost:
            return f"{square.name} can't be built on."
        if player.money < cost:
            return f"You need {money(cost)} to build."
        return None

    def _hotel_problem(self, player, board, square):
        max_hotels = self._rule("max_hotels_per_property")
        if not max_hotels:
            return "Hotels aren't allowed in this rule set."
        problem = self._group_build_problem(player, board, square)
        if problem:
            return problem
        if square.hotels >= max_hotels:
            return f"{square.name} already has the most hotels allowed ({max_hotels})."
        needed = self._rule("houses_before_hotel")
        if not square.hotels and square.houses < needed:
            return f"You need {needed} houses on {square.name} before building a hotel."
        cost = self.hotel_cost(square)
        if not cost:
            return f"{square.name} can't be built on."
        if player.money < cost:
            return f"You need {money(cost)} to build a hotel."
        return None

    def _sell_problem(self, player, board, square):
        if square.type != "property" or not (square.houses or square.hotels):
            return f"{square.name} has nothing to sell."
        members = board.group(square.attributes.get("group")) or [square]
        if self._level(square) < max(self._level(sq) for sq in members):
            return "Sell evenly: sell from the other properties in the group first."
        return None

    def _mortgage_problem(self, player, board, square):
        if square.mortgaged:
            return f"{square.name} is already mortgaged."
        if square.type == "property":
            members = board.group(square.attributes.get("group")) or [square]
            if any(sq.houses or sq.hotels for sq in members):
                return "Sell the buildings in this colour group first."
        return None

    def _unmortgage_problem(self, player, board, square):
        if not square.mortgaged:
            return f"{square.name} is not mortgaged."
        if player.debts:
            return "Pay your debts before unmortgaging."
        if player.money < self.unmortgage_cost(square):
            return f"You need {money(self.unmortgage_cost(square))} to unmortgage."
        return None

    def _manage(self, name, board_id, index, check):
        player = self._require_player(name)
        problem = self._manage_problem(player)
        if problem:
            raise GameError("not_allowed", problem)
        board, square = self._managed_square(player, board_id, index)
        problem = check(player, board, square)
        if problem:
            raise GameError("not_allowed", problem)
        return player, square

    def build_house(self, name, board_id, index):
        player, square = self._manage(name, board_id, index, self._build_problem)
        cost = square.attributes["house_cost"]
        player.money -= cost
        square.attributes["houses"] = square.houses + 1
        self._say(f"{player.name} built a house on {square.name} for {money(cost)}.")
        self._event("house_built", player,
                    details=f"square={square.name}; houses={square.houses}; cost={cost}")

    def build_hotel(self, name, board_id, index):
        """Build a hotel. The first hotel replaces the property's houses
        (they go back to the bank); rule sets may allow several hotels."""
        player, square = self._manage(name, board_id, index, self._hotel_problem)
        cost = self.hotel_cost(square)
        player.money -= cost
        square.attributes["houses"] = 0
        square.attributes["hotels"] = square.hotels + 1
        self._say(f"{player.name} built a hotel on {square.name} for {money(cost)}.")
        self._event("hotel_built", player,
                    details=f"square={square.name}; hotels={square.hotels}; cost={cost}")

    def sell_house(self, name, board_id, index):
        """Sell the top building on a property: a hotel if it has one (the
        last hotel sold becomes houses_before_hotel houses again), otherwise
        a house."""
        player, square = self._manage(name, board_id, index, self._sell_problem)
        if square.hotels:
            refund = self.sell_value(square, hotel=True)
            square.attributes["hotels"] = square.hotels - 1
            if not square.hotels:
                square.attributes["houses"] = self._rule("houses_before_hotel")
            what = "a hotel"
        else:
            refund = self.sell_value(square)
            square.attributes["houses"] = square.houses - 1
            what = "a house"
        player.money += refund
        self._say(f"{player.name} sold {what} on {square.name} for {money(refund)}.")
        self._event("building_sold", player,
                    details=f"square={square.name}; sold={what[2:]}; houses={square.houses}; "
                            f"hotels={square.hotels}; refund={refund}")

    def mortgage(self, name, board_id, index):
        player, square = self._manage(name, board_id, index, self._mortgage_problem)
        value = self.mortgage_value(square)
        square.attributes["mortgaged"] = True
        player.money += value
        self._say(f"{player.name} mortgaged {square.name} for {money(value)}.")
        self._event("mortgaged", player, details=f"square={square.name}; amount={value}")

    def unmortgage(self, name, board_id, index):
        player, square = self._manage(name, board_id, index, self._unmortgage_problem)
        cost = self.unmortgage_cost(square)
        square.attributes.pop("mortgaged", None)
        player.money -= cost
        self._say(f"{player.name} unmortgaged {square.name} for {money(cost)}.")
        self._event("unmortgaged", player, details=f"square={square.name}; amount={cost}")

    def property_actions(self, player):
        """What ``player`` may do with each square they own, for the UI:
        [{"board_id", "index", "can_build", "can_build_hotel", ...}]."""
        manage = self._manage_problem(player)
        actions = []
        for bid, sq in self.owned_squares(player.name):
            board = self.boards[bid]
            actions.append({
                "board_id": bid,
                "index": sq.index,
                "can_build": not manage and not self._build_problem(player, board, sq),
                "can_build_hotel": not manage and not self._hotel_problem(player, board, sq),
                "can_sell": not manage and not self._sell_problem(player, board, sq),
                "can_mortgage": not manage and not self._mortgage_problem(player, board, sq),
                "can_unmortgage": not manage and not self._unmortgage_problem(player, board, sq),
                "tradeable": not self._trade_square_problem(player, board, sq),
                "build_cost": sq.attributes.get("house_cost", 0),
                "hotel_cost": self.hotel_cost(sq),
                "sell_value": self.sell_value(sq, hotel=bool(sq.hotels)),
                "mortgage_value": self.mortgage_value(sq),
                "unmortgage_cost": self.unmortgage_cost(sq),
            })
        return actions

    # -- trading -----------------------------------------------------------

    def _trade_square_problem(self, owner, board, square):
        if square.pooled:
            return f"Stakes in {square.name} can't be traded."
        if square.type not in OWNABLE_TYPES or not _same(square.owner, owner.name):
            return f"{owner.name} doesn't own {square.name}."
        if square.type == "property":
            members = board.group(square.attributes.get("group")) or [square]
            if any(sq.houses or sq.hotels for sq in members):
                return f"Sell the buildings in {square.name}'s colour group before trading it."
        return None

    def _trade_squares(self, owner, positions):
        squares = []
        for pos in positions:
            try:
                board_id, index = int(pos[0]), int(pos[1])
            except (TypeError, ValueError, IndexError):
                raise GameError("bad_trade", "Invalid square in trade.") from None
            board = self.boards.get(board_id)
            if board is None or not 0 <= index < board.size:
                raise GameError("bad_trade", "Invalid square in trade.")
            square = board.square(index)
            problem = self._trade_square_problem(owner, board, square)
            if problem:
                raise GameError("bad_trade", problem)
            squares.append(square)
        return squares

    def _validate_trade(self, trade):
        """Check a trade can happen right now. Returns (giver, taker,
        give_squares, get_squares)."""
        giver = self.get_player(trade["from"])
        taker = self.get_player(trade["to"])
        if giver is None or taker is None or not giver.in_game or not taker.in_game:
            raise GameError("bad_trade", "Both players must still be in the game.")
        give = self._trade_squares(giver, trade["give_squares"])
        get = self._trade_squares(taker, trade["get_squares"])
        if giver.money < trade["give_money"]:
            raise GameError("bad_trade", f"{giver.name} doesn't have {money(trade['give_money'])}.")
        if taker.money < trade["get_money"]:
            raise GameError("bad_trade", f"{taker.name} doesn't have {money(trade['get_money'])}.")
        return giver, taker, give, get

    def describe_trade(self, trade):
        def side(amount, positions):
            items = [self.square_at(Position(b, i)).name for b, i in positions]
            if amount:
                items.append(money(amount))
            return " + ".join(items)
        give = side(trade["give_money"], trade["give_squares"])
        get = side(trade["get_money"], trade["get_squares"])
        if not get:
            return f"{trade['from']} gives {give} to {trade['to']}"
        if not give:
            return f"{trade['from']} asks {trade['to']} for {get}"
        return f"{trade['from']} gives {give} for {trade['to']}'s {get}"

    def propose_trade(self, name, to, give_money=0, give_squares=(), get_money=0, get_squares=()):
        """Offer a trade to another player. Trades can be proposed and
        answered at any time during the game."""
        self._require_in_progress()
        giver = self._require_player(name)
        taker = self.get_player(to)
        if taker is None or taker is giver:
            raise GameError("bad_trade", "Choose another player to trade with.")
        try:
            give_money, get_money = int(give_money or 0), int(get_money or 0)
        except (TypeError, ValueError):
            raise GameError("bad_trade", "Money amounts must be whole numbers.") from None
        if give_money < 0 or get_money < 0:
            raise GameError("bad_trade", "Money amounts can't be negative.")
        try:
            give_list = [[int(p[0]), int(p[1])] for p in give_squares or ()]
            get_list = [[int(p[0]), int(p[1])] for p in get_squares or ()]
        except (TypeError, ValueError, IndexError):
            raise GameError("bad_trade", "Invalid square in trade.") from None
        trade = {
            "id": self._next_trade_id,
            "from": giver.name,
            "to": taker.name,
            "give_money": give_money,
            "give_squares": give_list,
            "get_money": get_money,
            "get_squares": get_list,
        }
        if not (give_money or get_money or give_list or get_list):
            raise GameError("bad_trade", "A trade needs something in it.")
        self._validate_trade(trade)
        self._next_trade_id += 1
        trade["summary"] = self.describe_trade(trade)
        self.trades.append(trade)
        self._say(f"{giver.name} offered {taker.name} a trade: {trade['summary']}.")
        self._event("trade_proposed", giver, details=trade["summary"])
        return trade

    def _find_trade(self, trade_id):
        for trade in self.trades:
            if trade["id"] == trade_id:
                return trade
        raise GameError("bad_trade", "That trade is no longer open.")

    def respond_trade(self, name, trade_id, accept):
        self._require_in_progress()
        player = self._require_player(name)
        trade = self._find_trade(trade_id)
        if not _same(trade["to"], player.name):
            raise GameError("bad_trade", "That trade isn't addressed to you.")
        if not accept:
            self.trades.remove(trade)
            self._say(f"{player.name} rejected {trade['from']}'s trade.")
            self._event("trade_rejected", player, details=trade["summary"])
            return
        giver, taker, give, get = self._validate_trade(trade)
        self.trades.remove(trade)
        for sq in give:
            sq.set_owner(taker.name)
        for sq in get:
            sq.set_owner(giver.name)
        giver.money += trade["get_money"] - trade["give_money"]
        taker.money += trade["give_money"] - trade["get_money"]
        self._say(f"{taker.name} accepted the trade: {trade['summary']}.")
        self._event("trade_accepted", taker, details=trade["summary"])

    def cancel_trade(self, name, trade_id):
        self._require_in_progress()
        player = self._require_player(name)
        trade = self._find_trade(trade_id)
        if not _same(trade["from"], player.name):
            raise GameError("bad_trade", "Only the player who offered a trade can cancel it.")
        self.trades.remove(trade)
        self._say(f"{player.name} withdrew their trade offer to {trade['to']}.")

    def _cancel_trades_with(self, player):
        self.trades = [t for t in self.trades
                       if not _same(player.name, t["from"]) and not _same(player.name, t["to"])]

    # -- ending ------------------------------------------------------------

    def end(self, requested_by=None):
        """End the game (host only, unless requested_by is None = system).
        The player with the highest net worth wins."""
        if requested_by is not None and not self.is_host(requested_by):
            raise GameError("not_host", "Only the host can end the game.")
        if self.status == GameStatus.ENDED:
            raise GameError("ended", "The game has already ended.")
        if self.status == GameStatus.LOBBY:
            self.status = GameStatus.ENDED
            self.turn_state = TurnState.TURN_OVER
            self._say("The game has ended.")
            return
        standings = self.standings()
        winner = standings[0] if standings and standings[0].in_game else None
        self._finish(winner, "highest net worth")

    def _finish(self, winner, reason):
        self.status = GameStatus.ENDED
        self.turn_state = TurnState.TURN_OVER
        self.pending_decision = None
        self.trades = []
        self.winner = winner.name if winner else None
        if winner:
            self._say(f"The game has ended. {winner.name} wins ({reason})!")
        else:
            self._say("The game has ended.")
        for position, p in enumerate(self.standings(), start=1):
            if p is winner:
                result = "winner"
            elif p.bankrupt:
                result = "bankrupt"
            elif p.left:
                result = "left"
            else:
                result = "finished"
            self._event("game_ended", p, details=(
                f"net_worth={self.net_worth(p)}; position={position}; result={result}; "
                f"ruleset={self.ruleset.id}"))

    def standings(self):
        """Finishing order: players still in the game by net worth, then
        eliminated players, most recently eliminated first."""
        playing = sorted(self.players_in_game(), key=lambda p: (-self.net_worth(p), p.join_order))
        out = [self.get_player(name) for name in reversed(self.eliminated)]
        seen = {p.name for p in playing + out}
        # Players who left in the lobby phase or were never eliminated.
        rest = [p for p in self.players if p.name not in seen]
        return playing + out + rest

    # -- serialisation -----------------------------------------------------

    def ruleset_summary(self):
        """Key values of this game's rule set, for the lobby and game page."""
        r = self._rule
        yes_no = {True: "Yes", False: "No"}
        rows = [
            ("Starting money", money(r("starting_money"))),
            ("Go salary", money(r("go_salary"))),
            ("Jail fine", money(r("jail_fine"))),
            ("Players", f"{r('min_players')}-{r('max_players')}"),
            ("Houses per property", str(r("max_houses_per_property"))),
            ("Houses before a hotel", str(r("houses_before_hotel"))),
            ("Hotels per property", str(r("max_hotels_per_property"))),
            ("Doubles before jail", str(r("doubles_before_jail"))),
            ("Lap before buying", yes_no[r("must_lap_before_buying")]),
        ]
        pools = [sq for board in self.boards.values() for sq in board.pools()]
        for sq in pools:
            feeds = ", ".join(r("pool_receives")) or "nothing"
            trigger = {"on_landing": "whenever anyone lands",
                       "on_stakeholder_landing": "when a stakeholder lands"}[r("pool_payout_trigger")]
            split = {"by_stake": "by stake", "equal": "equally"}[r("pool_payout_split")]
            rows.append((sq.name, f"{sq.attributes['max_stakes']} stakes at "
                                  f"{money(sq.attributes['buy_in'])}; collects {feeds}; "
                                  f"pays out {trigger}, split {split}"))
        return [{"label": label, "value": value} for label, value in rows]

    def to_dict(self):
        current = self.current_player
        players = []
        for p in self.players:
            data = p.to_dict()
            data["properties"] = [
                {"board_id": bid, "index": sq.index} for bid, sq in self.owned_squares(p.name)
            ]
            data["stakes"] = [
                {"board_id": bid, "index": sq.index, "percent": stake["percent"]}
                for bid, sq, stake in self.stakes_of(p.name)
            ]
            data["property_actions"] = self.property_actions(p)
            data["net_worth"] = self.net_worth(p)
            players.append(data)
        return {
            "join_code": self.join_code,
            "status": self.status,
            "host": self.host_name,
            "players": players,
            "current_player": current.name if current and self.status == GameStatus.IN_PROGRESS else None,
            "turn_state": self.turn_state,
            "turn_number": self.turn_number,
            "last_roll": self.last_roll,
            "last_card": self.last_card,
            "pending_decision": self.pending_decision,
            "trades": [dict(t) for t in self.trades],
            "boards": {str(bid): b.to_dict() for bid, b in self.boards.items()},
            "ruleset": {
                "id": self.ruleset.id,
                "name": self.ruleset.name,
                "description": self.ruleset.description,
                "summary": self.ruleset_summary(),
            },
            "min_players": self._rule("min_players"),
            "max_players": self._rule("max_players"),
            "rules": {
                "jail_fine": self._rule("jail_fine"),
                "max_jail_turns": self._rule("max_jail_turns"),
                "max_houses_per_property": self._rule("max_houses_per_property"),
                "max_hotels_per_property": self._rule("max_hotels_per_property"),
                "houses_before_hotel": self._rule("houses_before_hotel"),
                "must_lap_before_buying": self._rule("must_lap_before_buying"),
            },
            "winner": self.winner,
            "standings": [
                {"name": p.name, "net_worth": self.net_worth(p), "bankrupt": p.bankrupt, "left": p.left}
                for p in self.standings()
            ] if self.status == GameStatus.ENDED else None,
            "log": self.log[-50:],
        }
