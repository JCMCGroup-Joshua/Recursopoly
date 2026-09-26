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
        self.assertEqual(self.game.current_player.name, "Bob")
        self.assertIn("Alice rolled 2 + 3 and moved to King's Cross Station.",
                      [line["message"] for line in self.game.log])

    def test_doubles_roll_again(self):
        result = self.game.roll("Alice", dice=(3, 3))
        self.assertTrue(result["roll_again"])
        self.assertEqual(self.game.current_player.name, "Alice")
        self.assertEqual(self.game.turn_state, TurnState.WAITING_TO_ROLL)
        self.game.roll("Alice", dice=(1, 2))
        self.assertEqual(self.game.current_player.name, "Bob")

    def test_three_doubles_goes_to_jail(self):
        for _ in range(2):
            self.game.roll("Alice", dice=(2, 2))
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
        self.game.roll("Alice", dice=(1, 2))
        self.assertEqual(self.game.current_player.name, "Carol")

    def test_current_player_disconnect_skips_after_grace(self):
        self.game.mark_disconnected("Alice", now=100)
        self.assertFalse(self.game.skip_turn_if_disconnected(now=102, grace=5))
        self.assertTrue(self.game.skip_turn_if_disconnected(now=106, grace=5))
        self.assertEqual(self.game.current_player.name, "Bob")

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


if __name__ == "__main__":
    unittest.main()
