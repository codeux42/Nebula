from __future__ import annotations

import asyncio
import json
import logging
import os
import random
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import discord
from discord import app_commands
from dotenv import load_dotenv
from playwright.async_api import async_playwright

from site_monitor import CatalogSnapshot, Release, SeenStore, SiteMonitor


ROOT = Path(__file__).resolve().parent
STATE_PATH = ROOT / "state.sqlite3"
CHANNEL_ENV = {
    "new_films": "CHANNEL_NOUVEAUX_FILMS_ID",
    "new_series": "CHANNEL_NOUVELLES_SERIES_ID",
    "trends": "CHANNEL_TENDANCES_ID",
    "suggestions": "CHANNEL_SUGGESTIONS_ID",
    "popular_films": "CHANNEL_FILMS_POPULAIRES_ID",
    "popular_series": "CHANNEL_SERIES_POPULAIRES_ID",
    "weekly_top": "CHANNEL_TOP_HEBDOMADAIRE_ID",
    "status": "CHANNEL_STATUT_ID",
}
DAY_NAMES = {
    "mon": 0,
    "tue": 1,
    "wed": 2,
    "thu": 3,
    "fri": 4,
    "sat": 5,
    "sun": 6,
}
STATUS_LABELS = {
    "fonctionnel": ("🟢", "FONCTIONNEL", discord.Color.green()),
    "maintenance": ("🟠", "EN MAINTENANCE", discord.Color.orange()),
    "incident": ("🔴", "INCIDENT EN COURS", discord.Color.red()),
}


@dataclass(frozen=True, slots=True)
class Config:
    token: str
    channels: dict[str, int]
    site_url: str
    status_image_url: str
    poll_interval: int
    weekly_day: int
    weekly_hour: int
    timezone: ZoneInfo


def read_config() -> Config:
    load_dotenv(ROOT / ".env")
    token = os.getenv("DISCORD_BOT_TOKEN", "").strip()
    site_url = os.getenv("SITE_URL", "https://nebulafr.site/").strip()
    status_image_url = os.getenv("STATUS_IMAGE_URL", "").strip()
    poll_text = os.getenv("POLL_INTERVAL_SECONDS", "300").strip()
    day_text = os.getenv("WEEKLY_TOP_DAY", "mon").strip().casefold()
    hour_text = os.getenv("WEEKLY_TOP_HOUR", "10").strip()
    timezone_name = os.getenv("TIMEZONE", "Europe/Paris").strip()

    if not token:
        raise SystemExit("DISCORD_BOT_TOKEN manque dans le fichier .env.")
    if not site_url.startswith(("https://", "http://")):
        raise SystemExit("SITE_URL doit commencer par https:// ou http://.")
    if status_image_url and not status_image_url.startswith(("https://", "http://")):
        raise SystemExit("STATUS_IMAGE_URL doit être une URL http:// ou https://.")
    if not poll_text.isdecimal() or int(poll_text) < 60:
        raise SystemExit("POLL_INTERVAL_SECONDS doit être un nombre d'au moins 60.")
    if day_text not in DAY_NAMES:
        raise SystemExit("WEEKLY_TOP_DAY doit être mon, tue, wed, thu, fri, sat ou sun.")
    if not hour_text.isdecimal() or not 0 <= int(hour_text) <= 23:
        raise SystemExit("WEEKLY_TOP_HOUR doit être une heure entre 0 et 23.")
    try:
        timezone = ZoneInfo(timezone_name)
    except ZoneInfoNotFoundError as exc:
        raise SystemExit(
            f"Base de fuseaux horaires introuvable pour {timezone_name}. "
            "Installe les dépendances avec le même Python que celui utilisé pour lancer le bot : "
            "python -m pip install -r requirements.txt."
        ) from exc

    channels: dict[str, int] = {}
    missing: list[str] = []
    for feed, env_name in CHANNEL_ENV.items():
        channel_text = os.getenv(env_name, "").strip()
        if not channel_text.isdecimal():
            missing.append(env_name)
            continue
        channels[feed] = int(channel_text)
    if missing:
        raise SystemExit("Renseigne l'ID de chaque salon dans .env : " + ", ".join(missing))

    return Config(
        token=token,
        channels=channels,
        site_url=site_url,
        status_image_url=status_image_url,
        poll_interval=int(poll_text),
        weekly_day=DAY_NAMES[day_text],
        weekly_hour=int(hour_text),
        timezone=timezone,
    )


def make_release_view(release: Release, intro: str) -> discord.ui.LayoutView:
    kind = release.category
    details = f"**Catégorie :** {kind}"
    if release.meta:
        details += f"\n**Informations :** {discord.utils.escape_markdown(release.meta[:400])}"
    content = (
        f"# {discord.utils.escape_markdown(release.title[:200])}\n\n"
        f"{intro}\n\n{details}\n\n-# Nébula · Veille du catalogue"
    )
    view = discord.ui.LayoutView(timeout=None)
    container_items: list[discord.ui.Item] = [discord.ui.TextDisplay(content)]
    container_items.append(
        discord.ui.ActionRow(
            discord.ui.Button(
                label="Ouvrir la fiche",
                style=discord.ButtonStyle.link,
                url=release.url,
            )
        )
    )
    if release.image_url.startswith(("https://", "http://")):
        container_items.append(
            discord.ui.MediaGallery(
                discord.MediaGalleryItem(release.image_url, description=release.title[:256])
            )
        )
    view.add_item(
        discord.ui.Container(
            *container_items,
            accent_color=discord.Color.from_rgb(102, 91, 255),
        )
    )
    return view


def ranked_lines(releases: list[Release], limit: int = 10) -> str:
    lines = []
    for rank, release in enumerate(releases[:limit], start=1):
        title = release.title.replace("[", "\\[").replace("]", "\\]")
        suffix = f" — {discord.utils.escape_markdown(release.meta)}" if release.meta else ""
        lines.append(f"**{rank}.** [{title}]({release.url}){suffix}")
    return "\n".join(lines) or "Aucune fiche disponible pour le moment."


def make_ranking_view(
    title: str,
    releases: list[Release],
    site_url: str,
    limit: int = 10,
) -> discord.ui.LayoutView:
    lines = ranked_lines(releases, limit=limit)[:3150]
    content = f"# {title}\n\n{lines}\n\n-# Classement relevé sur Nébula"
    view = discord.ui.LayoutView(timeout=None)
    view.add_item(
        discord.ui.Container(
            discord.ui.TextDisplay(content),
            discord.ui.ActionRow(
                discord.ui.Button(
                    label="Explorer Nébula",
                    style=discord.ButtonStyle.link,
                    url=site_url,
                )
            ),
            accent_color=discord.Color.from_rgb(255, 174, 66),
        )
    )
    return view


def make_weekly_top_view(snapshot: CatalogSnapshot, week_label: str, site_url: str) -> discord.ui.LayoutView:
    films = ranked_lines(snapshot.popular_films, limit=5)[:1300]
    series = ranked_lines(snapshot.popular_series, limit=5)[:1300]
    content = (
        f"# 🏆 Top hebdomadaire · {week_label}\n\n"
        f"## 🎬 Films populaires\n{films}\n\n"
        f"## 📺 Séries populaires\n{series}\n\n"
        "-# Classement relevé sur Nébula"
    )
    view = discord.ui.LayoutView(timeout=None)
    view.add_item(
        discord.ui.Container(
            discord.ui.TextDisplay(content),
            discord.ui.ActionRow(
                discord.ui.Button(
                    label="Explorer Nébula",
                    style=discord.ButtonStyle.link,
                    url=site_url,
                )
            ),
            accent_color=discord.Color.from_rgb(255, 190, 55),
        )
    )
    return view


class GiveawayJoinButton(discord.ui.Button):
    def __init__(self, bot: NebulaBot, *, disabled: bool = False) -> None:
        super().__init__(
            label="Participer",
            emoji="🎉",
            style=discord.ButtonStyle.primary,
            custom_id="nebula:giveaway:join",
            disabled=disabled,
        )
        self.bot = bot

    async def callback(self, interaction: discord.Interaction) -> None:
        await self.bot.handle_giveaway_join(interaction)


def make_giveaway_view(
    bot: NebulaBot,
    text: str,
    *,
    ended: bool = False,
) -> discord.ui.LayoutView:
    view = discord.ui.LayoutView(timeout=None)
    button = GiveawayJoinButton(bot, disabled=ended)
    view.add_item(
        discord.ui.Container(
            discord.ui.TextDisplay(text),
            discord.ui.ActionRow(button),
            accent_color=discord.Color.from_rgb(155, 90, 255),
        )
    )
    return view


def make_status_view(
    status: dict[str, object],
    site_url: str,
    image_url: str,
    timezone: ZoneInfo,
) -> discord.ui.LayoutView:
    key = str(status["overall"])
    emoji, label, color = STATUS_LABELS.get(key, STATUS_LABELS["incident"])
    checked_at = datetime.fromisoformat(str(status["checked_at"])).astimezone(timezone)
    content = (
        "# 📊・STATUT NÉBULA\n\n"
        f"## {emoji} **SITE : {label}**\n\n"
        f"🌐 **Service principal :** {status['service']}\n"
        f"🔐 **Connexion :** {status['connection']}\n"
        f"🗄️ **Serveurs :** {status['servers']}\n\n"
        f"**Dernière vérification :** {checked_at:%d.%m.%Y à %H:%M}\n\n"
        "> Ce salon est mis à jour régulièrement"
    )
    view = discord.ui.LayoutView(timeout=None)
    parts: list[discord.ui.Item] = [
        discord.ui.TextDisplay(content),
        discord.ui.ActionRow(
            discord.ui.Button(
                label="Ouvrir le site",
                emoji="🌐",
                style=discord.ButtonStyle.link,
                url=site_url,
            )
        ),
    ]
    if image_url.startswith(("https://", "http://")):
        parts.append(
            discord.ui.MediaGallery(
                discord.MediaGalleryItem(image_url, description="Image du site Nébula")
            )
        )
    view.add_item(discord.ui.Container(*parts, accent_color=color))
    return view


class GiveawayPersistentView(discord.ui.LayoutView):
    """Persistent component registration; individual messages use the same custom_id."""

    def __init__(self, bot: NebulaBot) -> None:
        super().__init__(timeout=None)
        self.add_item(
            discord.ui.Container(
                discord.ui.TextDisplay("Giveaway Nébula"),
                discord.ui.ActionRow(GiveawayJoinButton(bot)),
            )
        )


class NebulaBot(discord.Client):
    def __init__(self, config: Config, monitor: SiteMonitor, store: SeenStore) -> None:
        super().__init__(intents=discord.Intents.default())
        self.tree = app_commands.CommandTree(self)
        self.config = config
        self.monitor = monitor
        self.store = store
        self.monitor_task: asyncio.Task[None] | None = None
        self.giveaway_task: asyncio.Task[None] | None = None
        self.channel_cache: dict[str, discord.abc.Messageable] = {}
        self.current_image_url = config.status_image_url

    async def setup_hook(self) -> None:
        self.tree.add_command(
            app_commands.Command(
                name="suggestion",
                description="Proposer un film, une série ou un animé",
                callback=self.submit_suggestion,
            )
        )
        self.tree.add_command(
            app_commands.Command(
                name="help",
                description="Afficher l’aide du bot Nébula",
                callback=self.show_help,
            )
        )
        self.tree.add_command(
            app_commands.Command(
                name="hello",
                description="Dire bonjour au bot Nébula",
                callback=self.say_hello,
            )
        )
        self.tree.add_command(
            app_commands.Command(
                name="giveaway",
                description="Créer un giveaway avec bouton de participation",
                callback=self.start_giveaway,
            )
        )
        self.tree.add_command(
            app_commands.Command(
                name="giveaway-fin",
                description="Terminer un giveaway et tirer les gagnants",
                callback=self.finish_giveaway_command,
            )
        )
        self.tree.add_command(
            app_commands.Command(
                name="statut-site",
                description="Mettre à jour le statut du site Nébula",
                callback=self.set_site_status,
            )
        )
        self.tree.add_command(
            app_commands.Command(
                name="republier",
                description="Republier les dernières fiches d’une catégorie",
                callback=self.republish_category,
            )
        )
        self.add_view(GiveawayPersistentView(self))
        try:
            status_channel = await self.fetch_channel(self.config.channels["status"])
            guild = getattr(status_channel, "guild", None)
            if guild is None:
                raise RuntimeError("Le salon de statut n’est pas rattaché à un serveur Discord.")
        except (discord.HTTPException, RuntimeError):
            logging.exception(
                "Impossible de cibler le serveur depuis CHANNEL_STATUT_ID ; synchronisation globale de secours."
            )
            synced = await self.tree.sync()
            logging.info(
                "Commandes synchronisées globalement : %s. Leur affichage peut prendre du temps.",
                ", ".join(command.name for command in synced),
            )
            return

        guild_ref = discord.Object(id=guild.id)
        self.tree.copy_global_to(guild=guild_ref)
        synced = await self.tree.sync(guild=guild_ref)

        # Les commandes globales précédemment synchronisées causaient des doublons.
        # On ne garde que leur copie propre à ce serveur, qui se met à jour immédiatement.
        self.tree.clear_commands(guild=None)
        await self.tree.sync()
        logging.info(
            "Commandes synchronisées sur le serveur %s (%s) : %s. Anciennes commandes globales supprimées.",
            getattr(guild, "name", "serveur"),
            guild.id,
            ", ".join(command.name for command in synced),
        )

    async def on_ready(self) -> None:
        logging.info("Connecté à Discord en tant que %s", self.user)
        if self.monitor_task is None or self.monitor_task.done():
            self.monitor_task = asyncio.create_task(self.monitor_loop())
        if self.giveaway_task is None or self.giveaway_task.done():
            self.giveaway_task = asyncio.create_task(self.giveaway_loop())

    async def get_target_channel(self, feed: str) -> discord.abc.Messageable:
        cached = self.channel_cache.get(feed)
        if cached is not None:
            return cached

        channel_id = self.config.channels[feed]
        channel = self.get_channel(channel_id)
        if channel is None:
            channel = await self.fetch_channel(channel_id)
        if not callable(getattr(channel, "send", None)):
            raise RuntimeError(f"L'ID configuré pour {feed} ne correspond pas à un salon où écrire.")
        self.channel_cache[feed] = channel  # type: ignore[assignment]
        return channel  # type: ignore[return-value]

    async def send_release(self, feed: str, release: Release) -> None:
        intros = {
            "new_films": "🎬 Nouveau film repéré dans les nouveautés de Nébula.",
            "new_series": "📺 Nouvelle série ou animé repéré sur Nébula.",
            "trends": "🔥 Nouveau titre dans les tendances de Nébula.",
            "popular_films": "⭐ Film du classement populaire de Nébula.",
            "popular_series": "⭐ Série du classement populaire de Nébula.",
        }
        channel = await self.get_target_channel(feed)
        await channel.send(
            view=make_release_view(release, intros[feed]),
            allowed_mentions=discord.AllowedMentions.none(),
        )

    async def send_ranking(self, feed: str, week_key: str, view: discord.ui.LayoutView) -> None:
        channel = await self.get_target_channel(feed)
        await channel.send(view=view, allowed_mentions=discord.AllowedMentions.none())
        self.store.mark_weekly_posted(feed, week_key)

    async def post_weekly_rankings(self, snapshot: CatalogSnapshot) -> None:
        now = datetime.now(self.config.timezone)
        if now.weekday() < self.config.weekly_day:
            return
        if now.weekday() == self.config.weekly_day and now.hour < self.config.weekly_hour:
            return

        iso_calendar = now.isocalendar()
        week_key = f"{iso_calendar.year}-W{iso_calendar.week:02d}"
        week_label = f"Semaine {iso_calendar.week:02d}"
        scheduled_feeds = (
            ("popular_films", "Top 10 des films populaires", snapshot.popular_films),
            ("popular_series", "Top 10 des séries populaires", snapshot.popular_series),
        )
        for feed, title, releases in scheduled_feeds:
            if not releases or self.store.weekly_posted(feed, week_key):
                continue
            try:
                await self.send_ranking(
                    feed,
                    week_key,
                    make_ranking_view(title, releases, self.config.site_url),
                )
                logging.info("Classement hebdomadaire publié dans %s", CHANNEL_ENV[feed])
            except (discord.Forbidden, discord.HTTPException, discord.NotFound):
                self.channel_cache.pop(feed, None)
                logging.exception("Impossible de publier le classement dans %s", CHANNEL_ENV[feed])

        feed = "weekly_top"
        if snapshot.popular_films or snapshot.popular_series:
            if not self.store.weekly_posted(feed, week_key):
                try:
                    await self.send_ranking(
                        feed,
                        week_key,
                        make_weekly_top_view(snapshot, week_label, self.config.site_url),
                    )
                    logging.info("Top hebdomadaire publié dans %s", CHANNEL_ENV[feed])
                except (discord.Forbidden, discord.HTTPException, discord.NotFound):
                    self.channel_cache.pop(feed, None)
                    logging.exception("Impossible de publier le top hebdomadaire")

    async def update_status_message(self) -> None:
        status = self.store.get_site_status()
        channel = await self.get_target_channel("status")
        view = make_status_view(
            status,
            self.config.site_url,
            self.current_image_url,
            self.config.timezone,
        )
        message_id = status.get("message_id")
        if isinstance(message_id, int):
            try:
                message = await channel.fetch_message(message_id)  # type: ignore[attr-defined]
            except discord.NotFound:
                message = None
            if message is not None:
                await message.edit(view=view, allowed_mentions=discord.AllowedMentions.none())
                return

        message = await channel.send(view=view, allowed_mentions=discord.AllowedMentions.none())
        self.store.set_status_message_id(message.id)

    async def update_status_timestamp(self) -> None:
        status = self.store.get_site_status()
        self.store.update_site_status(
            str(status["overall"]),
            checked_at=datetime.now(timezone.utc).isoformat(),
        )
        try:
            await self.update_status_message()
        except (discord.Forbidden, discord.HTTPException, discord.NotFound, RuntimeError):
            self.channel_cache.pop("status", None)
            logging.exception("Impossible de mettre à jour le salon du statut du site")

    async def monitor_loop(self) -> None:
        await self.wait_until_ready()
        streams = {
            "new_films": lambda snapshot: snapshot.new_films,
            "new_series": lambda snapshot: snapshot.new_series,
            "trends": lambda snapshot: snapshot.trends,
        }

        while not self.is_closed():
            try:
                snapshot = await self.monitor.scan()
                for group in (snapshot.trends, snapshot.new_films, snapshot.new_series):
                    image = next((item.image_url for item in group if item.image_url.startswith(("https://", "http://"))), "")
                    if image:
                        if not self.config.status_image_url:
                            self.current_image_url = image
                        break

                for feed, get_releases in streams.items():
                    releases = get_releases(snapshot)
                    if not self.store.feed_initialized(feed):
                        self.store.initialize_feed(feed, releases)
                        logging.info("Premier relevé enregistré pour %s (%s fiches).", feed, len(releases))
                        continue

                    for release in releases:
                        if self.store.contains(feed, release.key):
                            continue
                        try:
                            await self.send_release(feed, release)
                        except (discord.Forbidden, discord.HTTPException, discord.NotFound):
                            self.channel_cache.pop(feed, None)
                            logging.exception("Impossible d'annoncer %s dans %s", release.title, feed)
                            continue
                        self.store.remember(feed, release)
                        logging.info("Annonce envoyée dans %s : %s", feed, release.title)

                await self.post_weekly_rankings(snapshot)
            except Exception:
                logging.exception("Échec du relevé ; nouvelle tentative dans %s secondes.", self.config.poll_interval)
            finally:
                await self.update_status_timestamp()

            await asyncio.sleep(self.config.poll_interval)

    async def giveaway_loop(self) -> None:
        await self.wait_until_ready()
        while not self.is_closed():
            try:
                await self.finish_expired_giveaways()
            except Exception:
                logging.exception("Impossible de traiter les giveaways arrivés à échéance")
            await asyncio.sleep(30)

    async def submit_suggestion(
        self,
        interaction: discord.Interaction,
        titre: app_commands.Range[str, 1, 200],
        lien: str = "",
    ) -> None:
        await interaction.response.defer(ephemeral=True)
        lien = lien.strip()
        if lien and not lien.startswith(("https://", "http://")):
            await interaction.followup.send(
                "Le lien doit commencer par https:// ou http://.",
                ephemeral=True,
            )
            return

        title = discord.utils.escape_markdown(titre[:200])
        content = f"# 💡 Nouvelle suggestion\n\n**{title}**\n\nProposée par **{discord.utils.escape_markdown(interaction.user.display_name)}**."
        view = discord.ui.LayoutView(timeout=None)
        parts: list[discord.ui.Item] = [discord.ui.TextDisplay(content)]
        if lien:
            parts.append(
                discord.ui.ActionRow(
                    discord.ui.Button(
                        label="Ouvrir le lien proposé",
                        style=discord.ButtonStyle.link,
                        url=lien,
                    )
                )
            )
        view.add_item(
            discord.ui.Container(
                *parts,
                accent_color=discord.Color.from_rgb(255, 205, 70),
            )
        )

        try:
            channel = await self.get_target_channel("suggestions")
            await channel.send(view=view, allowed_mentions=discord.AllowedMentions.none())
        except (discord.Forbidden, discord.HTTPException, discord.NotFound, RuntimeError):
            logging.exception("Impossible d'envoyer la suggestion de %s", interaction.user)
            await interaction.followup.send(
                "Je n’ai pas pu publier la suggestion. Vérifie la configuration du salon.",
                ephemeral=True,
            )
            return

        await interaction.followup.send("Suggestion envoyée !", ephemeral=True)

    async def show_help(self, interaction: discord.Interaction) -> None:
        content = (
            "# 🤖 Aide · Bot Nébula\n\n"
            "`/hello` · Dire bonjour au bot.\n"
            "`/suggestion` · Proposer un titre.\n"
            "`/giveaway` · Créer un tirage avec bouton de participation (administrateurs).\n"
            "`/giveaway-fin` · Terminer un tirage et tirer ses gagnants (administrateurs).\n"
            "`/republier` · Renvoyer les dernières fiches d’une catégorie (administrateurs).\n"
            "`/statut-site` · Afficher fonctionnel, maintenance ou incident (administrateurs).\n\n"
            "Les annonces de films, séries, tendances et classements sont publiées automatiquement."
        )
        view = discord.ui.LayoutView(timeout=120)
        view.add_item(
            discord.ui.Container(
                discord.ui.TextDisplay(content),
                discord.ui.ActionRow(
                    discord.ui.Button(
                        label="Ouvrir Nébula",
                        style=discord.ButtonStyle.link,
                        url=self.config.site_url,
                    )
                ),
                accent_color=discord.Color.from_rgb(102, 91, 255),
            )
        )
        await interaction.response.send_message(
            view=view,
            ephemeral=True,
            allowed_mentions=discord.AllowedMentions.none(),
        )

    async def say_hello(self, interaction: discord.Interaction) -> None:
        view = discord.ui.LayoutView(timeout=120)
        view.add_item(
            discord.ui.Container(
                discord.ui.TextDisplay(
                    "# 👋 Salut !\n\nJe suis le bot Nébula. Utilise `/help` pour voir mes commandes."
                ),
                discord.ui.ActionRow(
                    discord.ui.Button(
                        label="Ouvrir Nébula",
                        style=discord.ButtonStyle.link,
                        url=self.config.site_url,
                    )
                ),
                accent_color=discord.Color.from_rgb(102, 91, 255),
            )
        )
        await interaction.response.send_message(
            view=view,
            ephemeral=True,
            allowed_mentions=discord.AllowedMentions.none(),
        )

    async def republish_category(
        self,
        interaction: discord.Interaction,
        categorie: Literal[
            "films",
            "series",
            "tendances",
            "films-populaires",
            "series-populaires",
            "top-hebdomadaire",
        ],
        nombre: app_commands.Range[int, 1, 10] = 5,
    ) -> None:
        if not self.has_manage_guild(interaction):
            await interaction.response.send_message(
                "Cette commande est réservée aux membres qui peuvent gérer le serveur.",
                ephemeral=True,
            )
            return
        await interaction.response.defer(ephemeral=True)
        try:
            snapshot = await self.monitor.scan()
            if categorie == "films":
                feed, releases = "new_films", snapshot.new_films
                intro = "🎬 Film republié depuis les nouveautés de Nébula."
            elif categorie == "series":
                feed, releases = "new_series", snapshot.new_series
                intro = "📺 Série ou animé republié depuis Nébula."
            elif categorie == "tendances":
                feed, releases = "trends", snapshot.trends
                intro = "🔥 Titre republié depuis les tendances de Nébula."
            elif categorie == "films-populaires":
                releases = snapshot.popular_films
                if not releases:
                    await interaction.followup.send("Aucun classement disponible sur le site.", ephemeral=True)
                    return
                channel = await self.get_target_channel("popular_films")
                await channel.send(
                    view=make_ranking_view(
                        "Top des films populaires",
                        releases,
                        self.config.site_url,
                        limit=int(nombre),
                    ),
                    allowed_mentions=discord.AllowedMentions.none(),
                )
                await interaction.followup.send("Classement des films populaires republié.", ephemeral=True)
                return
            elif categorie == "series-populaires":
                releases = snapshot.popular_series
                if not releases:
                    await interaction.followup.send("Aucun classement disponible sur le site.", ephemeral=True)
                    return
                channel = await self.get_target_channel("popular_series")
                await channel.send(
                    view=make_ranking_view(
                        "Top des séries populaires",
                        releases,
                        self.config.site_url,
                        limit=int(nombre),
                    ),
                    allowed_mentions=discord.AllowedMentions.none(),
                )
                await interaction.followup.send("Classement des séries populaires republié.", ephemeral=True)
                return
            else:
                if not snapshot.popular_films and not snapshot.popular_series:
                    await interaction.followup.send("Aucun classement disponible sur le site.", ephemeral=True)
                    return
                channel = await self.get_target_channel("weekly_top")
                view = make_weekly_top_view(
                    snapshot,
                    f"Semaine {datetime.now(self.config.timezone).isocalendar().week:02d}",
                    self.config.site_url,
                )
                await channel.send(view=view, allowed_mentions=discord.AllowedMentions.none())
                await interaction.followup.send("Top hebdomadaire republié.", ephemeral=True)
                return

            if not releases:
                await interaction.followup.send("Aucun titre trouvé dans cette catégorie.", ephemeral=True)
                return

            channel = await self.get_target_channel(feed)
            count = 0
            for release in releases[:int(nombre)]:
                await channel.send(
                    view=make_release_view(release, intro),
                    allowed_mentions=discord.AllowedMentions.none(),
                )
                count += 1
            await interaction.followup.send(
                f"{count} fiche(s) republiée(s) dans le salon de cette catégorie.",
                ephemeral=True,
            )
        except Exception:
            logging.exception("Impossible de republier la catégorie %s", categorie)
            await interaction.followup.send(
                "Je n’ai pas pu republier ces fiches. Vérifie l’accès au site et au salon configuré.",
                ephemeral=True,
            )

    @staticmethod
    def has_manage_guild(interaction: discord.Interaction) -> bool:
        if not isinstance(interaction.user, discord.Member):
            return False
        permissions = interaction.user.guild_permissions
        return permissions.manage_guild or permissions.administrator

    async def start_giveaway(
        self,
        interaction: discord.Interaction,
        lot: app_commands.Range[str, 1, 200],
        duree_minutes: app_commands.Range[int, 5, 10080],
        nombre_gagnants: app_commands.Range[int, 1, 10] = 1,
    ) -> None:
        if not self.has_manage_guild(interaction):
            await interaction.response.send_message(
                "Cette commande est réservée aux membres qui peuvent gérer le serveur.",
                ephemeral=True,
            )
            return
        if interaction.channel is None or interaction.guild_id is None:
            await interaction.response.send_message("Cette commande doit être utilisée sur un serveur.", ephemeral=True)
            return

        now = datetime.now(timezone.utc)
        ends_at = now + timedelta(minutes=int(duree_minutes))
        end_timestamp = int(ends_at.timestamp())
        prize = lot.strip()
        escaped_prize = discord.utils.escape_markdown(prize)
        content = (
            f"# 🎉 Giveaway Nébula\n\n"
            f"**Lot :** {escaped_prize}\n"
            f"**Organisé par :** {interaction.user.mention}\n"
            f"**Gagnants :** {nombre_gagnants}\n"
            f"**Fin :** <t:{end_timestamp}:F> · <t:{end_timestamp}:R>\n\n"
            "Clique sur le bouton pour participer !"
        )
        await interaction.response.defer(ephemeral=True)
        try:
            message = await interaction.channel.send(
                view=make_giveaway_view(self, content),
                allowed_mentions=discord.AllowedMentions.none(),
            )
            self.store.create_giveaway(
                message.id,
                interaction.channel_id,
                prize,
                interaction.user.id,
                ends_at.isoformat(),
                int(nombre_gagnants),
            )
        except (discord.Forbidden, discord.HTTPException, RuntimeError):
            logging.exception("Impossible de créer le giveaway")
            await interaction.followup.send("Je n’ai pas pu publier le giveaway dans ce salon.", ephemeral=True)
            return
        await interaction.followup.send(f"Giveaway publié : {message.jump_url}", ephemeral=True)

    async def handle_giveaway_join(self, interaction: discord.Interaction) -> None:
        if interaction.message is None:
            await interaction.response.send_message("Impossible de retrouver ce giveaway.", ephemeral=True)
            return
        result = self.store.join_giveaway(
            interaction.message.id,
            interaction.user.id,
            datetime.now(timezone.utc).isoformat(),
        )
        messages = {
            "joined": "Ta participation est enregistrée 🎉",
            "already": "Tu participes déjà à ce giveaway.",
            "inactive": "Ce giveaway est terminé ou n’est plus disponible.",
        }
        await interaction.response.send_message(messages[result], ephemeral=True)

    async def finish_giveaway_record(self, giveaway: dict[str, object]) -> bool:
        message_id = int(giveaway["message_id"])
        entries = self.store.giveaway_entries(message_id)
        winner_count = int(giveaway["winner_count"])
        winners = random.sample(entries, min(winner_count, len(entries))) if entries else []
        if not self.store.finish_giveaway(message_id, winners):
            return False

        prize = discord.utils.escape_markdown(str(giveaway["prize"]))
        if winners:
            winner_text = ", ".join(f"<@{user_id}>" for user_id in winners)
            winner_section = f"**Gagnant(s) :** {winner_text}"
        else:
            winner_section = "Aucun participant n’a rejoint le giveaway."
        content = f"# 🏁 Giveaway terminé\n\n**Lot :** {prize}\n\n{winner_section}"
        view = make_giveaway_view(self, content, ended=True)

        try:
            channel_id = int(giveaway["channel_id"])
            channel = self.get_channel(channel_id) or await self.fetch_channel(channel_id)
            message = await channel.fetch_message(message_id)  # type: ignore[attr-defined]
            await message.edit(
                view=view,
                allowed_mentions=discord.AllowedMentions(users=True, roles=False, everyone=False),
            )
        except (discord.Forbidden, discord.HTTPException, discord.NotFound, AttributeError):
            logging.exception("Giveaway %s terminé mais son message n'a pas pu être actualisé", message_id)
        return True

    async def finish_expired_giveaways(self) -> None:
        due = self.store.due_giveaways(datetime.now(timezone.utc).isoformat())
        for giveaway in due:
            try:
                await self.finish_giveaway_record(giveaway)
            except Exception:
                logging.exception("Impossible de terminer le giveaway %s", giveaway["message_id"])

    async def finish_giveaway_command(
        self,
        interaction: discord.Interaction,
        message_id: str = "",
    ) -> None:
        if not self.has_manage_guild(interaction):
            await interaction.response.send_message(
                "Cette commande est réservée aux membres qui peuvent gérer le serveur.",
                ephemeral=True,
            )
            return
        if message_id.strip():
            if not message_id.strip().isdecimal():
                await interaction.response.send_message("L’identifiant du message doit être numérique.", ephemeral=True)
                return
            giveaway = self.store.get_giveaway(int(message_id.strip()))
        elif interaction.channel_id is not None:
            giveaway = self.store.latest_active_giveaway(interaction.channel_id)
        else:
            giveaway = None

        if giveaway is None or giveaway["status"] != "active":
            await interaction.response.send_message(
                "Aucun giveaway actif trouvé. Donne l’identifiant de son message ou lance la commande dans son salon.",
                ephemeral=True,
            )
            return
        await interaction.response.defer(ephemeral=True)
        finished = await self.finish_giveaway_record(giveaway)
        result = "Giveaway terminé et gagnant(s) tiré(s)." if finished else "Ce giveaway vient déjà d’être terminé."
        await interaction.followup.send(result, ephemeral=True)

    async def set_site_status(
        self,
        interaction: discord.Interaction,
        etat: Literal["fonctionnel", "maintenance", "incident"],
        service: Literal["🟢", "🟠", "🔴"] | None = None,
        connexion: Literal["🟢", "🟠", "🔴"] | None = None,
        serveurs: Literal["🟢", "🟠", "🔴"] | None = None,
    ) -> None:
        if not self.has_manage_guild(interaction):
            await interaction.response.send_message(
                "Cette commande est réservée aux membres qui peuvent gérer le serveur.",
                ephemeral=True,
            )
            return
        await interaction.response.defer(ephemeral=True)
        default_indicator = {
            "fonctionnel": "🟢",
            "maintenance": "🟠",
            "incident": "🔴",
        }[etat]
        self.store.update_site_status(
            etat,
            service=service or default_indicator,
            connection=connexion or default_indicator,
            servers=serveurs or default_indicator,
            checked_at=datetime.now(timezone.utc).isoformat(),
        )
        try:
            await self.update_status_message()
        except (discord.Forbidden, discord.HTTPException, discord.NotFound, RuntimeError):
            logging.exception("Impossible de mettre à jour le salon du statut du site")
            await interaction.followup.send(
                "Le statut est enregistré, mais je n’ai pas pu modifier le message. Vérifie le salon configuré.",
                ephemeral=True,
            )
            return
        label = STATUS_LABELS[etat][1]
        await interaction.followup.send(f"Statut du site mis à jour : **{label}**.", ephemeral=True)


async def run() -> None:
    config = read_config()
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    # Chromium Linux est versionné dans le dépôt GitHub (Git LFS).
    # Aucun téléchargement n'est effectué au démarrage du serveur.
    browsers_path = ROOT / "browsers"
    os.environ["PLAYWRIGHT_BROWSERS_PATH"] = str(browsers_path)
    logging.info("Chromium Playwright local : %s", browsers_path)

    store = SeenStore(STATE_PATH)
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(headless=True)
        bot: NebulaBot | None = None
        try:
            page = await browser.new_page()
            monitor = SiteMonitor(page, config.site_url)
            bot = NebulaBot(config, monitor, store)
            await bot.start(config.token)
        finally:
            if bot is not None and not bot.is_closed():
                await bot.close()
            await browser.close()
            store.close()


if __name__ == "__main__":
    try:
        asyncio.run(run())