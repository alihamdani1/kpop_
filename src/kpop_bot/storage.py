"""Couche de persistance SQLite. Un seul fichier, versionné dans le dépôt (voir TODO.md T3) —
recommis à la fin de chaque run GitHub Actions pour survivre aux runners jetables."""

from __future__ import annotations

import datetime as dt
import json
import sqlite3
from pathlib import Path

from kpop_bot.models import (
    ArticleRecord,
    ArticleStatus,
    Category,
    ClassificationResult,
    FetchedItem,
    Importance,
    Route,
    TikTokScriptResult,
    Virality,
    WritingResult,
)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS articles (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    fingerprint      TEXT NOT NULL UNIQUE,
    source           TEXT NOT NULL,
    title            TEXT NOT NULL,
    url              TEXT NOT NULL,
    published_at     TEXT NOT NULL,
    raw_summary      TEXT NOT NULL,
    status           TEXT NOT NULL DEFAULT 'NEW',
    category         TEXT,
    importance       TEXT,
    virality         TEXT,
    virality_reason  TEXT,
    route            TEXT,
    france_override  INTEGER NOT NULL DEFAULT 0,
    summary_fr       TEXT,
    video_summary    TEXT,
    tweet_draft      TEXT,
    tiktok_hook              TEXT,
    tiktok_on_screen_texte   TEXT,
    tiktok_script_body       TEXT,
    tiktok_closing_hook      TEXT,
    tiktok_visual_ideas      TEXT NOT NULL DEFAULT '[]',
    tiktok_caption_legende   TEXT,
    tiktok_caption_hashtags  TEXT NOT NULL DEFAULT '[]',
    artists          TEXT NOT NULL DEFAULT '[]',
    tokens_in        INTEGER NOT NULL DEFAULT 0,
    tokens_out       INTEGER NOT NULL DEFAULT 0,
    prompt_version   TEXT,
    created_at       TEXT NOT NULL,
    sent_at          TEXT,
    error            TEXT
);
CREATE INDEX IF NOT EXISTS idx_articles_status ON articles (status);
CREATE INDEX IF NOT EXISTS idx_articles_fingerprint ON articles (fingerprint);
"""

# Colonnes ajoutées après la création initiale de la table (T14) — `CREATE TABLE IF NOT
# EXISTS` ne les ajouterait pas à une base déjà existante (data/kpop.db est réelle, versionnée,
# avec des données en production). Migration idempotente via PRAGMA table_info : couvre à la
# fois les bases déjà existantes (ALTER TABLE) et les bases neuves (déjà créées avec ces
# colonnes par _SCHEMA ci-dessus, donc ce bloc est un no-op pour elles).
_MIGRATED_COLUMNS = {
    "tiktok_hook": "TEXT",
    "tiktok_on_screen_texte": "TEXT",
    "tiktok_script_body": "TEXT",
    "tiktok_closing_hook": "TEXT",
    "tiktok_visual_ideas": "TEXT NOT NULL DEFAULT '[]'",
    "tiktok_caption_legende": "TEXT",
    "tiktok_caption_hashtags": "TEXT NOT NULL DEFAULT '[]'",
}


def _migrate_schema(conn: sqlite3.Connection) -> None:
    existing = {row["name"] for row in conn.execute("PRAGMA table_info(articles)").fetchall()}
    for column, coltype in _MIGRATED_COLUMNS.items():
        if column not in existing:
            conn.execute(f"ALTER TABLE articles ADD COLUMN {column} {coltype}")
    conn.commit()


def init_db(path: Path) -> sqlite3.Connection:
    """Ouvre (et crée si besoin) la base. Création de schéma idempotente."""
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.executescript(_SCHEMA)
    conn.commit()
    _migrate_schema(conn)
    return conn


def fingerprint_exists(conn: sqlite3.Connection, fingerprint: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM articles WHERE fingerprint = ? LIMIT 1", (fingerprint,)
    ).fetchone()
    return row is not None


def insert_new_article(conn: sqlite3.Connection, item: FetchedItem) -> int:
    """Insère un item collecté avec le statut NEW. Suppose que le doublon a déjà été écarté
    par l'appelant (`fingerprint_exists`) — une insertion en doublon lèverait IntegrityError."""
    now = dt.datetime.now(dt.UTC).isoformat()
    cur = conn.execute(
        """
        INSERT INTO articles
            (fingerprint, source, title, url, published_at, raw_summary, status, created_at)
        VALUES (?, ?, ?, ?, ?, ?, 'NEW', ?)
        """,
        (
            item.fingerprint,
            item.source,
            item.title,
            item.url,
            item.published_at.isoformat(),
            item.raw_summary,
            now,
        ),
    )
    conn.commit()
    return int(cur.lastrowid)


def save_analysis(
    conn: sqlite3.Connection,
    article_id: int,
    classification: ClassificationResult,
    route: Route,
    france_override: bool,
    writing: WritingResult | None,
    tokens_in: int,
    tokens_out: int,
    prompt_version: str,
    tiktok: TikTokScriptResult | None = None,
) -> None:
    """Enregistre le résultat des appels IA. `writing` est None quand route == IGNORED (bruit
    inutile) — dans ce cas le statut final est FILTERED, sinon ANALYZED. `tiktok` reste None
    hors Route A ou si le salon dédié n'est pas configuré (voir T14) — colonnes laissées NULL,
    aucun impact sur le reste de l'enregistrement."""
    status = ArticleStatus.FILTERED if route == Route.IGNORED else ArticleStatus.ANALYZED
    conn.execute(
        """
        UPDATE articles SET
            status = ?, category = ?, importance = ?, virality = ?, virality_reason = ?,
            route = ?, france_override = ?, summary_fr = ?, video_summary = ?,
            tweet_draft = ?, tiktok_hook = ?, tiktok_on_screen_texte = ?,
            tiktok_script_body = ?, tiktok_closing_hook = ?, tiktok_visual_ideas = ?,
            tiktok_caption_legende = ?, tiktok_caption_hashtags = ?, artists = ?,
            tokens_in = tokens_in + ?, tokens_out = tokens_out + ?, prompt_version = ?,
            error = NULL
        WHERE id = ?
        """,
        (
            status.value,
            classification.category.value,
            classification.importance.value,
            classification.virality.value if classification.virality else None,
            classification.virality_reason,
            route.value,
            int(france_override),
            writing.summary_fr if writing else None,
            writing.video_summary if writing else None,
            writing.tweet_draft if writing else None,
            tiktok.hook if tiktok else None,
            tiktok.on_screen_texte if tiktok else None,
            tiktok.script_body if tiktok else None,
            tiktok.closing_hook if tiktok else None,
            json.dumps(tiktok.visual_ideas, ensure_ascii=False) if tiktok else "[]",
            tiktok.caption_seo.legende if tiktok else None,
            json.dumps(tiktok.caption_seo.hashtags, ensure_ascii=False) if tiktok else "[]",
            json.dumps(classification.artists, ensure_ascii=False),
            tokens_in,
            tokens_out,
            prompt_version,
            article_id,
        ),
    )
    conn.commit()


def mark_failed(conn: sqlite3.Connection, article_id: int, error: str) -> None:
    conn.execute(
        "UPDATE articles SET status = 'FAILED', error = ? WHERE id = ?",
        (error, article_id),
    )
    conn.commit()


def mark_sent(conn: sqlite3.Connection, article_id: int) -> None:
    now = dt.datetime.now(dt.UTC).isoformat()
    conn.execute(
        "UPDATE articles SET status = 'SENT', sent_at = ? WHERE id = ?",
        (now, article_id),
    )
    conn.commit()


def mark_filtered_sent(conn: sqlite3.Connection, article_id: int) -> None:
    """Marque un article FILTERED comme transmis à #info-a-verifier (T13) — évite de le
    renvoyer à chaque cycle. Statut distinct de SENT : ce n'est pas passé par Route A/B."""
    now = dt.datetime.now(dt.UTC).isoformat()
    conn.execute(
        "UPDATE articles SET status = 'FILTERED_SENT', sent_at = ? WHERE id = ?",
        (now, article_id),
    )
    conn.commit()


def reset_failed_to_new(conn: sqlite3.Connection) -> int:
    """Repêche les articles FAILED pour un nouveau cycle. Stratégie simple (MVP) : on repart
    de zéro sur la classification plutôt que de distinguer précisément où l'échec a eu lieu."""
    cur = conn.execute("UPDATE articles SET status = 'NEW', error = NULL WHERE status = 'FAILED'")
    conn.commit()
    return cur.rowcount


def pending(
    conn: sqlite3.Connection, status: ArticleStatus, limit: int | None = None
) -> list[ArticleRecord]:
    query = "SELECT * FROM articles WHERE status = ? ORDER BY published_at ASC"
    params: tuple = (status.value,)
    if limit is not None:
        query += " LIMIT ?"
        params = (status.value, limit)
    rows = conn.execute(query, params).fetchall()
    return [_row_to_record(row) for row in rows]


def _row_to_record(row: sqlite3.Row) -> ArticleRecord:
    return ArticleRecord(
        id=row["id"],
        fingerprint=row["fingerprint"],
        source=row["source"],
        title=row["title"],
        url=row["url"],
        published_at=dt.datetime.fromisoformat(row["published_at"]),
        raw_summary=row["raw_summary"],
        status=ArticleStatus(row["status"]),
        category=Category(row["category"]) if row["category"] else None,
        importance=Importance(row["importance"]) if row["importance"] else None,
        virality=Virality(row["virality"]) if row["virality"] else None,
        virality_reason=row["virality_reason"],
        route=Route(row["route"]) if row["route"] else None,
        france_override=bool(row["france_override"]),
        summary_fr=row["summary_fr"],
        video_summary=row["video_summary"],
        tweet_draft=row["tweet_draft"],
        tiktok_hook=row["tiktok_hook"],
        tiktok_on_screen_texte=row["tiktok_on_screen_texte"],
        tiktok_script_body=row["tiktok_script_body"],
        tiktok_closing_hook=row["tiktok_closing_hook"],
        tiktok_visual_ideas=json.loads(row["tiktok_visual_ideas"])
        if row["tiktok_visual_ideas"]
        else [],
        tiktok_caption_legende=row["tiktok_caption_legende"],
        tiktok_caption_hashtags=json.loads(row["tiktok_caption_hashtags"])
        if row["tiktok_caption_hashtags"]
        else [],
        artists=json.loads(row["artists"]) if row["artists"] else [],
        tokens_in=row["tokens_in"],
        tokens_out=row["tokens_out"],
        prompt_version=row["prompt_version"],
        created_at=dt.datetime.fromisoformat(row["created_at"]),
        sent_at=dt.datetime.fromisoformat(row["sent_at"]) if row["sent_at"] else None,
        error=row["error"],
    )
