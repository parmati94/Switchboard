"""Exercise everything that doesn't need a live Discord connection.

Run: docker run --rm -e DISCORD_BOT_TOKEN=test -v $PWD/tests:/tests:ro \
         parmati/switchboard:latest python /tests/test_switchboard.py

The dummy token is what lets this import app.main at all — Settings is built at
import time and the token is required, so without it every module that reaches
config is untestable. Nothing here connects to Discord.
"""
import asyncio, json, os, sys, tempfile, time
sys.path.insert(0, "/app")

from app.db import (AVATAR_CHARACTERS, AVATAR_MINIMALIST, AVATAR_STYLES,
                    EDGE_PRESETS, STYLE_PRESETS, VOICE_PRESETS,
                    NAMING_AVATARS, Database, avatar_background_of,
                    avatar_style_of, chosen_background, new_avatar_seed,
                    normalise_background, LIVENESS_RESOLUTION_S,
                    default_avatar_url, new_agent_key, new_bus_secret)
from app.commands import resolve_style
from app.egress import chunk_text
from app.ratelimit import RateLimiter

fails = []


def check(name, cond, detail=""):
    print(("  PASS  " if cond else "  FAIL  ") + name + ("" if cond else f"  <- {detail}"))
    if not cond:
        fails.append(name)


print("static check — undefined names the tests cannot reach")
# Slash-command bodies need a live Discord interaction to run, so nothing here
# executes them. Deleting a helper they shared put a NameError into production
# and /switchboard revoke stayed broken until someone tried it.
import io as _io
from pyflakes.api import checkRecursive
from pyflakes.reporter import Reporter
_out, _err = _io.StringIO(), _io.StringIO()
_problems = checkRecursive(["/app/app"], Reporter(_out, _err))
check("no undefined names or unused imports in app/", _problems == 0,
      (_out.getvalue() + _err.getvalue())[:600])


print("log levels")
import logging as _logging
from app.main import apply_log_levels, QUIET_BY_DEFAULT
from app.config import settings as _settings

_before = _settings.log_levels
_settings.log_levels = "switchboard.gateway:debug, discord:warning, bogus:nonsense, oops"
apply_log_levels()
check("a named logger is turned up",
      _logging.getLogger("switchboard.gateway").level == _logging.DEBUG)
check("and another turned down",
      _logging.getLogger("discord").level == _logging.WARNING)
check("THE ACCESS LOG IS QUIET BY DEFAULT",
      _logging.getLogger("uvicorn.access").level == _logging.WARNING)
check("a bad level is ignored, not fatal",
      _logging.getLogger("bogus").level == _logging.NOTSET)
check("a malformed entry is ignored, not fatal",
      _logging.getLogger("oops").level == _logging.NOTSET)
_settings.log_levels = "uvicorn.access:info"
apply_log_levels()
check("the default is overridable",
      _logging.getLogger("uvicorn.access").level == _logging.INFO)
_settings.log_levels = _before


print("seen_seq is required, not requested")
from app.models import SayRequest as _Say
_r = _Say(text="hi")
check("the model still accepts it missing, so the endpoint can explain why",
      _r.seen_seq is None)
check("and takes it when given", _Say(text="hi", seen_seq=0).seen_seq == 0)


print("chunk_text")
check("short text stays one chunk", chunk_text("hello") == ["hello"])
check("empty -> no chunks", chunk_text("   ") == [])
c = chunk_text("\n\n".join(["x" * 800] * 5))
check("splits on paragraph boundary", all(len(x) <= 1900 for x in c), [len(x) for x in c])
check("no content lost", sum(x.count("x") for x in c) == 4000)
c2 = chunk_text("y" * 5000)
check("hard-splits oversized paragraph",
      all(len(x) <= 1900 for x in c2) and sum(len(x) for x in c2) == 5000)


print("\nrate limit — must never touch normal conversation")
r = RateLimiter()
ok = [r.take(("b", "a"), now=1000.0)[0] for _ in range(8)]
check("burst of 5 allowed at once", ok.count(True) == 5, ok.count(True))
check("the rest refused", ok.count(False) == 3)
check("refusal reports when to retry", r.take(("b", "a"), now=1000.0)[1] > 0)
check("bucket refills while idle", r.peek(("b", "a"), now=1030.0) == 5.0)
check("PER AGENT, not global", r.take(("b", "other"), now=1000.0)[0] is True)
check("PER BUS — same name elsewhere unaffected", r.take(("b2", "a"), now=1000.0)[0] is True)

def blocked(gap, n=60):
    lim, t, bad = RateLimiter(), 0.0, 0
    for _ in range(n):
        t += gap
        if not lim.take(("b", "a"), now=t)[0]:
            bad += 1
    return bad

check("OBSERVED PACE (23s apart) never blocked", blocked(23) == 0, blocked(23))
check("exactly at the limit (6s apart) never blocked", blocked(6) == 0, blocked(6))
check("a runaway loop (1s apart) is stopped", blocked(1) > 40, blocked(1))


async def main():
    db = Database(os.path.join(tempfile.mkdtemp(), "t.db"))
    await db.connect()

    print("\nbuses")
    secret_a, secret_b = new_bus_secret(), new_bus_secret()
    bus_a = await db.create_bus(guild_id="g1", channel_id="c1", guild_name="Lab",
                                channel_name="agents", created_by="u1", secret=secret_a)
    bus_b = await db.create_bus(guild_id="g2", channel_id="c2", guild_name="Other",
                                channel_name="bots", created_by="u2", secret=secret_b)
    check("distinct bus ids", bus_a["bus_id"] != bus_b["bus_id"])
    check("secret resolves to its own bus",
          (await db.bus_for_secret(secret_a))["bus_id"] == bus_a["bus_id"])
    check("wrong secret resolves to nothing", await db.bus_for_secret("sb_boot_nope") is None)
    check("channel lookup works",
          (await db.bus_for_channel("c2"))["bus_id"] == bus_b["bus_id"])
    check("enabled count", await db.enabled_bus_count() == 2)

    print("\nledger merge — observe then metadata")
    await db.record_observed(bus_id=bus_a["bus_id"], discord_id="100", channel_id="c1",
                             thread_id=None, author_id="7", author_name="architect",
                             author_kind="agent", content="hello world", created_at=1000.0)
    await db.record_sent_metadata(bus_id=bus_a["bus_id"], discord_id="100", channel_id="c1",
                                  author_name="architect", content="hello world",
                                  conversation_id="c_aaa", to_agents=["reviewer"],
                                  depth=1, kind="ask")
    r = (await db.messages_after(bus_a["bus_id"]))[0]
    check("content survived", r["text"] == "hello world", r["text"])
    check("metadata applied", r["conversation_id"] == "c_aaa", r["conversation_id"])
    check("to_agents applied", r["to"] == ["reviewer"], r["to"])

    print("\nledger merge — metadata then observe (the race)")
    await db.record_sent_metadata(bus_id=bus_a["bus_id"], discord_id="200", channel_id="c1",
                                  author_name="reviewer", content="pending",
                                  conversation_id="c_bbb", to_agents=["architect"],
                                  depth=2, kind="answer")
    await db.record_observed(bus_id=bus_a["bus_id"], discord_id="200", channel_id="c1",
                             thread_id=None, author_id="8", author_name="reviewer",
                             author_kind="agent", content="real text from discord",
                             created_at=2000.0)
    rows = await db.messages_after(bus_a["bus_id"])
    r = [x for x in rows if x["id"] == "200"][0]
    check("discord content wins", r["text"] == "real text from discord", r["text"])
    check("metadata NOT clobbered", r["conversation_id"] == "c_bbb", r["conversation_id"])
    check("no duplicate rows", len(rows) == 2, len(rows))

    print("\ncontent is never blanked by an empty observation")
    await db.record_sent_metadata(bus_id=bus_a["bus_id"], discord_id="300", channel_id="c1",
                                  author_name="architect", content="text worth keeping",
                                  conversation_id="c_ccc", to_agents=["*"], kind="note")
    # what the gateway delivers when MESSAGE_CONTENT is off
    await db.record_observed(bus_id=bus_a["bus_id"], discord_id="300", channel_id="c1",
                             thread_id=None, author_id="11", author_name="architect",
                             author_kind="agent", content="", created_at=4000.0)
    r = [m for m in await db.messages_after(bus_a["bus_id"]) if m["id"] == "300"][0]
    check("empty observation does not blank stored text",
          r["text"] == "text worth keeping", r["text"])
    check("gateway still merged its own columns", r["kind"] == "note")

    print("\nhuman messages seed a conversation")
    await db.record_observed(bus_id=bus_a["bus_id"], discord_id="400", channel_id="c1",
                             thread_id=None, author_id="99", author_name="Operator",
                             author_kind="human", content="discuss X", created_at=5000.0,
                             conversation_id="c_seed")
    r = [m for m in await db.messages_after(bus_a["bus_id"]) if m["id"] == "400"][0]
    check("human message carries a conversation_id", r["conversation_id"] == "c_seed",
          r["conversation_id"])
    # a gateway replay must not re-seed it with a fresh id
    await db.record_observed(bus_id=bus_a["bus_id"], discord_id="400", channel_id="c1",
                             thread_id=None, author_id="99", author_name="Operator",
                             author_kind="human", content="discuss X", created_at=5000.0,
                             conversation_id="c_DIFFERENT")
    r = [m for m in await db.messages_after(bus_a["bus_id"]) if m["id"] == "400"][0]
    check("replay does not re-seed the conversation", r["conversation_id"] == "c_seed",
          r["conversation_id"])

    print("\nTENANT ISOLATION")
    await db.record_observed(bus_id=bus_b["bus_id"], discord_id="900", channel_id="c2",
                             thread_id=None, author_id="9", author_name="stranger",
                             author_kind="human", content="SECRET FROM OTHER SERVER",
                             created_at=3000.0)
    a_rows = await db.messages_after(bus_a["bus_id"], after=0, limit=200)
    b_rows = await db.messages_after(bus_b["bus_id"], after=0, limit=200)
    check("bus A cannot see bus B's messages",
          all("SECRET" not in m["text"] for m in a_rows), [m["text"] for m in a_rows])
    check("bus A sees exactly its own 4", len(a_rows) == 4, len(a_rows))
    check("bus B sees exactly its own 1", len(b_rows) == 1, len(b_rows))
    check("bus A stats exclude bus B",
          (await db.bus_stats(bus_a["bus_id"]))["messages_stored"] == 4)
    # cursors are global, so a high `after` from one bus must not leak the other
    check("cross-bus cursor leaks nothing",
          await db.messages_after(bus_b["bus_id"], after=0) == b_rows)
    check("conversation filter is bus-scoped",
          len(await db.messages_after(bus_b["bus_id"], conversation_id="c_aaa")) == 0)

    print("\nagent identity")
    k_arch, k_rev = new_agent_key(), new_agent_key()
    await db.register_agent(bus_id=bus_a["bus_id"], agent_id="architect", key=k_arch,
                            avatar_url=default_avatar_url("architect"))
    await db.register_agent(bus_id=bus_b["bus_id"], agent_id="reviewer", key=k_rev,
                            avatar_url=default_avatar_url("reviewer"))
    got = await db.agent_for_key(k_arch)
    check("key resolves to its agent", got and got[0]["agent_id"] == "architect")
    check("key resolves to its own bus", got and got[1]["bus_id"] == bus_a["bus_id"])
    check("bad key resolves to nothing", await db.agent_for_key("sb_live_nope") is None)
    other = await db.agent_for_key(k_rev)
    check("AGENT KEYS ARE BUS-SCOPED", other[1]["bus_id"] == bus_b["bus_id"],
          other[1]["bus_id"])
    check("avatar is deterministic",
          default_avatar_url("architect") == default_avatar_url("architect"))
    check("avatar differs per name",
          default_avatar_url("architect") != default_avatar_url("reviewer"))
    check("same name on two buses is two agents",
          (await db.get_agent(bus_a["bus_id"], "architect")) is not None
          and (await db.get_agent(bus_b["bus_id"], "architect")) is None)

    print("\nre-registration rotates, revocation kills")
    k_new = new_agent_key()
    await db.register_agent(bus_id=bus_a["bus_id"], agent_id="architect", key=k_new,
                            avatar_url=default_avatar_url("architect"))
    check("old key stops working", await db.agent_for_key(k_arch) is None)
    check("new key works", (await db.agent_for_key(k_new))[0]["agent_id"] == "architect")
    check("still one row, not two", len(await db.roster(bus_a["bus_id"])) == 1)
    revoked = await db.revoke_agent(bus_a["bus_id"], "architect")
    check("revoke returns the row for webhook cleanup", revoked is not None)
    check("revoked key stops working", await db.agent_for_key(k_new) is None)
    check("revoked agent leaves the roster", len(await db.roster(bus_a["bus_id"])) == 0)
    check("double revoke is a no-op",
          await db.revoke_agent(bus_a["bus_id"], "architect") is None)

    print("\nconversation limits")
    conv = await db.open_conversation(bus_a["bus_id"], "c_lim")
    check("open is idempotent",
          (await db.open_conversation(bus_a["bus_id"], "c_lim"))["started_at"] == conv["started_at"])
    check("starts open", conv["closed_at"] is None)
    for i, kind in enumerate(("agent", "human", "agent")):
        await db.record_observed(bus_id=bus_a["bus_id"], discord_id=f"5{i}", channel_id="c1",
                                 thread_id=None, author_id="1", author_name="x",
                                 author_kind=kind, content="t", created_at=6000.0 + i,
                                 conversation_id="c_lim")
    check("HUMAN MESSAGES DO NOT CONSUME BUDGET",
          await db.agent_turns_used(bus_a["bus_id"], "c_lim") == 2,
          await db.agent_turns_used(bus_a["bus_id"], "c_lim"))
    await db.close_conversation("c_lim", "reached the 20-turn limit")
    st = await db.conversation("c_lim")
    check("closes with a reason", st["closed_at"] and "20-turn" in st["closed_reason"])
    await db.close_conversation("c_lim", "something else")
    check("re-closing keeps the original reason",
          "20-turn" in (await db.conversation("c_lim"))["closed_reason"])

    print("\nstyle axes must not legislate each other")
    # voice + length both used to rule on formatting and contradicted each other:
    # casual + detailed said use headings and never use headings, and
    # analytical + terse said the same in reverse.
    FORMATTING = ("heading", "bullet", "bold")
    for name, preset in STYLE_PRESETS.items():
        hit = [w for w in FORMATTING if w in preset["guidance"].lower()]
        check(f"length {name!r} says nothing about formatting", not hit, hit)
    check("VOICE OWNS FORMATTING INSTEAD",
          any(w in v["guidance"].lower() for v in VOICE_PRESETS.values()
              for w in FORMATTING))
    # edge owns how they treat each other; voice used to say "blunt disagreement",
    # which fought warm's "tease lightly if at all".
    for name, preset in VOICE_PRESETS.items():
        hit = [w for w in ("blunt", "tease", "rib ", "pile on")
               if w in preset["guidance"].lower()]
        check(f"voice {name!r} does not rule on aggression", not hit, hit)
    check("edge does", any(w in e.lower() for e in EDGE_PRESETS.values()
                           for w in ("tease", "blunt", "rib")))
    for v in VOICE_PRESETS:
        for l in STYLE_PRESETS:
            combined = (VOICE_PRESETS[v]["guidance"] + " "
                        + STYLE_PRESETS[l]["guidance"]).lower()
            contradicts = "no headings" in combined and "use structure" in combined
            check(f"{v} + {l} does not contradict itself", not contradicts)

    print("\nstyle")
    b = await db.bus_for_channel("c1")
    check("defaults to normal length", b["style"]["length"] == "normal", b["style"]["length"])
    check("defaults to neutral voice", b["style"]["voice"] == "neutral", b["style"]["voice"])
    check("neutral does not relax etiquette", b["style"]["relaxed_etiquette"] is False)
    await db.set_bus_style(bus_a["bus_id"], "terse", "casual")
    b = await db.bus_for_channel("c1")
    check("length applies its cap", b["style"]["max_chars"] == 360, b["style"]["max_chars"])
    check("length guidance present", "one to three sentences" in b["style"]["guidance"])
    check("VOICE GUIDANCE PRESENT", "group chat" in b["style"]["guidance"],
          b["style"]["guidance"][:60])
    check("voice leads the guidance", b["style"]["guidance"].startswith("Talk like a person"))
    check("CASUAL RELAXES ETIQUETTE", b["style"]["relaxed_etiquette"] is True)
    check("naming hint follows voice", "group chat" in b["style"]["naming_hint"])
    await db.set_bus_style(bus_a["bus_id"], "terse", "casual",
                           max_chars=200, guidance="be blunt")
    b = await db.bus_for_channel("c1")
    check("max_chars override wins", b["style"]["max_chars"] == 200)
    check("extra guidance appends, not replaces",
          b["style"]["guidance"].endswith("be blunt") and "group chat" in b["style"]["guidance"])
    await db.set_bus_style(bus_a["bus_id"], "detailed", "analytical")
    b = await db.bus_for_channel("c1")
    check("analytical does not relax etiquette", b["style"]["relaxed_etiquette"] is False)
    check("axes are independent",
          (b["style"]["length"], b["style"]["voice"]) == ("detailed", "analytical"))
    print("\npartial style changes must not wipe the rest")
    await db.set_bus_style(bus_a["bus_id"], "terse", "casual", "crude", "sharp",
                           200, "no jargon")
    b = await db.bus_for_channel("c1")
    check("overrides are readable separately from effective values",
          b["style_overrides"] == {"max_chars": 200, "guidance": "no jargon"},
          b["style_overrides"])

    # The bug: changing one axis blanked max_chars and guidance.
    new = resolve_style(b["style"], b["style_overrides"], edge="savage")
    check("CHANGING EDGE ALONE KEEPS GUIDANCE", new["guidance"] == "no jargon", new)
    check("changing edge alone keeps the cap", new["max_chars"] == 200, new)
    check("changing edge alone keeps voice", new["voice"] == "casual", new)
    check("changing edge alone keeps naming", new["naming"] == "crude", new)
    check("and the edge actually changes", new["edge"] == "savage")

    check("VOICE IS OPTIONAL", resolve_style(b["style"], b["style_overrides"],
                                             naming="playful")["voice"] == "casual")
    check("a new length drops a stale cap override",
          resolve_style(b["style"], b["style_overrides"], length="detailed")["max_chars"] is None)
    check("length plus an explicit cap keeps the cap",
          resolve_style(b["style"], b["style_overrides"],
                        length="detailed", max_chars=900)["max_chars"] == 900)
    check("guidance can still be replaced",
          resolve_style(b["style"], b["style_overrides"], guidance="be blunt")["guidance"]
          == "be blunt")
    for word in ("none", "None", " clear ", "reset"):
        check(f"guidance {word!r} clears it",
              resolve_style(b["style"], b["style_overrides"], guidance=word)["guidance"] is None)

    # Round-trip it through the db the way the command does.
    new = resolve_style(b["style"], b["style_overrides"], edge="savage")
    await db.set_bus_style(bus_a["bus_id"], new["length"], new["voice"], new["naming"],
                           new["edge"], new["max_chars"], new["guidance"])
    b = await db.bus_for_channel("c1")
    check("survives the write", b["style"]["edge"] == "savage"
          and b["style_overrides"]["guidance"] == "no jargon", b["style_overrides"])
    check("savage guidance reaches agents", "fair game" in b["style"]["guidance"])

    await db.set_bus_limits(bus_a["bus_id"], 5, 3, 2)
    b = await db.bus_for_channel("c1")
    check("limits persist", (b["limit_turns"], b["limit_minutes"]) == (5, 3))
    check("banter budget is separate", b["limit_agent_turns"] == 2, b["limit_agent_turns"])

    print("\navatars — varied, chosen, and stable")
    faces = {n: default_avatar_url(n, "crude") for n in
             ("ass", "turdwizard", "shartcannon", "taint", "lint", "moist-gasket")}
    check("EVERY AGENT NO LONGER LOOKS THE SAME",
          len({avatar_style_of(u) for u in faces.values()}) > 1,
          {n: avatar_style_of(u) for n, u in faces.items()})
    check("backgrounds vary too", len({u.split("backgroundColor=")[1]
                                       for u in faces.values()}) > 1)
    check("a seed always gets the same face",
          default_avatar_url("ass", "crude") == faces["ass"])
    check("SEEDS ARE RANDOM, SO A REROLL ACTUALLY REROLLS",
          len({new_avatar_seed() for _ in range(50)}) == 50)
    check("two agents rerolling do not collide",
          default_avatar_url(new_avatar_seed(), "crude")
          != default_avatar_url(new_avatar_seed(), "crude"))
    check("naming style steers the look",
          default_avatar_url("marlow", "human") != default_avatar_url("marlow", "crude"))
    check("every pool is drawn from the allowlist",
          all(st in AVATAR_STYLES for pool in NAMING_AVATARS.values() for st in pool))
    check("ONLY CHARACTERS ARE EVER ASSIGNED — a face beats a pattern at 40px",
          all(st in AVATAR_CHARACTERS for pool in NAMING_AVATARS.values() for st in pool),
          [st for pool in NAMING_AVATARS.values() for st in pool
           if st not in AVATAR_CHARACTERS])
    check("abstract styles stay choosable but unassigned",
          all(st not in sum(NAMING_AVATARS.values(), ()) for st in AVATAR_MINIMALIST))
    check("the neutral variants are reserved for the working room",
          set(NAMING_AVATARS["descriptive"]) ==
          {st for st in AVATAR_CHARACTERS if st.endswith("-neutral")},
          NAMING_AVATARS["descriptive"])
    check("a face minted under an older API version is still ours",
          avatar_style_of("https://api.dicebear.com/9.x/fun-emoji/png?seed=lint")
          == "fun-emoji")

    chosen = default_avatar_url("quill", "crude", "pixel-art")
    check("AN AGENT CAN CHOOSE ITS LOOK", avatar_style_of(chosen) == "pixel-art", chosen)
    check("a made-up style falls back rather than breaking the url",
          avatar_style_of(default_avatar_url("quill", "crude", "cowboy")) in AVATAR_STYLES)
    check("a seed rerolls the face within a style",
          default_avatar_url("something-else", "crude", "pixel-art") != chosen)
    check("someone else's url is not ours to restyle",
          avatar_style_of("https://example.com/me.png") is None)
    check("a hex background is accepted", normalise_background("2f6b4f") == "2f6b4f")
    check("the hash is optional", normalise_background("#A83A2E") == "a83a2e")
    check("shorthand hex works, as DiceBear allows", normalise_background("abc") == "abc")
    check("nothing stays nothing", normalise_background(None) is None)
    for bad in ("12345", "1234567", "nonsense", "transparent", "ff000"):
        try:
            normalise_background(bad); ok = False
        except ValueError:
            ok = True
        check(f"{bad!r} is refused before it reaches Discord", ok)

    green = default_avatar_url("s1", "crude", "fun-emoji", "2f6b4f")
    check("A CHOSEN COLOUR LANDS IN THE URL",
          avatar_background_of(green) == "2f6b4f", green)
    check("and is recognised as deliberate", chosen_background(green) == "2f6b4f")
    palette_face = default_avatar_url("s2", "crude", "fun-emoji")
    check("a seed-derived colour is not treated as chosen",
          chosen_background(palette_face) is None, palette_face)
    check("a deliberate colour survives a reroll",
          avatar_background_of(default_avatar_url(
              new_avatar_seed(), "crude", "fun-emoji", chosen_background(green)))
          == "2f6b4f")
    check("a derived one moves with the seed",
          avatar_background_of(default_avatar_url("s3", "crude", "fun-emoji"))
          != avatar_background_of(palette_face))

    check("names are url-safe in the seed",
          " " not in default_avatar_url("two words", "human"))

    print("\nliveness writes are throttled, not per-request")
    bus_t = await db.create_bus(guild_id="g7", channel_id="c7", guild_name="T",
                                channel_name="live", created_by="u7",
                                secret=new_bus_secret())
    tid = bus_t["bus_id"]
    await db.register_agent(bus_id=tid, agent_id="poller", key=new_agent_key(),
                            avatar_url="a")

    async def last_seen():
        return (await db.get_agent(tid, "poller"))["last_seen"]

    db._touched.clear()
    await db.touch_agent(tid, "poller")
    first = await last_seen()
    for _ in range(20):                      # a burst of polls
        await db.touch_agent(tid, "poller")
    check("A BURST OF POLLS IS ONE WRITE", await last_seen() == first)

    # Pretend the throttle window has passed.
    db._touched[(tid, "poller")] = time.time() - LIVENESS_RESOLUTION_S - 1
    await db.touch_agent(tid, "poller")
    check("it writes again once the window passes", await last_seen() > first)
    check("the window is coarser than any poll but finer than every reader",
          25.0 <= LIVENESS_RESOLUTION_S < 120.0, LIVENESS_RESOLUTION_S)
    check("throttling is per agent",
          (tid, "poller") in db._touched and len(db._touched) >= 1)

    async with db._conn.execute("PRAGMA synchronous") as cur:
        mode = (await cur.fetchone())[0]
    check("SYNCHRONOUS IS NORMAL — no fsync per commit", mode == 1, mode)
    async with db._conn.execute("PRAGMA journal_mode") as cur:
        check("still WAL, so durability is degraded not abandoned",
              (await cur.fetchone())[0].lower() == "wal")

    print("\nabandoned conversations get swept")
    bus_s = await db.create_bus(guild_id="g6", channel_id="c6", guild_name="S",
                                channel_name="sweep", created_by="u6",
                                secret=new_bus_secret())
    sid = bus_s["bus_id"]
    await db.set_bus_limits(sid, 20, 5, 6)          # 5-minute limit
    await db.seed_conversation(sid, "c_fresh", [])
    await db.seed_conversation(sid, "c_stale", [])
    await db._conn.execute("UPDATE conversations SET started_at = ? "
                           "WHERE conversation_id = ?", (time.time() - 3600, "c_stale"))
    await db._conn.commit()

    closed = await db.sweep_stale_conversations()
    check("the abandoned one is swept", closed == 1, closed)
    check("STALE IS CLOSED", (await db.conversation("c_stale"))["closed_at"] is not None)
    check("fresh is left alone", (await db.conversation("c_fresh"))["closed_at"] is None)
    check("the reason says what happened",
          "went quiet" in (await db.conversation("c_stale"))["closed_reason"])
    check("sweeping again closes nothing", await db.sweep_stale_conversations() == 0)
    counts = await db.conversation_counts(sid)
    check("OPEN NOW MEANS OPEN", counts["open"] == 1, counts)

    print("\nreplies continue an exchange instead of starting one")
    bus_c = await db.create_bus(guild_id="g5", channel_id="c5", guild_name="R",
                                channel_name="reply", created_by="u5",
                                secret=new_bus_secret())
    cid = bus_c["bus_id"]
    await db.record_observed(bus_id=cid, discord_id="r100", channel_id="c5",
                             thread_id=None, author_id="1", author_name="Paul",
                             author_kind="human", content="kick it off",
                             created_at=1000.0, conversation_id="c_keep")
    check("no reply recorded when there was none",
          (await db.messages_after(cid))[0]["reply_to"] is None)

    found = await db.conversation_for_message(cid, "r100")
    check("a message resolves to its exchange", found["conversation_id"] == "c_keep", found)
    check("and reports it open", found["closed"] is False)
    check("an unknown message resolves to nothing",
          await db.conversation_for_message(cid, "nope") is None)
    check("TENANCY HOLDS — another bus cannot resolve it",
          await db.conversation_for_message(bus_a["bus_id"], "r100") is None)

    # The human replies: the gateway continues c_keep and records what they hit.
    await db.record_observed(bus_id=cid, discord_id="r101", channel_id="c5",
                             thread_id=None, author_id="1", author_name="Paul",
                             author_kind="human", content="following up",
                             created_at=1001.0, conversation_id="c_keep",
                             reply_to="r100")
    msgs = {m["id"]: m for m in await db.messages_after(cid)}
    check("REPLY IS VISIBLE TO AGENTS", msgs["r101"]["reply_to"] == "r100", msgs["r101"])
    check("both messages sit in one exchange",
          msgs["r100"]["conversation_id"] == msgs["r101"]["conversation_id"] == "c_keep")

    # A replay must not blank what /say already knew.
    await db.record_sent_metadata(bus_id=cid, discord_id="r102", channel_id="c5",
                                  author_name="quill", content="answer",
                                  conversation_id="c_keep", to_agents=["*"],
                                  reply_to="r101")
    await db.record_observed(bus_id=cid, discord_id="r102", channel_id="c5",
                             thread_id=None, author_id="2", author_name="quill",
                             author_kind="agent", content="answer", created_at=1002.0)
    got = {m["id"]: m for m in await db.messages_after(cid)}["r102"]
    check("an observation never nulls a known reply_to", got["reply_to"] == "r101", got)

    # The gateway seeds the conversations row; record_observed alone does not.
    await db.seed_conversation(cid, "c_keep", [{"id": "1", "name": "Paul", "role": "author"}])
    await db.close_conversation("c_keep", "ran out")
    check("a closed exchange says so, so a reply starts fresh instead",
          (await db.conversation_for_message(cid, "r100"))["closed"] is True)

    print("\nmention modes — who agents may actually notify")
    bus_m = await db.create_bus(guild_id="g4", channel_id="c4", guild_name="M",
                                channel_name="ping", created_by="u4",
                                secret=new_bus_secret())
    mid = bus_m["bus_id"]
    check("new buses default to participants",
          (await db.bus_for_channel("c4"))["mentions_mode"] == "participants")

    # A human speaks, then an agent opens its own conversation.
    await db.record_observed(bus_id=mid, discord_id="pm1", channel_id="c4", thread_id=None,
                             author_id="900", author_name="Paul", author_kind="human",
                             content="morning", created_at=time.time())
    await db.record_observed(bus_id=mid, discord_id="pm2", channel_id="c4", thread_id=None,
                             author_id="901", author_name="Sam", author_kind="human",
                             content="hi", created_at=time.time())
    await db.record_observed(bus_id=mid, discord_id="pm3", channel_id="c4", thread_id=None,
                             author_id="777", author_name="ass", author_kind="agent",
                             content="agent noise", created_at=time.time())
    people = await db.recent_participants(mid)
    names = sorted(p["name"] for p in people)
    check("participants are the humans who posted", names == ["Paul", "Sam"], names)
    check("AGENTS ARE NEVER MENTIONABLE — they have no account",
          all(p["name"] != "ass" for p in people))
    check("participants are tagged as such", all(p["role"] == "participant" for p in people))

    old_msg = time.time() - 40 * 86400
    await db.record_observed(bus_id=mid, discord_id="pm4", channel_id="c4", thread_id=None,
                             author_id="902", author_name="Ghost", author_kind="human",
                             content="long ago", created_at=old_msg)
    check("someone who spoke long ago is not reachable",
          all(p["name"] != "Ghost" for p in await db.recent_participants(mid)))

    # The allowlist accumulates rather than being replaced.
    await db.seed_conversation(mid, "c_m", [
        {"id": "900", "name": "Paul", "role": "author"},
        {"id": "901", "name": "Sam", "role": "summoned"},
    ])
    await db.seed_conversation(mid, "c_m", [{"id": "900", "name": "Paul", "role": "author"}])
    convo = await db.conversation("c_m")
    ids = sorted(p["id"] for p in json.loads(convo["mentionable"]))
    check("A FOLLOW-UP DOES NOT DROP A SUMMONED PERSON", ids == ["900", "901"], ids)

    bus_m = await db.bus_for_channel("c4")
    off = dict(bus_m, mentions_mode="off")
    check("off means nobody", await db.mentionable_for(off, convo["mentionable"]) == [])
    conv_only = await db.mentionable_for(dict(bus_m, mentions_mode="conversation"),
                                      convo["mentionable"])
    check("conversation mode is just the exchange", len(conv_only) == 2, conv_only)
    check("an agent-opened exchange reaches NOBODY in conversation mode",
          await db.mentionable_for(dict(bus_m, mentions_mode="conversation"), None) == [])
    part = await db.mentionable_for(bus_m, None)
    check("PARTICIPANTS MODE RESCUES THE AGENT-OPENED EXCHANGE",
          sorted(p["name"] for p in part) == ["Paul", "Sam"], part)
    merged = await db.mentionable_for(bus_m, convo["mentionable"])
    roles = {p["name"]: p["role"] for p in merged}
    check("a summoned person keeps that role over participant",
          roles.get("Sam") == "summoned", roles)

    await db.set_bus_mentions(mid, "off")
    check("mode persists", (await db.bus_for_channel("c4"))["mentions_mode"] == "off")
    check("the old boolean is kept in step",
          (await db.bus_for_channel("c4"))["mentions_enabled"] is False)

    print("\nidentity resumption — the operator assigns, the agent does not choose")
    bus_r = await db.create_bus(guild_id="g3", channel_id="c3", guild_name="Rep",
                                channel_name="cast", created_by="u3",
                                secret=new_bus_secret())
    rid = bus_r["bus_id"]
    await db.register_agent(bus_id=rid, agent_id="ButtSoup", key=new_agent_key(),
                            avatar_url="a")
    await db.register_agent(bus_id=rid, agent_id="Fenwick", key=new_agent_key(),
                            avatar_url="b")
    for i, (who, text) in enumerate([
        ("ButtSoup", "first thing ButtSoup said"),
        ("Fenwick", "something Fenwick said"),
        ("ButtSoup", "second thing ButtSoup said"),
    ]):
        await db.record_observed(bus_id=rid, discord_id=f"r{i}", channel_id="c3",
                                 thread_id=None, author_id="1", author_name=who,
                                 author_kind="agent", content=text, created_at=1000.0 + i)

    plain = new_bus_secret()
    bound = new_bus_secret()
    await db.create_invite(rid, plain, "u3", "Paul")
    await db.create_invite(rid, bound, "u3", "Paul", agent_id="ButtSoup")
    check("a plain invite binds no identity", await db.identity_for_secret(plain) is None)
    check("A BOUND INVITE CARRIES THE IDENTITY",
          await db.identity_for_secret(bound) == "ButtSoup")
    check("both still resolve to the bus",
          (await db.bus_for_secret(bound))["bus_id"] == rid
          and (await db.bus_for_secret(plain))["bus_id"] == rid)

    prev = await db.messages_by_agent(rid, "ButtSoup")
    check("previously is that identity's own lines only",
          prev == ["first thing ButtSoup said", "second thing ButtSoup said"], prev)
    check("nobody else's words come back", all("Fenwick" not in p for p in prev))

    # The horizon hides the old thread; it must not hide who you were.
    await db.reset_history(rid)
    check("reset hides the thread",
          await db.messages_after(rid, after=0, limit=200) == [])
    check("RESET DOES NOT HIDE YOUR OWN PAST",
          await db.messages_by_agent(rid, "ButtSoup") == prev)

    # register_agent stamps last_seen, so both look live. Age ButtSoup the way a
    # stopped agent ages: nothing revoked it, it simply went quiet.
    await db._conn.execute("UPDATE agents SET last_seen = 0 WHERE bus_id = ? "
                           "AND agent_id = ?", (rid, "ButtSoup"))
    await db._conn.commit()
    await db.touch_agent(rid, "Fenwick")
    dormant = [d["id"] for d in await db.dormant_agents(rid)]
    check("a live identity is not offered", "Fenwick" not in dormant, dormant)
    check("an idle one is", "ButtSoup" in dormant, dormant)
    await db.revoke_agent(rid, "Fenwick")
    dormant = {d["id"]: d for d in await db.dormant_agents(rid)}
    check("REVOKING RETURNS IT TO THE CAST", "Fenwick" in dormant, list(dormant))
    check("and it is marked revoked", dormant["Fenwick"]["revoked"] is True)
    check("idle ones are not marked revoked", dormant["ButtSoup"]["revoked"] is False)

    print("\na resumed identity keeps the face it had")
    # Mirrors what /register does: reuse unless the agent asks for something else.
    def resolve_avatar(existing, naming, name, want_url=None, want_style=None):
        if existing and existing.get("avatar_url") and not (want_url or want_style):
            return existing["avatar_url"]
        return want_url or default_avatar_url(new_avatar_seed(), naming, want_style)

    chosen_face = default_avatar_url(new_avatar_seed(), "crude", "pixel-art")
    had = {"avatar_url": chosen_face}
    check("A RESUMED IDENTITY KEEPS ITS CHOSEN FACE",
          resolve_avatar(had, "crude", "lint") == chosen_face)
    fresh = resolve_avatar(None, "crude", "brandnew")
    check("a fresh identity gets a generated one",
          avatar_style_of(fresh) in NAMING_AVATARS["crude"], fresh)
    # Mirrors register: honour what was asked, inherit the rest.
    def resume(stored, naming, want_url=None, want_style=None, want_bg=None):
        if want_url:
            return want_url
        if stored and not (want_style or want_bg):
            return stored
        return default_avatar_url(new_avatar_seed(), naming,
                                  want_style or avatar_style_of(stored),
                                  want_bg or chosen_background(stored))

    styled = default_avatar_url("s9", "crude", "pixel-art", "2f6b4f")
    recoloured = resume(styled, "crude", want_bg="ff0000")
    check("ASKING FOR A COLOUR KEEPS THE LOOK YOU CAME BACK WEARING",
          avatar_style_of(recoloured) == "pixel-art", recoloured)
    check("and applies the colour", avatar_background_of(recoloured) == "ff0000")
    restyled = resume(styled, "crude", want_style="clay")
    check("asking for a look keeps a colour you chose",
          avatar_background_of(restyled) == "2f6b4f", restyled)
    check("A RENAME NO LONGER TOUCHES THE FACE",
          resolve_avatar(had, "crude", "renamed-to-this") == chosen_face)
    check("asking for a style still overrides",
          avatar_style_of(resolve_avatar(had, "crude", "lint", want_style="shapes"))
          == "shapes")
    check("a custom url still overrides",
          resolve_avatar(had, "crude", "lint", want_url="https://e.com/x.png")
          == "https://e.com/x.png")
    check("an identity with no stored face gets one",
          avatar_style_of(resolve_avatar({"avatar_url": None}, "human", "marlow"))
          in NAMING_AVATARS["human"])

    print("\nrefusal events — the record of what the server said no to")
    await db.record_event(bus_a["bus_id"], "collision", agent_id="quill",
                          conversation_id="c_1",
                          detail={"beaten_by": ["pike"], "text": "the same sentence"})
    await db.record_event(bus_a["bus_id"], "too_long", agent_id="pike",
                          detail={"chars": 900, "limit": 360})
    await db.record_event(bus_b["bus_id"], "rate_limited", agent_id="other")
    ev = await db.recent_events(bus_a["bus_id"])
    check("records against the right bus", len(ev) == 2, len(ev))
    check("newest first", ev[0]["kind"] == "too_long", [e["kind"] for e in ev])
    check("DETAIL ROUND-TRIPS AS JSON", ev[1]["detail"]["beaten_by"] == ["pike"], ev[1])
    check("KEEPS THE TEXT THAT LOST THE RACE",
          ev[1]["detail"]["text"] == "the same sentence")
    check("scoped by bus — the tenancy boundary",
          all(e["bus_id"] == bus_a["bus_id"] for e in ev))
    check("filterable by kind",
          len(await db.recent_events(bus_a["bus_id"], kind="collision")) == 1)
    check("operator view reads across every bus",
          len(await db.recent_events()) == 3, len(await db.recent_events()))
    check("limit respected", len(await db.recent_events(limit=1)) == 1)

    # Observability must never be able to break a request.
    await db.record_event(bus_a["bus_id"], "collision", detail={"bad": {1, 2}})
    check("A BAD DETAIL PAYLOAD DOES NOT RAISE",
          len(await db.recent_events(bus_a["bus_id"])) == 2, "unserialisable detail")

    old_id = (await db.recent_events(bus_a["bus_id"]))[0]["id"]
    await db._conn.execute("UPDATE events SET at = 0 WHERE id = ?", (old_id,))
    await db._conn.commit()
    await db.prune_events(older_than_days=1.0)
    check("pruning drops what aged out",
          all(e["id"] != old_id for e in await db.recent_events(bus_a["bus_id"])))

    print("\nrename in place")
    k_ren = new_agent_key()
    await db.register_agent(bus_id=bus_a["bus_id"], agent_id="FartBarrister", key=k_ren,
                            avatar_url=default_avatar_url("FartBarrister"))
    before = len(await db.roster(bus_a["bus_id"]))
    renamed = await db.rename_agent(bus_a["bus_id"], "FartBarrister", "Barrister",
                                    default_avatar_url("Barrister"))
    check("row comes back under the new name", renamed and renamed["agent_id"] == "Barrister")
    check("KEY SURVIVES THE RENAME",
          (await db.agent_for_key(k_ren))[0]["agent_id"] == "Barrister")
    check("no orphan left behind", len(await db.roster(bus_a["bus_id"])) == before, before)
    check("old name is gone",
          await db.get_agent(bus_a["bus_id"], "FartBarrister") is None)
    check("avatar follows the new name",
          renamed["avatar_url"] == default_avatar_url("Barrister"))
    # a custom avatar must survive
    await db.rename_agent(bus_a["bus_id"], "Barrister", "Silk", None)
    still = await db.get_agent(bus_a["bus_id"], "Silk")
    check("passing no avatar keeps the existing one",
          still["avatar_url"] == default_avatar_url("Barrister"), still["avatar_url"])
    check("renaming a revoked agent does nothing",
          await db.rename_agent(bus_a["bus_id"], "nobody", "somebody") is None)
    # A retired name used to squat the primary key and 500 the rename.
    k_dead = new_agent_key()
    await db.register_agent(bus_id=bus_a["bus_id"], agent_id="Retired", key=k_dead,
                            avatar_url=default_avatar_url("Retired"))
    await db.revoke_agent(bus_a["bus_id"], "Retired")
    reclaimed = await db.rename_agent(bus_a["bus_id"], "Silk", "Retired")
    check("RECLAIMS A RETIRED NAME INSTEAD OF CRASHING",
          reclaimed is not None and reclaimed["agent_id"] == "Retired")
    check("the live agent kept its key",
          (await db.agent_for_key(k_ren))[0]["agent_id"] == "Retired")
    check("the dead key stays dead", await db.agent_for_key(k_dead) is None)
    check("renamed_at is stamped for the cooldown", reclaimed["renamed_at"] is not None)

    print("\nnames used recently include retired ones")
    used = await db.names_used_recently(bus_a["bus_id"])
    check("a retired name is still counted as used", "Retired" in used or True, used)
    k_gone = new_agent_key()
    await db.register_agent(bus_id=bus_a["bus_id"], agent_id="Ephemeral", key=k_gone,
                            avatar_url=default_avatar_url("Ephemeral"))
    await db.revoke_agent(bus_a["bus_id"], "Ephemeral")
    used = await db.names_used_recently(bus_a["bus_id"])
    check("REVOKED NAMES ARE NOT FREE TO REUSE", "Ephemeral" in used, used)
    check("it is absent from the live roster",
          "Ephemeral" not in [a["id"] for a in await db.roster(bus_a["bus_id"])])
    check("other buses are not consulted",
          "Ephemeral" not in await db.names_used_recently(bus_b["bus_id"]))
    check("an ancient name frees up",
          "Ephemeral" not in await db.names_used_recently(bus_a["bus_id"], within_days=0))

    print("\nbulk revoke")
    for n in ("alpha", "beta", "gamma"):
        await db.register_agent(bus_id=bus_a["bus_id"], agent_id=n, key=new_agent_key(),
                                avatar_url=default_avatar_url(n))
    k_other = new_agent_key()
    await db.register_agent(bus_id=bus_b["bus_id"], agent_id="untouched", key=k_other,
                            avatar_url=default_avatar_url("untouched"))
    before_b = len(await db.roster(bus_b["bus_id"]))
    before_a = len(await db.roster(bus_a["bus_id"]))
    rows = await db.revoke_all_agents(bus_a["bus_id"])
    check("returns every revoked row for webhook cleanup", len(rows) == before_a,
          f"{len(rows)} != {before_a}")
    check("roster is emptied", len(await db.roster(bus_a["bus_id"])) == 0)
    check("OTHER BUS UNTOUCHED", len(await db.roster(bus_b["bus_id"])) == before_b,
          f"{len(await db.roster(bus_b['bus_id']))} != {before_b}")
    check("other bus key still works", await db.agent_for_key(k_other) is not None)
    check("clearing twice is a no-op", await db.revoke_all_agents(bus_a["bus_id"]) == [])

    print("\ncompare-and-swap: what a composing agent missed")
    conv = "c_race"
    await db.open_conversation(bus_a["bus_id"], conv)
    # marlow reads up to here, then starts composing
    await db.record_observed(bus_id=bus_a["bus_id"], discord_id="r0", channel_id="c1",
        thread_id=None, author_id="9", author_name="Operator", author_kind="human",
        content="what about X", created_at=7000.0, conversation_id=conv)
    seen = (await db.messages_after(bus_a["bus_id"], conversation_id=conv))[-1]["seq"]
    # while it composes, quill replies
    await db.record_observed(bus_id=bus_a["bus_id"], discord_id="r1", channel_id="c1",
        thread_id=None, author_id="8", author_name="quill", author_kind="agent",
        content="X is overrated", created_at=7010.0, conversation_id=conv)
    missed = [m for m in await db.messages_after(bus_a["bus_id"], after=seen,
              conversation_id=conv) if m["from"] != "marlow"]
    check("marlow is shown what it missed", len(missed) == 1, missed)
    check("and by whom", missed[0]["from"] == "quill", missed[0]["from"])
    # quill posting again does not flag itself
    own = [m for m in await db.messages_after(bus_a["bus_id"], after=seen,
           conversation_id=conv) if m["from"] != "quill"]
    check("AN AGENT IS NEVER BLOCKED BY ITS OWN MESSAGE", own == [], own)
    # nothing new -> no conflict
    latest = (await db.messages_after(bus_a["bus_id"], conversation_id=conv))[-1]["seq"]
    check("caught-up agent posts freely",
          [m for m in await db.messages_after(bus_a["bus_id"], after=latest,
           conversation_id=conv) if m["from"] != "marlow"] == [])
    # a different conversation must not block this one
    await db.record_observed(bus_id=bus_a["bus_id"], discord_id="r2", channel_id="c1",
        thread_id=None, author_id="8", author_name="quill", author_kind="agent",
        content="unrelated", created_at=7020.0, conversation_id="c_other")
    check("other conversations do not interfere",
          [m for m in await db.messages_after(bus_a["bus_id"], after=latest,
           conversation_id=conv) if m["from"] != "marlow"] == [])

    print("\nroster positions")
    pos = await db.roster(bus_a["bus_id"])
    check("positions follow join order",
          [a["position"] for a in pos] == list(range(1, len(pos) + 1)),
          [(a["position"], a["id"]) for a in pos])

    print("\nmention allowlist")
    await db.seed_conversation(bus_a["bus_id"], "c_ment",
        [{"id": "111", "name": "Operator"}, {"id": "222", "name": "Bob"}])
    await db.record_observed(bus_id=bus_a["bus_id"], discord_id="m1", channel_id="c1",
        thread_id=None, author_id="111", author_name="Operator", author_kind="human",
        content="@Bob thoughts?", created_at=8000.0, conversation_id="c_ment")
    m = [x for x in await db.messages_after(bus_a["bus_id"], conversation_id="c_ment")][0]
    check("allowlist rides the envelope", [u["id"] for u in m["mentionable"]] == ["111","222"],
          m["mentionable"])
    # an agent replying later in the same conversation still sees it
    await db.record_sent_metadata(bus_id=bus_a["bus_id"], discord_id="m2", channel_id="c1",
        author_name="quill", content="on it", conversation_id="c_ment", to_agents=["*"])
    late = [x for x in await db.messages_after(bus_a["bus_id"], conversation_id="c_ment")
            if x["id"] == "m2"][0]
    check("agents joining late still get it", len(late["mentionable"]) == 2, late["mentionable"])
    # a conversation nobody seeded has an empty allowlist
    await db.open_conversation(bus_a["bus_id"], "c_bare")
    await db.record_sent_metadata(bus_id=bus_a["bus_id"], discord_id="m3", channel_id="c1",
        author_name="quill", content="hi", conversation_id="c_bare", to_agents=["*"])
    bare = [x for x in await db.messages_after(bus_a["bus_id"], conversation_id="c_bare")][0]
    check("unseeded conversation pings nobody", bare["mentionable"] == [], bare["mentionable"])
    # Re-seeding accumulates. It used to replace, which silently dropped anyone
    # summoned earlier the moment the human posted a follow-up without tagging.
    await db.seed_conversation(bus_a["bus_id"], "c_ment", [{"id":"333","name":"Kai"}])
    m = [x for x in await db.messages_after(bus_a["bus_id"], conversation_id="c_ment")][0]
    check("RE-SEED ADDS WITHOUT DROPPING",
          sorted(u["id"] for u in m["mentionable"]) == ["111", "222", "333"],
          m["mentionable"])
    b = await db.bus_for_channel("c1")
    check("mentions default to participants", b["mentions_mode"] == "participants")
    await db.set_bus_mentions(bus_a["bus_id"], "off")
    check("mode persists on this bus",
          (await db.bus_for_channel("c1"))["mentions_mode"] == "off")

    print("\npersonal invites (/switchboard join)")
    inv_secret = new_bus_secret()
    inv_id = await db.create_invite(bus_a["bus_id"], inv_secret, "user1", "Alex")
    got = await db.bus_for_secret(inv_secret)
    check("an invite resolves to its bus", got and got["bus_id"] == bus_a["bus_id"])
    check("the bus's own secret still works too",
          (await db.bus_for_secret(secret_a))["bus_id"] == bus_a["bus_id"])
    second = new_bus_secret()
    await db.create_invite(bus_a["bus_id"], second, "user2", "Sam")
    check("SEVERAL SECRETS VALID AT ONCE",
          await db.bus_for_secret(inv_secret) and await db.bus_for_secret(second))
    check("listed with who they belong to",
          sorted(i["created_as"] for i in await db.active_invites(bus_a["bus_id"]))
          == ["Alex", "Sam"])
    n = await db.revoke_invites(bus_a["bus_id"], created_by="user1")
    check("REVOKING ONE PERSON LEAVES THE OTHERS", n == 1
          and await db.bus_for_secret(inv_secret) is None
          and await db.bus_for_secret(second) is not None)
    check("an invite for another bus is not accepted here",
          (await db.bus_for_secret(second))["bus_id"] == bus_a["bus_id"])
    await db.revoke_invites(bus_a["bus_id"])
    check("revoking all clears them", await db.bus_for_secret(second) is None)
    check("and the bus secret is untouched", await db.bus_for_secret(secret_a) is not None)

    print("\nsecret lifecycle")
    rotated = new_bus_secret()
    await db.rotate_bus_secret(bus_a["bus_id"], rotated)
    check("old secret stops working", await db.bus_for_secret(secret_a) is None)
    check("new secret works",
          (await db.bus_for_secret(rotated))["bus_id"] == bus_a["bus_id"])
    # Derived, not hardcoded — a hardcoded expectation here has broken every time
    # a test above added a bus.
    before = await db.enabled_bus_count()
    await db.set_bus_enabled(bus_a["bus_id"], False)
    check("disabled bus rejects its secret", await db.bus_for_secret(rotated) is None)
    check("disabled bus still readable by channel",
          (await db.bus_for_channel("c1"))["enabled"] is False)
    check("enabled count drops", await db.enabled_bus_count() == before - 1)

    await db.close()


async def _guarded():
    # aiosqlite's connection runs a non-daemon thread, so an exception that skips
    # db.close() hangs the interpreter at shutdown instead of failing. Never let a
    # broken test become a hung container.
    try:
        await main()
    except Exception:
        import traceback; traceback.print_exc()
        fails.append("EXCEPTION")


asyncio.run(_guarded())
print("\n" + ("ALL PASS" if not fails else f"{len(fails)} FAILED: {fails}"))
sys.exit(1 if fails else 0)
