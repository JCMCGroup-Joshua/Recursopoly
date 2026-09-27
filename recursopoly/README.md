# Recursopoly

Recursopoly is a web-based, turn-based multiplayer board game in the spirit
of Monopoly. Friends join a shared game with a short code, take turns
rolling the dice, and race their tokens around the board. The name comes
from the game's big idea: **boards within boards**. Later versions add
smaller, pricier boards nested inside the outer one, linked by train
stations.

The game currently includes:

- **Phase 1:** movement, turns and real-time multiplayer
- **Phase 2:** properties, buying, rent and tax
- **Phase 3:** cards, jail, houses, mortgages, trading and bankruptcy
- **Phase 4:** rule sets, custom property sets, stakeholder ownership and
  the adult-themed **AMST** rule set

Everything runs on Flask and Flask-SocketIO. There is **no database**:
rule sets and boards are JSON files, server settings and card decks are
plain `.txt` files, and scores are appended to a `.csv` file.

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

## Features (Phase 3)

- **Chance and Community Chest.** Decks are defined in `cards/*.txt` and
  shuffled for every game. The last card drawn is shown in the middle of
  the board.
- **Jail.** You go to jail for landing on Go To Jail, drawing a Go to Jail
  card, or rolling three doubles in a row. On your turn you can:
  - pay the fine (`jail_fine`, £50 in Classic) and roll normally,
  - use a Get Out of Jail Free card, or
  - roll for doubles. Doubles free you (with no extra roll). After
    `max_jail_turns` (3) failed tries you must pay the fine and move.
- **Houses and hotels.** Once you own a whole colour group you can build on
  your turn:
  - Houses must be built evenly across the group. A hotel needs
    `houses_before_hotel` houses (4 in Classic) and replaces them.
  - Rent comes from each property's `house_rents` and `hotel_rents`.
  - Selling returns half the cost (`house_sell_percent`).
- **Mortgages.** Mortgaging pays out half the price (`mortgage_percent`).
  Unmortgaging costs that plus 10% (`unmortgage_interest_percent`).
  Mortgaged squares charge no rent, and a group must have no buildings
  before any of it is mortgaged.
- **Trading.** Offer any mix of properties and money to another player at
  any time. They accept or reject, or you withdraw the offer. A trade is
  checked again when it's accepted. Properties in a group with buildings
  can't be traded.
- **Debt and bankruptcy.** If you can't afford a payment, nothing is paid
  and the turn waits. Sell houses, mortgage or trade to raise the money,
  then pay, or declare bankruptcy.
  - When you go bankrupt, your cash, properties and jail cards go to the
    player you owe. If you owe the bank (or several players), they go back
    to the bank instead.
  - Debts run up on someone else's turn (for example a birthday card) are
    settled at the start of your next turn.
- **Winning.** The last player standing wins. If the host ends the game
  early, the highest **net worth** (money + property value + buildings at
  cost + stakes at their buy-in, minus debts) wins. A player who leaves mid-game is out, and their
  properties go back to the bank.

## Features (Phase 4)

- **Rule sets.** Every value that changes how the game plays lives in a
  rule set: a JSON file in `rulesets/`. The host picks one when creating a
  game, and the lobby and game page show its key values. `classic.json`
  reproduces Phases 1-3 exactly.
- **Custom property sets.** A rule set points at its own board file in
  `boards/`, with its own names, prices, rents, colour groups, icons and
  custom square types. It can also bring its own card decks.
- **Stakeholder ownership.** Ownership is a list of stakes (player +
  percentage). A normal property is one owner with 100%; a pooled square
  has numbered stakes, each a separate asset.
- **Pooled squares.** A square marked `"stakeholder": true` sells stakes
  (for example 4 stakes of 25%) to players who land on it.
  - Money paid to the bank can go into its pot, as the rule set says:
    taxes, jail fines, and/or card fees.
  - The pot is paid out to the stakeholders, by stake or equally, when
    anyone lands on the square (or only when a stakeholder does).
- **More building options.** Rule sets can allow several hotels per
  property (`max_hotels_per_property`) and change how many houses a hotel
  needs.
- **House rules.** `must_lap_before_buying` stops players buying anything
  until they have been round the board once. `doubles_before_jail` sets how
  many doubles in a row send you to jail.
- **AMST.** An adult-themed rule set with its own board ("AMST Board"):
  - The Velvet Lounge and Neon Alley (the red group, with house and hotel
    rents), Central Station, and bars, cabaret, tattoo studios, casinos
    and cellars
  - its own "Last Call" and "Lucky Dip" card decks
  - £2000 starting money, £250 Go salary and a £75 jail fine
  - up to 3 hotels per property, and a full lap before buying
  - **The Strip Club** (square 15) is a pooled square with 4 stakes at £150.
    Its pot collects taxes and fines and pays out to its stakeholders by
    stake whenever anyone lands on it.

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

## Configure the server

Edit `config.txt`. It uses `key=value` lines, `#` comments and blank lines.
Missing or invalid keys fall back to defaults. It only holds server
settings: everything about how the game plays comes from rule sets.

| Key | Default | Meaning |
| --- | --- | --- |
| `rulesets_dir` | rulesets | Folder of rule set JSON files |
| `default_ruleset` | classic | Rule set preselected on the create-game form |
| `join_code_length` | 6 | Length of generated join codes |
| `disconnect_grace_seconds` | 5 | How long a player can be disconnected (for example, while a page reloads) before their turn is skipped |
| `host` / `port` | 0.0.0.0 / 5000 | Where the server listens |
| `debug` | false | Flask debug mode |
| `scores_file` | scores.csv | Where scores are logged |

## Rule sets

A rule set is one JSON file in `rulesets/`. Its file name (without
`.json`) is its id. The server loads and checks every rule set at startup,
so a mistake stops the server with a message saying which file and value
is wrong. The host chooses one on the create-game form, and that game
follows it for every rule check.

`rulesets/classic.json` is the base. **Any value another rule set leaves
out is taken from classic**, so a variant only lists what it changes.
Here is AMST:

```json
{
  "name": "AMST",
  "description": "Adult-themed rule set with custom properties and pooled stakeholder squares",
  "board": "boards/amst_board.json",
  "cards": {"chance": "cards/amst_last_call.txt", "community_chest": "cards/amst_lucky_dip.txt"},
  "economy":     {"starting_money": 2000, "go_salary": 250, "jail_fine": 75},
  "building":    {"max_houses_per_property": 4, "houses_before_hotel": 4, "max_hotels_per_property": 3},
  "house_rules": {"doubles_before_jail": 3, "must_lap_before_buying": true},
  "pooled_squares": {"receives": ["taxes", "fines"],
                     "payout_trigger": "on_landing", "payout_split": "by_stake"}
}
```

| Section | Value | Classic | Meaning |
| --- | --- | --- | --- |
| (top) | `name`, `description` | | Shown on the create form and in the lobby |
| (top) | `board` | `boards/classic_board.json` | The property set / board file |
| (top) | `cards` | Chance and Community Chest | `{square type: deck file}`; a square of that type draws from that deck |
| `players` | `min_players` / `max_players` | 2 / 6 | Players needed to start / allowed to join |
| `economy` | `starting_money` | 1500 | Money each player starts with |
| `economy` | `go_salary` | 200 | Paid for passing or landing on Go |
| `economy` | `jail_fine` | 50 | Fine to leave jail |
| `economy` | `full_group_rent_multiplier` | 2 | Base rent multiplier for a whole colour group |
| `economy` | `house_sell_percent` | 50 | Refund when selling a house or hotel |
| `economy` | `mortgage_percent` | 50 | Percentage of the price paid out for a mortgage |
| `economy` | `unmortgage_interest_percent` | 10 | Interest added when unmortgaging |
| `building` | `max_houses_per_property` | 4 | Most houses on one property |
| `building` | `houses_before_hotel` | 4 | Houses needed before the first hotel (the hotel replaces them) |
| `building` | `max_hotels_per_property` | 1 | Most hotels on one property (0 = no hotels) |
| `house_rules` | `doubles_before_jail` | 3 | Doubles in a row that send a player to jail |
| `house_rules` | `max_jail_turns` | 3 | Tries at rolling doubles before the fine must be paid |
| `house_rules` | `must_lap_before_buying` | false | Players must pass Go once before buying anything |
| `pooled_squares` | `receives` | `[]` | Bank payments that go into a pooled square's pot: any of `taxes` (tax squares), `fines` (jail fines), `fees` (card payments and repairs) |
| `pooled_squares` | `payout_trigger` | `on_landing` | `on_landing`: anyone landing pays the pot out. `on_stakeholder_landing`: only a stakeholder landing does |
| `pooled_squares` | `sell_back_percent` | 50 | Percentage of a stake's buy-in the bank pays when a stake is sold back |
| `pooled_squares` | `payout_split` | `by_stake` | `by_stake`: in proportion to stakes (unsold stakes' share stays in the pot). `equal`: split evenly between stakeholders |

Board size is not a rule: it comes from the board file. A rule set may
only use the keys above; an unknown section or key (a typo, say) stops the
server with an error naming it.

### Writing a new rule set

1. Copy `rulesets/amst.json` to, say, `rulesets/speedy.json`.
2. Change `name` and `description`, and keep only the values you want to
   differ from classic.
3. Optionally point `board` at a new board file and `cards` at new decks
   (see below).
4. Restart the server. The new rule set appears on the create-game form.

No code changes are needed.

### Boards (property sets)

A board is a JSON file in `boards/`: an object with a `name`, optional
colour `groups`, and a `squares` array. Each square has `index` (its
position, with Go at 0), `name` and `type`, plus any keys for its type.

Squares can be listed in any order, and indexes can have gaps: missing
positions become blank squares. The board runs up to the highest index
given, or set `"size": 40` to fix the length.

```json
{
  "name": "Classic London",
  "groups": {"dark_blue": {"name": "Dark blue", "colour": "#0072bb"}},
  "squares": [
    {"index": 0, "name": "GO", "type": "go"},
    {"index": 39, "name": "Mayfair", "type": "property", "group": "dark_blue",
     "price": 400, "rent": 50, "house_rents": [200, 600, 1400, 1700],
     "hotel_rents": [2000], "house_cost": 200, "hotel_cost": 200}
  ]
}
```

| Type | Keys |
| --- | --- |
| `go`, `jail`, `go_to_jail`, `free_parking` (or `free`) | none |
| `property` | `group`, `price`, `rent` (base rent), `house_rents` (rent with 1, 2, ... houses), `hotel_rents` (rent with 1, 2, ... hotels), `house_cost`, `hotel_cost` |
| `station` | `price`, and `rents` (rent when the owner has 1, 2, 3, 4 stations) or a single `rent` |
| `utility` | `price`, `dice_multipliers` (dice total × this, by utilities owned) |
| `tax` | `amount` |
| `chance`, `community_chest` or any deck name | draws a card from the deck the rule set gives for that type |
| anything else | a custom type. It's a label unless it's a pooled square. Add `"icon"` to show an emoji on the board |

Squares without a `price` can't be bought. Group colours come from
`groups`, so a custom board can invent its own colour groups.

### Pooled (stakeholder) squares

Add `"stakeholder": true` to any square, whatever its type, to make it
pooled:

```json
{"index": 15, "name": "The Strip Club", "type": "pooled", "stakeholder": true, "max_stakes": 4, "buy_in": 150}
```

- `max_stakes` is how many equal stakes exist (4 means 25% each).
- A pot paid out "by stake" gives each stake an equal cut, so a player with
  2 of 4 stakes gets half; cuts for unsold stakes stay in the pot.
- `buy_in` is the price of one stake.
- Each stake is numbered (stake 1 to `max_stakes`) and is a separate
  asset: holding all 4 means 4 stakes to sell or trade individually.
- A player who lands on the square may buy one stake per visit (the
  lowest-numbered one left), while stakes remain. The same lap and debt
  rules as buying property apply.
- Each stake shows under **Your properties**. On your turn you can sell
  any one of them back to the bank for `sell_back_percent` of the buy-in
  (50% in Classic and AMST). That stake is then back on sale.
- Stakes can be traded like properties, one or several at a time; each
  appears as its own item in the trade dialog. Stakes can't be mortgaged.
- On bankruptcy stakes pass to the creditor; when a player leaves, their
  stakes return to the bank.
- The pot is filled and paid out as the rule set's `pooled_squares`
  section says.
  If a board has several pooled squares, the first one collects the pot.

### Card files

Each deck is a `.txt` file in `cards/`, and the rule set's `cards` key
maps a square type to it (Classic: `chance.txt` and `community_chest.txt`;
AMST: `amst_last_call.txt` and `amst_lucky_dip.txt`). One card per line:

```
text | effect | key=value; key=value
```

| Effect | Parameters |
| --- | --- |
| `move_to` | `square=<name>` (collects Go salary if Go is passed) |
| `move_by` | `steps=<n>` (negative moves back and never pays Go) |
| `move_to_nearest` | `type=station` or `utility`, optional `rent_multiplier` |
| `collect` / `pay` | `amount` (from / to the bank; `pay` counts as a fee) |
| `collect_from_each` / `pay_each` | `amount` (from / to every other player) |
| `repairs` | `house`, `hotel` (charge per building; counts as a fee) |
| `go_to_jail` | none |
| `get_out_of_jail_free` | none (kept until used) |

## Run

```bash
python app.py
```

Then open <http://localhost:5000> (or `http://<your-ip>:5000` from other
devices on your network).

## How to play

1. **Create a game.** On the Recursopoly home page, enter your name, pick
   a **rule set** (for example Classic or AMST) and click **Create game**.
   You become the host and land in the lobby. The lobby shows the join
   code in large letters and the rule set's key values.
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
6. **Build and mortgage.** On your turn, use **Your properties** to build
   houses and hotels, sell them, or mortgage and unmortgage squares.
7. **Buy stakes.** Land on a pooled square (AMST's Strip Club) to buy a
   stake. Its pot pays out to stakeholders as the rule set says.
8. **Trade.** Click **Propose a trade** to offer properties and money to
   another player. Offers to you appear under **Trades** with **Accept**
   and **Reject**.
9. **In jail?** Pay the fine, use a card, or roll for doubles.
10. **Can't pay?** Raise the money, then click **Pay**, or **Declare
   bankruptcy**.
11. **Rejoin.** If you drop out, rejoin from the home page with the same
    code and name.
12. **Finish.** The game ends when one player is left, or when the host
    clicks **End game** (highest net worth wins). Everyone sees the final
    standings.

## Score log

`scores.csv` is created automatically with a header row and is only ever
appended to. Columns:

```
timestamp, join_code, event_type, player_name, board_id, position, money, details
```

Events:

- **Turns:** `game_started`, `roll`, `passed_go`
- **Buying and paying:** `purchase`, `rent_paid`, `tax_paid`, `debt_paid`
- **Pooled squares:** `stake_purchased`, `stake_sold`, `pool_payout`
- **Cards and jail:** `card_drawn`, `jailed`, `released_from_jail`,
  `jail_fine_paid`, `jail_card_used`
- **Buildings and mortgages:** `house_built`, `hotel_built`,
  `building_sold`, `mortgaged`, `unmortgaged`
- **Trading:** `trade_proposed`, `trade_accepted`, `trade_rejected`
- **Endings:** `bankrupt`, `game_ended`

When a game ends, one `game_ended` row is written per player. Its details
hold their net worth, finishing position, result (`winner`, `finished`,
`bankrupt` or `left`) and the rule set played. `game_started` rows also
name the rule set. Writes are guarded by a lock, so several games can
log at once.

## Project layout

```
recursopoly/
    app.py              Flask + Flask-SocketIO server (routes, sockets, rooms)
    game_engine.py      Pure game logic: Board, Square, Player, Game (no Flask, no files)
    rulesets.py         Loads and checks rule sets, their boards and card decks
    config.py           Loads config.txt
    logger.py           Appends rows to scores.csv
    config.txt          Server settings
    rulesets/           classic.json, amst.json
    boards/             classic_board.json, amst_board.json
    cards/              Card decks (classic and AMST)
    templates/          index.html, lobby.html, game.html
    static/             recursopoly.css, recursopoly.js
    tests/              Unit tests for the engine and rule sets
```

The engine has no web dependencies, so it can be tested on its own:

```bash
python -m unittest discover tests
```

## Where later phases plug in

The code is shaped so later phases add to it instead of rewriting it.

Phases 2 to 4 are built on these hooks:

- Landing effects (buy, rent, tax, cards, Go To Jail) live in
  `Game._resolve_landing()`.
- Choices pause the turn in `TurnState.AWAITING_DECISION` with a
  `Game.pending_decision` (`buy`, `buy_stake` or `debt`). `Game.decide()` (the `decide`
  socket event) resumes it.
- Every payment goes through `Game._pay()`. A payment that can't be covered
  becomes a debt instead of a negative balance.
- Ownership (a list of stakes), houses, hotels, mortgages and pots are
  stored in `Square.attributes`. `Game.property_actions()` tells the page
  what each player may do.
- Every tunable value is read with `Game._rule()` from the game's rule set.
  A new rule is a new key in `rulesets/classic.json` (and in
  `REQUIRED_VALUES` in `rulesets.py`) that the engine reads.
- Each rule action (`build_house`, `mortgage`, `propose_trade`, ...) is a
  `Game` method with a matching socket event in `app.py`.

Still to come:

- **Phase 5: nested boards and train travel**
  - A rule set lists several board files instead of one `board`. The
    engine already keeps boards in `Game.boards`, keyed by `board_id`.
  - Positions are already `Position(board_id, index)`, and movement uses
    the player's current board.
  - Per-board Go salaries come from a `go_salary` key in a board file and
    are read by `Game.go_salary_for()`.
  - Station tickets are another `AWAITING_DECISION` choice.
  - The board centre in `recursopoly.js` is where inner boards get drawn.
- **Phase 6: stats and polish**
  - `ScoreLogger.read_rows()` reads `scores.csv` back for the leaderboard
    and history pages.
  - Spectators join the Socket.IO room without taking a seat.
  - A turn timer can reuse the background check that `app.py` already runs
    for disconnects.
  - Chat is one more room broadcast.
