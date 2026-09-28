"""Dashboard v2 — the square, the house tabs, the world tab.

The chat board is the run's reading surface now: every spoken line
once and attributed (_chat_history dedupes the world log's own-row /
heard-row doubling), one tab per house carries its state card,
holdings ledger and feed, and the old aggregate page lives whole
under the World tab. The live wiring only mounts when meta says the
run is live and gave a world URL — the finished artifact must stay
self-contained."""

from experiments.agent.dashboard import _chat_history, build_dashboard


def _snap() -> dict:
    return {
        "round": 1, "ticks": [1, 2], "conditions": ["WARMTH"],
        "market": [{"symbol": "WOOD", "last_price": "1.0"}],
        "events_by_type": {"say": 2},
        "world_map": {
            "places": [{"key": "HEARTH", "name": "Hearth"},
                       {"key": "RIVER", "name": "River"}],
            "roads": [{"from": "HEARTH", "to": "RIVER", "cost_ticks": 1}],
            "entities": [
                {"id": "e1", "name": "House Alpha", "place": "HEARTH",
                 "status": "active"},
                {"id": "e2", "name": "House Beta", "place": "HEARTH",
                 "status": "active"}]},
        "activity": {"world": [], "dynasties": {
            "House Alpha": [
                {"tick": 1, "text": 'says: "Alpha holds the hearth."',
                 "witnessed": False},
                {"tick": 2, "text": 'says: "POST: selling JERKY 1.05"',
                 "witnessed": True},
                {"tick": 2, "text": "traded WOOD", "witnessed": False}],
            "House Beta": [
                {"tick": 1, "text": 'says: "Alpha holds the hearth."',
                 "witnessed": True},
                {"tick": 2, "text": 'says: "POST: selling JERKY 1.05"',
                 "witnessed": True}]}},
        "dynasties": {
            "House Alpha": {
                "model": "m1",
                "holdings": [{"symbol": "COIN", "quantity": "5"},
                             {"symbol": "WOOD", "quantity": "3"},
                             {"symbol": "WARMTH", "quantity": "0.5"}],
                "needs": [{"need": "FOOD", "satisfaction": "0.8"}],
                "leaderboard": {"unlocks": 2, "status": "ACTIVE"},
                "entry": {"entity": "e1", "accepted": True,
                          "action": "edit", "attempts": 1,
                          "thoughts": "plan"},
                "behaviour": {"sha": "abc", "state": None,
                              "source": "-- x"}},
            "House Beta": {
                "model": "m2",
                "holdings": [{"symbol": "COIN", "quantity": "1"}],
                "needs": [], "leaderboard": {"unlocks": 0,
                                             "status": "ACTIVE"},
                "entry": {"entity": "e2", "accepted": False,
                          "action": "keep", "attempts": 1},
                "behaviour": {"sha": "def", "state": None,
                              "source": "-- y"}}}}


def test_chat_history_attributes_dedupes_and_flags_the_post():
    hist = _chat_history([_snap()])
    # the heard rows collapse into the speaker's own line
    assert len(hist) == 2
    assert hist[0] == {"t": 1, "who": "House Alpha",
                       "text": "Alpha holds the hearth.", "k": "say"}
    # the merchant bark has no own row — the counter speaks
    assert hist[1]["who"] == "POST"
    assert hist[1]["text"].startswith("POST:")


def test_chat_history_marks_anonymous_heards():
    snap = _snap()
    snap["activity"]["dynasties"]["House Beta"].append(
        {"tick": 3, "text": 'says: "a voice in the dark"',
         "witnessed": True})
    hist = _chat_history([snap])
    assert hist[-1]["who"] == "—" and hist[-1]["t"] == 3


def test_build_dashboard_mounts_the_tabs_and_feeds():
    h = build_dashboard([_snap()], {"title": "t"})
    for probe in ('id="pane-chat"', 'id="pane-h0"', 'id="pane-h1"',
                  'id="pane-world"', 'data-tab="chat"', 'data-tab="world"',
                  'data-feed="e1"', 'data-feed="e2"', 'id="chat-log"',
                  "localStorage"):
        assert probe in h, probe
    # the board seeds the say history; the tabs survive the per-round
    # page rewrite via localStorage
    assert '"text": "Alpha holds the hearth."' in h
    # the house tab carries the state card and the latest holdings
    assert 'class="chip">3.00 WOOD' in h and 'data-feed="e1"' in h
    # a finished run carries no stream — the artifact stays self-contained
    assert "EventSource" not in h


def test_build_dashboard_live_wires_the_stream():
    h = build_dashboard(
        [_snap()], {"title": "t", "status": "live",
                    "live_url": "http://127.0.0.1:8925",
                    "ticks_per_round": 24, "refresh_s": 60})
    assert "EventSource" in h and "__chatLine" in h
    # the live script routes says into the board AND the feeds
    assert "data-feed" in h and "chatLine" in h


def _snap_with_kill_lines() -> dict:
    """The stone-age shape: conditions with numeric kill lines (#213)
    riding the snapshot — one healthy, one riding the alarm band."""
    snap = _snap()
    snap["conditions"] = ["WARMTH", "THIRST"]
    snap["kill_lines"] = {"WARMTH": "3.0", "THIRST": "7.5"}
    snap["dynasties"]["House Alpha"]["holdings"] += [
        {"symbol": "THIRST", "quantity": "2.0"}]
    return snap


def test_condition_meters_draw_against_the_kill_line():
    h = build_dashboard([_snap_with_kill_lines()], {"title": "t"})
    # the bar is FULLNESS: WARMTH 0.5 of 3.0 = 17% climbed, so the bar
    # is 83% full and green — below the alarm band
    assert 'class="meter m-ok"' in h
    assert "WARMTH 0.50 / 3.00" in h
    assert 'width:83.3%' in h
    # THIRST 2.0 of 7.5 = 27% climbed: amber (the same 25% band the
    # #213 alarm reads) with 73% of the bar left
    assert 'class="meter m-warn"' in h
    assert "THIRST 2.00 / 7.50" in h
    assert 'width:73.3%' in h
    assert 'title="THIRST 2.00 of kill line 7.50 (26.67%)' in h
    # the ledger grid washes the same climb as cell heat
    assert 'class="num cond heat-warn">2.00' in h


def test_condition_meters_stay_quiet_without_kill_lines():
    """A world (or a resumed pre-#213 snapshot) without kill lines
    renders exactly as before: no meters, no heat, no crash."""
    h = build_dashboard([_snap()], {"title": "t"})
    assert '<div class="meter' not in h and "num cond heat-" not in h
    # the old amber number cells still render
    assert 'class="num cond">0.50' in h or 'num cond">0.50' in h


def _extinct_snaps() -> list[dict]:
    """The run-50 shape: two rounds, Alpha dying in the second —
    needs frozen at the death tick, the map flips to incapacitated."""
    s1, s2 = _snap(), _snap()
    s2["round"] = 2
    s2["ticks"] = [3, 4]
    s2["world_map"]["entities"][0]["status"] = "incapacitated"
    s2["dynasties"]["House Alpha"]["needs"] = [
        {"need": "FOOD", "satisfaction": "0.0", "updated_tick": 3}]
    s1["kill_lines"] = {"HUNGER": "15.0"}
    s1["dynasties"]["House Alpha"]["holdings"].append(
        {"symbol": "HUNGER", "quantity": "13.5"})
    return [s1, s2]


def _meta(status: str = "extinct") -> dict:
    return {"title": "t", "status": status, "round": 2, "rounds_total": 24,
            "elapsed_s": 3600}


def test_the_banner_says_what_happened_to_the_run():
    """Run 50's lesson: the watcher saw the dashboard go dark and had
    to ask. The extinct banner names the reason, the round it stopped,
    and who was lost."""
    h = build_dashboard(_extinct_snaps(), _meta())
    assert 'bn-extinct">☠ run ended — total extinction' in h
    assert "stopped after round 2 of 24" in h
    assert "lost: House Alpha, House Beta" in h
    h = build_dashboard([_snap()], _meta("complete"))
    assert "✓ finished</span><span>all 24 rounds" in h


def test_the_roster_cards_carry_the_deaths_and_the_climb():
    h = build_dashboard(_extinct_snaps(), _meta())
    # Alpha: dead at the frozen-need tick, with the honest prior-round
    # climb against its kill line
    assert 'chip-dead">dead t3' in h
    assert "climbing HUNGER 13.50/15.00 (90%)" in h
    # Beta: still alive, still placed
    assert 'chip-alive">alive' in h
    # wildlife is counted off the map (none here: no beasts named)
    assert 'class="rcard-wild"' not in h


def test_the_roster_carries_the_post_and_the_beasts():
    """The cast view (run 50): the business gets a card with its lamp
    and coin; wolves and boars are counted from the map."""
    snaps = [_snap()]
    s = snaps[0]
    s["world_map"]["entities"] += [
        {"id": "p1", "name": "Trading Post", "place": "POST",
         "status": "active", "entity_type": "business"},
        {"id": "w1", "name": "Wolf Pack I", "place": "FOREST",
         "status": "active"},
        {"id": "w2", "name": "Wolf Pack II", "place": "FOREST",
         "status": "incapacitated"}]
    s["cast"] = {
        "businesses": [{
            "name": "Trading Post", "place": "POST", "status": "active",
            "accounts": [{"currency": "COIN", "balance": "9.78"}],
            "holdings": [{"symbol": "SHOP_OPEN", "quantity": "0.44"},
                         {"symbol": "JERKY", "quantity": "2.7"}],
            "processes": [{"recipe": "WHOLESALE_BUY_EGGS", "status": "running",
                           "completes_tick": 7}]}],
        "wildlife": {"Wolf": {"active": 1, "down": 1}}}
    h = build_dashboard(snaps, _meta("live"))
    assert 'chip-post">the post' in h
    assert "lamp 0.44" in h and "9.78 coin" in h
    assert "Wolf 1 alive/2" in h
    # the merchant tab rides the cast: charts, shelf, errand, book
    assert 'data-tab="post"' in h
    assert "SHOP_OPEN — the lamp" in h and "post coin" in h
    assert "WHOLESALE_BUY_EGGS" in h
    assert "the key rests" not in h


def test_the_post_tab_survives_a_run_without_cast_data():
    """Pre-cast snapshots (every run before this change) still render:
    the tab says so, quietly, and nothing crashes."""
    h = build_dashboard([_snap()], _meta("complete"))
    assert "no cast data" in h
    assert 'data-tab="post"' in h
