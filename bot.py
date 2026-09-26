import os
import sqlite3
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import discord
from discord import app_commands
from discord.ext import commands, tasks
from dotenv import load_dotenv

load_dotenv()

TOKEN = os.getenv("DISCORD_TOKEN")
DATABASE = os.path.join(os.getenv("DATA_DIR", "."), "events.db")
os.makedirs(os.path.dirname(os.path.abspath(DATABASE)), exist_ok=True)
LOCAL_TZ = ZoneInfo("Asia/Kolkata")  # IST. Change this if you want another timezone.

if not TOKEN:
    raise RuntimeError("DISCORD_TOKEN is missing. Put it in your .env file.")

intents = discord.Intents.default()
bot = commands.Bot(command_prefix="!", intents=intents)

db = sqlite3.connect(DATABASE, check_same_thread=False)
db.row_factory = sqlite3.Row

def db_init():
    db.execute("""
        CREATE TABLE IF NOT EXISTS events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            guild_id INTEGER NOT NULL,
            channel_id INTEGER NOT NULL,
            message_id INTEGER,
            title TEXT NOT NULL,
            description TEXT NOT NULL,
            max_players INTEGER NOT NULL,
            event_time TEXT NOT NULL,
            sent INTEGER NOT NULL DEFAULT 0,
            created_by INTEGER NOT NULL
        )
    """)
    db.execute("""
        CREATE TABLE IF NOT EXISTS participants (
            event_id INTEGER NOT NULL,
            user_id INTEGER NOT NULL,
            joined_at TEXT NOT NULL,
            PRIMARY KEY (event_id, user_id)
        )
    """)
    db.commit()

def get_event(event_id):
    return db.execute("SELECT * FROM events WHERE id = ?", (event_id,)).fetchone()

def get_participants(event_id):
    return db.execute("""
        SELECT user_id FROM participants
        WHERE event_id = ?
        ORDER BY joined_at ASC
    """, (event_id,)).fetchall()

def participant_ids(event_id):
    return [row["user_id"] for row in get_participants(event_id)]

def make_embed(event_id):
    event = get_event(event_id)
    if not event:
        return None

    users = participant_ids(event_id)
    limit = event["max_players"]
    main = users[:limit]
    waiting = users[limit:]

    embed = discord.Embed(
        title=f"🎮 {event['title']}",
        description=event["description"],
        color=discord.Color.blurple()
    )

    dt = datetime.fromisoformat(event["event_time"]).astimezone(LOCAL_TZ)
    embed.add_field(
        name="🕒 Event Time",
        value=f"<t:{int(dt.timestamp())}:F>\n<t:{int(dt.timestamp())}:R>",
        inline=False
    )

    main_text = "\n".join(f"**{i+1}.** <@{uid}>" for i, uid in enumerate(main))
    if not main_text:
        main_text = "*No signups yet.*"

    wait_text = "\n".join(f"**{i+1}.** <@{uid}>" for i, uid in enumerate(waiting))
    if not wait_text:
        wait_text = "*Empty*"

    embed.add_field(
        name=f"✅ Main Roster ({len(main)}/{limit})",
        value=main_text,
        inline=False
    )
    embed.add_field(
        name=f"⏳ Waiting List ({len(waiting)})",
        value=wait_text,
        inline=False
    )

    embed.set_footer(text=f"Event ID: {event_id} • Click a button below to join or leave")
    return embed

class EventView(discord.ui.View):
    def __init__(self, event_id: int):
        super().__init__(timeout=None)
        self.event_id = event_id

        self.add_item(SignUpButton(event_id))
        self.add_item(LeaveButton(event_id))

class SignUpButton(discord.ui.Button):
    def __init__(self, event_id: int):
        super().__init__(
            label="Sign Up",
            style=discord.ButtonStyle.success,
            emoji="✅",
            custom_id=f"event_signup:{event_id}"
        )
        self.event_id = event_id

    async def callback(self, interaction: discord.Interaction):
        event = get_event(self.event_id)
        if not event:
            return await interaction.response.send_message(
                "❌ This event no longer exists.", ephemeral=True
            )

        users = participant_ids(self.event_id)

        if interaction.user.id in users:
            return await interaction.response.send_message(
                "⚠️ You are already signed up for this event.", ephemeral=True
            )

        db.execute(
            "INSERT INTO participants (event_id, user_id, joined_at) VALUES (?, ?, ?)",
            (self.event_id, interaction.user.id, datetime.now(timezone.utc).isoformat())
        )
        db.commit()

        # Update the original event message.
        try:
            await interaction.message.edit(
                embed=make_embed(self.event_id),
                view=EventView(self.event_id)
            )
        except discord.HTTPException:
            pass

        users = participant_ids(self.event_id)
        position = users.index(interaction.user.id) + 1
        if position <= event["max_players"]:
            text = f"✅ You joined **{event['title']}**!"
        else:
            text = (
                f"⏳ The main roster is full. You were added to the waiting list "
                f"at position **{position - event['max_players']}**."
            )

        await interaction.response.send_message(text, ephemeral=True)

class LeaveButton(discord.ui.Button):
    def __init__(self, event_id: int):
        super().__init__(
            label="Leave Event",
            style=discord.ButtonStyle.danger,
            emoji="❌",
            custom_id=f"event_leave:{event_id}"
        )
        self.event_id = event_id

    async def callback(self, interaction: discord.Interaction):
        event = get_event(self.event_id)
        if not event:
            return await interaction.response.send_message(
                "❌ This event no longer exists.", ephemeral=True
            )

        cur = db.execute(
            "DELETE FROM participants WHERE event_id = ? AND user_id = ?",
            (self.event_id, interaction.user.id)
        )
        db.commit()

        if cur.rowcount == 0:
            return await interaction.response.send_message(
                "⚠️ You are not signed up for this event.", ephemeral=True
            )

        try:
            await interaction.message.edit(
                embed=make_embed(self.event_id),
                view=EventView(self.event_id)
            )
        except discord.HTTPException:
            pass

        await interaction.response.send_message(
            f"✅ You left **{event['title']}**.", ephemeral=True
        )

@bot.event
async def on_ready():
    db_init()

    # Re-register buttons for every event that has a Discord message.
    rows = db.execute("""
        SELECT id FROM events
        WHERE message_id IS NOT NULL
    """).fetchall()

    for row in rows:
        bot.add_view(EventView(row["id"]))

    if not scheduler.is_running():
        scheduler.start()

    print(f"Logged in as {bot.user} ({bot.user.id})")

@bot.tree.command(name="event_create", description="Create and schedule a new RP event.")
@app_commands.checks.has_permissions(manage_guild=True)
@app_commands.describe(
    title="Event name",
    description="Event description/rules",
    max_players="Maximum players in the main roster",
    date="Date in DD-MM-YYYY format, e.g. 27-09-2026",
    time="Time in 24-hour IST, e.g. 21:30",
    channel="Channel where the event message will be posted"
)
async def event_create(
    interaction: discord.Interaction,
    title: str,
    description: str,
    max_players: app_commands.Range[int, 1, 100],
    date: str,
    time: str,
    channel: discord.TextChannel
):
    try:
        local_dt = datetime.strptime(
            f"{date} {time}", "%d-%m-%Y %H:%M"
        ).replace(tzinfo=LOCAL_TZ)
        event_dt = local_dt.astimezone(timezone.utc)
    except ValueError:
        return await interaction.response.send_message(
            "❌ Invalid date/time. Use `DD-MM-YYYY` and `HH:MM`.\n"
            "Example: `27-09-2026` and `21:30`.",
            ephemeral=True
        )

    if event_dt <= datetime.now(timezone.utc):
        return await interaction.response.send_message(
            "❌ The event time must be in the future.",
            ephemeral=True
        )

    cur = db.execute("""
        INSERT INTO events
        (guild_id, channel_id, title, description, max_players, event_time, created_by)
        VALUES (?, ?, ?, ?, ?, ?, ?)
    """, (
        interaction.guild_id,
        channel.id,
        title,
        description,
        int(max_players),
        event_dt.isoformat(),
        interaction.user.id
    ))
    db.commit()

    event_id = cur.lastrowid

    await interaction.response.send_message(
        f"✅ Event **{title}** created.\n"
        f"📅 Scheduled for <t:{int(local_dt.timestamp())}:F>\n"
        f"📍 {channel.mention}\n"
        f"🆔 Event ID: `{event_id}`",
        ephemeral=True
    )

@bot.tree.command(name="event_list", description="Show your scheduled events.")
@app_commands.checks.has_permissions(manage_guild=True)
async def event_list(interaction: discord.Interaction):
    rows = db.execute("""
        SELECT * FROM events
        WHERE guild_id = ? AND sent = 0
        ORDER BY event_time ASC
    """, (interaction.guild_id,)).fetchall()

    if not rows:
        return await interaction.response.send_message(
            "📭 There are no pending events.", ephemeral=True
        )

    lines = []
    for e in rows[:20]:
        dt = datetime.fromisoformat(e["event_time"]).astimezone(LOCAL_TZ)
        lines.append(
            f"`{e['id']}` • **{e['title']}** • <t:{int(dt.timestamp())}:F>"
        )

    await interaction.response.send_message(
        "📅 **Pending Events**\n" + "\n".join(lines),
        ephemeral=True
    )

@bot.tree.command(name="event_cancel", description="Cancel a scheduled event.")
@app_commands.checks.has_permissions(manage_guild=True)
@app_commands.describe(event_id="Event ID from /event_list")
async def event_cancel(interaction: discord.Interaction, event_id: int):
    event = get_event(event_id)

    if not event or event["guild_id"] != interaction.guild_id:
        return await interaction.response.send_message(
            "❌ Event not found.", ephemeral=True
        )

    db.execute("DELETE FROM participants WHERE event_id = ?", (event_id,))
    db.execute("DELETE FROM events WHERE id = ?", (event_id,))
    db.commit()

    await interaction.response.send_message(
        f"🗑️ Cancelled event **{event['title']}**.",
        ephemeral=True
    )

@tasks.loop(seconds=5)
async def scheduler():
    now = datetime.now(timezone.utc)

    rows = db.execute("""
        SELECT * FROM events
        WHERE sent = 0 AND event_time <= ?
        ORDER BY event_time ASC
    """, (now.isoformat(),)).fetchall()

    for event in rows:
        try:
            channel = bot.get_channel(event["channel_id"])
            if channel is None:
                channel = await bot.fetch_channel(event["channel_id"])

            message = await channel.send(
                content="📢 **RP Event Sign-up is open!**",
                embed=make_embed(event["id"]),
                view=EventView(event["id"])
            )

            db.execute(
                "UPDATE events SET sent = 1, message_id = ? WHERE id = ?",
                (message.id, event["id"])
            )
            db.commit()

            # Add persistent view so buttons continue working after restart.
            bot.add_view(EventView(event["id"]), message_id=message.id)

        except Exception as exc:
            print(f"Could not send event {event['id']}: {exc}")

@scheduler.before_loop
async def before_scheduler():
    await bot.wait_until_ready()

@bot.tree.error
async def on_app_command_error(
    interaction: discord.Interaction,
    error: app_commands.AppCommandError
):
    if isinstance(error, app_commands.MissingPermissions):
        message = "❌ You need **Manage Server** permission to use this command."
    else:
        print("Command error:", repr(error))
        message = "❌ Something went wrong. Check the bot console for details."

    if interaction.response.is_done():
        await interaction.followup.send(message, ephemeral=True)
    else:
        await interaction.response.send_message(message, ephemeral=True)

async def main():
    db_init()
    await bot.start(TOKEN)

import asyncio
asyncio.run(main())
