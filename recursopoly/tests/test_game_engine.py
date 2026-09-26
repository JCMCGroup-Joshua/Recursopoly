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
    GameError,
    GameStatus,
    Position,
    TurnState,
    generate_join_code,
    load_boards,
    parse_board_text,
)

BOARDS_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "boards")


def make_game(players=("Alice", "Bob"), **overrides):
    settings = Config({k: str(v) for k, v in overrides.items()})
    game = Game("TEST42", load_boards(BOARDS_DIR, {0: settings.board_size}), settings)
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
        self.assertTrue(all("final_score=1500" in e.details for e in events))
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

    def test_rent_can_go_negative(self):
        self.give("Bob", 39)
        self.alice.money = 10
        self.alice.position = Position(0, 33)
        self.game.roll("Alice", dice=(2, 4))  # Mayfair, rent 50
        self.assertEqual(self.alice.money, -40)

    def test_disconnect_during_decision_skips(self):
        self.game.roll("Alice", dice=(1, 2))
        self.game.mark_disconnected("Alice", now=0)
        self.assertTrue(self.game.skip_turn_if_disconnected(now=10, grace=5))
        self.assertIsNone(self.game.pending_decision)
        self.assertNotIn("owner", self.board.square(3).attributes)
        self.assertEqual(self.game.current_player.name, "Bob")


if __name__ == "__main__":
    unittest.main()
