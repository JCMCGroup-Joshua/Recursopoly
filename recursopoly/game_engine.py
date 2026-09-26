"""Recursopoly game engine.

Pure Python game rules with no Flask (or any web) imports, so everything here
can be unit-tested on its own. The web layer (app.py) calls into this module
for every rule decision and simply broadcasts the resulting state.

Key design points:

* Every :class:`Board` has a ``board_id``; a :class:`Game` holds its boards in
  a dict keyed by that id (the outer board is 0).
* A player's position is a :class:`Position` of ``(board_id, index)``, never a
  bare integer, so Phase 4 train travel can move tokens between boards.
* A :class:`Square` has a ``type`` plus a free-form ``attributes`` dict for
  prices, rents, owners, houses, mortgages, station links, etc.
* Turns are a small state machine (:class:`TurnState`). ``AWAITING_DECISION``
  pauses the turn until the active player answers ``Game.pending_decision``
  (buy or decline, settle a debt or go bankrupt, and in Phase 4 train
  tickets).
* Payments go through ``Game._pay``. A player who can't pay runs up a debt
  and must raise money (sell houses, mortgage, trade) or declare bankruptcy.
* The engine never writes files. Anything worth logging is queued as a
  :class:`GameEvent` which the caller drains with :meth:`Game.drain_events`
  and hands to the score logger.
"""

import os
import random
import secrets
import time
from dataclasses import dataclass, field

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

SQUARE_TYPES = (
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

# Square types that can be bought and charge rent.
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

DECK_LABELS = {"chance": "Chance", "community_chest": "Community Chest"}

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
    # pay a debt or go bankrupt, ...) before the turn can continue.
    AWAITING_DECISION = "awaiting_decision"
    TURN_OVER = "turn_over"


class GameError(Exception):
    """A rule was broken. ``code`` is machine-readable, str(err) is for humans."""

    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


def money(amount):
    return f"£{amount}"


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
    # Extensible: group, price, rent, house_rents, owner, houses, mortgaged,
    # station links, tax amount, ...
    attributes: dict = field(default_factory=dict)

    @property
    def owner(self):
        return self.attributes.get("owner")

    @property
    def houses(self):
        """Buildings on the square: 1-4 houses, max_houses + 1 is a hotel."""
        return self.attributes.get("houses", 0)

    @property
    def mortgaged(self):
        return bool(self.attributes.get("mortgaged"))

    def house_rents(self):
        """Rents with 1..n houses then a hotel, from 'house_rents=10/30/...'."""
        raw = self.attributes.get("house_rents")
        if raw is None:
            return []
        if isinstance(raw, int):
            return [raw]
        return [int(part) for part in str(raw).split("/") if part.strip()]

    def to_dict(self):
        return {
            "index": self.index,
            "name": self.name,
            "type": self.type,
            "attributes": dict(self.attributes),
        }


class Board:
    """One looping track of squares."""

    def __init__(self, board_id, squares, name=None, attributes=None):
        if not squares:
            raise ValueError("A Recursopoly board needs at least one square")
        self.board_id = board_id
        self.name = name or f"Board {board_id}"
        self.squares = list(squares)
        # Board-level settings from '@key=value' lines (Phase 4: go_salary,
        # depth, ticket prices, ...).
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
            if sq.name.lower() == name.lower():
                return sq.index
        return None

    def group(self, group):
        """All property squares in colour group ``group``."""
        return [sq for sq in self.squares
                if sq.type == "property" and sq.attributes.get("group") == group]

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
            "attributes": dict(self.attributes),
            "squares": [sq.to_dict() for sq in self.squares],
        }


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


def parse_board_text(text, board_id, size=None):
    """Build a :class:`Board` from board-file text.

    Format (see boards/board_0.txt): one square per line as
    ``name | type | key=value; key=value``. ``#`` starts a comment line and
    ``@key=value`` lines set board metadata.

    If ``size`` is given the board is trimmed or padded (with Free Parking
    squares) to exactly that many squares, which lets config.txt control the
    board length without editing the board file.
    """
    squares = []
    meta = {}
    for line_no, raw in enumerate(text.splitlines(), start=1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("@"):
            key, _, value = line[1:].partition("=")
            meta[key.strip()] = _parse_value(value)
            continue
        parts = [p.strip() for p in line.split("|")]
        if len(parts) < 2 or not parts[0]:
            raise ValueError(f"board {board_id} line {line_no}: expected 'name | type'")
        name, sq_type = parts[0], parts[1].lower()
        if sq_type not in SQUARE_TYPES:
            raise ValueError(f"board {board_id} line {line_no}: unknown square type {sq_type!r}")
        attrs = parse_attributes(parts[2]) if len(parts) > 2 else {}
        squares.append(Square(len(squares), name, sq_type, attrs))

    if size is not None:
        squares = squares[:size]
        while len(squares) < size:
            squares.append(Square(len(squares), "Free Space", "free_parking", {}))

    name = meta.pop("name", None)
    return Board(board_id, squares, name=name, attributes=meta)


def load_board(path, board_id, size=None):
    with open(path, encoding="utf-8") as fh:
        return parse_board_text(fh.read(), board_id, size=size)


def load_boards(boards_dir, board_sizes=None):
    """Load every ``board_<n>.txt`` in ``boards_dir`` into a dict by board_id.

    ``board_sizes`` optionally maps board_id to a forced size. Phase 1 ships
    only board_0.txt; Phase 4 simply adds board_1.txt, board_2.txt, ...
    """
    board_sizes = board_sizes or {}
    boards = {}
    for filename in sorted(os.listdir(boards_dir)):
        if not (filename.startswith("board_") and filename.endswith(".txt")):
            continue
        try:
            board_id = int(filename[len("board_"):-len(".txt")])
        except ValueError:
            continue
        boards[board_id] = load_board(
            os.path.join(boards_dir, filename), board_id, size=board_sizes.get(board_id)
        )
    if 0 not in boards:
        raise FileNotFoundError(f"Recursopoly needs {boards_dir}/board_0.txt")
    return boards


# ---------------------------------------------------------------------------
# Cards
# ---------------------------------------------------------------------------


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


def load_decks(cards_dir):
    """Load every ``<deck>.txt`` in ``cards_dir`` as {deck name: [Card, ...]}.

    The deck name matches the square type that draws from it
    (chance.txt -> "chance" squares).
    """
    decks = {}
    if not os.path.isdir(cards_dir):
        return decks
    for filename in sorted(os.listdir(cards_dir)):
        if filename.endswith(".txt"):
            deck = filename[:-len(".txt")]
            with open(os.path.join(cards_dir, filename), encoding="utf-8") as fh:
                decks[deck] = parse_cards_text(fh.read(), deck)
    return decks


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
    in_jail: bool = False
    jail_turns: int = 0  # failed attempts to roll doubles while in jail
    jail_cards: list = field(default_factory=list)  # held Get Out of Jail Free Cards
    # Unpaid amounts: [{"creditor": name or None for the bank, "amount", "reason"}]
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
    """One Recursopoly game session."""

    def __init__(self, join_code, boards, settings, rng=None, decks=None):
        """
        ``boards``: dict of board_id -> Board.
        ``settings``: a Config (or anything with attribute access / .get).
        ``rng``: a random.Random, injectable so tests can fix dice and decks.
        ``decks``: dict of deck name -> list of Card (shuffled per game).
        """
        self.join_code = join_code
        self.boards = dict(boards)
        self.settings = settings
        self.rng = rng or random.SystemRandom()
        self.decks = {name: Deck(name, cards, self.rng) for name, cards in (decks or {}).items()}

        self.status = GameStatus.LOBBY
        self.players = []  # join order
        self.host_name = None
        self.current_index = None  # index into self.players
        self.turn_state = None
        self.turn_number = 0
        self.last_roll = None
        self.last_card = None  # {"player", "deck", "text"} of the latest card drawn
        # What the active player must choose before the turn can continue
        # (only set while turn_state is AWAITING_DECISION), e.g.
        # {"type": "buy", "player", "board_id", "index", "square", "price"} or
        # {"type": "debt", "player", "amount", "creditors"}.
        self.pending_decision = None
        self.trades = []  # open trade offers
        self._next_trade_id = 1
        self.eliminated = []  # names, in the order players went bankrupt or left
        self.winner = None
        self.created_at = time.time()

        self.log = []  # human-readable lines for the event log panel
        self._events = []  # GameEvent queue for scores.csv

    # -- helpers -----------------------------------------------------------

    def _setting(self, key):
        getter = getattr(self.settings, "get", None)
        value = getter(key) if getter else None
        return value if value is not None else getattr(self.settings, key)

    @property
    def start_board_id(self):
        return min(self.boards)

    def board_for(self, player):
        return self.boards[player.position.board_id]

    def square_at(self, position):
        return self.boards[position.board_id].square(position.index)

    def go_salary_for(self, board):
        # Phase 4: boards can override the Go salary with "@go_salary=...".
        return board.attributes.get("go_salary", self._setting("go_salary"))

    def get_player(self, name):
        for p in self.players:
            if p.name.lower() == (name or "").lower():
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
        if len(self.players) >= self._setting("max_players"):
            raise GameError("full", "That game is full.")

        order = len(self.players)
        player = Player(
            name=name,
            colour=PLAYER_COLOURS[order % len(PLAYER_COLOURS)],
            money=self._setting("starting_money"),
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
        the game has started they are out: their properties go back to the
        bank and they never play again.
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
        return self.host_name is not None and name.lower() == self.host_name.lower()

    def start(self, requested_by):
        if not self.is_host(requested_by):
            raise GameError("not_host", "Only the host can start the game.")
        if self.status != GameStatus.LOBBY:
            raise GameError("started", "The game has already started.")
        playing = [p for p in self.players if not p.left]
        if len(playing) < self._setting("min_players"):
            raise GameError(
                "not_enough_players",
                f"At least {self._setting('min_players')} players are needed to start.",
            )
        if not any(p.active for p in playing):
            raise GameError("not_enough_players", "No connected players to start with.")

        self.status = GameStatus.IN_PROGRESS
        self.turn_number = 1
        self.current_index = None
        self._say("The game of Recursopoly has started!")
        for p in self.players:
            self._event("game_started", p, details=f"players={len(self.players)}")
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
        if decision and decision["type"] == "buy":
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

        max_doubles = self._setting("max_doubles")
        if doubles and player.doubles_in_a_row >= max_doubles:
            self._say(f"{player.name} rolled {d1} + {d2}: {max_doubles} doubles in a row!")
            self._event("roll", player, details=roll_details)
            self._send_to_jail(player, f"{max_doubles} doubles in a row")
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

        * "buy" decisions: choice "buy" or "decline".
        * "debt" decisions: choice "pay" (once enough money is raised) or
          "bankrupt".
        Phase 4 adds train tickets here.
        """
        player = self._require_current(name)
        decision = self.pending_decision
        if self.turn_state != TurnState.AWAITING_DECISION or decision is None:
            raise GameError("bad_state", "There is nothing to decide right now.")

        if decision["type"] == "buy":
            if choice not in ("buy", "decline"):
                raise GameError("bad_choice", "Choose to buy or decline.")
            square = self.square_at(Position(decision["board_id"], decision["index"]))
            if choice == "buy":
                self._buy(player, square)
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
        max_turns = self._setting("max_jail_turns")
        if player.jail_turns < max_turns:
            self._say(f"{player.name} rolled {d1} + {d2} and stays in jail "
                      f"(attempt {player.jail_turns} of {max_turns}).")
            self._event("roll", player, details=f"{roll_details}; in_jail=yes")
            self._advance_turn()
            return result

        fine = self._setting("jail_fine")
        self._say(f"{player.name} rolled {d1} + {d2} on their last try and must pay the "
                  f"{money(fine)} fine.")
        self._pay(player, fine, None, "jail fine")
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
        fine = self._setting("jail_fine")
        if player.money < fine:
            raise GameError("cant_afford", f"You need {money(fine)} to pay the fine.")
        player.money -= fine
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

        Buy offers pause the turn (AWAITING_DECISION); rent and tax are
        charged through _pay; card squares draw from their deck. Go's salary
        is handled while moving. Phase 4 adds station travel offers.
        """
        if square.type in OWNABLE_TYPES:
            if square.owner is None:
                self._offer_purchase(player, square)
            elif square.owner.lower() != player.name.lower():
                self._charge_rent(player, square, rent_multiplier)
        elif square.type == "tax":
            amount = square.attributes.get("amount", 0)
            if amount and self._pay(player, amount, None, square.name):
                self._say(f"{player.name} paid {money(amount)} {square.name}.")
                self._event("tax_paid", player, details=f"square={square.name}; amount={amount}")
        elif square.type == "go_to_jail":
            self._send_to_jail(player, f"landed on {square.name}")
        elif square.type in self.decks:
            self._draw_card(player, square.type, depth)

    def _offer_purchase(self, player, square):
        price = square.attributes.get("price")
        if price is None:
            return  # no price in the board file: not for sale
        if player.debts:
            return  # no shopping while in debt
        if player.money < price:
            self._say(f"{player.name} can't afford {square.name} ({money(price)}).")
            return
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
        square.attributes["owner"] = player.name
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

    # -- cards -------------------------------------------------------------

    def _draw_card(self, player, deck_name, depth):
        card = self.decks[deck_name].draw()
        if card is None:
            return
        label = DECK_LABELS.get(deck_name, deck_name.replace("_", " ").title())
        self.last_card = {"player": player.name, "deck": deck_name, "label": label, "text": card.text}
        self._say(f"{player.name} drew {label}: \"{card.text}\"")
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
            self._pay(player, int(p.get("amount", 0)), None, card.text)
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
                self._pay(player, cost, None, card.text)
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

    def _pay(self, payer, amount, creditor=None, reason=""):
        """Move ``amount`` from ``payer`` to ``creditor`` (None = the bank).

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
            return True
        payer.debts.append({
            "creditor": creditor.name if creditor else None,
            "amount": amount,
            "reason": reason,
        })
        to = creditor.name if creditor else "the bank"
        self._say(f"{payer.name} can't pay {money(amount)} to {to} ({reason}) "
                  f"and must raise the money or go bankrupt.")
        return False

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
                            "Sell houses, mortgage or trade to raise money.")
        for debt in player.debts:
            player.money -= debt["amount"]
            creditor = self.get_player(debt["creditor"]) if debt["creditor"] else None
            if creditor is not None and creditor.in_game:
                creditor.money += debt["amount"]
            to = creditor.name if creditor else "the bank"
            self._say(f"{player.name} paid {money(debt['amount'])} to {to} ({debt['reason']}).")
            self._event("debt_paid", player,
                        details=f"creditor={to}; amount={debt['amount']}; reason={debt['reason']}")
        player.debts = []

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
        the creditor gets all cash, properties (mortgages and all) and jail
        cards."""
        for bid, sq in self.owned_squares(player.name):
            if sq.houses:
                player.money += self.sell_value(sq) * sq.houses
                sq.attributes["houses"] = 0
            sq.attributes["owner"] = creditor.name
        creditor.money += max(player.money, 0)
        creditor.jail_cards.extend(player.jail_cards)
        player.jail_cards = []

    def _release_assets_to_bank(self, player):
        """Return a player's properties (unmortgaged, unbuilt) and jail cards
        to the bank."""
        for bid, sq in self.owned_squares(player.name):
            for key in ("owner", "houses", "mortgaged"):
                sq.attributes.pop(key, None)
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
        """All (board_id, Square) pairs owned by ``owner_name``."""
        found = []
        for bid, board in self.boards.items():
            if board_id is not None and bid != board_id:
                continue
            for sq in board.squares:
                owner = sq.owner
                if owner and owner.lower() == owner_name.lower() and \
                        (square_type is None or sq.type == square_type):
                    found.append((bid, sq))
        return found

    def owns_full_group(self, owner_name, board_id, group):
        members = self.boards[board_id].group(group)
        return bool(members) and all((sq.owner or "").lower() == owner_name.lower() for sq in members)

    @property
    def hotel_level(self):
        return self._setting("max_houses") + 1

    def building_counts(self, owner_name):
        """(houses, hotels) owned by ``owner_name`` across all boards."""
        houses = hotels = 0
        for _, sq in self.owned_squares(owner_name, square_type="property"):
            if sq.houses >= self.hotel_level:
                hotels += 1
            else:
                houses += sq.houses
        return houses, hotels

    def rent_for(self, board_id, square, dice_total):
        """Rent due for landing on an owned, unmortgaged ``square``."""
        owner = square.owner
        if not owner or square.mortgaged:
            return 0
        attrs = square.attributes
        if square.type == "property":
            if square.houses:
                rents = square.house_rents()
                if rents:
                    return rents[min(square.houses, len(rents)) - 1]
            rent = attrs.get("rent", 0)
            group = attrs.get("group")
            if group and self.owns_full_group(owner, board_id, group):
                rent *= self._setting("full_group_rent_multiplier")
            return rent
        if square.type == "station":
            count = len(self.owned_squares(owner, board_id, "station"))
            return attrs.get("rent", 0) * 2 ** (max(count, 1) - 1)
        if square.type == "utility":
            utilities = [sq for sq in self.boards[board_id].squares if sq.type == "utility"]
            owned = self.owned_squares(owner, board_id, "utility")
            if len(owned) == len(utilities):
                multiplier = attrs.get("full_set_dice_multiplier", attrs.get("dice_multiplier", 0))
            else:
                multiplier = attrs.get("dice_multiplier", 0)
            return dice_total * multiplier
        return 0

    def mortgage_value(self, square):
        return square.attributes.get("price", 0) * self._setting("mortgage_percent") // 100

    def unmortgage_cost(self, square):
        value = self.mortgage_value(square)
        return value + (value * self._setting("unmortgage_interest_percent") + 99) // 100

    def sell_value(self, square):
        """Refund for selling one house (or a hotel) back to the bank."""
        return square.attributes.get("house_cost", 0) * self._setting("house_sell_percent") // 100

    def net_worth(self, player):
        """Money plus property value (mortgaged squares count for what is left
        after the mortgage) plus buildings at cost, minus unpaid debts."""
        worth = player.money - player.debt_total
        for _, sq in self.owned_squares(player.name):
            price = sq.attributes.get("price", 0)
            worth += price - self.mortgage_value(sq) if sq.mortgaged else price
            worth += sq.houses * sq.attributes.get("house_cost", 0)
        return worth

    # -- buildings and mortgages ---------------------------------------------

    def _managed_square(self, player, board_id, index):
        board = self.boards.get(board_id)
        if board is None or not 0 <= index < board.size:
            raise GameError("bad_square", "No such square.")
        square = board.square(index)
        if square.type not in OWNABLE_TYPES or (square.owner or "").lower() != player.name.lower():
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

    def _build_problem(self, player, board, square):
        if square.type != "property":
            return "You can only build on properties."
        group = square.attributes.get("group")
        if not group or not self.owns_full_group(player.name, board.board_id, group):
            return "You need the whole colour group to build."
        members = board.group(group)
        if any(sq.mortgaged for sq in members):
            return "Unmortgage the colour group before building."
        if square.houses >= self.hotel_level:
            return f"{square.name} already has a hotel."
        if square.houses > min(sq.houses for sq in members):
            return "Build evenly: add to the other properties in the group first."
        cost = square.attributes.get("house_cost", 0)
        if not cost:
            return f"{square.name} can't be built on."
        if player.debts:
            return "Pay your debts before building."
        if player.money < cost:
            return f"You need {money(cost)} to build."
        return None

    def _sell_problem(self, player, board, square):
        if square.type != "property" or not square.houses:
            return f"{square.name} has nothing to sell."
        members = board.group(square.attributes.get("group"))
        if square.houses < max(sq.houses for sq in members):
            return "Sell evenly: sell from the other properties in the group first."
        return None

    def _mortgage_problem(self, player, board, square):
        if square.mortgaged:
            return f"{square.name} is already mortgaged."
        if square.type == "property":
            members = board.group(square.attributes.get("group")) or [square]
            if any(sq.houses for sq in members):
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
        what = "a hotel" if square.houses >= self.hotel_level else "a house"
        self._say(f"{player.name} built {what} on {square.name} for {money(cost)}.")
        self._event("house_built", player,
                    details=f"square={square.name}; houses={square.houses}; cost={cost}")

    def sell_house(self, name, board_id, index):
        player, square = self._manage(name, board_id, index, self._sell_problem)
        what = "a hotel" if square.houses >= self.hotel_level else "a house"
        refund = self.sell_value(square)
        square.attributes["houses"] = square.houses - 1
        player.money += refund
        self._say(f"{player.name} sold {what} on {square.name} for {money(refund)}.")
        self._event("house_sold", player,
                    details=f"square={square.name}; houses={square.houses}; refund={refund}")

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
        [{"board_id", "index", "can_build", "can_sell", ...}]."""
        manage = self._manage_problem(player)
        actions = []
        for bid, sq in self.owned_squares(player.name):
            board = self.boards[bid]
            actions.append({
                "board_id": bid,
                "index": sq.index,
                "can_build": not manage and not self._build_problem(player, board, sq),
                "can_sell": not manage and not self._sell_problem(player, board, sq),
                "can_mortgage": not manage and not self._mortgage_problem(player, board, sq),
                "can_unmortgage": not manage and not self._unmortgage_problem(player, board, sq),
                "tradeable": not self._trade_square_problem(player, board, sq),
                "build_cost": sq.attributes.get("house_cost", 0),
                "sell_value": self.sell_value(sq),
                "mortgage_value": self.mortgage_value(sq),
                "unmortgage_cost": self.unmortgage_cost(sq),
            })
        return actions

    # -- trading -----------------------------------------------------------

    def _trade_square_problem(self, owner, board, square):
        if square.type not in OWNABLE_TYPES or (square.owner or "").lower() != owner.name.lower():
            return f"{owner.name} doesn't own {square.name}."
        if square.type == "property":
            members = board.group(square.attributes.get("group")) or [square]
            if any(sq.houses for sq in members):
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
        trade = {
            "id": self._next_trade_id,
            "from": giver.name,
            "to": taker.name,
            "give_money": give_money,
            "give_squares": [[int(p[0]), int(p[1])] for p in give_squares or ()],
            "get_money": get_money,
            "get_squares": [[int(p[0]), int(p[1])] for p in get_squares or ()],
        }
        if not (give_money or get_money or trade["give_squares"] or trade["get_squares"]):
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
        if trade["to"].lower() != player.name.lower():
            raise GameError("bad_trade", "That trade isn't addressed to you.")
        if not accept:
            self.trades.remove(trade)
            self._say(f"{player.name} rejected {trade['from']}'s trade.")
            self._event("trade_rejected", player, details=trade["summary"])
            return
        giver, taker, give, get = self._validate_trade(trade)
        self.trades.remove(trade)
        for sq in give:
            sq.attributes["owner"] = taker.name
        for sq in get:
            sq.attributes["owner"] = giver.name
        giver.money += trade["get_money"] - trade["give_money"]
        taker.money += trade["give_money"] - trade["get_money"]
        self._say(f"{taker.name} accepted the trade: {trade['summary']}.")
        self._event("trade_accepted", taker, details=trade["summary"])

    def cancel_trade(self, name, trade_id):
        self._require_in_progress()
        player = self._require_player(name)
        trade = self._find_trade(trade_id)
        if trade["from"].lower() != player.name.lower():
            raise GameError("bad_trade", "Only the player who offered a trade can cancel it.")
        self.trades.remove(trade)
        self._say(f"{player.name} withdrew their trade offer to {trade['to']}.")

    def _cancel_trades_with(self, player):
        self.trades = [t for t in self.trades
                       if player.name.lower() not in (t["from"].lower(), t["to"].lower())]

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
                f"net_worth={self.net_worth(p)}; position={position}; result={result}"))

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

    def to_dict(self):
        current = self.current_player
        players = []
        for p in self.players:
            data = p.to_dict()
            data["properties"] = [
                {"board_id": bid, "index": sq.index} for bid, sq in self.owned_squares(p.name)
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
            "min_players": self._setting("min_players"),
            "max_players": self._setting("max_players"),
            "rules": {
                "jail_fine": self._setting("jail_fine"),
                "max_jail_turns": self._setting("max_jail_turns"),
                "hotel_level": self.hotel_level,
            },
            "winner": self.winner,
            "standings": [
                {"name": p.name, "net_worth": self.net_worth(p), "bankrupt": p.bankrupt, "left": p.left}
                for p in self.standings()
            ] if self.status == GameStatus.ENDED else None,
            "log": self.log[-50:],
        }
