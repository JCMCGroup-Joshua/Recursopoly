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

    var DIE_FACES = ["", "\u2680", "\u2681", "\u2682", "\u2683", "\u2684", "\u2685"];

    function formatMoney(amount) { return "\u00a3" + amount; }

    // =====================================================================
    // Landing page
    // =====================================================================

    function initIndex() {
        var pending = null;  // {code?, name} of the request in flight

        var picker = $("create-ruleset");
        function describeRuleset() {
            var opt = picker.options[picker.selectedIndex];
            $("ruleset-description").textContent = opt ? opt.dataset.description : "";
        }
        picker.addEventListener("change", describeRuleset);
        describeRuleset();

        $("create-form").addEventListener("submit", function (ev) {
            ev.preventDefault();
            pending = { name: $("create-name").value.trim() };
            socket.emit("create_game", { name: pending.name, ruleset: picker.value });
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
            showError("Connection lost. Trying to reconnect\u2026");
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

    // Name, description and key values of the game's rule set.
    function renderRuleset(state) {
        var rs = state.ruleset;
        if (!rs || !$("ruleset-name")) return;
        $("ruleset-name").textContent = rs.name;
        $("ruleset-description").textContent = rs.description;
        var list = $("ruleset-summary");
        list.replaceChildren();
        rs.summary.forEach(function (row) {
            list.appendChild(el("dt", null, row.label));
            list.appendChild(el("dd", null, row.value));
        });
        if ($("header-ruleset")) $("header-ruleset").textContent = rs.name + " \u00b7";
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
        if (player.bankrupt) li.appendChild(el("span", "tag tag-bankrupt", "bankrupt"));
        else if (player.left) li.appendChild(el("span", "tag", "left"));
        else if (!player.connected) li.appendChild(el("span", "tag", "offline"));
        if (player.in_jail) li.appendChild(el("span", "tag tag-jail", "in jail"));
        if (showMoney) {
            var money = el("span", "money", formatMoney(player.money));
            if (player.money < 0) money.classList.add("is-negative");
            li.appendChild(money);
        }
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
            renderRuleset(state);

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
                status.textContent = "Waiting for at least " + state.min_players + " players\u2026";
            } else if (host) {
                status.textContent = "Ready! Press Start when everyone is here.";
            } else {
                status.textContent = "Waiting for " + state.host + " to start the game\u2026";
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
        community_chest: "\u2709",
        station: "\u{1F686}",
        utility: "\u26a1",
        tax: "\u00a3",
        jail: "\u2593",
        go_to_jail: "\u2192",
        free_parking: "P",
        go: "\u2190"
    };

    var OWNABLE = { property: true, station: true, utility: true };
    var currentGroups = {};    // colour groups of the board being shown

    var boardBuiltFor = null;  // board signature, so we only rebuild on change
    var squareNodes = [];      // token holder per square index
    var squareEls = [];        // whole square element per square index
    var buildingNodes = [];    // houses / hotel holder per square index
    var potNodes = [];         // pot display per pooled square index
    var latestState = null;    // most recent game_state, for dialogs

    // Colour for a property's group: from the board file, else the CSS palette.
    function groupColour(sq) {
        var group = sq.attributes && sq.attributes.group;
        if (!group) return null;
        var info = currentGroups[group];
        return (info && info.colour) || "var(--group-" + group + ", #999)";
    }

    function plural(n, word) { return n + " " + word + (n === 1 ? "" : "s"); }

    function buildingText(sq) {
        var a = sq.attributes || {};
        if (a.hotels) return plural(a.hotels, "hotel");
        if (a.houses) return plural(a.houses, "house");
        return "";
    }

    function moneyList(values) {
        return [].concat(values).map(function (v) { return formatMoney(v); }).join(", ");
    }

    // Tooltip text: name, type, price, rent, buildings and owner.
    function squareTitle(sq, state) {
        var a = sq.attributes || {};
        var parts = [sq.name + " (" + sq.type.replace(/_/g, " ") + ")"];
        if (a.price) parts.push("Price " + formatMoney(a.price));
        if (sq.type === "property" && a.rent) parts.push("Rent " + formatMoney(a.rent));
        if (sq.type === "property" && a.house_rents) parts.push("With houses: " + moneyList(a.house_rents));
        if (sq.type === "property" && a.hotel_rents) parts.push("With hotels: " + moneyList(a.hotel_rents));
        if (sq.type === "property" && a.house_cost) {
            parts.push("Houses cost " + formatMoney(a.house_cost) +
                (a.hotel_cost ? ", hotels " + formatMoney(a.hotel_cost) : ""));
        }
        if (sq.type === "station" && a.rents) parts.push("Rent by stations owned: " + moneyList(a.rents));
        if (sq.type === "utility" && a.dice_multipliers) {
            parts.push("Rent: dice \u00d7 " + [].concat(a.dice_multipliers).join(" / \u00d7 ") +
                " (by utilities owned)");
        }
        if (sq.type === "tax" && a.amount) parts.push("Pay " + formatMoney(a.amount));
        if (a.stakeholder) {
            parts.push(a.max_stakes + " stakes at " + formatMoney(a.buy_in) + " each");
            parts.push("Pot " + formatMoney(a.pot || 0));
            (a.stakes || []).forEach(function (st) { parts.push(st.player + ": " + st.percent + "%"); });
        }
        if (a.owner) parts.push("Owned by " + a.owner);
        if (a.houses || a.hotels) parts.push(buildingText(sq));
        if (a.mortgaged) parts.push("Mortgaged");
        return parts.join("\n");
    }

    function buildBoard(board) {
        var container = $("board");
        var n = board.size;
        var s = Math.ceil(n / 4);
        container.replaceChildren();
        container.style.setProperty("--cells", s + 1);
        squareNodes = [];
        squareEls = [];
        buildingNodes = [];
        potNodes = [];

        board.squares.forEach(function (sq) {
            var cell = squareCell(sq.index, n);
            var node = el("div", "square type-" + sq.type);
            if (sq.index % s === 0) node.classList.add("corner");
            node.style.gridRow = cell.row;
            node.style.gridColumn = cell.col;

            if (groupColour(sq) && sq.type === "property") {
                var band = el("div", "band");
                band.style.background = groupColour(sq);
                var buildings = el("div", "buildings");
                band.appendChild(buildings);
                buildingNodes[sq.index] = buildings;
                node.appendChild(band);
            }
            node.appendChild(el("div", "sq-name", sq.name));
            var a = sq.attributes || {};
            var icon = a.icon || SQUARE_ICONS[sq.type];
            if (icon && sq.type !== "property") node.appendChild(el("div", "sq-icon", icon));
            var cost = OWNABLE[sq.type] ? a.price : (sq.type === "tax" ? a.amount : null);
            if (cost) node.appendChild(el("div", "sq-price", formatMoney(cost)));
            if (a.stakeholder) {
                node.classList.add("pooled");
                var pot = el("div", "sq-pot");
                potNodes[sq.index] = pot;
                node.appendChild(pot);
            }
            var tokens = el("div", "tokens");
            node.appendChild(tokens);
            container.appendChild(node);
            squareNodes[sq.index] = tokens;
            squareEls[sq.index] = node;
        });

        // The centre shows the last card drawn; in Phase 4 the next board
        // inward is drawn here.
        var centre = el("div", "board-centre");
        centre.style.gridRow = "2 / " + (s + 1);
        centre.style.gridColumn = "2 / " + (s + 1);
        centre.appendChild(el("div", "centre-logo", "Recursopoly"));
        var card = el("div", "drawn-card");
        card.id = "drawn-card";
        card.hidden = true;
        centre.appendChild(card);
        container.appendChild(centre);
    }

    // Owner outlines, buildings and mortgages on each square.
    function renderOwnership(state, board) {
        var colours = {};
        state.players.forEach(function (p) { colours[p.name] = p.colour; });
        board.squares.forEach(function (sq) {
            var node = squareEls[sq.index];
            if (!node) return;
            var a = sq.attributes || {};
            node.classList.toggle("owned", !!a.owner);
            node.classList.toggle("mortgaged", !!a.mortgaged);
            if (a.owner) node.style.setProperty("--owner", colours[a.owner] || "#666");
            else node.style.removeProperty("--owner");
            node.title = squareTitle(sq, state);

            var holder = buildingNodes[sq.index];
            if (holder) {
                holder.replaceChildren();
                var i;
                for (i = 0; i < (a.hotels || 0); i++) holder.appendChild(el("span", "hotel"));
                for (i = 0; i < (a.houses || 0); i++) holder.appendChild(el("span", "house"));
            }

            // Pooled squares: the pot, and a dot per stakeholder in their colour.
            var pot = potNodes[sq.index];
            if (pot) {
                pot.replaceChildren(el("span", null, "Pot " + formatMoney(a.pot || 0)));
                (a.stakes || []).forEach(function (st) {
                    var dot = el("span", "stake-dot");
                    dot.style.background = colours[st.player] || "#666";
                    dot.title = st.player + " " + st.percent + "%";
                    pot.appendChild(dot);
                });
            }
        });
    }

    function renderTokens(state, boardId) {
        squareNodes.forEach(function (node) { node.replaceChildren(); });
        state.players.forEach(function (p) {
            if (p.position.board_id !== boardId || p.left || p.bankrupt) return;
            var holder = squareNodes[p.position.index];
            if (!holder) return;
            var token = el("span", "token", p.name.charAt(0).toUpperCase());
            token.style.background = p.colour;
            token.title = p.name + (p.in_jail ? " (in jail)" : "");
            if (state.current_player === p.name) token.classList.add("is-current");
            if (!p.connected) token.classList.add("is-offline");
            if (p.in_jail) token.classList.add("is-jailed");
            holder.appendChild(token);
        });
    }

    function renderCard(state) {
        var box = $("drawn-card");
        if (!box) return;
        var card = state.last_card;
        box.hidden = !card;
        if (!card) return;
        box.className = "drawn-card deck-" + card.deck;
        box.replaceChildren(
            el("div", "drawn-card-deck", card.label),
            el("div", "drawn-card-text", card.text),
            el("div", "drawn-card-player", "drawn by " + card.player)
        );
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

    function myPlayer(state) {
        var found = null;
        state.players.forEach(function (p) { if (isMe(p.name)) found = p; });
        return found;
    }

    function renderTurn(state) {
        var indicator = $("turn-indicator");
        var self = myPlayer(state);
        var myTurn = state.status === "in_progress" && isMe(state.current_player);
        var decision = state.status === "in_progress" && state.turn_state === "awaiting_decision"
            ? state.pending_decision : null;
        indicator.classList.toggle("is-you", myTurn);

        if (state.status === "ended") {
            indicator.textContent = state.winner ? state.winner + " wins!" : "Game over";
        } else if (self && self.bankrupt) {
            indicator.textContent = "You are bankrupt. You can keep watching.";
        } else if (myTurn && decision && decision.type === "debt") {
            indicator.textContent = "You owe money! Raise it or declare bankruptcy.";
        } else if (myTurn && decision) {
            indicator.textContent = "Your turn! Make your choice.";
        } else if (myTurn && self.in_jail) {
            indicator.textContent = "Your turn. You're in jail.";
        } else if (myTurn) {
            indicator.textContent = "Your turn! Roll the dice.";
        } else if (decision && decision.type === "buy") {
            indicator.textContent = state.current_player + " is deciding whether to buy " + decision.square;
        } else if (decision && decision.type === "buy_stake") {
            indicator.textContent = state.current_player + " is deciding whether to buy a stake in " +
                decision.square;
        } else if (decision && decision.type === "debt") {
            indicator.textContent = state.current_player + " owes " + formatMoney(decision.amount) +
                " and must raise money";
        } else if (state.current_player) {
            indicator.textContent = state.current_player + "'s turn";
        } else {
            indicator.textContent = "Waiting for players\u2026";
        }

        var canRoll = myTurn && state.turn_state === "waiting_to_roll";
        $("roll-btn").disabled = !canRoll;
        $("roll-btn").textContent = myTurn && self.in_jail ? "Roll for doubles" : "Roll dice";

        renderJail(state, self, canRoll && self.in_jail);
        renderBuy(state, self, myTurn && decision && (decision.type === "buy" || decision.type === "buy_stake"));
        renderDebt(state, self, myTurn && decision && decision.type === "debt");
    }

    // Pay the fine / use a card, shown before a jailed player rolls.
    function renderJail(state, self, show) {
        $("jail-actions").hidden = !show;
        if (!show) return;
        var fine = state.rules.jail_fine;
        $("jail-text").textContent = "In jail: roll doubles to get out (try " +
            (self.jail_turns + 1) + " of " + state.rules.max_jail_turns + "), or:";
        $("pay-fine-btn").textContent = "Pay " + formatMoney(fine) + " fine";
        $("pay-fine-btn").disabled = self.money < fine;
        $("use-card-btn").hidden = !self.jail_cards;
    }

    // Buy / Decline prompt for the active player.
    function renderBuy(state, self, show) {
        $("decision").hidden = !show;
        if (!show) return;
        var d = state.pending_decision;
        $("decision-text").textContent = d.type === "buy_stake"
            ? "Buy a " + d.percent + "% stake in " + d.square + " for " + formatMoney(d.price) + "? (" +
              plural(d.stakes_left, "stake") + " left)"
            : "Buy " + d.square + " for " + formatMoney(d.price) + "?";
        $("buy-btn").disabled = !self || self.money < d.price;
        $("decline-btn").disabled = false;
    }

    // Settle a debt or go bankrupt.
    function renderDebt(state, self, show) {
        $("debt").hidden = !show;
        if (!show) return;
        var d = state.pending_decision;
        $("debt-text").textContent = "You owe " + formatMoney(d.amount) + " to " +
            d.creditors.join(" and ") + " but have " + formatMoney(self.money) +
            ". Sell houses, mortgage or trade to raise money, then pay.";
        $("pay-debt-btn").textContent = "Pay " + formatMoney(d.amount);
        $("pay-debt-btn").disabled = self.money < d.amount;
    }

    function renderPlayers(state) {
        var list = $("game-players");
        list.replaceChildren();
        var board = state.boards["0"];
        state.players.forEach(function (p) {
            var li = playerRow(p, state, true);
            var props = (p.properties || [])
                .filter(function (pos) { return pos.board_id === 0; })
                .map(function (pos) { return board.squares[pos.index]; })
                .sort(function (a, b) { return a.index - b.index; });
            var info = el("div", "player-info");
            if (!p.bankrupt && !p.left) info.appendChild(el("span", null, "Worth " + formatMoney(p.net_worth)));
            if (p.jail_cards) info.appendChild(el("span", null, "\u{1F511} " + p.jail_cards + " jail card" +
                (p.jail_cards > 1 ? "s" : "")));
            if (p.debt) info.appendChild(el("span", "is-negative", "Owes " + formatMoney(p.debt)));
            if (state.rules.must_lap_before_buying && !p.laps && !p.bankrupt && !p.left) {
                info.appendChild(el("span", null, "No lap yet: can't buy"));
            }
            li.appendChild(info);
            var stakes = (p.stakes || []).filter(function (st) { return st.board_id === 0; });
            if (props.length || stakes.length) {
                var ul = el("ul", "props");
                stakes.forEach(function (st) {
                    var chip = el("li", "prop-chip prop-stake",
                        board.squares[st.index].name + " " + st.percent + "%");
                    chip.style.setProperty("--chip", "#7b1fa2");
                    chip.title = squareTitle(board.squares[st.index], state);
                    ul.appendChild(chip);
                });
                props.forEach(function (sq) {
                    var label = sq.name + (buildingText(sq) ? " (" + buildingText(sq) + ")" : "");
                    var chip = el("li", "prop-chip", label);
                    chip.style.setProperty("--chip", groupColour(sq) || "#777");
                    if (sq.attributes.mortgaged) chip.classList.add("is-mortgaged");
                    chip.title = squareTitle(sq, state);
                    ul.appendChild(chip);
                });
                li.appendChild(ul);
            }
            list.appendChild(li);
        });
    }

    // Build, sell, mortgage and unmortgage buttons for the viewer's squares.
    // The server works out what's allowed; we only reflect it.
    function renderMyProperties(state) {
        var self = myPlayer(state);
        var section = $("my-props-card");
        var actions = (self && self.property_actions) || [];
        section.hidden = !actions.length || state.status !== "in_progress";
        if (section.hidden) return;
        var list = $("my-props");
        list.replaceChildren();
        var myTurn = isMe(state.current_player);
        $("my-props-hint").textContent = myTurn ? "" : "You can build, sell and mortgage on your turn.";

        actions.slice().sort(function (a, b) { return a.index - b.index; }).forEach(function (act) {
            var sq = state.boards[String(act.board_id)].squares[act.index];
            var li = el("li", "my-prop");
            li.style.setProperty("--chip", groupColour(sq) || "#777");
            var label = el("div", "my-prop-name", sq.name);
            var status = buildingText(sq) || (sq.attributes.mortgaged ? "Mortgaged" : "");
            if (status) label.appendChild(el("span", "my-prop-status", status));
            li.appendChild(label);

            var buttons = el("div", "my-prop-actions");
            function add(text, event, enabled) {
                var b = el("button", "btn btn-small", text);
                b.type = "button";
                b.disabled = !enabled;
                b.addEventListener("click", function () {
                    b.disabled = true;
                    socket.emit(event, { board_id: act.board_id, index: act.index });
                });
                buttons.appendChild(b);
            }
            if (sq.type === "property" && sq.attributes.house_cost) {
                add("House " + formatMoney(act.build_cost), "build_house", act.can_build);
                if (state.rules.max_hotels_per_property) {
                    add("Hotel " + formatMoney(act.hotel_cost), "build_hotel", act.can_build_hotel);
                }
                add("Sell +" + formatMoney(act.sell_value), "sell_house", act.can_sell);
            }
            if (sq.attributes.mortgaged) {
                add("Unmortgage " + formatMoney(act.unmortgage_cost), "unmortgage", act.can_unmortgage);
            } else {
                add("Mortgage +" + formatMoney(act.mortgage_value), "mortgage", act.can_mortgage);
            }
            li.appendChild(buttons);
            list.appendChild(li);
        });
    }

    // Open trade offers, with Accept / Reject / Cancel for those involved.
    function renderTrades(state) {
        var self = myPlayer(state);
        var inGame = self && !self.bankrupt && !self.left && state.status === "in_progress";
        $("trades-card").hidden = state.status !== "in_progress";
        $("propose-trade-btn").hidden = !inGame;
        var list = $("trades");
        list.replaceChildren();
        if (!state.trades.length) {
            list.appendChild(el("li", "muted", "No open trade offers."));
            return;
        }
        state.trades.forEach(function (t) {
            var li = el("li", "trade");
            li.appendChild(el("div", null, t.summary));
            var buttons = el("div", "trade-actions");
            function add(text, cls, event, payload) {
                var b = el("button", "btn btn-small " + cls, text);
                b.type = "button";
                b.addEventListener("click", function () { b.disabled = true; socket.emit(event, payload); });
                buttons.appendChild(b);
            }
            if (isMe(t.to)) {
                add("Accept", "btn-primary", "respond_trade", { trade_id: t.id, accept: true });
                add("Reject", "", "respond_trade", { trade_id: t.id, accept: false });
            } else if (isMe(t.from)) {
                add("Withdraw", "", "cancel_trade", { trade_id: t.id });
            }
            if (buttons.children.length) li.appendChild(buttons);
            list.appendChild(li);
        });
    }

    // ---- Trade dialog ------------------------------------------------------

    function tradeCheckboxes(container, player, state) {
        container.replaceChildren();
        var tradeable = (player && player.property_actions || []).filter(function (a) { return a.tradeable; });
        if (!tradeable.length) {
            container.appendChild(el("p", "muted", "No tradeable properties."));
            return;
        }
        tradeable.forEach(function (act) {
            var sq = state.boards[String(act.board_id)].squares[act.index];
            var label = el("label", "trade-option");
            var box = el("input");
            box.type = "checkbox";
            box.value = act.board_id + ":" + act.index;
            label.appendChild(box);
            var chip = el("span", "prop-chip", sq.name + (sq.attributes.mortgaged ? " (mortgaged)" : ""));
            chip.style.setProperty("--chip", groupColour(sq) || "#777");
            label.appendChild(chip);
            container.appendChild(label);
        });
    }

    function checkedSquares(container) {
        return Array.prototype.map.call(container.querySelectorAll("input:checked"), function (box) {
            return box.value.split(":").map(Number);
        });
    }

    function fillTradePartner() {
        var state = latestState;
        var partner = null;
        state.players.forEach(function (p) { if (p.name === $("trade-to").value) partner = p; });
        $("trade-get-label").textContent = "You get from " + (partner ? partner.name : "them");
        tradeCheckboxes($("trade-get-squares"), partner, state);
    }

    function openTradeDialog() {
        var state = latestState;
        var select = $("trade-to");
        select.replaceChildren();
        state.players.forEach(function (p) {
            if (isMe(p.name) || p.bankrupt || p.left) return;
            var opt = el("option", null, p.name);
            opt.value = p.name;
            select.appendChild(opt);
        });
        if (!select.options.length) { showError("There's nobody to trade with."); return; }
        tradeCheckboxes($("trade-give-squares"), myPlayer(state), state);
        $("trade-give-money").value = 0;
        $("trade-get-money").value = 0;
        fillTradePartner();
        $("trade-dialog").showModal();
    }

    function initTrades() {
        $("propose-trade-btn").addEventListener("click", openTradeDialog);
        $("trade-to").addEventListener("change", fillTradePartner);
        $("trade-cancel-btn").addEventListener("click", function () { $("trade-dialog").close(); });
        $("trade-form").addEventListener("submit", function (ev) {
            ev.preventDefault();
            socket.emit("propose_trade", {
                to: $("trade-to").value,
                give_money: Number($("trade-give-money").value) || 0,
                give_squares: checkedSquares($("trade-give-squares")),
                get_money: Number($("trade-get-money").value) || 0,
                get_squares: checkedSquares($("trade-get-squares"))
            });
            $("trade-dialog").close();
        });
    }

    // ---- Log and game over -------------------------------------------------

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
        $("winner").textContent = state.winner ? state.winner + " wins!" : "Game over";
        var list = $("final-standings");
        list.replaceChildren();
        (state.standings || []).forEach(function (row) {
            var li = el("li", null, row.name + " \u2014 net worth " + formatMoney(row.net_worth));
            if (row.bankrupt) li.appendChild(el("span", "tag", "bankrupt"));
            else if (row.left) li.appendChild(el("span", "tag", "left"));
            list.appendChild(li);
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

        function decide(choice, buttons) {
            buttons.forEach(function (id) { $(id).disabled = true; });
            socket.emit("decide", { choice: choice });
        }
        $("buy-btn").addEventListener("click", function () { decide("buy", ["buy-btn", "decline-btn"]); });
        $("decline-btn").addEventListener("click", function () { decide("decline", ["buy-btn", "decline-btn"]); });
        $("pay-debt-btn").addEventListener("click", function () { decide("pay", ["pay-debt-btn"]); });
        $("bankrupt-btn").addEventListener("click", function () {
            if (confirm("Declare bankruptcy? You will be out of the game.")) decide("bankrupt", ["bankrupt-btn"]);
        });
        $("pay-fine-btn").addEventListener("click", function () { socket.emit("pay_jail_fine"); });
        $("use-card-btn").addEventListener("click", function () { socket.emit("use_jail_card"); });
        initTrades();

        initSeat(function (state) {
            if (state.status === "lobby") { goTo("lobby", state.join_code); return; }
            latestState = state;

            // Phase 3 shows only the outer board (id 0); Phase 4 renders all.
            var board = state.boards["0"];
            currentGroups = board.groups || {};
            var signature = board.size + ":" + board.name;
            if (boardBuiltFor !== signature) {
                buildBoard(board);
                boardBuiltFor = signature;
            }
            renderRuleset(state);
            renderOwnership(state, board);
            renderTokens(state, 0);
            renderCard(state);
            renderDice(state);
            renderTurn(state);
            renderMyProperties(state);
            renderTrades(state);
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
