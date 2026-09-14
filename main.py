import asyncio
import os
import sqlite3
from datetime import datetime

import discord
from discord import app_commands
from discord.ext import commands
from dotenv import load_dotenv

load_dotenv()
TOKEN = os.getenv("DISCORD_TOKEN")

DELETED_CACHE_TTL = 300  # sekund, po kterých zpráva v mezipaměti "vyprší"

# ZDE VYPLŇ ID KANÁLU (číslo)
HLASOVANI_KANAL_ID = 1549011167287050332  

intents = discord.Intents.default()
intents.message_content = True
intents.messages = True
intents.guilds = True
intents.members = True

bot = commands.Bot(command_prefix="!", intents=intents)

conn = sqlite3.connect("hlasovani.db")
cursor = conn.cursor()
cursor.execute("""
    CREATE TABLE IF NOT EXISTS historie_hlasovani (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        datum TEXT,
        tema TEXT,
        vitezna_moznost TEXT,
        skore INTEGER,
        hlasu_pro INTEGER,
        hlasu_proti INTEGER
    )
""")
conn.commit()

deleted_messages_cache = {}

# Jeden jediný globální stav hlasování pro celého bota
hlasovani = {
    "aktivni": False, 
    "tema": "", 
    "moznosti": [], 
    "autor_id": None, 
    "auto_ukoncit_task": None, 
    "spusteno": False
}

def spravny_kanal(interaction: discord.Interaction) -> bool:
    return interaction.channel_id == HLASOVANI_KANAL_ID

def je_zakladatel(interaction: discord.Interaction) -> bool:
    return interaction.user.id == hlasovani["autor_id"]


class RestoreView(discord.ui.View):
    """Tlačítka pod hláškou o smazané zprávě. Po TTL se sama zneplatní."""
    def __init__(self, message_id: int, original_author_id: int):
        super().__init__(timeout=DELETED_CACHE_TTL)
        self.message_id = message_id
        self.original_author_id = original_author_id
        self.status_message: discord.Message | None = None

    async def on_timeout(self):
        deleted_messages_cache.pop(self.message_id, None)
        for item in self.children:
            item.disabled = True
        if self.status_message:
            try:
                await self.status_message.edit(view=self)
            except discord.HTTPException:
                pass

    @discord.ui.button(label="Obnovit zprávu", style=discord.ButtonStyle.green)
    async def restore_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.original_author_id:
            await interaction.response.send_message("Zprávu může obnovit jen její autor.", ephemeral=True)
            return

        data = deleted_messages_cache.get(self.message_id)
        if not data:
            await interaction.response.send_message("Záloha už vypršela.", ephemeral=True)
            return

        embed = discord.Embed(title="Obnovená zpráva", description=data["content"], color=discord.Color.green())
        embed.set_author(name=data["author"].display_name, icon_url=data["author"].display_avatar.url)
        await interaction.channel.send(embed=embed)

        deleted_messages_cache.pop(self.message_id, None)
        self.stop()
        for item in self.children:
            item.disabled = True
        await interaction.response.edit_message(view=self)

    @discord.ui.button(label="Definitivně smazat", style=discord.ButtonStyle.red)
    async def delete_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.original_author_id:
            await interaction.response.send_message("Zálohu může smazat jen autor zprávy.", ephemeral=True)
            return

        deleted_messages_cache.pop(self.message_id, None)
        self.stop()
        for item in self.children:
            item.disabled = True
        await interaction.response.edit_message(view=self)
        await interaction.followup.send("Zpráva trvale smazána z mezipaměti.", ephemeral=True)


@bot.event
async def on_message_delete(message: discord.Message):
    if message.author.bot or not message.guild:
        return

    deleter_name = f"{message.author.mention} (pravděpodobně smazal sám autor)"
    await asyncio.sleep(1)
    try:
        async for entry in message.guild.audit_logs(limit=3, action=discord.AuditLogAction.message_delete):
            if entry.target.id == message.author.id and (discord.utils.utcnow() - entry.created_at).total_seconds() < 5:
                deleter_name = entry.user.mention
                break
    except discord.Forbidden:
        pass

    deleted_messages_cache[message.id] = {
        "author": message.author,
        "content": message.content,
        "deleter": deleter_name,
    }

    embed = discord.Embed(title="Zpráva byla smazána", color=discord.Color.gold())
    embed.add_field(name="Autor", value=message.author.mention, inline=True)
    embed.add_field(name="Smazal", value=deleter_name, inline=True)
    embed.set_footer(text=f"Záloha vyprší za {DELETED_CACHE_TTL // 60} min")

    view = RestoreView(message_id=message.id, original_author_id=message.author.id)
    view.status_message = await message.channel.send(embed=embed, view=view)


class MoznostModal(discord.ui.Modal, title="Přidat možnost"):
    napad = discord.ui.TextInput(label="Tvoje možnost", style=discord.TextStyle.paragraph, max_length=1000)

    async def on_submit(self, interaction: discord.Interaction):
        if not hlasovani["aktivni"]:
            await interaction.response.send_message("Sběr možností už neprobíhá.", ephemeral=True)
            return
        hlasovani["moznosti"].append({
            "text": self.napad.value,
            "autor_id": interaction.user.id,
            "hlasy": {"pro": set(), "proti": set(), "zdrzelse": set()},
        })
        await interaction.response.send_message("Možnost přidána.", ephemeral=True)


class SberMoznostiView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="Přidat možnost", style=discord.ButtonStyle.primary, emoji="➕")
    async def btn_moznost(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(MoznostModal())


class TajneHlasovaniView(discord.ui.View):
    def __init__(self, moznost_index: int):
        super().__init__(timeout=None)
        self.moznost_index = moznost_index

    def zaznamenej_hlas(self, user_id: int, volba: str):
        hlasy = hlasovani["moznosti"][self.moznost_index]["hlasy"]
        for klic in hlasy:
            hlasy[klic].discard(user_id)
        hlasy[volba].add(user_id)

    @discord.ui.button(emoji="🟩", label="Hlasovat pro", style=discord.ButtonStyle.success)
    async def btn_pro(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.zaznamenej_hlas(interaction.user.id, "pro")
        await interaction.response.send_message("Hlas PRO zaznamenán.", ephemeral=True)

    @discord.ui.button(emoji="➖", label="Zdržet se hlasování", style=discord.ButtonStyle.primary)
    async def btn_zdrzelse(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.zaznamenej_hlas(interaction.user.id, "zdrzelse")
        await interaction.response.send_message("Hlas ZDRŽEL SE zaznamenán.", ephemeral=True)

    @discord.ui.button(emoji="🟥", label="Hlasovat proti", style=discord.ButtonStyle.danger)
    async def btn_proti(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.zaznamenej_hlas(interaction.user.id, "proti")
        await interaction.response.send_message("Hlas PROTI zaznamenán.", ephemeral=True)


@bot.tree.command(name="hlasovani_1_zahajit", description="Zahájí hlasování — buď sběr možností od lidí, nebo rovnou tvoje vlastní řešení")
@app_commands.describe(
    tema="O čem se hlasuje",
    reseni="Nepovinné: vlastní řešení oddělená středníkem ';' — pokud vyplníš, přeskočí se sběr od lidí a jde se rovnou hlasovat",
    cas_dny="Za kolik dní se hlasování samo uzavře (nepovinné)",
)
async def hlasovani_zahajit(interaction: discord.Interaction, tema: str, reseni: str | None = None, cas_dny: float | None = None):
    if not spravny_kanal(interaction):
        await interaction.response.send_message(f"Hlasování se dá spustit jen v <#{HLASOVANI_KANAL_ID}>.", ephemeral=True)
        return
    if hlasovani["aktivni"]:
        await interaction.response.send_message("Už probíhá jiné hlasování, nejdřív ho ukonči nebo zruš.", ephemeral=True)
        return

    moznosti = []
    if reseni:
        moznosti = [
            {"text": r.strip(), "autor_id": interaction.user.id, "hlasy": {"pro": set(), "proti": set(), "zdrzelse": set()}}
            for r in reseni.split(";") if r.strip()
        ]

    hlasovani.update({
        "aktivni": True,
        "tema": tema,
        "moznosti": moznosti,
        "autor_id": interaction.user.id,
        "auto_ukoncit_task": None,
        "spusteno": bool(moznosti),
    })

    popis = f"**Téma:** {tema}\nZaložil: {interaction.user.mention}"
    if cas_dny:
        popis += f"\nAutomaticky se uzavře za {cas_dny:g} dní."
        hlasovani["auto_ukoncit_task"] = asyncio.create_task(auto_uzavri_hlasovani(interaction.channel, cas_dny))

    if moznosti:
        embed = discord.Embed(title="Hlasování spuštěno", description=popis, color=discord.Color.blue())
        await interaction.response.send_message(embed=embed)
        for index, moznost in enumerate(moznosti):
            m_embed = discord.Embed(title=f"Řešení č. {index + 1}", description=moznost["text"], color=discord.Color.gold())
            await interaction.channel.send(embed=m_embed, view=TajneHlasovaniView(index))
    else:
        embed = discord.Embed(title="Sběr možností", description=popis, color=discord.Color.blue())
        await interaction.response.send_message(embed=embed, view=SberMoznostiView())


@bot.tree.command(name="hlasovani_jednoduche", description="Rychlé hlasování o jedné problematice s řešením — jen pro/proti/zdržel se")
@app_commands.describe(tema="Problematika", reseni="Navrhované řešení", cas_dny="Za kolik dní se hlasování samo uzavře (nepovinné)")
async def hlasovani_jednoduche(interaction: discord.Interaction, tema: str, reseni: str, cas_dny: float | None = None):
    if not spravny_kanal(interaction):
        await interaction.response.send_message(f"Hlasování se dá spustit jen v <#{HLASOVANI_KANAL_ID}>.", ephemeral=True)
        return
    if hlasovani["aktivni"]:
        await interaction.response.send_message("Už probíhá jiné hlasování, nejdřív ho ukonči nebo zruš.", ephemeral=True)
        return

    hlasovani.update({
        "aktivni": True,
        "tema": tema,
        "moznosti": [{"text": reseni, "autor_id": interaction.user.id, "hlasy": {"pro": set(), "proti": set(), "zdrzelse": set()}}],
        "autor_id": interaction.user.id,
        "auto_ukoncit_task": None,
        "spusteno": True,
    })

    popis = f"**Problematika:** {tema}\n**Řešení:** {reseni}\nZaložil: {interaction.user.mention}"
    if cas_dny:
        popis += f"\nAutomaticky se uzavře za {cas_dny:g} dní."
        hlasovani["auto_ukoncit_task"] = asyncio.create_task(auto_uzavri_hlasovani(interaction.channel, cas_dny))

    embed = discord.Embed(title="Hlasování", description=popis, color=discord.Color.gold())
    await interaction.response.send_message(embed=embed, view=TajneHlasovaniView(0))


async def auto_uzavri_hlasovani(channel: discord.abc.Messageable, cas_dny: float):
    await asyncio.sleep(cas_dny * 86400)
    if not hlasovani["aktivni"]:
        return
    await channel.send("Čas vypršel, hlasování se automaticky uzavírá.")
    embed = sestav_vysledky_embed()
    zresetuj_hlasovani()
    await channel.send(embed=embed)


@bot.tree.command(name="hlasovani_2_spustit", description="Uzavře sběr a spustí tajné hlasování")
async def hlasovani_spustit(interaction: discord.Interaction):
    if not spravny_kanal(interaction):
        await interaction.response.send_message(f"Tenhle příkaz jde použít jen v <#{HLASOVANI_KANAL_ID}>.", ephemeral=True)
        return
    if not hlasovani["aktivni"]:
        await interaction.response.send_message("Momentálně neprobíhá žádné hlasování.", ephemeral=True)
        return
    if not je_zakladatel(interaction):
        await interaction.response.send_message("Jen zakladatel hlasování ho může posunout do další fáze.", ephemeral=True)
        return
    if hlasovani["spusteno"]:
        await interaction.response.send_message("Tohle hlasování už jednou spuštěné bylo.", ephemeral=True)
        return
    if not hlasovani["moznosti"]:
        await interaction.response.send_message("Zatím nepřišla žádná možnost.", ephemeral=True)
        return

    hlasovani["spusteno"] = True
    await interaction.response.send_message(f"**Hlasování spuštěno**\nTéma: {hlasovani['tema']}")
    for index, moznost in enumerate(hlasovani["moznosti"]):
        embed = discord.Embed(title=f"Možnost č. {index + 1}", description=moznost["text"], color=discord.Color.gold())
        await interaction.channel.send(embed=embed, view=TajneHlasovaniView(index))


def sestav_vysledky_embed() -> discord.Embed:
    embed = discord.Embed(title="Výsledky hlasování", description=f"**Téma:** {hlasovani['tema']}", color=discord.Color.green())
    vysledky = []
    for moznost in hlasovani["moznosti"]:
        pro = len(moznost["hlasy"]["pro"])
        proti = len(moznost["hlasy"]["proti"])
        zdrzel = len(moznost["hlasy"]["zdrzelse"])
        vysledky.append({"text": moznost["text"], "pro": pro, "proti": proti, "zdrzel": zdrzel, "skore": pro - proti})
    vysledky.sort(key=lambda x: x["skore"], reverse=True)

    nejvyssi_skore = vysledky[0]["skore"] if vysledky else None
    remizujici = [v for v in vysledky if v["skore"] == nejvyssi_skore] if vysledky else []
    je_remiza = len(remizujici) > 1 or (len(vysledky) == 1 and vysledky[0]["skore"] == 0)

    for i, v in enumerate(vysledky):
        if je_remiza and v["skore"] == nejvyssi_skore:
            znak = "🤝 Remíza"
        elif not je_remiza and i == 0:
            znak = "🏆 Vítěz"
        else:
            znak = f"{i + 1}. místo"
        embed.add_field(
            name=f"{znak} (skóre {v['skore']})",
            value=f"*{v['text']}*\n🟩 {v['pro']} | 🟥 {v['proti']} | ➖ {v['zdrzel']}",
            inline=False,
        )

    if je_remiza:
        if len(remizujici) == 1:
            zprava_remiza = f"Hlasy se vyrovnaly (pro = proti) u „{remizujici[0]['text']}“. Do historie se nic neukládá — chce to znovu probrat nebo dohlasovat."
        else:
            nazvy = ", ".join(f"„{v['text']}“" for v in remizujici)
            zprava_remiza = f"Remíza mezi: {nazvy}. Do historie se nic neukládá — nejspíš to chce dohlasovat mezi těmito možnostmi."
        embed.add_field(name="Co teď?", value=zprava_remiza, inline=False)
    elif vysledky:
        vitez = vysledky[0]
        cursor.execute(
            "INSERT INTO historie_hlasovani (datum, tema, vitezna_moznost, skore, hlasu_pro, hlasu_proti) VALUES (?, ?, ?, ?, ?, ?)",
            (datetime.now().strftime("%d.%m.%Y %H:%M"), hlasovani["tema"], vitez["text"], vitez["skore"], vitez["pro"], vitez["proti"]),
        )
        conn.commit()

    return embed


def zresetuj_hlasovani():
    task = hlasovani.get("auto_ukoncit_task")
    if task and not task.done():
        task.cancel()
    hlasovani.update({"aktivni": False, "tema": "", "moznosti": [], "autor_id": None, "auto_ukoncit_task": None, "spusteno": False})


@bot.tree.command(name="hlasovani_3_ukoncit", description="Ukončí hlasování a uloží výsledek")
async def hlasovani_ukoncit(interaction: discord.Interaction):
    if not spravny_kanal(interaction):
        await interaction.response.send_message(f"Tenhle příkaz jde použít jen v <#{HLASOVANI_KANAL_ID}>.", ephemeral=True)
        return
    if not hlasovani["aktivni"]:
        await interaction.response.send_message("Momentálně nic neprobíhá.", ephemeral=True)
        return
    if not je_zakladatel(interaction):
        await interaction.response.send_message("Jen zakladatel hlasování ho může ukončit.", ephemeral=True)
        return

    await interaction.response.defer()
    embed = sestav_vysledky_embed()
    zresetuj_hlasovani()
    await interaction.followup.send(embed=embed)


@bot.tree.command(name="hlasovani_zrusit", description="Zruší aktuální hlasování bez uložení výsledku")
async def hlasovani_zrusit(interaction: discord.Interaction):
    if not spravny_kanal(interaction):
        await interaction.response.send_message(f"Tenhle příkaz jde použít jen v <#{HLASOVANI_KANAL_ID}>.", ephemeral=True)
        return
    if not hlasovani["aktivni"]:
        await interaction.response.send_message("Momentálně nic neprobíhá.", ephemeral=True)
        return
    if not je_zakladatel(interaction):
        await interaction.response.send_message("Jen zakladatel hlasování ho může zrušit.", ephemeral=True)
        return

    tema = hlasovani["tema"]
    zresetuj_hlasovani()
    await interaction.response.send_message(f"Hlasování na téma **{tema}** bylo zrušeno, nic se neukládá.")


@bot.tree.command(name="hlasovani_historie", description="Zobrazí historii hlasování")
async def hlasovani_historie(interaction: discord.Interaction):
    if not spravny_kanal(interaction):
        await interaction.response.send_message(f"Tenhle příkaz jde použít jen v <#{HLASOVANI_KANAL_ID}>.", ephemeral=True)
        return
    cursor.execute("SELECT datum, tema, vitezna_moznost, skore FROM historie_hlasovani ORDER BY id DESC LIMIT 10")
    zaznamy = cursor.fetchall()
    if not zaznamy:
        await interaction.response.send_message("Historie je zatím prázdná.", ephemeral=True)
        return
    embed = discord.Embed(title="Historie hlasování", color=discord.Color.dark_gold())
    for datum, tema, vitez, skore in zaznamy:
        embed.add_field(name=f"{datum} · {tema}", value=f"Vyhrálo: *{vitez}* (skóre {skore})", inline=False)
    await interaction.response.send_message(embed=embed)


# --- IRL srazy ---
akce_storage: dict[int, dict] = {}

def sestav_akce_embed(data: dict, guild: discord.Guild) -> discord.Embed:
    embed = discord.Embed(title="Sraz", color=discord.Color.purple())
    embed.add_field(name="Kdy", value=data["kdy"], inline=True)
    embed.add_field(name="Místo srazu", value=data["misto_srazu"], inline=True)
    embed.add_field(name="Co", value=data["co"], inline=False)

    skupiny = {"ano": [], "ne": [], "nevim": []}
    for user_id, volba in data["odpovedi"].items():
        member = guild.get_member(user_id)
        jmeno = member.mention if member else f"<@{user_id}>"
        skupiny[volba].append(jmeno)

    embed.add_field(name=f"✅ Jde ({len(skupiny['ano'])})", value="\n".join(skupiny["ano"]) or "—", inline=True)
    embed.add_field(name=f"❌ Nejde ({len(skupiny['ne'])})", value="\n".join(skupiny["ne"]) or "—", inline=True)
    embed.add_field(name=f"❓ Neví ({len(skupiny['nevim'])})", value="\n".join(skupiny["nevim"]) or "—", inline=True)
    return embed


class AkceView(discord.ui.View):
    def __init__(self, message_id: int | None = None):
        super().__init__(timeout=None)
        self.message_id = message_id

    async def zaznamenej(self, interaction: discord.Interaction, volba: str):
        data = akce_storage.get(self.message_id)
        if not data:
            await interaction.response.send_message("Tento sraz už není evidovaný.", ephemeral=True)
            return
        data["odpovedi"][interaction.user.id] = volba
        embed = sestav_akce_embed(data, interaction.guild)
        await interaction.response.edit_message(embed=embed, view=self)

    @discord.ui.button(label="Zúčastním se", style=discord.ButtonStyle.green, emoji="✅")
    async def btn_ano(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.zaznamenej(interaction, "ano")

    @discord.ui.button(label="Nezúčastním se", style=discord.ButtonStyle.red, emoji="❌")
    async def btn_ne(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.zaznamenej(interaction, "ne")

    @discord.ui.button(label="Nevím", style=discord.ButtonStyle.gray, emoji="❓")
    async def btn_nevim(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.zaznamenej(interaction, "nevim")


class SrazModal(discord.ui.Modal, title="Naplánovat sraz"):
    kdy = discord.ui.TextInput(label="Kdy", placeholder="např. sobota 20. 9. od 17:00", max_length=200)
    misto_srazu = discord.ui.TextInput(label="Místo srazu", placeholder="např. u Honzy na chatě", max_length=200)
    co = discord.ui.TextInput(label="Co se bude dělat", style=discord.TextStyle.paragraph, max_length=500)

    async def on_submit(self, interaction: discord.Interaction):
        data = {
            "kdy": str(self.kdy),
            "misto_srazu": str(self.misto_srazu),
            "co": str(self.co),
            "autor_id": interaction.user.id,
            "odpovedi": {},
        }
        embed = sestav_akce_embed(data, interaction.guild)
        view = AkceView()
        await interaction.response.send_message(
            content="@everyone",
            embed=embed,
            view=view,
            allowed_mentions=discord.AllowedMentions(everyone=True),
        )
        zprava = await interaction.original_response()
        view.message_id = zprava.id
        akce_storage[zprava.id] = data


@bot.tree.command(name="sraz", description="Naplánuje IRL sraz a zeptá se všech, jestli dorazí")
async def sraz(interaction: discord.Interaction):
    await interaction.response.send_modal(SrazModal())

@bot.tree.command(name="clear", description="Smaže veškerý obsah tohoto kanálu (pouze pro majitele)")
async def clear_channel(interaction: discord.Interaction):
    # Kontrola, zda příkaz spouští oprávněný uživatel
    if interaction.user.id != 1548780627602444369:
        await interaction.response.send_message("❌ Na tento příkaz nemáš oprávnění.", ephemeral=True)
        return

    # Defer je důležitý, protože mazání velkého množství zpráv může trvat 
    # déle než 3 sekundy, což by jinak vyvolalo timeout chybu u interakce.
    await interaction.response.defer(ephemeral=True)

    try:
        # purge(limit=None) projde a smaže všechny dostupné zprávy v kanálu
        deleted = await interaction.channel.purge(limit=None)
        await interaction.followup.send(f"✅ Kanál byl vyčištěn. Smazáno zpráv: {len(deleted)}", ephemeral=True)
    except discord.Forbidden:
        await interaction.followup.send("❌ Nemám oprávnění mazat zprávy v tomto kanálu. Zkontroluj moje role.", ephemeral=True)
    except discord.HTTPException as e:
        await interaction.followup.send(f"❌ Došlo k chybě při mazání: {e}", ephemeral=True)


@bot.event
async def on_ready():
    await bot.tree.sync()
    print(f"Bot {bot.user} je online.")


if TOKEN is None:
    print("Token nebyl nalezen. Vytvoř soubor .env s proměnnou DISCORD_TOKEN.")
else:
    bot.run(TOKEN)