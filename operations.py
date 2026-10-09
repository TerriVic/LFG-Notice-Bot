"""Process-local aggregate metrics and shared command error reporting."""
import logging
import time
from collections import Counter

import discord
from discord import app_commands
from discord.ext import commands

logger = logging.getLogger("lfg.operations")


class OperationalStats:
    def __init__(self):
        self.started_at = time.monotonic()
        self.counters = Counter()

    def increment(self, name):
        self.counters[name] += 1

    def snapshot(self):
        return dict(self.counters)


stats = OperationalStats()


def log_command_error(kind, error, command=None):
    original = getattr(error, "original", error)
    logger.error(
        "%s command failed: %s", kind, command or "unknown",
        exc_info=(type(original), original, original.__traceback__),
    )


async def handle_app_command_error(interaction, error):
    if isinstance(error, app_commands.CommandOnCooldown):
        msg = f"⏳ Easy there! You can use this command again in **{round(error.retry_after)} seconds**."
    elif isinstance(error, app_commands.CheckFailure):
        msg = "⛔ You do not have permission to run this command here."
    else:
        stats.increment("command_errors")
        command = getattr(interaction, "command", None)
        log_command_error("Slash", error, getattr(command, "qualified_name", None))
        msg = "❌ An unexpected error occurred while running this command. The error has been logged. Please try again later."
    try:
        if interaction.response.is_done():
            await interaction.followup.send(msg, ephemeral=True)
        else:
            await interaction.response.send_message(msg, ephemeral=True)
    except discord.HTTPException:
        logger.warning("Could not deliver slash-command error response", exc_info=True)


async def handle_prefix_command_error(ctx, error):
    if isinstance(error, commands.CommandNotFound):
        return
    if isinstance(error, commands.CheckFailure):
        return
    if isinstance(error, commands.UserInputError):
        msg = "❌ Invalid command arguments. Check the owner command syntax in the README."
    else:
        stats.increment("command_errors")
        log_command_error("Prefix", error, getattr(ctx.command, "qualified_name", None))
        msg = "❌ An unexpected error occurred while running this command. The error has been logged."
    try:
        await ctx.send(msg)
    except discord.HTTPException:
        logger.warning("Could not deliver prefix-command error response", exc_info=True)
