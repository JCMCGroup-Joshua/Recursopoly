"""Hosting a game with a locked rule set needs its passcode."""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import app as web  # noqa: E402


class LockedRuleSetTests(unittest.TestCase):
    def setUp(self):
        locked = web.RULESETS["classic"].with_values()
        locked.id, locked.name, locked.passcode = "locked_test", "Locked Test", "letmein"
        web.RULESETS["locked_test"] = locked
        web.password_failures.clear()
        self.addCleanup(web.RULESETS.pop, "locked_test", None)
        self.addCleanup(web.password_failures.clear)

    def create(self, **extra):
        client = web.socketio.test_client(web.app)
        self.addCleanup(client.disconnect)
        client.emit("create_game", {"name": "Alice", "ruleset": "locked_test", **extra})
        return {e["name"]: e["args"][0] for e in client.get_received()}

    def test_needs_passcode(self):
        events = self.create()
        self.assertEqual(events["error_message"]["code"], "bad_passcode")
        self.assertIn("locked", events["error_message"]["message"])
        self.assertEqual(self.create(passcode="nope")["error_message"]["code"], "bad_passcode")

    def test_right_passcode_creates_game(self):
        events = self.create(passcode="letmein")
        self.assertNotIn("error_message", events)
        game = web.games.pop(events["joined"]["join_code"])
        self.assertEqual(game.ruleset.id, "locked_test")

    def test_wrong_passcodes_are_rate_limited(self):
        for _ in range(web.MAX_PASSWORD_FAILURES):
            self.create(passcode="nope")
        message = self.create(passcode="letmein")["error_message"]["message"]
        self.assertIn("Too many wrong tries", message)

    def test_open_rule_sets_need_no_passcode(self):
        client = web.socketio.test_client(web.app)
        self.addCleanup(client.disconnect)
        client.emit("create_game", {"name": "Bob", "ruleset": "classic"})
        self.assertNotIn("error_message", [e["name"] for e in client.get_received()])


if __name__ == "__main__":
    unittest.main()
