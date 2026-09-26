# Recursopoly

A web-based, turn-based multiplayer board game in the spirit of Monopoly,
built with Python, Flask and Flask-SocketIO. Players join a shared game with
a short code and take turns rolling the dice around the board. Future
versions add **boards within boards**: smaller, pricier boards nested inside
the outer one and linked by train stations.

The game lives in [`recursopoly/`](recursopoly/). See
[`recursopoly/README.md`](recursopoly/README.md) for installation,
configuration, how to play, and the roadmap for later phases.

```bash
cd recursopoly
pip install -r requirements.txt
python app.py        # then open http://localhost:5000
```
