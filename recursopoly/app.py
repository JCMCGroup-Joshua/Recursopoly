"""Recursopoly web server.

Flask serves the pages; Flask-SocketIO carries the real-time game traffic.
Each game is a SocketIO room named after its join code. This module only
handles routes, socket events and room bookkeeping: every rule decision is
delegated to game_engine.Game.

Run with:  python app.py
"""

import logging
import threading
import time
from datetime import datetime

from flask import Flask, abort, jsonify, redirect, render_template, request, url_for
from flask_socketio import SocketIO, emit, join_room, leave_room

from config import BASE_DIR, SERVER_FIELDS, load_config, save_config
from game_engine import (
    Game,
    GameError,
    GameStatus,
    generate_join_code,
    normalise_join_code,
)
from logger import ScoreLogger
from rulesets import (
    FIELDS as RULE_FIELDS,
    coerce_values,
    load_rulesets,
    ruleset_file_data,
    save_ruleset,
    validate_values,
)
import stats

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
# Spectators: socket id -> (join_code, display name). They see everything
# but hold no seat, so every game action from them is refused.
spectator_sids = {}
# Chat per game: join_code -> recent messages. In memory only, like games.
chats = {}
CHAT_HISTORY = 100
CHAT_MAX_LENGTH = 200
CHAT_MIN_INTERVAL = 0.5  # seconds between messages from one socket
last_chat_at = {}  # socket id -> time of its last chat message
# One lock guards `games`, `sessions` and every Game object. Socket handlers
# run on separate threads in threading mode.
state_lock = threading.RLock()


# Rule sets (rulesets/*.json), each with its board and card decks, loaded and
# checked at startup so a broken file fails fast. Every new game gets its own
# copy of the board to play on.
RULESETS = load_rulesets(CONFIG.path("rulesets_dir"), BASE_DIR)


def default_ruleset_id():
    return CONFIG.default_ruleset if CONFIG.default_ruleset in RULESETS else "classic"


def reload_rulesets():
    """Re-read every rule set after one is saved from the web. Games already
    created keep the rule set they started with."""
    fresh = load_rulesets(CONFIG.path("rulesets_dir"), BASE_DIR)
    RULESETS.clear()
    RULESETS.update(fresh)


MAX_TURN_TIMER = 3600


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
        default_ruleset=default_ruleset_id(),
    )


@app.template_filter("when")
def format_when(timestamp, time_only=False):
    """Show a scores.csv ISO timestamp as '27 Sep 2026 14:05' (UTC)."""
    try:
        moment = datetime.fromisoformat(timestamp)
    except (TypeError, ValueError):
        return timestamp or ""
    return moment.strftime("%H:%M:%S" if time_only else "%d %b %Y %H:%M")


def _ruleset_names():
    return {rid: rs.name for rid, rs in RULESETS.items()}


@app.route("/leaderboard")
def leaderboard_page():
    """All-time stats, read from scores.csv."""
    rows = score_logger.read_rows()
    table = stats.leaderboard(rows)
    # (label, entry, key, prefix, unit)
    highlights = [
        ("Most wins", stats.top_by(table, "wins"), "wins", "", "wins"),
        ("Highest net worth", stats.top_by(table, "best_net_worth"), "best_net_worth", "\u00a3", "in one game"),
        ("Most properties owned", stats.top_by(table, "most_properties"), "most_properties", "",
         "at the end of a game"),
        ("Most journeys between boards", stats.top_by(table, "journeys"), "journeys", "", "train trips"),
        ("Most games played", stats.top_by(table, "games"), "games", "", "games"),
    ]
    return render_template("leaderboard.html", table=table, highlights=highlights,
                           game_count=len(stats.games(rows)))


@app.route("/history")
def history_page():
    """Past games from scores.csv; ?code=XXXX jumps straight to one."""
    code = normalise_join_code(request.args.get("code"))
    if code:
        return redirect(url_for("history_game", code=code))
    return render_template("history.html", games=stats.games(score_logger.read_rows()), game=None,
                           ruleset_names=_ruleset_names(), error=request.args.get("error"))


@app.route("/history/<code>")
def history_game(code):
    detail = stats.game_detail(score_logger.read_rows(), code)
    if detail is None:
        return redirect(url_for("history_page", error=f"No game {normalise_join_code(code)} in the history."))
    return render_template("history.html", game=detail, games=None, ruleset_names=_ruleset_names(),
                           error=None)


@app.route("/settings")
def settings_page():
    """The settings page. It is locked: the page itself holds no settings,
    they are only sent by /settings/unlock once the admin password is
    accepted."""
    return render_template("settings.html", saving_enabled=bool(CONFIG.admin_password))


def _settings_data():
    """Everything the settings page edits: rule sets and server settings."""
    rulesets = [{
        "id": rs.id, "name": rs.name, "description": rs.description,
        "values": rs.values, "board_count": len(rs.boards),
        "boards": [b.get("name", path) for b, path in zip(rs.boards, rs.board_paths)],
    } for rs in RULESETS.values()]
    return {
        "rule_fields": RULE_FIELDS,
        "rulesets": rulesets,
        "server_fields": SERVER_FIELDS,
        "server_values": {f["key"]: CONFIG.get(f["key"]) for f in SERVER_FIELDS},
    }


# Wrong admin passwords per client address, to slow down guessing.
MAX_PASSWORD_FAILURES = 5
PASSWORD_LOCKOUT_SECONDS = 300
password_failures = {}  # address -> [times of recent failures]


def _password_error(given):
    """None if ``given`` is the admin password, else (message, http status).
    After MAX_PASSWORD_FAILURES wrong tries an address must wait."""
    if not CONFIG.admin_password:
        return ("Saving is turned off: set admin_password in config.txt and restart the "
                "server.", 403)
    address = request.remote_addr or "?"
    now = time.time()
    recent = [t for t in password_failures.get(address, []) if now - t < PASSWORD_LOCKOUT_SECONDS]
    if len(recent) >= MAX_PASSWORD_FAILURES:
        wait = int(PASSWORD_LOCKOUT_SECONDS - (now - recent[0])) + 1
        return f"Too many wrong passwords. Try again in {wait} seconds.", 429
    if not CONFIG.check_admin_password(given):
        recent.append(now)
        password_failures[address] = recent
        return "Wrong admin password.", 403
    password_failures.pop(address, None)
    return None


def _password_problem(given):
    """Like _password_error, as a JSON error response for HTTP routes."""
    error = _password_error(given)
    return _json_error(*error) if error else None


@app.route("/settings/unlock", methods=["POST"])
def unlock_settings():
    """{"password"}: returns the settings once the admin password is right."""
    problem = _password_problem((request.get_json(silent=True) or {}).get("password"))
    if problem:
        return problem
    with state_lock:
        return jsonify({"ok": True, **_settings_data()})


def _json_error(message, status=400):
    return jsonify({"ok": False, "error": message}), status


@app.route("/settings/ruleset", methods=["POST"])
def save_ruleset_route():
    """{"password", "id", "name", "description", "source", "values"}: save
    a rule set (a new id, or overwrite an existing one)."""
    data = request.get_json(silent=True) or {}
    problem = _password_problem(data.get("password"))
    if problem:
        return problem
    with state_lock:
        source = RULESETS.get(data.get("source") or data.get("id"))
        if source is None:
            return _json_error("Unknown rule set to copy the boards and cards from.")
        try:
            values = {**source.values, **coerce_values(data.get("values"))}
            rid = _save_ruleset_file(data.get("id"), data.get("name"), data.get("description"),
                                     source, values)
        except (ValueError, OSError) as err:
            return _json_error(str(err))
    return jsonify({"ok": True, "id": rid, "message": f"Saved rule set '{RULESETS[rid].name}'."})


@app.route("/settings/server", methods=["POST"])
def save_server_settings():
    """{"password", "values": {...}, "new_password"}: save server settings
    to config.txt and apply them (host, port and debug need a restart)."""
    data = request.get_json(silent=True) or {}
    problem = _password_problem(data.get("password"))
    if problem:
        return problem
    allowed = {f["key"]: f for f in SERVER_FIELDS}
    changes = dict(data.get("values") or {})
    unknown = sorted(set(changes) - set(allowed))
    if unknown:
        return _json_error(f"These settings can't be changed here: {', '.join(unknown)}")
    if "default_ruleset" in changes and changes["default_ruleset"] not in RULESETS:
        return _json_error("Unknown default rule set.")
    new_password = (data.get("new_password") or "").strip()
    if new_password:
        changes["admin_password"] = new_password
    with state_lock:
        before = CONFIG.as_dict()
        try:
            typed = CONFIG.update(changes)
            save_config(typed)
        except (ValueError, OSError) as err:
            CONFIG.update({k: before[k] for k in changes})
            return _json_error(str(err))
    restart = [allowed[k]["label"] for k in typed if k in allowed and allowed[k].get("restart")
               and before.get(k) != typed[k]]
    message = "Saved to config.txt."
    if restart:
        message += " Restart the server for these to take effect: " + ", ".join(restart) + "."
    return jsonify({"ok": True, "message": message})


@app.route("/lobby/<code>")
def lobby(code):
    code = normalise_join_code(code)
    with state_lock:
        if code not in games:
            return redirect(url_for("index", error=f"No Recursopoly game with code {code}."))
    return render_template("lobby.html", join_code=code, rule_fields=RULE_FIELDS,
                           max_turn_timer=MAX_TURN_TIMER, saving_enabled=bool(CONFIG.admin_password))


@app.route("/game/<code>")
def game_page(code):
    code = normalise_join_code(code)
    with state_lock:
        if code not in games:
            return redirect(url_for("index", error=f"No Recursopoly game with code {code}."))
    return render_template("game.html", join_code=code, spectator=False, watch_name="")


@app.route("/watch/<code>")
def watch_page(code):
    """Spectator view: the game page with no seat and no controls."""
    code = normalise_join_code(code)
    with state_lock:
        if code not in games:
            return redirect(url_for("index", error=f"No Recursopoly game with code {code}."))
    return render_template("game.html", join_code=code, spectator=True,
                           watch_name=(request.args.get("name") or "").strip()[:20])


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
    state = game.to_dict()
    state["spectators"] = sorted(name for code, name in spectator_sids.values() if code == game.join_code)
    # Turn timer: the client shows a countdown to turn_deadline, corrected
    # for clock differences using server_time.
    state["server_time"] = time.time()
    state["turn_deadline"] = (game.turn_activity_at + game.turn_timer
                              if game.turn_timer and game.turn_activity_at else None)
    socketio.emit("game_state", state, to=game.join_code)


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
    emit("chat_history", chats.get(game.join_code, []))


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


def note_activity(game, name):
    """Reset the turn timer when the active player does something."""
    current = game.current_player
    if current is not None and current.name == name:
        game.touch()


def turn_timer_loop():
    """Background task: end turns that have been idle too long."""
    while True:
        socketio.sleep(1)
        with state_lock:
            for game in list(games.values()):
                if game.end_idle_turn(game.turn_timer):
                    log.info("Recursopoly game %s: turn timed out", game.join_code)
                    broadcast_state(game)


def cleanup_if_abandoned(game):
    """Forget ended games once nobody is watching, and empty lobbies."""
    if not any(p.connected for p in game.players):
        if game.status == GameStatus.ENDED or not game.players:
            games.pop(game.join_code, None)
            chats.pop(game.join_code, None)
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
    ruleset = RULESETS.get(data.get("ruleset") or default_ruleset_id())
    if ruleset is None:
        return send_error("Unknown rule set.", "bad_ruleset")
    with state_lock:
        code = generate_join_code(CONFIG.join_code_length, existing=games.keys())
        game = Game(code, ruleset)
        game.turn_timer = CONFIG.turn_timer_seconds
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
        note_activity(game, name)
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
        note_activity(game, name)
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
        note_activity(game, name)
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


@socketio.on("sell_stake")
def on_sell_stake(data):
    """{"board_id", "index", "stake": stake number}"""
    player_action(lambda g, n: g.sell_stake(n, _int(data, "board_id"), _int(data, "index"), _int(data, "stake")))


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


def _turn_timer_value(raw):
    try:
        value = int(raw)
    except (TypeError, ValueError):
        raise ValueError("Turn timer must be a whole number of seconds.") from None
    if not 0 <= value <= MAX_TURN_TIMER:
        raise ValueError(f"Turn timer must be between 0 and {MAX_TURN_TIMER} seconds.")
    return value


@socketio.on("update_rules")
def on_update_rules(data):
    """The host changes this game's rules in the lobby. Only this game is
    affected; no files change. {"values": {key: value}, "turn_timer": n}
    or {"reset": true} to go back to the rule set's values."""
    data = data or {}
    with state_lock:
        game, name = current_session()
        if game is None:
            return send_error("You are not in a game.", "no_session")
        try:
            if data.get("reset"):
                values, timer = dict(game.ruleset.values), CONFIG.turn_timer_seconds
            else:
                values = {**game.rules, **coerce_values(data.get("values"))}
                validate_values(values, len(game.boards), "These rules")
                timer = _turn_timer_value(data.get("turn_timer", game.turn_timer))
            game.set_rules(name, values, turn_timer=timer)
        except ValueError as err:
            return send_error(str(err), "bad_rules")
        except GameError as err:
            return send_error(str(err), err.code)
        broadcast_state(game)


def _save_ruleset_file(rid, name, description, source, values):
    """Write rulesets/<rid>.json (boards and cards from ``source``) and
    reload the rule sets. Returns the saved id."""
    rid = (rid or "").strip().lower()
    name = (name or "").strip() or rid
    base = None if rid == "classic" else RULESETS["classic"].values
    validate_values(values, len(source.board_paths), f"Rule set '{rid}'")
    data = ruleset_file_data(name, (description or "").strip(), source.board_paths,
                             source.card_paths, values, base)
    save_ruleset(CONFIG.path("rulesets_dir"), rid, data)
    try:
        reload_rulesets()
    except (OSError, ValueError) as err:
        raise ValueError(f"Saved, but the rule sets failed to reload: {err}") from None
    log.info("Recursopoly rule set '%s' saved from the web", rid)
    return rid


@socketio.on("save_ruleset")
def on_save_ruleset(data):
    """The host saves this game's current rules as a rule set file, so they
    can be picked for future games. Needs the admin password.
    {"id", "name", "description", "password"}"""
    data = data or {}
    with state_lock:
        game, name = current_session()
        if game is None:
            return send_error("You are not in a game.", "no_session")
        if not game.is_host(name):
            return send_error("Only the host can save the rules.", "not_host")
        error = _password_error(data.get("password"))
        if error:
            return send_error(error[0], "bad_password")
        try:
            rid = _save_ruleset_file(data.get("id"), data.get("name"), data.get("description"),
                                     game.ruleset, dict(game.rules))
        except (ValueError, OSError) as err:
            return send_error(str(err), "save_failed")
        emit("ruleset_saved", {"id": rid, "name": RULESETS[rid].name})


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


@socketio.on("spectate")
def on_spectate(data):
    """{"code", "name" (optional)}: watch a game without taking a seat."""
    data = data or {}
    code = normalise_join_code(data.get("code"))
    name = " ".join((data.get("name") or "").split())[:20] or "Spectator"
    with state_lock:
        game = games.get(code)
        if game is None:
            return send_error(f"No Recursopoly game found with code '{code}'.", "bad_code")
        spectator_sids[request.sid] = (code, name)
        join_room(code)
        emit("watching", {"join_code": code, "name": name})
        emit("chat_history", chats.get(code, []))
        broadcast_state(game)


@socketio.on("chat")
def on_chat(data):
    """{"text"}: a chat message from a player or spectator in the room."""
    text = " ".join(str((data or {}).get("text") or "").split())[:CHAT_MAX_LENGTH]
    if not text:
        return
    now = time.time()
    if now - last_chat_at.get(request.sid, 0) < CHAT_MIN_INTERVAL:
        return send_error("You're sending messages too quickly.", "slow_down")
    with state_lock:
        game, name = current_session()
        spectator = False
        if game is None and request.sid in spectator_sids:
            code, name = spectator_sids[request.sid]
            game, spectator = games.get(code), True
        if game is None:
            return send_error("You are not in a game.", "no_session")
        last_chat_at[request.sid] = now
        message = {"time": now, "name": name, "text": text, "spectator": spectator}
        history = chats.setdefault(game.join_code, [])
        history.append(message)
        del history[:-CHAT_HISTORY]
        socketio.emit("chat_message", message, to=game.join_code)


@socketio.on("disconnect")
def on_disconnect(*_args):
    with state_lock:
        last_chat_at.pop(request.sid, None)
        watching = spectator_sids.pop(request.sid, None)
        if watching and watching[0] in games:
            broadcast_state(games[watching[0]])
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


def start_background_tasks():
    # Always running: hosts can turn the timer on for a single game.
    socketio.start_background_task(turn_timer_loop)


if __name__ == "__main__":
    log.info("Starting Recursopoly on %s:%s", CONFIG.host, CONFIG.port)
    start_background_tasks()
    socketio.run(
        app,
        host=CONFIG.host,
        port=CONFIG.port,
        debug=CONFIG.debug,
        allow_unsafe_werkzeug=True,
    )
