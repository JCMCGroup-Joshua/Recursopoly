"""Unit tests for the Recursopoly game engine (no Flask needed).

Run from the recursopoly/ folder:  python -m unittest discover tests
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from config import Config, parse_config_text  # noqa: E402
from game_engine import (  # noqa: E402
    JOIN_CODE_ALPHABET,
    Game,
    parse_cards_text,
    GameError,
    GameStatus,
    Position,
    TurnState,
    generate_join_code,
    load_boards,
    parse_board_text,
)

BOARDS_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "boards")


def make_game(players=("Alice", "Bob"), decks=None, **overrides):
    settings = Config({k: str(v) for k, v in overrides.items()})
    game = Game("TEST42", load_boards(BOARDS_DIR, {0: settings.board_size}), settings, decks=decks)
    for name in players:
        game.add_player(name)
    return game


class ConfigTests(unittest.TestCase):
    def test_parse_ignores_comments_and_blanks(self):
        values = parse_config_text("# comment\n\nstarting_money=900\nnot a setting\nport = 8000 # inline\n")
        self.assertEqual(values, {"starting_money": "900", "port": "8000"})

    def test_defaults_and_conversion(self):
        cfg = Config({"starting_money": "900", "go_salary": "oops", "debug": "yes"})
        self.assertEqual(cfg.starting_money, 900)
        self.assertEqual(cfg.go_salary, 200)  # bad value falls back
        self.assertTrue(cfg.debug)
        self.assertEqual(cfg.max_players, 6)  # missing key falls back


class BoardTests(unittest.TestCase):
    def test_board_0_loads_with_40_squares(self):
        board = load_boards(BOARDS_DIR)[0]
        self.assertEqual(board.size, 40)
        self.assertEqual(board.square(0).type, "go")
        self.assertEqual(board.square(5).name, "King's Cross Station")
        self.assertEqual(board.jail_index, 10)
        self.assertEqual(board.square(1).attributes["group"], "brown")

    def test_board_is_padded_or_trimmed_to_size(self):
        text = "@name=Tiny\nGO | go\nA | property | group=red; price=60\n"
        board = parse_board_text(text, board_id=3, size=4)
        self.assertEqual(board.size, 4)
        self.assertEqual(board.name, "Tiny")
        self.assertEqual(board.square(1).attributes, {"group": "red", "price": 60})
        self.assertEqual(parse_board_text(text, 3, size=1).size, 1)

    def test_unknown_square_type_rejected(self):
        with self.assertRaises(ValueError):
            parse_board_text("GO | go\nX | castle\n", 0)


class JoinCodeTests(unittest.TestCase):
    def test_code_is_unique_and_unambiguous(self):
        code = generate_join_code(6, existing={"AAAAAA"})
        self.assertEqual(len(code), 6)
        self.assertTrue(all(c in JOIN_CODE_ALPHABET for c in code))
        for bad in "O0I1":
            self.assertNotIn(bad, JOIN_CODE_ALPHABET)


class LobbyTests(unittest.TestCase):
    def test_first_player_is_host(self):
        game = make_game()
        self.assertEqual(game.host_name, "Alice")
        self.assertEqual(game.players[0].position, Position(0, 0))

    def test_duplicate_name_rejected_while_connected(self):
        game = make_game()
        with self.assertRaises(GameError) as ctx:
            game.add_player("alice")
        self.assertEqual(ctx.exception.code, "duplicate_name")

    def test_full_game_rejected(self):
        game = make_game(players=("A", "B"), max_players=2)
        with self.assertRaises(GameError) as ctx:
            game.add_player("C")
        self.assertEqual(ctx.exception.code, "full")

    def test_only_host_starts_and_needs_min_players(self):
        game = make_game(players=("Alice",))
        with self.assertRaises(GameError):
            game.start("Alice")
        game.add_player("Bob")
        with self.assertRaises(GameError):
            game.start("Bob")
        game.start("Alice")
        self.assertEqual(game.status, GameStatus.IN_PROGRESS)
        self.assertEqual(game.current_player.name, "Alice")

    def test_cannot_join_started_game_but_can_rejoin(self):
        game = make_game()
        game.start("Alice")
        with self.assertRaises(GameError) as ctx:
            game.add_player("Carol")
        self.assertEqual(ctx.exception.code, "started")
        game.mark_disconnected("Bob")
        player, rejoined = game.add_player("Bob")
        self.assertTrue(rejoined)
        self.assertTrue(player.connected)

    def test_token_reclaims_connected_seat(self):
        game = make_game()
        bob = game.get_player("Bob")
        player, rejoined = game.add_player("Bob", token=bob.token)
        self.assertIs(player, bob)
        self.assertTrue(rejoined)

    def test_host_leaving_lobby_passes_host_on(self):
        game = make_game(players=("Alice", "Bob", "Carol"))
        game.remove_player("Alice")
        self.assertEqual(game.host_name, "Bob")
        self.assertEqual([p.join_order for p in game.players], [0, 1])


class TurnTests(unittest.TestCase):
    def setUp(self):
        self.game = make_game(players=("Alice", "Bob", "Carol"))
        self.game.start("Alice")
        self.game.drain_events()

    def test_only_active_player_can_roll(self):
        with self.assertRaises(GameError) as ctx:
            self.game.roll("Bob", dice=(1, 2))
        self.assertEqual(ctx.exception.code, "not_your_turn")

    def test_move_and_pass_turn(self):
        result = self.game.roll("Alice", dice=(2, 3))
        alice = self.game.get_player("Alice")
        self.assertEqual(result["total"], 5)
        self.assertEqual(alice.position, Position(0, 5))
        # King's Cross is for sale, so the turn waits for Alice's decision.
        self.assertEqual(self.game.turn_state, TurnState.AWAITING_DECISION)
        self.game.decide("Alice", "decline")
        self.assertEqual(self.game.current_player.name, "Bob")
        self.assertIn("Alice rolled 2 + 3 and moved to King's Cross Station.",
                      [line["message"] for line in self.game.log])

    def test_doubles_roll_again(self):
        result = self.game.roll("Alice", dice=(1, 1))  # Community Chest
        self.assertTrue(result["roll_again"])
        self.assertEqual(self.game.current_player.name, "Alice")
        self.assertEqual(self.game.turn_state, TurnState.WAITING_TO_ROLL)
        self.game.roll("Alice", dice=(2, 3))  # Chance
        self.assertEqual(self.game.current_player.name, "Bob")

    def test_three_doubles_goes_to_jail(self):
        self.game.roll("Alice", dice=(1, 1))  # Community Chest
        self.game.roll("Alice", dice=(1, 1))  # Income Tax
        result = self.game.roll("Alice", dice=(5, 5))
        alice = self.game.get_player("Alice")
        self.assertTrue(result["jailed"])
        self.assertEqual(alice.position, Position(0, 10))
        self.assertEqual(self.game.current_player.name, "Bob")
        self.assertIn("jailed", [e.event_type for e in self.game.drain_events()])

    def test_passing_go_pays_salary(self):
        alice = self.game.get_player("Alice")
        alice.position = Position(0, 38)
        self.game.roll("Alice", dice=(1, 3))
        self.assertEqual(alice.position, Position(0, 2))
        self.assertEqual(alice.money, 1500 + 200)
        self.assertIn("passed_go", [e.event_type for e in self.game.drain_events()])

    def test_landing_on_go_pays_salary(self):
        alice = self.game.get_player("Alice")
        alice.position = Position(0, 35)
        self.game.roll("Alice", dice=(2, 3))
        self.assertEqual(alice.position, Position(0, 0))
        self.assertEqual(alice.money, 1700)

    def test_disconnected_players_are_skipped(self):
        self.game.mark_disconnected("Bob", now=100)
        self.game.roll("Alice", dice=(3, 4))  # Chance
        self.assertEqual(self.game.current_player.name, "Carol")

    def test_current_player_disconnect_skips_after_grace(self):
        self.game.mark_disconnected("Alice", now=100)
        self.assertFalse(self.game.skip_turn_if_disconnected(now=102, grace=5))
        self.assertTrue(self.game.skip_turn_if_disconnected(now=106, grace=5))
        self.assertEqual(self.game.current_player.name, "Bob")

    def test_disconnect_announced_only_after_grace(self):
        self.game.mark_disconnected("Bob", now=100)
        messages = lambda: [line["message"] for line in self.game.log]
        self.assertNotIn("Bob disconnected.", messages())
        self.game.add_player("Bob")  # quick page change: no log noise
        self.assertNotIn("Bob reconnected.", messages())
        self.game.mark_disconnected("Bob", now=200)
        self.assertTrue(self.game.check_disconnect("Bob", now=206, grace=5))
        self.game.add_player("Bob")
        self.assertIn("Bob disconnected.", messages())
        self.assertIn("Bob reconnected.", messages())

    def test_end_game_logs_final_scores(self):
        with self.assertRaises(GameError):
            self.game.end("Bob")
        self.game.end("Alice")
        events = self.game.drain_events()
        self.assertEqual(self.game.status, GameStatus.ENDED)
        self.assertEqual([e.event_type for e in events], ["game_ended"] * 3)
        self.assertTrue(all("net_worth=1500" in e.details for e in events))
        self.assertIn("position=1; result=winner", events[0].details)
        with self.assertRaises(GameError):
            self.game.roll("Alice", dice=(1, 2))


class PropertyTests(unittest.TestCase):
    """Phase 2: buying, rent and tax."""

    def setUp(self):
        self.game = make_game(players=("Alice", "Bob"))
        self.game.start("Alice")
        self.alice = self.game.get_player("Alice")
        self.bob = self.game.get_player("Bob")
        self.board = self.game.boards[0]

    def give(self, name, *indexes):
        for i in indexes:
            self.board.square(i).attributes["owner"] = name

    def test_buy_property(self):
        result = self.game.roll("Alice", dice=(1, 2))  # Whitechapel Road, 60
        self.assertEqual(result["decision"]["price"], 60)
        with self.assertRaises(GameError):
            self.game.roll("Alice", dice=(1, 2))  # must decide first
        with self.assertRaises(GameError):
            self.game.decide("Bob", "buy")
        self.game.decide("Alice", "buy")
        self.assertEqual(self.alice.money, 1440)
        self.assertEqual(self.board.square(3).attributes["owner"], "Alice")
        self.assertEqual(self.game.current_player.name, "Bob")
        state = self.game.to_dict()
        self.assertEqual(state["players"][0]["properties"], [{"board_id": 0, "index": 3}])
        self.assertIn("purchase", [e.event_type for e in self.game.drain_events()])

    def test_decline_leaves_square_unowned(self):
        self.game.roll("Alice", dice=(1, 2))
        self.game.decide("Alice", "decline")
        self.assertNotIn("owner", self.board.square(3).attributes)
        self.assertEqual(self.alice.money, 1500)

    def test_doubles_resume_after_decision(self):
        self.game.roll("Alice", dice=(3, 3))  # The Angel Islington
        self.game.decide("Alice", "buy")
        self.assertEqual(self.game.current_player.name, "Alice")
        self.assertEqual(self.game.turn_state, TurnState.WAITING_TO_ROLL)

    def test_cannot_afford(self):
        self.alice.money = 50
        self.game.roll("Alice", dice=(1, 2))
        self.assertEqual(self.game.turn_state, TurnState.WAITING_TO_ROLL)
        self.assertEqual(self.game.current_player.name, "Bob")

    def test_rent_and_full_group_doubles_it(self):
        self.give("Bob", 39)
        self.alice.position = Position(0, 33)
        self.game.roll("Alice", dice=(2, 4))  # Mayfair, rent 50
        self.assertEqual((self.alice.money, self.bob.money), (1450, 1550))
        self.assertIn("rent_paid", [e.event_type for e in self.game.drain_events()])
        self.give("Bob", 37)  # Park Lane completes the dark blue set
        self.assertEqual(self.game.rent_for(0, self.board.square(39), 6), 100)

    def test_no_rent_on_own_property(self):
        self.give("Alice", 3)
        self.game.roll("Alice", dice=(1, 2))
        self.assertEqual(self.alice.money, 1500)
        self.assertEqual(self.game.current_player.name, "Bob")

    def test_station_rent_scales(self):
        self.give("Bob", 5)
        self.assertEqual(self.game.rent_for(0, self.board.square(5), 7), 25)
        self.give("Bob", 15, 25)
        self.assertEqual(self.game.rent_for(0, self.board.square(5), 7), 100)
        self.give("Bob", 35)
        self.assertEqual(self.game.rent_for(0, self.board.square(5), 7), 200)

    def test_utility_rent_uses_dice(self):
        self.give("Bob", 12)
        self.assertEqual(self.game.rent_for(0, self.board.square(12), 7), 28)
        self.give("Bob", 28)
        self.assertEqual(self.game.rent_for(0, self.board.square(12), 7), 70)

    def test_tax(self):
        self.game.roll("Alice", dice=(1, 3))  # Income Tax, 200
        self.assertEqual(self.alice.money, 1300)
        self.assertIn("tax_paid", [e.event_type for e in self.game.drain_events()])

    def test_unaffordable_rent_becomes_a_debt(self):
        self.give("Bob", 39)
        self.alice.money = 10
        self.alice.position = Position(0, 33)
        result = self.game.roll("Alice", dice=(2, 4))  # Mayfair, rent 50
        self.assertEqual(self.alice.money, 10)  # nothing moves until settled
        self.assertEqual(result["decision"]["type"], "debt")
        self.assertEqual(result["decision"]["amount"], 50)
        with self.assertRaises(GameError):
            self.game.decide("Alice", "pay")

    def test_disconnect_during_decision_skips(self):
        self.game.roll("Alice", dice=(1, 2))
        self.game.mark_disconnected("Alice", now=0)
        self.assertTrue(self.game.skip_turn_if_disconnected(now=10, grace=5))
        self.assertIsNone(self.game.pending_decision)
        self.assertNotIn("owner", self.board.square(3).attributes)
        self.assertEqual(self.game.current_player.name, "Bob")


CHANCE = """
Advance to GO | move_to | square=GO
Go to Jail | go_to_jail
Get Out of Jail Free | get_out_of_jail_free
Go back three spaces | move_by | steps=-3
Nearest station, pay double | move_to_nearest | type=station; rent_multiplier=2
Pay £15 | pay | amount=15
Collect £10 from each player | collect_from_each | amount=10
Pay each player £50 | pay_each | amount=50
Repairs | repairs | house=25; hotel=100
"""


def deck_of(line):
    """A Chance deck holding just the card whose text starts with ``line``."""
    cards = [c for c in parse_cards_text(CHANCE, "chance") if c.text.startswith(line)]
    return {"chance": cards}


class Phase3Base(unittest.TestCase):
    decks = None

    def setUp(self):
        self.game = make_game(players=("Alice", "Bob", "Carol"), decks=self.decks)
        self.game.start("Alice")
        self.alice, self.bob, self.carol = (self.game.get_player(n) for n in ("Alice", "Bob", "Carol"))
        self.board = self.game.boards[0]

    def give(self, name, *indexes, **attrs):
        for i in indexes:
            self.board.square(i).attributes.update(owner=name, **attrs)

    def to_turn(self, name):
        self.game.current_index = self.game.players.index(self.game.get_player(name))


class CardTests(Phase3Base):
    def draw(self, card_line, start=4):
        """Alice lands on Chance (index 7) holding a deck of one card."""
        self.game.decks = Game("X", self.game.boards, self.game.settings,
                               decks=deck_of(card_line)).decks
        self.alice.position = Position(0, start)
        return self.game.roll("Alice", dice=(1, 2))

    def test_card_file_parses(self):
        cards = parse_cards_text(CHANCE, "chance")
        self.assertEqual(len(cards), 9)
        with self.assertRaises(ValueError):
            parse_cards_text("Bad | fly_away", "chance")

    def test_decks_in_cards_folder_load(self):
        from game_engine import load_decks
        decks = load_decks(os.path.join(os.path.dirname(BOARDS_DIR), "cards"))
        self.assertEqual(set(decks), {"chance", "community_chest"})
        self.assertGreater(len(decks["chance"]), 10)

    def test_advance_to_go_collects_salary(self):
        self.draw("Advance to GO")
        self.assertEqual(self.alice.position, Position(0, 0))
        self.assertEqual(self.alice.money, 1700)
        self.assertEqual(self.game.last_card["text"], "Advance to GO")

    def test_go_to_jail_card(self):
        self.draw("Go to Jail")
        self.assertTrue(self.alice.in_jail)
        self.assertEqual(self.alice.position, Position(0, 10))
        self.assertEqual(self.alice.money, 1500)  # no Go salary
        self.assertEqual(self.game.current_player.name, "Bob")

    def test_jail_card_is_kept_and_returned(self):
        self.draw("Get Out of Jail Free")
        self.assertEqual(len(self.alice.jail_cards), 1)
        self.assertEqual(self.game.decks["chance"].cards, [])
        self.alice.in_jail = True
        self.to_turn("Alice")
        self.game.use_jail_card("Alice")
        self.assertFalse(self.alice.in_jail)
        self.assertEqual(len(self.game.decks["chance"].cards), 1)

    def test_go_back_three_lands_on_tax(self):
        self.draw("Go back three spaces")
        self.assertEqual(self.alice.position, Position(0, 4))
        self.assertEqual(self.alice.money, 1300)  # Income Tax

    def test_nearest_station_double_rent(self):
        self.give("Bob", 15)
        self.draw("Nearest station")
        self.assertEqual(self.alice.position, Position(0, 15))
        self.assertEqual(self.alice.money, 1450)
        self.assertEqual(self.bob.money, 1550)

    def test_collect_from_each_and_pay_each(self):
        self.draw("Collect £10")
        self.assertEqual((self.alice.money, self.bob.money, self.carol.money), (1520, 1490, 1490))
        self.to_turn("Alice")
        self.draw("Pay each player")
        self.assertEqual((self.alice.money, self.bob.money, self.carol.money), (1420, 1540, 1540))

    def test_repairs(self):
        self.give("Alice", 1, 3, houses=2)
        self.board.square(39).attributes.update(owner="Alice", houses=5)
        self.draw("Repairs")
        self.assertEqual(self.alice.money, 1500 - (4 * 25 + 100))


class JailTests(Phase3Base):
    def test_go_to_jail_square(self):
        self.alice.position = Position(0, 27)
        self.game.roll("Alice", dice=(1, 2))
        self.assertTrue(self.alice.in_jail)
        self.assertEqual(self.alice.position, Position(0, 10))

    def test_doubles_leave_jail_without_extra_roll(self):
        self.alice.in_jail = True
        self.alice.position = Position(0, 10)
        result = self.game.roll("Alice", dice=(1, 1))  # Electric Company
        self.assertFalse(self.alice.in_jail)
        self.assertEqual(self.alice.position, Position(0, 12))
        self.assertFalse(result["roll_again"])

    def test_three_failed_rolls_force_the_fine(self):
        self.alice.in_jail = True
        self.alice.position = Position(0, 10)
        for attempt in range(2):
            self.to_turn("Alice")
            self.game.roll("Alice", dice=(1, 2))
            self.assertTrue(self.alice.in_jail)
            self.assertEqual(self.alice.position, Position(0, 10))
        self.to_turn("Alice")
        self.game.roll("Alice", dice=(1, 6))  # 17: Community Chest (no deck here)
        self.assertFalse(self.alice.in_jail)
        self.assertEqual(self.alice.money, 1450)
        self.assertEqual(self.alice.position, Position(0, 17))

    def test_pay_fine_then_roll(self):
        self.alice.in_jail = True
        with self.assertRaises(GameError):
            self.game.use_jail_card("Alice")  # no card
        self.game.pay_jail_fine("Alice")
        self.assertFalse(self.alice.in_jail)
        self.assertEqual(self.alice.money, 1450)
        self.assertEqual(self.game.current_player.name, "Alice")


class BuildingTests(Phase3Base):
    def setUp(self):
        super().setUp()
        self.give("Alice", 37, 39)  # Park Lane + Mayfair

    def test_build_evenly_and_rent(self):
        self.game.build_house("Alice", 0, 39)
        self.assertEqual(self.board.square(39).houses, 1)
        with self.assertRaises(GameError):
            self.game.build_house("Alice", 0, 39)  # must build Park Lane first
        self.game.build_house("Alice", 0, 37)
        self.game.build_house("Alice", 0, 39)
        self.assertEqual(self.alice.money, 1500 - 3 * 200)
        self.assertEqual(self.game.rent_for(0, self.board.square(39), 7), 600)

    def test_hotel_and_selling(self):
        self.alice.money = 5000
        for _ in range(5):
            self.game.build_house("Alice", 0, 37)
            self.game.build_house("Alice", 0, 39)
        self.assertEqual(self.game.rent_for(0, self.board.square(39), 7), 2000)
        with self.assertRaises(GameError):
            self.game.build_house("Alice", 0, 39)  # already a hotel
        money_before = self.alice.money
        self.game.sell_house("Alice", 0, 39)
        self.assertEqual(self.alice.money, money_before + 100)
        with self.assertRaises(GameError):
            self.game.sell_house("Alice", 0, 39)  # sell evenly

    def test_needs_full_group_and_own_turn(self):
        self.give("Alice", 1)
        with self.assertRaises(GameError):
            self.game.build_house("Alice", 0, 1)
        self.to_turn("Bob")
        with self.assertRaises(GameError):
            self.game.build_house("Alice", 0, 39)

    def test_mortgage_rules(self):
        self.game.mortgage("Alice", 0, 39)
        self.assertEqual(self.alice.money, 1700)
        self.assertEqual(self.game.rent_for(0, self.board.square(39), 7), 0)
        with self.assertRaises(GameError):
            self.game.build_house("Alice", 0, 37)  # group has a mortgage
        self.game.unmortgage("Alice", 0, 39)
        self.assertEqual(self.alice.money, 1700 - 220)
        self.game.build_house("Alice", 0, 37)
        with self.assertRaises(GameError):
            self.game.mortgage("Alice", 0, 39)  # group has buildings

    def test_no_rent_on_mortgaged(self):
        self.board.square(39).attributes["mortgaged"] = True
        self.to_turn("Bob")
        self.bob.position = Position(0, 33)
        self.game.roll("Bob", dice=(2, 4))
        self.assertEqual(self.bob.money, 1500)

    def test_property_actions(self):
        actions = {a["index"]: a for a in self.game.property_actions(self.alice)}
        self.assertTrue(actions[39]["can_build"])
        self.assertTrue(actions[39]["can_mortgage"])
        self.assertFalse(actions[39]["can_sell"])
        bob_view = self.game.property_actions(self.bob)
        self.assertEqual(bob_view, [])


class DebtTests(Phase3Base):
    def land_on_mayfair(self, money_left):
        self.give("Bob", 37, 39)
        self.alice.money = money_left
        self.alice.position = Position(0, 33)
        return self.game.roll("Alice", dice=(2, 4))

    def test_raise_money_then_pay(self):
        self.give("Alice", 1)
        self.land_on_mayfair(40)  # owes 100 (full set)
        self.game.mortgage("Alice", 0, 1)  # +30
        self.game.propose_trade("Alice", "Carol", give_squares=[[0, 1]], get_money=40)
        self.game.respond_trade("Carol", 1, True)  # +40 -> 110
        self.game.decide("Alice", "pay")
        self.assertEqual(self.alice.money, 10)
        self.assertEqual(self.bob.money, 1600)
        self.assertEqual(self.game.current_player.name, "Bob")

    def test_bankrupt_to_player(self):
        self.give("Alice", 1, 3, houses=1)
        self.alice.jail_cards.append(parse_cards_text(CHANCE, "chance")[2])
        self.land_on_mayfair(40)
        self.game.decide("Alice", "bankrupt")
        self.assertTrue(self.alice.bankrupt)
        self.assertEqual(self.board.square(1).owner, "Bob")
        self.assertEqual(self.board.square(1).houses, 0)
        self.assertEqual(self.bob.money, 1500 + 40 + 2 * 25)  # cash + houses sold at half
        self.assertEqual(len(self.bob.jail_cards), 1)
        self.assertEqual(self.game.current_player.name, "Bob")
        self.assertEqual(self.game.status, "in_progress")

    def test_bankrupt_to_bank_releases_properties(self):
        self.give("Alice", 1, mortgaged=True)
        self.alice.money = 50
        self.alice.position = Position(0, 1)
        self.game.roll("Alice", dice=(1, 2))  # Income Tax 200
        self.game.decide("Alice", "bankrupt")
        self.assertNotIn("owner", self.board.square(1).attributes)
        self.assertNotIn("mortgaged", self.board.square(1).attributes)

    def test_last_player_standing_wins(self):
        self.land_on_mayfair(0)
        self.game.decide("Alice", "bankrupt")
        self.game.remove_player("Carol")
        self.assertEqual(self.game.status, "ended")
        self.assertEqual(self.game.winner, "Bob")
        standings = [p.name for p in self.game.standings()]
        self.assertEqual(standings, ["Bob", "Carol", "Alice"])
        events = [e for e in self.game.drain_events() if e.event_type == "game_ended"]
        self.assertEqual(len(events), 3)
        self.assertIn("result=winner", events[0].details)

    def test_debt_from_another_turn_settled_at_turn_start(self):
        self.game.decks = Game("X", self.game.boards, self.game.settings,
                               decks=deck_of("Collect £10")).decks
        self.bob.money = 5
        self.alice.position = Position(0, 4)
        self.game.roll("Alice", dice=(1, 2))  # Chance: collect 10 from each
        self.assertEqual(self.game.current_player.name, "Bob")
        self.assertEqual(self.game.pending_decision["type"], "debt")
        with self.assertRaises(GameError):
            self.game.roll("Bob", dice=(1, 2))
        self.bob.money = 20
        self.game.decide("Bob", "pay")
        self.assertEqual(self.game.turn_state, "waiting_to_roll")
        self.assertEqual(self.alice.money, 1500 + 10 + 10)


class TradeTests(Phase3Base):
    def test_trade_swaps_property_and_money(self):
        self.give("Alice", 1)
        self.give("Bob", 3)
        trade = self.game.propose_trade("Alice", "Bob", give_squares=[[0, 1]], give_money=50,
                                        get_squares=[[0, 3]])
        with self.assertRaises(GameError):
            self.game.respond_trade("Carol", trade["id"], True)
        self.game.respond_trade("Bob", trade["id"], True)
        self.assertEqual(self.board.square(1).owner, "Bob")
        self.assertEqual(self.board.square(3).owner, "Alice")
        self.assertEqual((self.alice.money, self.bob.money), (1450, 1550))
        self.assertEqual(self.game.trades, [])

    def test_invalid_trades(self):
        self.give("Bob", 3)
        with self.assertRaises(GameError):
            self.game.propose_trade("Alice", "Bob", give_squares=[[0, 3]])  # not Alice's
        with self.assertRaises(GameError):
            self.game.propose_trade("Alice", "Alice", give_money=5)
        with self.assertRaises(GameError):
            self.game.propose_trade("Alice", "Bob", give_money=5000)
        self.give("Bob", 1, houses=1)
        with self.assertRaises(GameError):
            self.game.propose_trade("Alice", "Bob", get_squares=[[0, 3]])  # group has houses

    def test_reject_and_cancel(self):
        t1 = self.game.propose_trade("Alice", "Bob", give_money=10)
        self.game.respond_trade("Bob", t1["id"], False)
        t2 = self.game.propose_trade("Alice", "Bob", give_money=10)
        with self.assertRaises(GameError):
            self.game.cancel_trade("Bob", t2["id"])
        self.game.cancel_trade("Alice", t2["id"])
        self.assertEqual(self.game.trades, [])
        self.assertEqual(self.alice.money, 1500)

    def test_trade_revalidated_on_accept(self):
        self.give("Alice", 1)
        trade = self.game.propose_trade("Alice", "Bob", give_squares=[[0, 1]])
        self.board.square(1).attributes["owner"] = "Carol"
        with self.assertRaises(GameError):
            self.game.respond_trade("Bob", trade["id"], True)


class NetWorthTests(Phase3Base):
    def test_net_worth_and_host_end(self):
        self.give("Bob", 39, houses=2)
        self.give("Carol", 37, mortgaged=True)
        self.assertEqual(self.game.net_worth(self.bob), 1500 + 400 + 2 * 200)
        self.assertEqual(self.game.net_worth(self.carol), 1500 + 350 - 175)
        self.game.end("Alice")
        self.assertEqual(self.game.winner, "Bob")
        self.assertEqual([p.name for p in self.game.standings()], ["Bob", "Carol", "Alice"])


if __name__ == "__main__":
    unittest.main()
