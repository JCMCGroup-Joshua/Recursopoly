# Recursopoly

Recursopoly is a web-based, turn-based multiplayer board game in the spirit
of Monopoly. Friends join a shared game with a short code, take turns
rolling the dice, and race their tokens around the board. The name comes
from the game's big idea: **boards within boards**. Later versions add
smaller, pricier boards nested inside the outer one, linked by train
stations.

The game currently includes **Phase 1** (movement, turns and real-time
multiplayer) and **Phase 2** (properties, buying, rent and tax). Everything
runs on Flask and Flask-SocketIO. There is **no database**: settings and
board layouts are plain `.txt` files, and scores are appended to a `.csv`
file.

## Features (Phase 1)

- Create a game and get a short join code (for example `K7QX2M`). Codes
  avoid the look-alike characters O/0 and I/1.
- Friends join with the code and a display name. The host starts the game
  once enough players are in.
- A 40-square board laid out around the edge like a Monopoly board
  (the size can be changed in the config).
- Server-side dice: two six-sided dice, with a roll again on doubles and
  jail for three doubles in a row.
- Passing or landing on Go pays the Go salary.
- Live updates for everyone in the game: tokens, money, whose turn it is,
  the last roll and an event log.
- Disconnects are handled gracefully. A missing player's turn is skipped,
  and they can rejoin with the same code and name.
- The host can end the game at any time. Final scores are written to
  `scores.csv`.

## Features (Phase 2)

- Properties, stations and utilities have prices. Landing on an unowned
  one offers the active player **Buy** or **Decline**, and the turn waits
  until they choose. A player who can't afford the price isn't offered it.
- Landing on a square someone else owns charges rent automatically:
  - **Properties:** the base rent from the board file. It is doubled
    (`full_group_rent_multiplier`) when one player owns the whole colour
    group.
  - **Stations:** £25 with one station, doubling for each extra station the
    owner has (£50, £100, £200).
  - **Utilities:** the dice total × 4, or × 10 when the owner has both.
- Tax squares deduct a fixed amount.
- Owned squares are outlined in the owner's colour, each player's
  properties are listed under their name, and hovering a square shows its
  price, rent and owner.
- Money can go negative (shown in red). Bankruptcy arrives in Phase 3.
- A player who leaves keeps their squares, but no rent is charged on them.

## Requirements

- Python 3.9 or newer
- The packages in `requirements.txt` (Flask, Flask-SocketIO, simple-websocket)
- A browser that can reach `cdn.socket.io` (the Socket.IO client is loaded
  from there)

## Install

```bash
cd recursopoly
python -m venv .venv
source .venv/bin/activate        # Windows: .venv\Scripts\activate
pip install -r requirements.txt
```

## Configure

Edit `config.txt`. It uses `key=value` lines, `#` comments and blank lines.
Missing or invalid keys fall back to defaults.

| Key | Default | Meaning |
| --- | --- | --- |
| `starting_money` | 1500 | Money each player starts with |
| `go_salary` | 200 | Paid for passing or landing on Go |
| `full_group_rent_multiplier` | 2 | Base rent multiplier when one player owns a whole colour group |
| `board_size` | 40 | Squares on the outer board (`board_0.txt` is padded or trimmed to fit) |
| `min_players` / `max_players` | 2 / 6 | Players needed to start / allowed to join |
| `join_code_length` | 6 | Length of generated join codes |
| `max_doubles` | 3 | Doubles in a row that send a player to jail |
| `disconnect_grace_seconds` | 5 | How long a player can be disconnected (for example, while a page reloads) before their turn is skipped |
| `host` / `port` | 0.0.0.0 / 5000 | Where the server listens |
| `debug` | false | Flask debug mode |
| `scores_file` | scores.csv | Where scores are logged |
| `boards_dir` | boards | Folder of board layout files |

### Board files

`boards/board_0.txt` lists one square per line, in order from Go:

```
name | type | key=value; key=value
```

`type` is one of `go`, `property`, `station`, `utility`, `tax`, `chance`,
`community_chest`, `jail`, `free_parking` or `go_to_jail`. The optional
third column holds attributes:

| Type | Attributes |
| --- | --- |
| `property` | `group` (colour set), `price`, `rent` (base rent) |
| `station` | `price`, `rent` (rent with one station) |
| `utility` | `price`, `dice_multiplier`, `full_set_dice_multiplier` |
| `tax` | `amount` |

For example: `Mayfair | property | group=dark_blue; price=400; rent=50`.
Squares without a `price` can't be bought. Lines starting with `#` are
comments, and lines starting with `@` set board metadata such as
`@name=Outer Ring`.

## Run

```bash
python app.py
```

Then open <http://localhost:5000> (or `http://<your-ip>:5000` from other
devices on your network).

## How to play

1. **Create a game.** On the Recursopoly home page, enter your name and
   click **Create game**. You become the host and land in the lobby, where
   the join code is shown in large letters.
2. **Invite friends.** Share the join code. Each friend opens the home
   page, types the code and a name under **Join with code**, and joins.
3. **Start.** When enough players have joined, the host clicks **Start
   game**.
4. **Take turns.** The **Roll dice** button is enabled only for the active
   player. Rolling doubles gives you another roll. Three doubles in a row
   sends you to jail.
5. **Buy and collect rent.** Land on an unowned property, station or
   utility to get a **Buy** / **Decline** prompt. Other players pay you
   rent when they land on your squares.
6. **Rejoin.** If you drop out, rejoin from the home page with the same code
   and name.
7. **Finish.** The host clicks **End game**. Everyone sees the final
   standings, and each player's money is logged as their score.

## Score log

`scores.csv` is created automatically with a header row and is only ever
appended to. Columns:

```
timestamp, join_code, event_type, player_name, board_id, position, money, details
```

Events are `game_started`, `roll`, `passed_go`, `jailed`, `purchase`,
`rent_paid`, `tax_paid` and `game_ended`. The game writes one `game_ended` row per player, with their
final score and rank. Writes are guarded by a lock, so several games can log
at once.

## Project layout

```
recursopoly/
    app.py              Flask + Flask-SocketIO server (routes, sockets, rooms)
    game_engine.py      Pure game logic: Board, Square, Player, Game (no Flask)
    config.py           Loads config.txt
    logger.py           Appends rows to scores.csv
    config.txt          Settings
    boards/board_0.txt  Outer board layout
    cards/              Chance / Community Chest decks (Phase 3)
    templates/          index.html, lobby.html, game.html
    static/             recursopoly.css, recursopoly.js
    tests/              Unit tests for the engine
```

The engine has no web dependencies, so it can be tested on its own:

```bash
python -m unittest discover tests
```

## Where later phases plug in

The code is shaped so later phases add to it instead of rewriting it.

Phase 2 is built on these hooks:

- Buy, rent and tax live in `Game._resolve_landing()`.
- A buy offer sets `Game.pending_decision` and pauses the turn in
  `TurnState.AWAITING_DECISION`. `Game.decide()` (the `decide` socket
  event) resumes it.
- Owners are stored in `Square.attributes["owner"]`, and each player's
  properties are derived from them in the broadcast state.

Still to come:

- **Phase 3: full classic rules**
  - Card decks go in `cards/` as `.txt` files, loaded next to the boards.
  - Jail state is already flagged in `Player.attributes["in_jail"]`.
  - `Game.standings()` switches from money to net worth.
  - Trading, building and mortgaging become new `Game` methods, each with a
    socket event.
- **Phase 4: nested boards and train travel**
  - Add `boards/board_1.txt`, `board_2.txt` and so on. `load_boards()`
    already loads every `board_<n>.txt` into `Game.boards`, keyed by
    `board_id`.
  - Positions are already `Position(board_id, index)`, and movement uses
    the player's current board.
  - Per-board Go salaries come from an `@go_salary=` line and are read by
    `Game.go_salary_for()`.
  - Station tickets are another `AWAITING_DECISION` choice.
  - The board centre in `recursopoly.js` is where inner boards get drawn.
- **Phase 5: stats and polish**
  - `ScoreLogger.read_rows()` reads `scores.csv` back for the leaderboard
    and history pages.
  - Spectators join the Socket.IO room without taking a seat.
  - A turn timer can reuse the background check that `app.py` already runs
    for disconnects.
  - Chat is one more room broadcast.
