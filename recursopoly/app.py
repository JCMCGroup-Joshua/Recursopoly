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

from config import BASE_DIR, load_config
from game_engine import (
    Game,
    GameError,
    GameStatus,
    generate_join_code,
    normalise_join_code,
)
from logger import ScoreLogger
from rulesets import load_rulesets

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


# Rule sets (rulesets/*.json), each with its board and card decks, loaded and
# checked at startup so a broken file fails fast. Every new game gets its own
# copy of the board to play on.
RULESETS = load_rulesets(CONFIG.path("rulesets_dir"), BASE_DIR)
DEFAULT_RULESET = CONFIG.default_ruleset if CONFIG.default_ruleset in RULESETS else "classic"
for _rs in RULESETS.values():
    log.info(
        "Recursopoly rule set '%s' (%s): %d squares, decks: %s",
        _rs.id, _rs.name, len(_rs.board["squares"]),
        ", ".join(f"{name} ({len(cards)})" for name, cards in _rs.decks.items()) or "none",
    )


# ---------------------------------------------------------------------------
# HTTP routes
# ---------------------------------------------------------------------------


@app.route("/")
def index():
    return render_template(
        "index.html",
        error=request.args.get("error"),
        rulesets=list(RULESETS.values()),
        default_ruleset=DEFAULT_RULESET,
    )


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
    """{"name": host's display name, "ruleset": rule set id}"""
    data = data or {}
    ruleset = RULESETS.get(data.get("ruleset") or DEFAULT_RULESET)
    if ruleset is None:
        return send_error("Unknown rule set.", "bad_ruleset")
    with state_lock:
        code = generate_join_code(CONFIG.join_code_length, existing=games.keys())
        game = Game(code, ruleset)
        try:
            player, _ = game.add_player(data.get("name"))
        except GameError as err:
            return send_error(str(err), err.code)
        games[code] = game
        log.info("Recursopoly game %s created by %s with the %s rule set", code, player.name, ruleset.id)
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


def player_action(action):
    """Run ``action(game, player_name)`` for the calling socket's player,
    reporting rule errors to them and broadcasting the new state."""
    with state_lock:
        game, name = current_session()
        if game is None:
            return send_error("You are not in a game.", "no_session")
        try:
            action(game, name)
        except GameError as err:
            return send_error(str(err), err.code)
        broadcast_state(game)


def _int(data, key):
    try:
        return int((data or {}).get(key))
    except (TypeError, ValueError):
        raise GameError("bad_request", f"Missing or invalid '{key}'.") from None


def _square_action(method_name):
    """Handler for events that act on one square: {"board_id", "index"}."""
    def handler(data):
        def action(game, name):
            getattr(game, method_name)(name, _int(data, "board_id"), _int(data, "index"))
        player_action(action)
    return handler


# Jail
socketio.on_event("pay_jail_fine", lambda _data=None: player_action(lambda g, n: g.pay_jail_fine(n)))
socketio.on_event("use_jail_card", lambda _data=None: player_action(lambda g, n: g.use_jail_card(n)))

# Buildings and mortgages: {"board_id": 0, "index": 39}
for _event in ("build_house", "build_hotel", "sell_house", "mortgage", "unmortgage"):
    socketio.on_event(_event, _square_action(_event))


@socketio.on("propose_trade")
def on_propose_trade(data):
    """{"to", "give_money", "give_squares": [[board_id, index], ...],
    "get_money", "get_squares"}"""
    data = data or {}
    player_action(lambda g, n: g.propose_trade(
        n, data.get("to"),
        give_money=data.get("give_money", 0), give_squares=data.get("give_squares") or [],
        get_money=data.get("get_money", 0), get_squares=data.get("get_squares") or [],
    ))


@socketio.on("respond_trade")
def on_respond_trade(data):
    """{"trade_id", "accept": true/false}"""
    player_action(lambda g, n: g.respond_trade(n, _int(data, "trade_id"), bool((data or {}).get("accept"))))


@socketio.on("cancel_trade")
def on_cancel_trade(data):
    player_action(lambda g, n: g.cancel_trade(n, _int(data, "trade_id")))


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
