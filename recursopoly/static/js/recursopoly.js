/*
 * Recursopoly front end.
 *
 * One script for all three pages; <body data-page="..."> picks which set of
 * handlers runs. The browser never decides anything about the rules: it sends
 * intentions ("roll_dice", "start_game", ...) and redraws from the full
 * game_state the server broadcasts after every action.
 */
(function () {
    "use strict";

    var page = document.body.dataset.page;
    var pageCode = document.body.dataset.joinCode || null;
    var socket = io();

    // ---- Seat credentials ----------------------------------------------
    // Stored per tab (sessionStorage) so several players can share one
    // browser while testing. The token lets a new page reclaim the seat.

    function credsKey(code) { return "recursopoly:" + code; }

    function saveCreds(code, name, token) {
        try {
            sessionStorage.setItem(credsKey(code), JSON.stringify({ name: name, token: token }));
        } catch (e) { /* storage unavailable: rejoin by name still works */ }
    }

    function loadCreds(code) {
        try {
            return JSON.parse(sessionStorage.getItem(credsKey(code)) || "null");
        } catch (e) {
            return null;
        }
    }

    function clearCreds(code) {
        try { sessionStorage.removeItem(credsKey(code)); } catch (e) { /* ignore */ }
    }

    // ---- Small DOM helpers ------------------------------------------------

    function $(id) { return document.getElementById(id); }

    function el(tag, className, text) {
        var node = document.createElement(tag);
        if (className) node.className = className;
        if (text !== undefined && text !== null) node.textContent = text;
        return node;
    }

    function showError(message) {
        var box = $("error");
        if (!box) { alert(message); return; }
        box.textContent = message;
        box.hidden = false;
        clearTimeout(showError.timer);
        showError.timer = setTimeout(function () { box.hidden = true; }, 6000);
    }

    function showFatal(message) {
        var box = $("error");
        box.textContent = message + " ";
        var link = el("a", null, "Back to the Recursopoly home page");
        link.href = "/";
        box.appendChild(link);
        box.hidden = false;
    }

    var navigating = false;

    function goTo(status, code) {
        // Several game_state broadcasts can arrive back to back; only follow
        // the first redirect so we don't abort our own navigation.
        if (navigating) return;
        navigating = true;
        if (status === "lobby") window.location.href = "/lobby/" + code;
        else window.location.href = "/game/" + code;
    }

    var DIE_FACES = ["", "⚀", "⚁", "⚂", "⚃", "⚄", "⚅"];

    function formatMoney(amount) { return "£" + amount; }

    // =====================================================================
    // Landing page
    // =====================================================================

    function initIndex() {
        var pending = null;  // {code?, name} of the request in flight

        $("create-form").addEventListener("submit", function (ev) {
            ev.preventDefault();
            pending = { name: $("create-name").value.trim() };
            socket.emit("create_game", { name: pending.name });
        });

        $("join-form").addEventListener("submit", function (ev) {
            ev.preventDefault();
            var code = $("join-code").value.trim().toUpperCase();
            var name = $("join-name").value.trim();
            var creds = loadCreds(code);
            pending = { code: code, name: name };
            socket.emit("join_game", {
                code: code,
                name: name,
                token: creds && creds.name.toLowerCase() === name.toLowerCase() ? creds.token : null
            });
        });

        socket.on("joined", function (info) {
            saveCreds(info.join_code, info.name, info.token);
            goTo(info.status, info.join_code);
        });

        socket.on("error_message", function (err) { showError(err.message); });
    }

    // =====================================================================
    // Shared by lobby and game pages
    // =====================================================================

    var me = null;  // {name, token}

    function initSeat(onState) {
        me = loadCreds(pageCode);
        if (!me) {
            showFatal("You haven't joined game " + pageCode + " in this tab.");
            return;
        }

        // Re-sent on every (re)connect, including socket.io auto-reconnects.
        socket.on("connect", function () {
            socket.emit("rejoin", { code: pageCode, name: me.name, token: me.token });
        });

        socket.on("joined", function (info) {
            me.name = info.name;
            me.token = info.token;
            saveCreds(info.join_code, info.name, info.token);
        });

        socket.on("game_state", onState);

        socket.on("left", function () {
            clearCreds(pageCode);
            window.location.href = "/";
        });

        socket.on("error_message", function (err) {
            if (err.code === "bad_code" || err.code === "unknown_player" || err.code === "left") {
                clearCreds(pageCode);
                showFatal(err.message);
            } else {
                showError(err.message);
            }
        });

        socket.on("disconnect", function () {
            showError("Connection lost. Trying to reconnect…");
        });

        var leave = $("leave-btn");
        if (leave) {
            leave.addEventListener("click", function () {
                if (confirm("Leave this game of Recursopoly?")) socket.emit("leave_game");
            });
        }

        var end = $("end-btn");
        if (end) {
            end.addEventListener("click", function () {
                if (confirm("End the game for everyone?")) socket.emit("end_game");
            });
        }
    }

    function isMe(name) {
        return me && name && name.toLowerCase() === me.name.toLowerCase();
    }

    function playerRow(player, state, showMoney) {
        var li = el("li");
        if (state.current_player === player.name) li.classList.add("is-current");
        if (!player.connected) li.classList.add("is-offline");

        var swatch = el("span", "swatch");
        swatch.style.background = player.colour;
        li.appendChild(swatch);

        li.appendChild(el("span", "name", player.name + (isMe(player.name) ? " (you)" : "")));
        if (player.name === state.host) li.appendChild(el("span", "tag tag-host", "host"));
        if (player.left) li.appendChild(el("span", "tag", "left"));
        else if (!player.connected) li.appendChild(el("span", "tag", "offline"));
        if (showMoney) li.appendChild(el("span", "money", formatMoney(player.money)));
        return li;
    }

    // =====================================================================
    // Lobby page
    // =====================================================================

    function initLobby() {
        $("copy-code").addEventListener("click", function () {
            if (navigator.clipboard) navigator.clipboard.writeText(pageCode);
        });

        $("start-btn").addEventListener("click", function () {
            socket.emit("start_game");
        });

        initSeat(function (state) {
            if (state.status === "in_progress") { goTo("game", state.join_code); return; }

            var list = $("lobby-players");
            list.replaceChildren();
            state.players.forEach(function (p) {
                if (!p.left) list.appendChild(playerRow(p, state, false));
            });

            var count = state.players.length;
            $("player-count").textContent = "(" + count + " / " + state.max_players + ")";

            var host = isMe(state.host);
            var enough = count >= state.min_players;
            $("start-btn").hidden = !host;
            $("start-btn").disabled = !enough || state.status !== "lobby";
            $("end-btn").hidden = !host || state.status !== "lobby";

            var status = $("lobby-status");
            if (state.status === "ended") {
                status.textContent = "This game was cancelled by the host.";
            } else if (!enough) {
                status.textContent = "Waiting for at least " + state.min_players + " players…";
            } else if (host) {
                status.textContent = "Ready! Press Start when everyone is here.";
            } else {
                status.textContent = "Waiting for " + state.host + " to start the game…";
            }
        });
    }

    // =====================================================================
    // Game page
    // =====================================================================

    // Grid cell (1-based row/col) for square `i` on a board of `n` squares,
    // walking anticlockwise from the bottom-right corner like Monopoly.
    function squareCell(i, n) {
        var s = Math.ceil(n / 4);
        if (i <= s) return { row: s + 1, col: s + 1 - i };            // bottom, right -> left
        if (i <= 2 * s) return { row: s + 1 - (i - s), col: 1 };      // left, bottom -> top
        if (i <= 3 * s) return { row: 1, col: 1 + (i - 2 * s) };      // top, left -> right
        return { row: 1 + (i - 3 * s), col: s + 1 };                  // right, top -> bottom
    }

    var SQUARE_ICONS = {
        chance: "?",
        community_chest: "✉",
        station: "\u{1F686}",
        utility: "⚡",
        tax: "£",
        jail: "▓",
        go_to_jail: "→",
        free_parking: "P",
        go: "←"
    };

    var boardBuiltFor = null;  // board signature, so we only rebuild on change
    var squareNodes = [];

    function buildBoard(board) {
        var container = $("board");
        var n = board.size;
        var s = Math.ceil(n / 4);
        container.replaceChildren();
        container.style.setProperty("--cells", s + 1);
        squareNodes = [];

        board.squares.forEach(function (sq) {
            var cell = squareCell(sq.index, n);
            var node = el("div", "square type-" + sq.type);
            if (sq.index % s === 0) node.classList.add("corner");
            node.style.gridRow = cell.row;
            node.style.gridColumn = cell.col;
            node.title = sq.name + " (" + sq.type.replace(/_/g, " ") + ")";

            var group = sq.attributes && sq.attributes.group;
            if (group && sq.type === "property") {
                var band = el("div", "band");
                band.style.background = "var(--group-" + group + ", #999)";
                node.appendChild(band);
            }
            node.appendChild(el("div", "sq-name", sq.name));
            if (SQUARE_ICONS[sq.type] && sq.type !== "property") {
                node.appendChild(el("div", "sq-icon", SQUARE_ICONS[sq.type]));
            }
            var tokens = el("div", "tokens");
            node.appendChild(tokens);
            container.appendChild(node);
            squareNodes[sq.index] = tokens;
        });

        // The centre: in Phase 4 the next board inward is drawn here.
        var centre = el("div", "board-centre");
        centre.style.gridRow = "2 / " + (s + 1);
        centre.style.gridColumn = "2 / " + (s + 1);
        centre.appendChild(el("div", "centre-logo", "Recursopoly"));
        container.appendChild(centre);
    }

    function renderTokens(state, boardId) {
        squareNodes.forEach(function (node) { node.replaceChildren(); });
        state.players.forEach(function (p) {
            if (p.position.board_id !== boardId || p.left) return;
            var holder = squareNodes[p.position.index];
            if (!holder) return;
            var token = el("span", "token", p.name.charAt(0).toUpperCase());
            token.style.background = p.colour;
            token.title = p.name;
            if (state.current_player === p.name) token.classList.add("is-current");
            if (!p.connected) token.classList.add("is-offline");
            holder.appendChild(token);
        });
    }

    function renderDice(state) {
        var box = $("dice");
        box.replaceChildren();
        var roll = state.last_roll;
        if (!roll) {
            box.appendChild(el("span", "roll-caption", "No dice rolled yet"));
            return;
        }
        roll.dice.forEach(function (d) { box.appendChild(el("span", "die", DIE_FACES[d])); });
        box.appendChild(el("span", "roll-caption",
            roll.player + ": " + roll.total + (roll.doubles ? " (doubles!)" : "")));
    }

    function renderTurn(state) {
        var indicator = $("turn-indicator");
        var myTurn = state.status === "in_progress" && isMe(state.current_player);
        indicator.classList.toggle("is-you", myTurn);
        if (state.status === "ended") indicator.textContent = "Game over";
        else if (myTurn) indicator.textContent = "Your turn! Roll the dice.";
        else if (state.current_player) indicator.textContent = state.current_player + "'s turn";
        else indicator.textContent = "Waiting for players…";

        $("roll-btn").disabled = !(myTurn && state.turn_state === "waiting_to_roll");
    }

    function renderPlayers(state) {
        var list = $("game-players");
        list.replaceChildren();
        state.players.forEach(function (p) { list.appendChild(playerRow(p, state, true)); });
    }

    function renderLog(state) {
        var list = $("event-log");
        list.replaceChildren();
        // Newest first, so the latest roll is always visible without scrolling.
        state.log.slice().reverse().forEach(function (entry) {
            list.appendChild(el("li", null, entry.message));
        });
    }

    function renderGameOver(state) {
        var overlay = $("game-over");
        if (state.status !== "ended") { overlay.hidden = true; return; }
        var list = $("final-standings");
        list.replaceChildren();
        var byName = {};
        state.players.forEach(function (p) { byName[p.name] = p; });
        (state.standings || []).forEach(function (name) {
            list.appendChild(el("li", null, name + " — " + formatMoney(byName[name].money)));
        });
        overlay.hidden = false;
        $("end-btn").hidden = true;
        $("leave-btn").hidden = true;
    }

    function initGame() {
        $("roll-btn").addEventListener("click", function () {
            $("roll-btn").disabled = true;  // re-enabled by the next state if we roll again
            socket.emit("roll_dice");
        });

        initSeat(function (state) {
            if (state.status === "lobby") { goTo("lobby", state.join_code); return; }

            // Phase 1 shows only the outer board (id 0); Phase 4 renders all.
            var board = state.boards["0"];
            var signature = board.size + ":" + board.name;
            if (boardBuiltFor !== signature) {
                buildBoard(board);
                boardBuiltFor = signature;
            }
            renderTokens(state, 0);
            renderDice(state);
            renderTurn(state);
            renderPlayers(state);
            renderLog(state);
            $("end-btn").hidden = !isMe(state.host) || state.status !== "in_progress";
            renderGameOver(state);
        });
    }

    // ---- Boot ------------------------------------------------------------

    if (page === "index") initIndex();
    else if (page === "lobby") initLobby();
    else if (page === "game") initGame();
})();
