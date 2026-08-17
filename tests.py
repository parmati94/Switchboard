"""Exercise everything that doesn't need a live Discord connection.

Run: docker run --rm -v $PWD/tests.py:/t.py:ro parmati/switchboard:latest python /t.py
"""
import asyncio, os, sys, tempfile
sys.path.insert(0, "/app")

from app.db import Database, new_bus_secret
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

    print("\nTENANT ISOLATION")
    await db.record_observed(bus_id=bus_b["bus_id"], discord_id="900", channel_id="c2",
                             thread_id=None, author_id="9", author_name="stranger",
                             author_kind="human", content="SECRET FROM OTHER SERVER",
                             created_at=3000.0)
    a_rows = await db.messages_after(bus_a["bus_id"], after=0, limit=200)
    b_rows = await db.messages_after(bus_b["bus_id"], after=0, limit=200)
    check("bus A cannot see bus B's messages",
          all("SECRET" not in m["text"] for m in a_rows), [m["text"] for m in a_rows])
    check("bus A sees exactly its own 2", len(a_rows) == 2, len(a_rows))
    check("bus B sees exactly its own 1", len(b_rows) == 1, len(b_rows))
    check("bus A stats exclude bus B",
          (await db.bus_stats(bus_a["bus_id"]))["messages_stored"] == 2)
    # cursors are global, so a high `after` from one bus must not leak the other
    check("cross-bus cursor leaks nothing",
          await db.messages_after(bus_b["bus_id"], after=0) == b_rows)
    check("conversation filter is bus-scoped",
          len(await db.messages_after(bus_b["bus_id"], conversation_id="c_aaa")) == 0)

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


asyncio.run(main())
print("\n" + ("ALL PASS" if not fails else f"{len(fails)} FAILED: {fails}"))
sys.exit(1 if fails else 0)
