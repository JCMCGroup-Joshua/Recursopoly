"""Unit tests for Recursopoly's scores.csv statistics (stats.py)."""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import stats  # noqa: E402


def row(code, event, player="", money="", details=""):
    return {"timestamp": "2026-09-27T10:00:00+00:00", "join_code": code, "event_type": event,
            "player_name": player, "board_id": "0", "position": "0", "money": str(money),
            "details": details}


ROWS = [
    # An old Phase 1 game (final_score / rank format).
    row("OLD111", "game_started", "Alice", 1500),
    row("OLD111", "game_started", "Bob", 1500),
    row("OLD111", "game_ended", "Alice", 1700, "final_score=1700; rank=1"),
    row("OLD111", "game_ended", "Bob", 1300, "final_score=1300; rank=2"),
    # A Recursopoly game with journeys.
    row("NEW222", "game_started", "alice", 1500, "players=3; ruleset=recursopoly"),
    row("NEW222", "game_started", "Bob", 1500, "players=3; ruleset=recursopoly"),
    row("NEW222", "game_started", "Carol", 1500, "players=3; ruleset=recursopoly"),
    row("NEW222", "purchase", "Bob", 1300, "square=Mayfair; price=400"),
    row("NEW222", "ticket_purchased", "Bob", 1150, "from=King's Cross Station; to=Core Terminal; price=300"),
    row("NEW222", "ticket_purchased", "Bob", 1000, "from=Core Terminal; to=Marylebone Station; price=50"),
    row("NEW222", "ticket_purchased", "Carol", 1350, "from=a; to=b; price=150"),
    row("NEW222", "game_ended", "Bob", 900,
        "net_worth=4200; position=1; result=winner; properties=6; stakes=0; journeys=2; ruleset=recursopoly"),
    row("NEW222", "game_ended", "Carol", 800,
        "net_worth=1900; position=2; result=finished; properties=2; stakes=1; journeys=1; ruleset=recursopoly"),
    row("NEW222", "game_ended", "alice", 0,
        "net_worth=0; position=3; result=bankrupt; properties=0; stakes=0; journeys=0; ruleset=recursopoly"),
    # A game nobody finished.
    row("OPEN33", "game_started", "Dave", 2000, "players=2; ruleset=amst"),
    row("OPEN33", "roll", "Dave", 2000, "dice=1+2"),
]


class StatsTests(unittest.TestCase):
    def test_parse_details(self):
        self.assertEqual(stats.parse_details("a=1; b=two words; junk"), {"a": "1", "b": "two words"})
        self.assertEqual(stats.parse_details(""), {})

    def test_leaderboard(self):
        table = {e["name"].lower(): e for e in stats.leaderboard(ROWS)}
        self.assertEqual(table["alice"]["games"], 2)
        self.assertEqual(table["alice"]["wins"], 1)  # the old-format win counts
        self.assertEqual(table["alice"]["best_net_worth"], 1700)
        self.assertEqual(table["bob"]["wins"], 1)
        self.assertEqual(table["bob"]["best_net_worth"], 4200)
        self.assertEqual(table["bob"]["most_properties"], 6)
        self.assertEqual(table["bob"]["journeys"], 2)
        self.assertEqual(table["carol"]["journeys"], 1)
        self.assertEqual(table["dave"]["games"], 1)
        self.assertEqual(stats.leaderboard(ROWS)[0]["name"], "Bob")  # wins, then net worth

    def test_top_by(self):
        table = stats.leaderboard(ROWS)
        self.assertEqual(stats.top_by(table, "journeys")["name"], "Bob")
        self.assertIsNone(stats.top_by([], "journeys"))

    def test_games_newest_first(self):
        found = stats.games(ROWS)
        self.assertEqual([g["join_code"] for g in found], ["OPEN33", "NEW222", "OLD111"])
        new = found[1]
        self.assertTrue(new["finished"])
        self.assertEqual(new["winner"], "Bob")
        self.assertEqual(new["ruleset"], "recursopoly")
        self.assertEqual(new["players"], ["alice", "Bob", "Carol"])
        self.assertEqual([s["name"] for s in new["standings"]], ["Bob", "Carol", "alice"])
        self.assertFalse(found[0]["finished"])
        self.assertEqual(found[2]["winner"], "Alice")
        self.assertEqual(found[2]["ruleset"], "classic")

    def test_game_detail(self):
        detail = stats.game_detail(ROWS, "new222")
        self.assertEqual(detail["join_code"], "NEW222")
        self.assertEqual(len(detail["events"]), 10)
        self.assertIsNone(stats.game_detail(ROWS, "NOPE99"))

    def test_rows_from_a_real_game(self):
        from test_game_engine import make_game
        from game_engine import Position
        game = make_game(players=("Alice", "Bob"), ruleset="recursopoly")
        game.start("Alice")
        game.boards[0].square(39).set_owner("Bob")
        game.roll("Alice", dice=(2, 3))
        game.decide("Alice", "decline")
        game.decide("Alice", "travel:2:2")
        game.end("Alice")
        rows = [row("REAL44", e.event_type, e.player_name, e.money, e.details) for e in game.drain_events()]
        table = {e["name"]: e for e in stats.leaderboard(rows)}
        self.assertEqual(table["Alice"]["journeys"], 1)
        self.assertEqual(table["Bob"]["most_properties"], 1)
        self.assertEqual(stats.games(rows)[0]["ruleset"], "recursopoly")


if __name__ == "__main__":
    unittest.main()
