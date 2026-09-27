"""Phase 7: games are saved to files and restored after a restart."""

import json
import os
import sys
import tempfile
import time
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from game_engine import Card, Game, Position, TurnState  # noqa: E402
from rulesets import load_rulesets  # noqa: E402
from saves import delete_save, load_game, load_games, save_game  # noqa: E402

RULESETS = load_rulesets(os.path.join(ROOT, "rulesets"), ROOT)


def comparable(game):
    """The game's public state without values that change on restore."""
    state = game.to_dict()
    for p in state["players"]:
        p.pop("connected")
    state.pop("turn_activity_at")
    return state


class SaveTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = self.tmp.name

    def played_game(self, ruleset="recursopoly_amst"):
        rs = RULESETS[ruleset].with_values(auction_enabled=True)
        game = Game("SAVE42", rs)
        for name in ("Alice", "Bob", "Carol"):
            game.add_player(name)
        game.start("Alice")
        alice, bob = game.get_player("Alice"), game.get_player("Bob")
        board = game.boards[0]
        buyable = next(sq for sq in board.squares if sq.type == "property")
        buyable.set_owner("Bob")
        buyable.attributes["houses"] = 2
        alice.jail_cards.append(Card("chance", "Get out of jail free", "get_out_of_jail_free"))
        alice.attributes["journeys"] = 3
        bob.debts.append({"creditor": "Alice", "amount": 25, "reason": "rent", "category": None})
        game.propose_trade("Alice", "Carol", give_money=10)
        game._say("A test line")
        return game

    def test_round_trip_keeps_everything(self):
        game = self.played_game()
        before = comparable(game)
        deck_order = {name: [c.text for c in d.cards] for name, d in game.decks.items()}
        path = save_game(self.dir, game, chat=[{"name": "Bob", "text": "hi"}])
        self.assertTrue(path.endswith("SAVE42.json"))
        restored, chat = load_game(path)
        self.assertEqual(comparable(restored), before)
        self.assertEqual(chat, [{"name": "Bob", "text": "hi"}])
        self.assertEqual({name: [c.text for c in d.cards] for name, d in restored.decks.items()}, deck_order)
        self.assertEqual(restored.get_player("Alice").jail_cards[0].effect, "get_out_of_jail_free")
        self.assertEqual(restored.get_player("Alice").token, game.get_player("Alice").token)
        self.assertEqual(restored.ruleset.id, "recursopoly_amst")
        self.assertEqual(len(restored.boards), len(game.boards))
        self.assertTrue(all(not p.connected for p in restored.players))

    def test_players_rejoin_and_play_on(self):
        game = Game("PLAY22", RULESETS["classic"])
        for name in ("Alice", "Bob"):
            game.add_player(name)
        game.start("Alice")
        game.roll("Alice", dice=(1, 2))
        game.decide("Alice", "buy")
        token = game.get_player("Bob").token
        restored, _ = load_game(save_game(self.dir, game))
        player, rejoined = restored.add_player("Bob", token=token)
        self.assertTrue(rejoined and player.connected)
        restored.roll("Bob", dice=(2, 3))
        self.assertEqual(restored.get_player("Bob").position, Position(0, 5))
        self.assertEqual(restored.boards[0].square(3).owner, "Alice")

    def test_running_auction_survives_with_a_fresh_timer(self):
        game = Game("AUCT33", RULESETS["classic"].with_values(auction_enabled=True))
        for name in ("Alice", "Bob"):
            game.add_player(name)
        game.start("Alice")
        game.roll("Alice", dice=(1, 2))
        game.decide("Alice", "decline")
        game.bid("Bob", 30)
        restored, _ = load_game(save_game(self.dir, game), now=1000)
        self.assertEqual(restored.auction["high_bidder"], "Bob")
        self.assertEqual(restored.auction["ends_at"], 1000 + restored.auction["seconds"])
        self.assertEqual(restored.turn_state, TurnState.AWAITING_DECISION)
        restored.pass_auction("Alice")
        self.assertEqual(restored.boards[0].square(3).owner, "Bob")

    def test_game_keeps_its_rules_if_the_rule_set_changes(self):
        game = Game("RULE44", RULESETS["classic"].with_values(starting_money=999))
        game.add_player("Alice")
        path = save_game(self.dir, game)
        restored, _ = load_game(path)
        self.assertEqual(restored.get_player("Alice").money, 999)
        self.assertEqual(restored.ruleset.values["starting_money"], 999)
        self.assertEqual(restored.status, "lobby")

    def test_save_file_is_json_and_has_no_half_writes(self):
        save_game(self.dir, self.played_game())
        self.assertEqual(os.listdir(self.dir), ["SAVE42.json"])
        with open(os.path.join(self.dir, "SAVE42.json"), encoding="utf-8") as fh:
            self.assertEqual(json.load(fh)["version"], 1)

    def test_load_games_skips_broken_and_expired_saves(self):
        save_game(self.dir, self.played_game())
        old = Game("QLD555", RULESETS["classic"])
        old.add_player("Alice")
        old_path = save_game(self.dir, old)
        os.utime(old_path, (time.time() - 20 * 86400,) * 2)
        with open(os.path.join(self.dir, "BAD666.json"), "w", encoding="utf-8") as fh:
            fh.write("{not json")
        loaded = load_games(self.dir, keep_days=14)
        self.assertEqual(sorted(loaded), ["SAVE42"])
        self.assertFalse(os.path.exists(old_path))
        self.assertTrue(os.path.exists(os.path.join(self.dir, "BAD666.json.broken")))
        self.assertEqual(load_games(os.path.join(self.dir, "missing")), {})

    def test_delete_save(self):
        save_game(self.dir, self.played_game())
        delete_save(self.dir, "SAVE42")
        delete_save(self.dir, "SAVE42")  # already gone: no error
        delete_save(self.dir, "../etc")  # not a join code: ignored
        self.assertEqual(os.listdir(self.dir), [])


if __name__ == "__main__":
    unittest.main()
