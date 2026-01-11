import discord
from discord.ext import commands
from discord import app_commands
from discord import Interaction
from discord.ui import View, Button, Select
import datetime
import asyncio
import traceback

# =========================
# STORAGE
# =========================
active_shifts = {}
grace_periods = {}  # {user_id: {shift_id, left_at, task}}
high_ranks_role = "brotato"  # Role name for high ranks

# =========================
# HELPER FUNCTIONS
# =========================
def round_attendance(fraction):
    if fraction >= 0.875:
        return 1.0
    elif fraction >= 0.625:
        return 0.75
    elif fraction >= 0.375:
        return 0.5
    elif fraction >= 0.125:
        return 0.25
    else:
        return 0.0

def calculate_attendance_from_sessions(sessions, shift_start, shift_end):
    total_present_seconds = 0
    if shift_end < shift_start:
        shift_end = shift_start + datetime.timedelta(seconds=1)
    for start, end in sessions:
        ses_start = max(start, shift_start)
        ses_end = end if end else shift_end
        ses_end = min(ses_end, shift_end)
        if ses_start > ses_end:
            continue
        total_present_seconds += max(0, (ses_end - ses_start).total_seconds())
    shift_duration_seconds = (shift_end - shift_start).total_seconds()
    if shift_duration_seconds <= 0:
        shift_duration_seconds = 1
    if total_present_seconds > shift_duration_seconds:
        total_present_seconds = shift_duration_seconds
    fraction = total_present_seconds / shift_duration_seconds
    return round_attendance(fraction)

def format_time_delta(delta):
    total_seconds = int(max(0, delta.total_seconds()))
    hours = total_seconds // 3600
    minutes = (total_seconds % 3600) // 60
    if hours > 0:
        return f"{hours}h {minutes}m"
    return f"{minutes}m"

# ==== DEFENSIVE UTILS ====
def safe_get_member(guild, user_id):
    # Try to get a Member, fallback to User if can't
    member = None
    try:
        if guild:
            member = guild.get_member(user_id)
    except:
        member = None
    return member

def safe_get_user(bot, user_id):
    # Try to get a cached user, falls back to fetch_user
    user = None
    try:
        user = bot.get_user(user_id)
    except Exception:
        user = None
    return user

def can_edit_message(msg):
    try:
        return hasattr(msg, "guild") and msg.guild is not None and msg.channel.permissions_for(msg.guild.me).manage_messages
    except Exception:
        return True  # If not possible to check, optimistically say yes

def ensure_embed_fields(embed, shift, bot):
    # Always at least these fields
    if len(embed.fields) < 5:
        embed.clear_fields()
        host = safe_get_user(bot, shift["host"])
        host_mention = host.mention if getattr(host,"mention",None) else f"<@{shift['host']}>"
        vchan = bot.get_channel(shift["voice"])
        vname = vchan.mention if vchan else "Unknown"
        embed.add_field(name="👤 Host", value=host_mention, inline=True)
        embed.add_field(name="⏱️ Elapsed Time", value="0m", inline=True)
        embed.add_field(name="🎙️ Voice Channel", value=vname, inline=True)
        embed.add_field(name="📅 Start", value=f"<t:{int(shift['start'].timestamp())}:R>", inline=True)
        embed.add_field(name="👥 Present (0)", value="—", inline=False)

# =========================
# COG & SLASH COMMAND
# =========================
class ClockInCreate(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    def has_brotato_role(self, member: discord.Member):
        return any(r.name.lower() == high_ranks_role.lower() for r in getattr(member, "roles", []))

    @commands.Cog.listener()
    async def on_voice_state_update(self, member, before, after):
        try:
            if member.bot:
                return

            # Clear up ghosted/ended shifts
            for sid, shift in list(active_shifts.items()):
                if shift.get("ended", False) and sid in active_shifts:
                    # Defensive prune
                    continue

            # User left a tracked shift voice channel
            if before and before.channel:
                for sid, shift in list(active_shifts.items()):
                    if shift.get("ended"):
                        continue
                    if before.channel.id == shift["voice"] and member.id in shift["attendees"]:
                        if member.id not in grace_periods:
                            left_at = datetime.datetime.utcnow()
                            task = asyncio.create_task(grace_period_task(member.id, sid, self.bot))
                            grace_periods[member.id] = {
                                "shift_id": sid,
                                "left_at": left_at,
                                "task": task
                            }
                            attendee = shift["attendees"].get(member.id)
                            now = left_at
                            if attendee is not None:
                                if attendee["sessions"] and attendee["sessions"][-1][1] is None:
                                    attendee["sessions"][-1] = (attendee["sessions"][-1][0], now)
                                attendee["leave"] = now
                            await update_embed(shift, self.bot)

            # User returned to a tracked shift voice channel
            if after and after.channel:
                if member.id in grace_periods:
                    ginfo = grace_periods[member.id]
                    shift = active_shifts.get(ginfo["shift_id"])
                    if shift and after.channel.id == shift["voice"]:
                        try:
                            ginfo["task"].cancel()
                        except Exception:
                            pass
                        del grace_periods[member.id]
                        attendee = shift["attendees"].get(member.id)
                        if attendee is not None:
                            attendee["leave"] = None
                            if attendee["sessions"]:
                                if attendee["sessions"][-1][1] is not None:
                                    attendee["sessions"].append((datetime.datetime.utcnow(), None))
                            else:
                                attendee["sessions"] = [(datetime.datetime.utcnow(), None)]
                        await update_embed(shift, self.bot)
        except Exception:
            traceback.print_exc()

    @app_commands.command(
        name="clockincreate",
        description="Create a clock-in shift (only members with brotato role)"
    )
    @app_commands.describe(
        title="Shift name",
        voice="Voice channel to monitor",
        min_attendance="Minimum presence ratio to pass (0.25 = 25%)"
    )
    async def clockincreate_slash(
        self,
        interaction: Interaction,
        title: str,
        voice: discord.VoiceChannel,
        min_attendance: float = 0.25
    ):
        try:
            if not isinstance(interaction.user, discord.Member):
                await interaction.response.send_message("❌ You need to be in a server.", ephemeral=True)
                return
            if not self.has_brotato_role(interaction.user):
                await interaction.response.send_message(
                    f"❌ Only members with role `{high_ranks_role}` can create clock-ins.",
                    ephemeral=True)
                return

            now = datetime.datetime.utcnow()
            host = interaction.user
            shift_id = f"{interaction.guild_id}-{interaction.id}"

            embed = discord.Embed(
                title=f"🟢 {title}",
                description="**Active Clock-in Shift**\nUse the button to count your attendance.",
                color=discord.Color.green(),
                timestamp=now
            )
            embed.set_author(
                name=f"Host: {host.display_name}",
                icon_url=host.display_avatar.url if getattr(host, "display_avatar", None) else None
            )
            embed.add_field(name="👤 Host", value=host.mention, inline=True)
            embed.add_field(name="⏱️ Elapsed Time", value="0m", inline=True)
            embed.add_field(name="🎙️ Voice Channel", value=voice.mention, inline=True)
            embed.add_field(name="📅 Start", value=f"<t:{int(now.timestamp())}:R>", inline=True)
            embed.add_field(name="👥 Present ({})".format(0), value="—", inline=False)
            embed.set_footer(text="Click ✅ Join to register for the shift")

            view = ClockInView(shift_id, self.bot)
            await interaction.response.send_message(embed=embed, view=view)
            msg = await interaction.original_response()

            active_shifts[shift_id] = {
                "host": host.id,
                "title": title,
                "min_attendance": min_attendance,
                "voice": voice.id,
                "guild_id": interaction.guild_id,
                "start": now,
                "attendees": {},
                "embed": embed,
                "message": msg,
                "ended": False,
                "shift_id": shift_id
            }
        except Exception as e:
            traceback.print_exc()
            await interaction.response.send_message(f"❌ Failed to create clock-in shift: {e}", ephemeral=True)

    async def cog_unload(self):
        tree = getattr(self.bot, "tree", None)
        if tree:
            try:
                tree.remove_command("clockincreate", type=discord.AppCommandType.chat_input)
            except Exception as e:
                print(f"[ClockInCreate] Could not remove /clockincreate: {e}")

# =========================
# BUTTON VIEW
# =========================
class ClockInView(View):
    def __init__(self, shift_id, bot):
        super().__init__(timeout=24*60*60)  # Force 1 day for safety instead of None
        self.shift_id = shift_id
        self.bot = bot

    def has_permission(self, user: discord.Member, shift) -> bool:
        if getattr(user, "id", None) == shift["host"]:
            return True
        return any(getattr(role, "name", "").lower() == high_ranks_role.lower() for role in getattr(user, "roles", []))

    @discord.ui.button(label="Join", style=discord.ButtonStyle.success, emoji="✅")
    async def join(self, interaction: Interaction, button: Button):
        try:
            shift = active_shifts.get(self.shift_id)
            if not shift or shift["ended"]:
                await interaction.response.send_message("❌ This shift has already ended.", ephemeral=True)
                return

            user = interaction.user
            if not isinstance(user, discord.Member):
                await interaction.response.send_message("❌ Error finding server member.", ephemeral=True)
                return
            voice_channel = self.bot.get_channel(shift["voice"])
            if not voice_channel:
                await interaction.response.send_message("❌ Voice channel not found.", ephemeral=True)
                return

            if user.voice is None or user.voice.channel.id != shift["voice"]:
                await interaction.response.send_message(
                    f"❌ You need to be in {voice_channel.mention} to join the shift.", ephemeral=True)
                return

            if user.id in shift["attendees"]:
                await interaction.response.send_message("⚠️ You are already registered in the shift.", ephemeral=True)
                return

            if user.id in grace_periods:
                try:
                    grace_periods[user.id]["task"].cancel()
                except Exception:
                    pass
                del grace_periods[user.id]

            now = datetime.datetime.utcnow()
            shift["attendees"][user.id] = {
                "join": now,
                "leave": None,
                "sessions": [(now, None)]
            }
            await update_embed(shift, self.bot)
            await interaction.response.send_message("✅ Registered in the shift!", ephemeral=True)
        except Exception:
            traceback.print_exc()
            await interaction.response.send_message("❌ Error while joining shift. Try again later.", ephemeral=True)

    @discord.ui.button(label="Leave", style=discord.ButtonStyle.secondary, emoji="❌")
    async def leave(self, interaction: Interaction, button: Button):
        try:
            shift = active_shifts.get(self.shift_id)
            if not shift or shift["ended"]:
                await interaction.response.send_message("❌ This shift has already ended.", ephemeral=True)
                return

            user = interaction.user
            attendee = shift["attendees"].get(user.id)
            if attendee is None:
                await interaction.response.send_message("❌ You were not part of this shift.", ephemeral=True)
                return

            now = datetime.datetime.utcnow()
            if user.id in grace_periods:
                info = grace_periods[user.id]
                task = info.get("task")
                if task:
                    try:
                        task.cancel()
                    except Exception:
                        pass
                del grace_periods[user.id]

            if attendee["sessions"] and attendee["sessions"][-1][1] is None:
                attendee["sessions"][-1] = (attendee["sessions"][-1][0], now)
            attendee["leave"] = now

            await update_embed(shift, self.bot)
            await interaction.response.send_message("❌ You left the shift.", ephemeral=True)
        except Exception:
            traceback.print_exc()
            await interaction.response.send_message("❌ Error while leaving shift. Try again later.", ephemeral=True)

    @discord.ui.button(label="Finish", style=discord.ButtonStyle.danger, emoji="⛔", custom_id=None)
    async def finish(self, interaction: Interaction, button: Button):
        shift = active_shifts.get(self.shift_id)
        try:
            if not shift:
                await interaction.response.send_message("❌ This shift was not found.", ephemeral=True)
                return
            user = interaction.user
            if not isinstance(user, discord.Member):
                await interaction.response.send_message("❌ Error identifying member.", ephemeral=True)
                return
            if not self.has_permission(user, shift):
                await interaction.response.send_message("❌ No permission to end this shift.", ephemeral=True)
                return
            if shift.get("ended", False):
                await interaction.response.send_message("⛔ This shift is already ended.", ephemeral=True)
                try:
                    if hasattr(interaction, "message") and can_edit_message(interaction.message):
                        await interaction.message.edit(view=None)
                except Exception as e:
                    print(f"[WARN] Failed to remove shift view after already ended: {e}")
                return

            # --- CRITICAL DEFENSIVE BLOCK: Clean up all grace periods for this shift ---
            for user_id, info in list(grace_periods.items()):
                if info["shift_id"] == shift["shift_id"]:
                    attendee = shift["attendees"].get(user_id)
                    left_at = info.get("left_at", shift.get("end_time") or datetime.datetime.utcnow())
                    if attendee:
                        if attendee["sessions"] and (attendee["sessions"][-1][1] is None):
                            attendee["sessions"][-1] = (attendee["sessions"][-1][0], left_at)
                        if not attendee.get("leave") or (attendee.get("leave") and left_at < attendee["leave"]):
                            attendee["leave"] = left_at
                    tsk = info.get("task")
                    if tsk:
                        try:
                            tsk.cancel()
                        except Exception:
                            pass
                    del grace_periods[user_id]

            await end_shift(shift, self.bot)
            await interaction.response.send_message("⛔ Shift finished! Attendance and results have been calculated.", ephemeral=True)
        except Exception as e:
            print(f"[ERROR] finish button exception: {e}")
            traceback.print_exc()
            try:
                await interaction.response.send_message(f"❌ Error ending shift: {e}", ephemeral=True)
            except Exception as e2:
                print(f"[ERROR] Also failed sending error: {e2}")

    @discord.ui.button(label="Edit", style=discord.ButtonStyle.primary, emoji="🛠️")
    async def edit(self, interaction: Interaction, button: Button):
        shift = active_shifts.get(self.shift_id)
        try:
            if not shift or shift["ended"]:
                await interaction.response.send_message("❌ This shift has already ended.", ephemeral=True)
                return
            user = interaction.user
            if not isinstance(user, discord.Member):
                await interaction.response.send_message("❌ Error identifying member.", ephemeral=True)
                return
            if not self.has_permission(user, shift):
                await interaction.response.send_message("❌ No permission to edit this shift.", ephemeral=True)
                return

            options = []
            for uid in shift["attendees"]:
                member = interaction.guild.get_member(uid)
                name = member.display_name if member else f"User {uid}"
                options.append(discord.SelectOption(
                    label=name,
                    value=str(uid),
                    description="Remove from shift"
                ))

            if not options:
                await interaction.response.send_message("❌ No participants to edit.", ephemeral=True)
                return

            select = Select(
                placeholder="Select a member to remove...",
                options=options[:25]
            )

            async def select_callback(select_interaction: Interaction):
                if not self.has_permission(select_interaction.user, shift):
                    await select_interaction.response.send_message("❌ No permission.", ephemeral=True)
                    return

                removed_id = int(select.values[0])
                if removed_id in shift["attendees"]:
                    del shift["attendees"][removed_id]
                    await update_embed(shift, self.bot)
                    await select_interaction.response.send_message(f"✅ Removed <@{removed_id}> from the shift.", ephemeral=True)
                else:
                    await select_interaction.response.send_message("❌ Member not found in shift.", ephemeral=True)

            select.callback = select_callback
            new_view = View()
            new_view.add_item(select)
            await interaction.response.send_message(
                "Select who to remove from the shift:",
                view=new_view,
                ephemeral=True
            )
        except Exception:
            traceback.print_exc()
            await interaction.response.send_message("❌ Error. Could not edit shift participants.", ephemeral=True)

    @discord.ui.button(label="Delete", style=discord.ButtonStyle.danger, emoji="🗑️")
    async def delete(self, interaction: Interaction, button: Button):
        shift = active_shifts.get(self.shift_id)
        try:
            if not shift:
                await interaction.response.send_message("❌ Shift not found.", ephemeral=True)
                return
            user = interaction.user
            if not isinstance(user, discord.Member):
                await interaction.response.send_message("❌ Error identifying member.", ephemeral=True)
                return
            if not self.has_permission(user, shift):
                await interaction.response.send_message("❌ No permission to delete this shift.", ephemeral=True)
                return
            if self.shift_id in active_shifts:
                del active_shifts[self.shift_id]
            try:
                if hasattr(interaction, "message") and can_edit_message(interaction.message):
                    await interaction.message.delete()
            except Exception as e:
                print(f"[WARN] Could not delete shift message: {e}")
            await interaction.response.send_message("🗑️ Shift deleted.", ephemeral=True)
        except Exception:
            traceback.print_exc()
            await interaction.response.send_message("❌ Error. Could not delete the shift.", ephemeral=True)

# =========================
# EMBED/SHIFT UPDATE
# =========================
async def update_embed(shift, bot):
    if not shift.get("message"):
        return

    embed = shift["embed"]

    try:
        if shift.get("ended"):
            elapsed = shift.get("end_time", datetime.datetime.utcnow()) - shift["start"]
        else:
            elapsed = datetime.datetime.utcnow() - shift["start"]
        time_str = format_time_delta(elapsed)
        duration_minutes = elapsed.total_seconds() / 60 if elapsed.total_seconds() > 0 else 1

        if shift.get("ended"):
            embed.color = discord.Color.red()
            embed.title = f"🔴 {shift['title']}"
            embed.description = "**Shift Ended**"
        else:
            embed.color = discord.Color.green()
            embed.title = f"🟢 {shift['title']}"

        ensure_embed_fields(embed, shift, bot)

        try:
            embed.set_field_at(
                index=1,
                name="⏱️ Elapsed Time",
                value=time_str,
                inline=True
            )
        except Exception:
            ensure_embed_fields(embed, shift, bot)

        attendees_list = []
        shift_start = shift["start"]
        shift_end = shift.get("end_time", datetime.datetime.utcnow()) if shift.get("ended") else datetime.datetime.utcnow()
        for uid, attendee in shift["attendees"].items():
            # Try to get Member, fallback to User
            member = safe_get_user(bot, uid)
            if member:
                name = getattr(member, "display_name", getattr(member, "name", f"User {uid}"))
            else:
                name = f"User {uid}"

            if not shift.get("ended"):
                # Show red if attendee.get('leave') is set (button or voice left) or in grace_period
                in_grace = (uid in grace_periods) and (grace_periods[uid]["shift_id"] == shift["shift_id"])
                if in_grace or attendee.get("leave") is not None:
                    status_emoji = "🔴"
                    leave_cap = attendee.get("leave", shift_end)
                    sessions = attendee.get("sessions", [])
                    total_present_seconds = 0
                    for session_start, session_end in sessions:
                        ses_start = max(session_start, shift_start)
                        ses_end = session_end if session_end else leave_cap
                        ses_end = min(ses_end, leave_cap)
                        if ses_start > ses_end:
                            continue
                        total_present_seconds += max(0, (ses_end - ses_start).total_seconds())
                    total_present_minutes = total_present_seconds / 60
                    hours = int(total_present_minutes // 60)
                    minutes = int(total_present_minutes % 60)
                    if hours > 0:
                        time_present = f"{hours}h {minutes}m"
                    else:
                        time_present = f"{minutes}m"
                    attendees_list.append(f"{status_emoji} {name} ({time_present})")
                else:
                    status_emoji = "🟢"
                    sessions = attendee.get("sessions", [])
                    total_present_seconds = 0
                    for session_start, session_end in sessions:
                        ses_start = max(session_start, shift_start)
                        ses_end = session_end if session_end else shift_end
                        ses_end = min(ses_end, shift_end)
                        if ses_start > ses_end:
                            continue
                        total_present_seconds += max(0, (ses_end - ses_start).total_seconds())
                    total_present_minutes = total_present_seconds / 60
                    hours = int(total_present_minutes // 60)
                    minutes = int(total_present_minutes % 60)
                    if hours > 0:
                        time_present = f"{hours}h {minutes}m"
                    else:
                        time_present = f"{minutes}m"
                    attendees_list.append(f"{status_emoji} {name} ({time_present})")
            else:
                sessions = attendee.get("sessions", [])
                attendance = calculate_attendance_from_sessions(sessions, shift_start, shift_end)
                if attendance >= shift["min_attendance"]:
                    attendees_list.append(f"✅ {name} ({attendance*100:.0f}%)")
                else:
                    attendees_list.append(f"❌ {name} ({attendance*100:.0f}%)")

        attendees_text = "\n".join(attendees_list) if attendees_list else "—"
        try:
            embed.set_field_at(
                index=4,
                name=f"👥 Present ({len(shift['attendees'])})",
                value=attendees_text[:1024] if len(attendees_text) <= 1024 else attendees_text[:1021] + "...",
                inline=False
            )
        except Exception:
            try:
                embed.add_field(
                    name=f"👥 Present ({len(shift['attendees'])})",
                    value=attendees_text[:1024] if len(attendees_text) <= 1024 else attendees_text[:1021] + "...",
                    inline=False
                )
            except Exception as e:
                print(f"[WARN] Could not update embed attendance field: {e}")

        if shift.get("ended"):
            embed.set_footer(text="Shift Ended")
        else:
            embed.set_footer(text="Click ✅ Join to register for the shift")

        msg = shift.get("message")

        try:
            if msg and can_edit_message(msg):
                await msg.edit(embed=embed)
            else:
                print(f"[WARN] Can't edit message for shift {shift.get('title','')} (no permissions?) - skipping update")
        except Exception as e:
            print(f"[WARN] Failed to update shift embed: {e}")
            traceback.print_exc()

    except Exception as e:
        print(f"[WARN] update_embed global problem: {e}")
        traceback.print_exc()

# =========================
# SHIFT END
# =========================
async def end_shift(shift, bot):
    try:
        if shift.get("ended", False):
            return
        shift["ended"] = True
        shift["end_time"] = datetime.datetime.utcnow()
        for user_id in list(grace_periods.keys()):
            if grace_periods[user_id]["shift_id"] == shift["shift_id"]:
                attendee = shift["attendees"].get(user_id)
                left_at = grace_periods[user_id].get("left_at", shift["end_time"])
                if attendee:
                    if attendee["sessions"] and attendee["sessions"][-1][1] is None:
                        attendee["sessions"][-1] = (attendee["sessions"][-1][0], left_at)
                    if not attendee.get("leave") or (attendee.get("leave") and left_at < attendee["leave"]):
                        attendee["leave"] = left_at
                try:
                    grace_periods[user_id]["task"].cancel()
                except Exception:
                    pass
                del grace_periods[user_id]
        for uid, attendee in shift["attendees"].items():
            if attendee["sessions"] and attendee["sessions"][-1][1] is None:
                leave_at = attendee.get("leave") or shift["end_time"]
                session_end = min(leave_at, shift["end_time"])
                attendee["sessions"][-1] = (attendee["sessions"][-1][0], session_end)
            if not attendee.get("leave"):
                attendee["leave"] = shift["end_time"]
        await update_embed(shift, bot)
        try:
            msg = shift.get("message")
            if msg and can_edit_message(msg):
                await msg.edit(view=None)
        except Exception as e:
            print(f"[WARN] Failed to remove shift view: {e}")
            traceback.print_exc()
        await send_shift_log(shift, bot)
    except Exception as e:
        print(f"[ERROR] end_shift exception: {e}")
        traceback.print_exc()

# =========================
# SHIFT LOG
# =========================
async def send_shift_log(shift, bot):
    embed = discord.Embed(
        title=f"📊 Shift Results: {shift['title']}",
        color=discord.Color.blue(),
        timestamp=shift["end_time"]
    )
    host = safe_get_user(bot, shift["host"])
    embed.add_field(name="👤 Host", value=host.mention if getattr(host,"mention",None) else f"<@{shift['host']}>", inline=True)
    actual_duration = shift["end_time"] - shift["start"]
    duration_str = format_time_delta(actual_duration)
    duration_minutes = actual_duration.total_seconds() / 60
    actual_duration = max(actual_duration, datetime.timedelta(seconds=1))
    actual_duration_minutes = duration_minutes if duration_minutes > 0 else 1

    embed.add_field(
        name="⏱️ Duration",
        value=f"{duration_str} ({duration_minutes:.1f} min)",
        inline=True
    )

    passed = []
    failed = []
    shift_start = shift["start"]
    shift_end = shift["end_time"]

    for uid, attendee in shift["attendees"].items():
        member = safe_get_user(bot, uid)
        name = getattr(member, "display_name", getattr(member,"name", f"User {uid}")) if member else f"User {uid}"
        sessions = attendee.get("sessions", [])
        attendance = calculate_attendance_from_sessions(sessions, shift_start, shift_end)
        if attendance >= shift["min_attendance"]:
            passed.append(f"✅ {name}: {attendance*100:.0f}%")
        else:
            failed.append(f"❌ {name}: {attendance*100:.0f}%")
    if passed:
        embed.add_field(
            name=f"✅ Passed ({len(passed)})",
            value="\n".join(passed[:20]) if len(passed) <= 20 else "\n".join(passed[:20]) + f"\n... and {len(passed)-20} more",
            inline=False
        )
    if failed:
        embed.add_field(
            name=f"❌ Failed ({len(failed)})",
            value="\n".join(failed[:20]) if len(failed) <= 20 else "\n".join(failed[:20]) + f"\n... and {len(failed)-20} more",
            inline=False
        )
    print(f"\n{'='*50}")
    print(f"SHIFT LOG: {shift['title']}")
    print(f"Host: {host}")
    print(f"Duration: {duration_str}")
    print(f"Passed: {len(passed)}, Failed: {len(failed)}")
    print(f"{'='*50}\n")
    # Try to send log as a followup if present
    try:
        msg = shift.get("message")
        if msg and hasattr(msg, "channel"):
            await msg.channel.send(embed=embed)
    except Exception:
        traceback.print_exc()

# =========================
# GRACE PERIOD TASK
# =========================
async def grace_period_task(user_id, shift_id, bot):
    try:
        await asyncio.sleep(300)
        info = grace_periods.get(user_id)
        if not info or info["shift_id"] != shift_id:
            return
        shift = active_shifts.get(shift_id)
        if not shift or shift.get("ended",False):
            if user_id in grace_periods:
                del grace_periods[user_id]
            return
        user = safe_get_user(bot, user_id)
        left_at = info.get("left_at") or datetime.datetime.utcnow()
        guild = bot.get_guild(shift["guild_id"])
        # Defensive: making sure user didn't return
        still_left = True
        try:
            if guild:
                member = guild.get_member(user_id)
                if member and member.voice and member.voice.channel and member.voice.channel.id == shift["voice"]:
                    still_left = False
        except Exception:
            pass
        if not still_left:
            if user_id in grace_periods:
                del grace_periods[user_id]
            attendee = shift["attendees"].get(user_id)
            if attendee is not None:
                attendee["leave"] = None
                if attendee["sessions"]:
                    if attendee["sessions"][-1][1] is not None:
                        attendee["sessions"].append((datetime.datetime.utcnow(), None))
                else:
                    attendee["sessions"] = [(datetime.datetime.utcnow(), None)]
            await update_embed(shift, bot)
            return
        if shift and user_id in shift["attendees"]:
            attendee = shift["attendees"][user_id]
            if attendee["sessions"] and attendee["sessions"][-1][1] is None:
                attendee["sessions"][-1] = (attendee["sessions"][-1][0], left_at)
            if not attendee.get("leave") or left_at < attendee["leave"]:
                attendee["leave"] = left_at
            try:
                if user:
                    await user.send(
                        f"⚠️ You left the voice channel for {shift['title']} and did not return within 5 minutes. "
                        f"Your attendance has been recorded."
                    )
            except Exception as e:
                print(f"[WARN] Could not notify user {user_id} about grace period: {e}")
                traceback.print_exc()
            await update_embed(shift, bot)
        if user_id in grace_periods:
            del grace_periods[user_id]
    except asyncio.CancelledError:
        pass
    except Exception as e:
        print(f"[WARN] grace_period_task failed: {e}")
        traceback.print_exc()

async def setup(bot):
    await bot.add_cog(ClockInCreate(bot))
    print("\n✓ Loaded extension: commands.clockincreate")
    tree = getattr(bot, "tree", None)
    if tree:
        registered = [cmd.name for cmd in tree.get_commands(type=discord.AppCommandType.chat_input)]
        if "clockincreate" in registered:
            print("\n[Tree] Registered commands in app_commands tree:\n")
            print("  /clockincreate - Create a clock-in shift (only members with brotato role)")