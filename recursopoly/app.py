"""Recursopoly web server.

Flask serves the pages; Flask-SocketIO carries the real-time game traffic.
Each game is a SocketIO room named after its join code. This module only
handles routes, socket events and room bookkeeping: every rule decision is
delegated to game_engine.Game.

Run with:  python app.py
"""

import logging
import threading

from flask import Flask, abort, jsonify, redirect, render_template, request, url_for
from flask_socketio import SocketIO, emit, join_room, leave_room

from config import load_config
from game_engine import (
    Game,
    GameError,
    GameStatus,
    generate_join_code,
    load_boards,
    normalise_join_code,
)
from logger import ScoreLogger

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(name)s] %(message)s")
log = logging.getLogger("recursopoly")

CONFIG = load_config()

app = Flask(__name__)
app.config["SECRET_KEY"] = CONFIG.get("secret_key") or "recursopoly-dev"
# manage_session=False: Recursopoly keeps no Flask session data (players are
# tracked by socket id and seat token). It also avoids a crash on Flask 3.1.3+
# with Flask-SocketIO < 5.6.1, which try to assign the now read-only
# RequestContext.session on every event.
socketio = SocketIO(app, async_mode="threading", manage_session=False)

score_logger = ScoreLogger(CONFIG.path("scores_file"))

# Active games, keyed by join code. In memory only: Recursopoly uses no database.
games = {}
# socket id -> (join_code, player_name), so we know who sent each event.
sessions = {}
# (join_code, player_name) -> the player's most recent socket id.
seat_sids = {}
# One lock guards `games`, `sessions` and every Game object. Socket handlers
# run on separate threads in threading mode.
state_lock = threading.RLock()


def _load_boards():
    """Load board files fresh for each game (games may mutate square
    attributes such as owners in later phases)."""
    return load_boards(CONFIG.path("boards_dir"), board_sizes={0: CONFIG.board_size})


# Fail fast at startup if the board file is broken.
_startup_boards = _load_boards()
log.info(
    "Recursopoly loaded %d board(s); outer board has %d squares",
    len(_startup_boards), _startup_boards[0].size,
)


# ---------------------------------------------------------------------------
# HTTP routes
# ---------------------------------------------------------------------------


@app.route("/")
def index():
    return render_template("index.html", error=request.args.get("error"))


@app.route("/lobby/<code>")
def lobby(code):
    code = normalise_join_code(code)
    with state_lock:
        if code not in games:
            return redirect(url_for("index", error=f"No Recursopoly game with code {code}."))
    return render_template("lobby.html", join_code=code)


@app.route("/game/<code>")
def game_page(code):
    code = normalise_join_code(code)
    with state_lock:
        if code not in games:
            return redirect(url_for("index", error=f"No Recursopoly game with code {code}."))
    return render_template("game.html", join_code=code)


@app.route("/api/game/<code>")
def game_state_api(code):
    """Read-only JSON snapshot of a game (handy for debugging)."""
    with state_lock:
        game = games.get(normalise_join_code(code))
        if game is None:
            abort(404)
        return jsonify(game.to_dict())


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def broadcast_state(game):
    """Send the full game state to everyone in the game's room, and flush any
    queued score-log events."""
    score_logger.log_events(game.join_code, game.drain_events())
    socketio.emit("game_state", game.to_dict(), to=game.join_code)


def send_error(message, code="error"):
    emit("error_message", {"code": code, "message": message})


def current_session():
    """Return (game, player_name) for the calling socket, or (None, None)."""
    info = sessions.get(request.sid)
    if not info:
        return None, None
    code, name = info
    game = games.get(code)
    if game is None or game.get_player(name) is None:
        return None, None
    return game, name


def seat_socket(game, player, rejoined):
    """Bind the calling socket to ``player`` in ``game`` and tell it so."""
    sessions[request.sid] = (game.join_code, player.name)
    seat_sids[(game.join_code, player.name)] = request.sid  # latest socket wins
    join_room(game.join_code)
    emit("joined", {
        "join_code": game.join_code,
        "name": player.name,
        "token": player.token,
        "is_host": game.is_host(player.name),
        "rejoined": rejoined,
        "status": game.status,
    })


def schedule_disconnect_check(code, name, delay):
    """After ``delay`` seconds, announce the player as gone and skip their
    turn if they still haven't reconnected."""

    def worker():
        socketio.sleep(delay + 0.2)
        with state_lock:
            game = games.get(code)
            if game and game.check_disconnect(name, grace=delay):
                broadcast_state(game)

    socketio.start_background_task(worker)


def cleanup_if_abandoned(game):
    """Forget ended games once nobody is watching, and empty lobbies."""
    if not any(p.connected for p in game.players):
        if game.status == GameStatus.ENDED or not game.players:
            games.pop(game.join_code, None)
            for key in [k for k in seat_sids if k[0] == game.join_code]:
                del seat_sids[key]
            log.info("Recursopoly game %s removed", game.join_code)


# ---------------------------------------------------------------------------
# Socket events
# ---------------------------------------------------------------------------


@socketio.on("create_game")
def on_create_game(data):
    data = data or {}
    with state_lock:
        code = generate_join_code(CONFIG.join_code_length, existing=games.keys())
        game = Game(code, _load_boards(), CONFIG)
        try:
            player, _ = game.add_player(data.get("name"))
        except GameError as err:
            return send_error(str(err), err.code)
        games[code] = game
        log.info("Recursopoly game %s created by %s", code, player.name)
        seat_socket(game, player, rejoined=False)
        broadcast_state(game)


@socketio.on("join_game")
def on_join_game(data):
    data = data or {}
    code = normalise_join_code(data.get("code"))
    with state_lock:
        game = games.get(code)
        if game is None:
            return send_error(f"No Recursopoly game found with code '{code}'.", "bad_code")
        try:
            player, rejoined = game.add_player(data.get("name"), token=data.get("token"))
        except GameError as err:
            return send_error(str(err), err.code)
        log.info("Recursopoly game %s: %s %s", code, player.name, "rejoined" if rejoined else "joined")
        seat_socket(game, player, rejoined)
        if rejoined:
            game.skip_turn_if_disconnected(grace=CONFIG.disconnect_grace_seconds)
        broadcast_state(game)


@socketio.on("rejoin")
def on_rejoin(data):
    """Sent by the lobby and game pages on load to reclaim the player's seat
    (the socket changes on every page navigation)."""
    data = data or {}
    code = normalise_join_code(data.get("code"))
    name = data.get("name") or ""
    with state_lock:
        game = games.get(code)
        if game is None:
            return send_error(f"No Recursopoly game found with code '{code}'.", "bad_code")
        if game.get_player(name) is None:
            return send_error("You are not a player in this game. Join from the home page.", "unknown_player")
        try:
            player, _ = game.add_player(name, token=data.get("token"))
        except GameError as err:
            return send_error(str(err), err.code)
        seat_socket(game, player, rejoined=True)
        game.skip_turn_if_disconnected(grace=CONFIG.disconnect_grace_seconds)
        broadcast_state(game)


@socketio.on("start_game")
def on_start_game(_data=None):
    with state_lock:
        game, name = current_session()
        if game is None:
            return send_error("You are not in a game.", "no_session")
        try:
            game.start(name)
        except GameError as err:
            return send_error(str(err), err.code)
        log.info("Recursopoly game %s started with %d players", game.join_code, len(game.players))
        broadcast_state(game)


@socketio.on("roll_dice")
def on_roll_dice(_data=None):
    with state_lock:
        game, name = current_session()
        if game is None:
            return send_error("You are not in a game.", "no_session")
        try:
            game.roll(name)  # the server rolls; the client sends no dice values
        except GameError as err:
            return send_error(str(err), err.code)
        broadcast_state(game)


@socketio.on("decide")
def on_decide(data):
    """The active player answers a pending decision, e.g. {"choice": "buy"}."""
    data = data or {}
    with state_lock:
        game, name = current_session()
        if game is None:
            return send_error("You are not in a game.", "no_session")
        try:
            game.decide(name, data.get("choice"))
        except GameError as err:
            return send_error(str(err), err.code)
        broadcast_state(game)


@socketio.on("end_game")
def on_end_game(_data=None):
    with state_lock:
        game, name = current_session()
        if game is None:
            return send_error("You are not in a game.", "no_session")
        try:
            game.end(name)
        except GameError as err:
            return send_error(str(err), err.code)
        log.info("Recursopoly game %s ended by %s", game.join_code, name)
        broadcast_state(game)


@socketio.on("leave_game")
def on_leave_game(_data=None):
    with state_lock:
        game, name = current_session()
        if game is None:
            return
        try:
            game.remove_player(name)
        except GameError as err:
            return send_error(str(err), err.code)
        sessions.pop(request.sid, None)
        leave_room(game.join_code)
        emit("left", {"join_code": game.join_code})
        broadcast_state(game)
        cleanup_if_abandoned(game)


@socketio.on("disconnect")
def on_disconnect(*_args):
    with state_lock:
        info = sessions.pop(request.sid, None)
        if not info:
            return
        code, name = info
        game = games.get(code)
        if game is None:
            return
        player = game.get_player(name)
        # Ignore stale sockets: the player may already be on a newer one
        # (e.g. they navigated from the lobby to the game page).
        if player is None or seat_sids.get((code, player.name)) != request.sid:
            return
        game.mark_disconnected(name)
        broadcast_state(game)
        schedule_disconnect_check(code, player.name, CONFIG.disconnect_grace_seconds)
        cleanup_if_abandoned(game)


if __name__ == "__main__":
    log.info("Starting Recursopoly on %s:%s", CONFIG.host, CONFIG.port)
    socketio.run(
        app,
        host=CONFIG.host,
        port=CONFIG.port,
        debug=CONFIG.debug,
        allow_unsafe_werkzeug=True,
    )
