import discord
from discord.ext import commands
import logging
from dotenv import load_dotenv
import os
import asyncio
import sys

# Load environment variables and bot token
dotenv_loaded = load_dotenv()  # <--- Apenas chama .env sem caminho explícito

# Tenta obter o token de 'TOKEN', se não, tenta 'DISCORD_TOKEN'
token = os.environ.get("TOKEN")
if token is None or len(str(token).strip()) == 0:
    # Diagnóstico extra para casos onde existe 'DISCORD_TOKEN' e não 'TOKEN'
    alt_token = os.environ.get("DISCORD_TOKEN")
    if alt_token is not None and len(str(alt_token).strip()) > 0:
        print("[WARN] Variável de ambiente 'TOKEN' não definida, mas 'DISCORD_TOKEN' foi encontrada.")
        print("[INFO] Usando 'DISCORD_TOKEN' para iniciar o bot.")
        token = alt_token
    else:
        print("[ERROR] No TOKEN found in environment!")
        print("Diagnostic details:")
        print(f"  Did .env load? {dotenv_loaded}")
        print(f"  Current working directory: {os.getcwd()}")
        print(f"  .env exists: {os.path.exists('.env')}")
        print("  Environment variable keys:", list(os.environ.keys()))
        print("\n[HELP] Certifique-se que existe um ficheiro .env no mesmo diretório que main.py e contém a linha:")
        print("TOKEN=seu_token_aqui")
        print("\n[DEBUG] Se estiver numa plataforma que renomeia a variável (ex: Railway, Render, alguns deploys no VSCode), use DISCORD_TOKEN em vez de TOKEN.")
        sys.exit(1)

intents = discord.Intents.default()
intents.message_content = True
intents.members = True
bot = commands.Bot(command_prefix="!", intents=intents)

# Carregar só clockincreate.py de forma segura e mostrar prints honestos:
@bot.event
async def on_connect(): 
    # Tenta carregar só a extensão clockincreate (apenas se existir)
    extension = "commands.clockincreate"
    try:
        await bot.load_extension(extension)
        print(f"✓ Loaded extension: {extension}")
    except commands.ExtensionAlreadyLoaded:
        print(f'⚠ Extension already loaded: {extension}')
    except commands.ExtensionNotFound:
        print(f'✗ Extension not found: {extension}')
    except commands.NoEntryPointError:
        print(f'✗ No setup() entry point in: {extension}')
    except Exception as e:
        print(f'✗ Failed to load {extension}: {type(e).__name__}: {e}')

    # Synca e mostra os slash commands registados
    await asyncio.sleep(1)  # Permitir o registo dos comandos
    tree_commands = bot.tree.get_commands()
    if tree_commands:
        print('\n[Tree] Registered commands in app_commands tree:')
        for c in tree_commands:
            print(f'  /{c.name} - {c.description}')
    else:
        print('\n[Tree] Registered commands in app_commands tree:')
        print('  [nenhum slash command registado]')
    print('\n[Tree] Syncing application commands...')
    try:
        await bot.tree.sync()
    except Exception as e:
        print(f'[WARN] Failed syncing slash commands: {e}')
    if not tree_commands:
        print('[WARN] No slash commands found: are they registered in your Cogs?')

@bot.event
async def on_ready():
    print(f'Bot is online as {bot.user} (ID: {bot.user.id})')
    print("Guilds:", [guild.name for guild in bot.guilds])

@bot.event
async def on_message(message):
    if message.author.bot:
        return
    # Block the number 67
    if '67' in message.content.lower():
        await message.delete()
        await message.channel.send(f'{message.author.mention}, please do not say the number 67 in this server.')
    await bot.process_commands(message)

# -- CLEAR SLASH COMMANDS ON SHUTDOWN --
async def clear_commands_on_shutdown():
    """Remove all slash (app) commands for all guilds and globally before bot closes."""
    try:
        print('\n[Shutdown] Clearing app_commands before shutdown...')
        if bot.guilds:
            for guild in bot.guilds:
                try:
                    bot.tree.clear_commands(guild=guild)
                    await asyncio.wait_for(bot.tree.sync(guild=guild), timeout=3.0)
                    print(f'✓ Cleared commands for guild: {guild.name}')
                except asyncio.TimeoutError:
                    print(f'⚠ Timeout clearing commands for guild: {guild.name}')
                except Exception as e:
                    print(f'✗ Failed clearing for guild {guild.name}: {e}')
        # Global
        try:
            bot.tree.clear_commands(guild=None)
            await asyncio.wait_for(bot.tree.sync(), timeout=3.0)
            print('✓ Cleared global commands')
        except asyncio.TimeoutError:
            print('⚠ Timeout clearing global commands')
        except Exception as e:
            print(f'✗ Failed clearing global commands: {e}')
        print('[Shutdown] Done clearing commands')
    except Exception as e:
        print(f'[Shutdown] Error clearing commands: {e}')

# Monkeypatch .close to clear slash commands on shutdown (if possible)
_original_close = bot.close
async def close_and_clear():
    try:
        await clear_commands_on_shutdown()
    except Exception as e:
        print(f'Error during clear_commands_on_shutdown: {e}')
    finally:
        await _original_close()
bot.close = close_and_clear

bot.run(token)