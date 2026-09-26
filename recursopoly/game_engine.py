"""Recursopoly game engine.

Pure Python game rules with no Flask (or any web) imports, so everything here
can be unit-tested on its own. The web layer (app.py) calls into this module
for every rule decision and simply broadcasts the resulting state.

Key design points that later phases build on:

* Every :class:`Board` has a ``board_id``; a :class:`Game` holds its boards in
  a dict keyed by that id (Phase 1 has only board 0).
* A player's position is a :class:`Position` of ``(board_id, index)``, never a
  bare integer, so Phase 4 train travel can move tokens between boards.
* A :class:`Square` has a ``type`` plus a free-form ``attributes`` dict for
  prices, rents, owners, station links, etc.
* Turns are a small state machine (:class:`TurnState`). Phase 1 only uses
  ``WAITING_TO_ROLL`` and ``TURN_OVER``; ``AWAITING_DECISION`` is where Phase 2+
  will pause the turn for "buy or decline", ticket purchases, and so on.
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


class GameStatus:
    LOBBY = "lobby"
    IN_PROGRESS = "in_progress"
    ENDED = "ended"


class TurnState:
    """States of the active player's turn."""

    WAITING_TO_ROLL = "waiting_to_roll"
    # Reserved for Phase 2+: the active player must make a choice (buy a
    # property, buy a train ticket, ...) before the turn can continue.
    AWAITING_DECISION = "awaiting_decision"
    TURN_OVER = "turn_over"


class GameError(Exception):
    """A rule was broken. ``code`` is machine-readable, str(err) is for humans."""

    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


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
    # Extensible: group, price, rent, owner, station links, tax amount, ...
    attributes: dict = field(default_factory=dict)

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
    doubles_in_a_row: int = 0
    # Extensible per-player state for later phases (properties, jail turns,
    # get-out-of-jail cards, journeys, ...).
    attributes: dict = field(default_factory=dict)

    @property
    def active(self):
        """Can this player take a turn right now?"""
        return self.connected and not self.left

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

    def __init__(self, join_code, boards, settings, rng=None):
        """
        ``boards``: dict of board_id -> Board.
        ``settings``: a Config (or anything with attribute access / .get) that
        provides starting_money, go_salary, min_players, max_players,
        max_doubles.
        ``rng``: a random.Random, injectable so tests can fix the dice.
        """
        self.join_code = join_code
        self.boards = dict(boards)
        self.settings = settings
        self.rng = rng or random.SystemRandom()

        self.status = GameStatus.LOBBY
        self.players = []  # join order
        self.host_name = None
        self.current_index = None  # index into self.players
        self.turn_state = None
        self.turn_number = 0
        self.last_roll = None
        self.winner = None  # Phase 3
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
            if p.name.lower() == name.lower():
                return p
        return None

    def _require_player(self, name):
        player = self.get_player(name or "")
        if player is None:
            raise GameError("unknown_player", "You are not a player in this game.")
        return player

    @property
    def current_player(self):
        if self.current_index is None or not self.players:
            return None
        return self.players[self.current_index]

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
        if not player.left and not player.attributes.get("announced_offline"):
            player.attributes["announced_offline"] = True
            self._say(f"{player.name} disconnected.")
            changed = True
        return self.skip_turn_if_disconnected(now=now, grace=grace) or changed

    def remove_player(self, name):
        """A player leaves voluntarily.

        In the lobby their seat is freed (and host passes on if needed). Once
        the game has started they stay on the board but never play again.
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
        player.left = True
        player.connected = False
        self._say(f"{player.name} left the game.")
        if self.status == GameStatus.IN_PROGRESS and was_current:
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
        """Index of the next connected player after ``from_index`` (wrapping),
        or None if nobody is connected."""
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
        player = self.players[index]
        player.doubles_in_a_row = 0
        self.turn_state = TurnState.WAITING_TO_ROLL
        self._say(f"It's {player.name}'s turn.")

    def _advance_turn(self):
        if self.status != GameStatus.IN_PROGRESS:
            return
        self.turn_state = TurnState.TURN_OVER
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
        if not player.left and player.disconnected_since is not None:
            now = time.time() if now is None else now
            if now - player.disconnected_since < grace:
                return False
        before = self.current_index
        self._say(f"{player.name} is not connected; skipping their turn.")
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
        if self.status != GameStatus.IN_PROGRESS:
            raise GameError("not_started", "The game is not in progress.")
        player = self._require_player(name)
        if player is not self.current_player:
            raise GameError("not_your_turn", "It's not your turn.")
        if self.turn_state != TurnState.WAITING_TO_ROLL:
            raise GameError("bad_state", "You can't roll right now.")

        d1, d2 = dice if dice is not None else self.roll_dice()
        total = d1 + d2
        doubles = d1 == d2
        self.last_roll = {"player": player.name, "dice": [d1, d2], "total": total, "doubles": doubles}
        result = {"dice": [d1, d2], "total": total, "doubles": doubles,
                  "passed_go": 0, "jailed": False, "roll_again": False}

        if doubles:
            player.doubles_in_a_row += 1
        else:
            player.doubles_in_a_row = 0

        max_doubles = self._setting("max_doubles")
        if doubles and player.doubles_in_a_row >= max_doubles:
            self._send_to_jail(player)
            self._say(
                f"{player.name} rolled {d1} + {d2}: {max_doubles} doubles in a row! "
                f"Off to {self.square_at(player.position).name}."
            )
            self._event("roll", player, details=f"dice={d1}+{d2}; total={total}; doubles=yes")
            self._event("jailed", player, details=f"{max_doubles} doubles in a row")
            result["jailed"] = True
            self._advance_turn()
            return result

        passed = self._move_forward(player, total)
        square = self.square_at(player.position)
        self._say(f"{player.name} rolled {d1} + {d2} and moved to {square.name}.")
        self._event(
            "roll", player,
            details=f"dice={d1}+{d2}; total={total}; doubles={'yes' if doubles else 'no'}; square={square.name}",
        )
        result["passed_go"] = passed
        result["square"] = square.to_dict()

        self._resolve_landing(player, square)

        if self.turn_state == TurnState.AWAITING_DECISION:
            # Phase 2+: the turn pauses here until the player decides.
            return result
        if doubles:
            result["roll_again"] = True
            self._say(f"{player.name} rolled doubles and rolls again.")
            self.turn_state = TurnState.WAITING_TO_ROLL
        else:
            self._advance_turn()
        return result

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
            self._say(f"{player.name} passed Go and collected {salary}.")
            self._event("passed_go", player, details=f"salary={salary}")
        return passes

    def _send_to_jail(self, player):
        board = self.board_for(player)
        player.position = Position(board.board_id, board.jail_index)
        player.doubles_in_a_row = 0
        # Phase 3 reads this to apply proper jail rules.
        player.attributes["in_jail"] = True

    def _resolve_landing(self, player, square):
        """Apply the effect of landing on ``square``.

        Phase 1: squares are only labels (Go's salary is handled while moving).
        Phase 2 adds buy/rent/tax here (setting AWAITING_DECISION for buy
        choices), Phase 3 adds cards and go_to_jail, Phase 4 adds station
        travel offers.
        """
        return None

    # -- ending ------------------------------------------------------------

    def end(self, requested_by=None):
        """End the game (host only, unless requested_by is None = system)."""
        if requested_by is not None and not self.is_host(requested_by):
            raise GameError("not_host", "Only the host can end the game.")
        if self.status == GameStatus.ENDED:
            raise GameError("ended", "The game has already ended.")
        was_running = self.status == GameStatus.IN_PROGRESS
        self.status = GameStatus.ENDED
        self.turn_state = TurnState.TURN_OVER
        self._say("The game has ended.")
        if was_running:
            for rank, p in enumerate(self.standings(), start=1):
                self._event("game_ended", p, details=f"final_score={p.money}; rank={rank}")

    def standings(self):
        """Players ordered by score (Phase 1: money; Phase 3: net worth)."""
        return sorted(self.players, key=lambda p: (-p.money, p.join_order))

    # -- serialisation -----------------------------------------------------

    def to_dict(self):
        current = self.current_player
        return {
            "join_code": self.join_code,
            "status": self.status,
            "host": self.host_name,
            "players": [p.to_dict() for p in self.players],
            "current_player": current.name if current and self.status == GameStatus.IN_PROGRESS else None,
            "turn_state": self.turn_state,
            "turn_number": self.turn_number,
            "last_roll": self.last_roll,
            "boards": {str(bid): b.to_dict() for bid, b in self.boards.items()},
            "min_players": self._setting("min_players"),
            "max_players": self._setting("max_players"),
            "standings": [p.name for p in self.standings()] if self.status == GameStatus.ENDED else None,
            "log": self.log[-50:],
        }
