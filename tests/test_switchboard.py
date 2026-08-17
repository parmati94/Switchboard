"""Exercise everything that doesn't need a live Discord connection.

Run: docker run --rm -v $PWD/tests:/tests:ro parmati/switchboard:latest \
         python /tests/test_switchboard.py
"""
import asyncio, os, sys, tempfile
sys.path.insert(0, "/app")

from app.db import Database, new_agent_key, new_bus_secret, default_avatar_url
from app.egress import chunk_text

fails = []


def check(name, cond, detail=""):
    print(("  PASS  " if cond else "  FAIL  ") + name + ("" if cond else f"  <- {detail}"))
    if not cond:
        fails.append(name)


print("chunk_text")
check("short text stays one chunk", chunk_text("hello") == ["hello"])
check("empty -> no chunks", chunk_text("   ") == [])
c = chunk_text("\n\n".join(["x" * 800] * 5))
check("splits on paragraph boundary", all(len(x) <= 1900 for x in c), [len(x) for x in c])
check("no content lost", sum(x.count("x") for x in c) == 4000)
c2 = chunk_text("y" * 5000)
check("hard-splits oversized paragraph",
      all(len(x) <= 1900 for x in c2) and sum(len(x) for x in c2) == 5000)


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
    await db.set_bus_limits(bus_a["bus_id"], 5, 3, 2)
    b = await db.bus_for_channel("c1")
    check("limits persist", (b["limit_turns"], b["limit_minutes"]) == (5, 3))
    check("banter budget is separate", b["limit_agent_turns"] == 2, b["limit_agent_turns"])

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
    # re-seeding replaces rather than appends
    await db.seed_conversation(bus_a["bus_id"], "c_ment", [{"id":"333","name":"Kai"}])
    m = [x for x in await db.messages_after(bus_a["bus_id"], conversation_id="c_ment")][0]
    check("re-seed replaces the list", [u["id"] for u in m["mentionable"]] == ["333"],
          m["mentionable"])
    b = await db.bus_for_channel("c1")
    check("mentions default to enabled", b["mentions_enabled"] is True)
    await db.set_bus_mentions(bus_a["bus_id"], False)
    check("toggle persists", (await db.bus_for_channel("c1"))["mentions_enabled"] is False)

    print("\nsecret lifecycle")
    rotated = new_bus_secret()
    await db.rotate_bus_secret(bus_a["bus_id"], rotated)
    check("old secret stops working", await db.bus_for_secret(secret_a) is None)
    check("new secret works",
          (await db.bus_for_secret(rotated))["bus_id"] == bus_a["bus_id"])
    await db.set_bus_enabled(bus_a["bus_id"], False)
    check("disabled bus rejects its secret", await db.bus_for_secret(rotated) is None)
    check("disabled bus still readable by channel",
          (await db.bus_for_channel("c1"))["enabled"] is False)
    check("enabled count drops", await db.enabled_bus_count() == 1)

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
