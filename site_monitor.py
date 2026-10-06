from __future__ import annotations

import sqlite3
import unicodedata
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urljoin, urlsplit

from playwright.async_api import Page


CARD_SELECTOR = ".znx-row a.zenix-card[href*='details.html']"
FALLBACK_SECTION_BY_INDEX = {
    0: "trends",
    1: "new_films",
    2: "popular_films",
    3: "popular_series",
    5: "new_series",
}


@dataclass(frozen=True, slots=True)
class Release:
    media_type: str
    content_id: str
    title: str
    meta: str
    url: str
    site_url: str
    image_url: str

    @property
    def key(self) -> str:
        return f"{self.media_type}:{self.content_id}"

    @property
    def category(self) -> str:
        if "animation" in self.meta.casefold():
            if self.media_type == "tv":
                return "Animé / série d’animation"
            return "Film d’animation"
        if self.media_type == "tv":
            return "Série"
        return "Film"


@dataclass(frozen=True, slots=True)
class CatalogSnapshot:
    new_films: list[Release]
    new_series: list[Release]
    trends: list[Release]
    popular_films: list[Release]
    popular_series: list[Release]


def normalized_words(value: str) -> str:
    without_accents = unicodedata.normalize("NFKD", value).encode("ascii", "ignore").decode()
    return " ".join("".join(char if char.isalnum() else " " for char in without_accents).casefold().split())


def section_for_row(index: int, heading: str, kicker: str) -> str | None:
    label = normalized_words(f"{heading} {kicker}")
    if "tendances du moment" in label or "regarde en ce moment" in label:
        return "trends"
    if "films a l affiche" in label or "nouveautes au cinema" in label:
        return "new_films"
    if "series en cours" in label:
        return "new_series"
    if "series populaires" in label or "series les plus regardees" in label:
        return "popular_series"
    if "films populaires" in label or "films les plus regardes" in label:
        return "popular_films"
    return FALLBACK_SECTION_BY_INDEX.get(index)


class SiteMonitor:
    def __init__(self, page: Page, site_url: str) -> None:
        self.page = page
        self.site_url = site_url.rstrip("/") + "/"
        self.allowed_host = urlsplit(self.site_url).netloc.casefold()

    async def scan(self) -> CatalogSnapshot:
        response = await self.page.goto(
            self.site_url,
            wait_until="domcontentloaded",
            timeout=45_000,
        )
        if response is not None and response.status >= 400:
            raise RuntimeError(f"Le site a répondu avec le statut HTTP {response.status}.")

        await self.page.locator(CARD_SELECTOR).first.wait_for(state="attached", timeout=45_000)
        raw_rows = await self.page.locator(".znx-row").evaluate_all(
            """rows => rows.map((row, index) => ({
                index,
                heading: row.querySelector('.znx-row__title')?.textContent?.trim() || '',
                kicker: row.querySelector('.znx-row__kicker')?.textContent?.trim() || '',
                cards: Array.from(row.querySelectorAll('a.zenix-card[href*=\"details.html\"]')).map(card => ({
                    id: card.dataset.id || '',
                    type: card.dataset.type || '',
                    title: card.querySelector('.zenix-card__title')?.textContent?.trim() || '',
                    meta: card.querySelector('.zenix-card__meta')?.textContent?.replace(/\\s+/g, ' ').trim() || '',
                    image: card.querySelector('img')?.currentSrc || card.querySelector('img')?.src || '',
                    href: card.getAttribute('href') || ''
                }))
            }))"""
        )

        sections: dict[str, dict[str, Release]] = {}
        visible_count = 0
        for row in raw_rows:
            section = section_for_row(
                int(row.get("index", -1)),
                str(row.get("heading", "")),
                str(row.get("kicker", "")),
            )
            if section is None:
                continue

            section_cards = sections.setdefault(section, {})
            for card in row.get("cards", []):
                release = self._release_from_card(card)
                if release is None:
                    continue
                visible_count += 1
                section_cards.setdefault(release.key, release)

        if visible_count == 0:
            raise RuntimeError("Aucune fiche de film ou de série n'a été trouvée sur la page d'accueil.")

        new_series = self._combine(sections.get("new_series", {}), sections.get("popular_series", {}))
        return CatalogSnapshot(
            new_films=list(sections.get("new_films", {}).values()),
            new_series=new_series,
            trends=list(sections.get("trends", {}).values()),
            popular_films=list(sections.get("popular_films", {}).values()),
            popular_series=list(sections.get("popular_series", {}).values()),
        )

    def _release_from_card(self, card: dict[str, str]) -> Release | None:
        content_id = str(card.get("id", "")).strip()
        media_type = str(card.get("type", "")).strip().casefold()
        title = str(card.get("title", "")).strip()
        meta = str(card.get("meta", "")).strip()
        href = str(card.get("href", "")).strip()
        if not content_id.isdecimal() or media_type not in {"movie", "tv"} or not title or not href:
            return None

        absolute_url = urljoin(self.site_url, href)
        if urlsplit(absolute_url).netloc.casefold() != self.allowed_host:
            return None

        return Release(
            media_type=media_type,
            content_id=content_id,
            title=title,
            meta=meta,
            url=absolute_url,
            site_url=self.site_url,
            image_url=str(card.get("image", "")).strip(),
        )

    @staticmethod
    def _combine(*groups: dict[str, Release]) -> list[Release]:
        combined: dict[str, Release] = {}
        for group in groups:
            combined.update(group)
        return list(combined.values())


class SeenStore:
    def __init__(self, path: Path) -> None:
        self.connection = sqlite3.connect(path)
        self.connection.execute(
            """
            CREATE TABLE IF NOT EXISTS seen_releases (
                feed TEXT NOT NULL,
                release_key TEXT NOT NULL,
                title TEXT NOT NULL,
                url TEXT NOT NULL,
                first_seen_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (feed, release_key)
            )
            """
        )
        self.connection.execute(
            """
            CREATE TABLE IF NOT EXISTS initialized_feeds (
                feed TEXT PRIMARY KEY,
                initialized_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        self.connection.execute(
            """
            CREATE TABLE IF NOT EXISTS weekly_posts (
                feed TEXT NOT NULL,
                week_key TEXT NOT NULL,
                posted_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (feed, week_key)
            )
            """
        )
        self.connection.execute(
            """
            CREATE TABLE IF NOT EXISTS giveaways (
                message_id INTEGER PRIMARY KEY,
                channel_id INTEGER NOT NULL,
                prize TEXT NOT NULL,
                host_id INTEGER NOT NULL,
                ends_at TEXT NOT NULL,
                winner_count INTEGER NOT NULL,
                status TEXT NOT NULL DEFAULT 'active',
                winner_ids TEXT NOT NULL DEFAULT '[]',
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        self.connection.execute(
            """
            CREATE TABLE IF NOT EXISTS giveaway_entries (
                message_id INTEGER NOT NULL,
                user_id INTEGER NOT NULL,
                joined_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (message_id, user_id)
            )
            """
        )
        self.connection.execute(
            """
            CREATE TABLE IF NOT EXISTS site_status (
                singleton_id INTEGER PRIMARY KEY CHECK (singleton_id = 1),
                overall TEXT NOT NULL DEFAULT 'fonctionnel',
                service TEXT NOT NULL DEFAULT '🟢',
                connection TEXT NOT NULL DEFAULT '🟢',
                servers TEXT NOT NULL DEFAULT '🟢',
                message_id INTEGER,
                checked_at TEXT NOT NULL
            )
            """
        )
        self.connection.execute(
            """INSERT OR IGNORE INTO site_status (singleton_id, checked_at) VALUES (1, ?)""",
            (datetime.now(timezone.utc).isoformat(),),
        )
        self.connection.commit()

    def feed_initialized(self, feed: str) -> bool:
        return self.connection.execute(
            "SELECT 1 FROM initialized_feeds WHERE feed = ?",
            (feed,),
        ).fetchone() is not None

    def initialize_feed(self, feed: str, releases: list[Release]) -> None:
        self.connection.executemany(
            "INSERT OR IGNORE INTO seen_releases (feed, release_key, title, url) VALUES (?, ?, ?, ?)",
            [(feed, release.key, release.title, release.url) for release in releases],
        )
        self.connection.execute("INSERT OR IGNORE INTO initialized_feeds (feed) VALUES (?)", (feed,))
        self.connection.commit()

    def contains(self, feed: str, release_key: str) -> bool:
        return self.connection.execute(
            "SELECT 1 FROM seen_releases WHERE feed = ? AND release_key = ?",
            (feed, release_key),
        ).fetchone() is not None

    def remember(self, feed: str, release: Release) -> None:
        self.connection.execute(
            "INSERT OR IGNORE INTO seen_releases (feed, release_key, title, url) VALUES (?, ?, ?, ?)",
            (feed, release.key, release.title, release.url),
        )
        self.connection.commit()

    def weekly_posted(self, feed: str, week_key: str) -> bool:
        return self.connection.execute(
            "SELECT 1 FROM weekly_posts WHERE feed = ? AND week_key = ?",
            (feed, week_key),
        ).fetchone() is not None

    def mark_weekly_posted(self, feed: str, week_key: str) -> None:
        self.connection.execute(
            "INSERT OR IGNORE INTO weekly_posts (feed, week_key) VALUES (?, ?)",
            (feed, week_key),
        )
        self.connection.commit()

    def create_giveaway(
        self,
        message_id: int,
        channel_id: int,
        prize: str,
        host_id: int,
        ends_at: str,
        winner_count: int,
    ) -> None:
        self.connection.execute(
            """INSERT INTO giveaways (message_id, channel_id, prize, host_id, ends_at, winner_count)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (message_id, channel_id, prize, host_id, ends_at, winner_count),
        )
        self.connection.commit()

    def get_giveaway(self, message_id: int) -> dict[str, object] | None:
        row = self.connection.execute(
            """SELECT message_id, channel_id, prize, host_id, ends_at, winner_count,
                      status, winner_ids FROM giveaways WHERE message_id = ?""",
            (message_id,),
        ).fetchone()
        return self._giveaway_dict(row) if row else None

    @staticmethod
    def _giveaway_dict(row: tuple[object, ...]) -> dict[str, object]:
        keys = ("message_id", "channel_id", "prize", "host_id", "ends_at", "winner_count", "status", "winner_ids")
        return dict(zip(keys, row))

    def latest_active_giveaway(self, channel_id: int) -> dict[str, object] | None:
        row = self.connection.execute(
            """SELECT message_id, channel_id, prize, host_id, ends_at, winner_count,
                      status, winner_ids FROM giveaways
               WHERE channel_id = ? AND status = 'active' ORDER BY created_at DESC LIMIT 1""",
            (channel_id,),
        ).fetchone()
        return self._giveaway_dict(row) if row else None

    def due_giveaways(self, now_iso: str) -> list[dict[str, object]]:
        rows = self.connection.execute(
            """SELECT message_id, channel_id, prize, host_id, ends_at, winner_count,
                      status, winner_ids FROM giveaways
               WHERE status = 'active' AND ends_at <= ?""",
            (now_iso,),
        ).fetchall()
        return [self._giveaway_dict(row) for row in rows]

    def join_giveaway(self, message_id: int, user_id: int, now_iso: str) -> str:
        row = self.connection.execute(
            "SELECT status, ends_at FROM giveaways WHERE message_id = ?",
            (message_id,),
        ).fetchone()
        if row is None or row[0] != "active" or str(row[1]) <= now_iso:
            return "inactive"
        cursor = self.connection.execute(
            "INSERT OR IGNORE INTO giveaway_entries (message_id, user_id) VALUES (?, ?)",
            (message_id, user_id),
        )
        self.connection.commit()
        return "joined" if cursor.rowcount else "already"

    def giveaway_entries(self, message_id: int) -> list[int]:
        rows = self.connection.execute(
            "SELECT user_id FROM giveaway_entries WHERE message_id = ?",
            (message_id,),
        ).fetchall()
        return [int(row[0]) for row in rows]

    def finish_giveaway(self, message_id: int, winner_ids: list[int]) -> bool:
        cursor = self.connection.execute(
            """UPDATE giveaways SET status = 'ended', winner_ids = ?
               WHERE message_id = ? AND status = 'active'""",
            (json.dumps(winner_ids), message_id),
        )
        self.connection.commit()
        return cursor.rowcount == 1

    def update_site_status(
        self,
        overall: str,
        service: str | None = None,
        connection: str | None = None,
        servers: str | None = None,
        checked_at: str | None = None,
    ) -> None:
        current = self.get_site_status()
        self.connection.execute(
            """UPDATE site_status SET overall = ?, service = ?, connection = ?, servers = ?, checked_at = ?
               WHERE singleton_id = 1""",
            (
                overall,
                service if service is not None else current["service"],
                connection if connection is not None else current["connection"],
                servers if servers is not None else current["servers"],
                checked_at or datetime.now(timezone.utc).isoformat(),
            ),
        )
        self.connection.commit()

    def get_site_status(self) -> dict[str, object]:
        row = self.connection.execute(
            """SELECT overall, service, connection, servers, message_id, checked_at
               FROM site_status WHERE singleton_id = 1"""
        ).fetchone()
        keys = ("overall", "service", "connection", "servers", "message_id", "checked_at")
        return dict(zip(keys, row))

    def set_status_message_id(self, message_id: int) -> None:
        self.connection.execute(
            "UPDATE site_status SET message_id = ? WHERE singleton_id = 1",
            (message_id,),
        )
        self.connection.commit()

    def close(self) -> None:
        self.connection.close()