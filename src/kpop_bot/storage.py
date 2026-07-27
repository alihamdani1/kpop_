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
    SelectionStatus,
    ThreadAngle,
    ThreadRecord,
    ThreadSelectionRecord,
    ThreadStatus,
    ThreadTheme,
    ThreadTopicIdea,
    ThreadTopicRecord,
    ThreadWritingResult,
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

-- T15 : backlog de Topics, sélection Discord (réactions), threads générés.
CREATE TABLE IF NOT EXISTS thread_topics (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    group_name       TEXT NOT NULL,
    theme            TEXT NOT NULL,
    title            TEXT NOT NULL,
    premise          TEXT NOT NULL,
    created_at       TEXT NOT NULL,
    last_offered_at  TEXT,
    source           TEXT NOT NULL DEFAULT 'ai_ideation'
);

CREATE TABLE IF NOT EXISTS thread_selections (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    discord_message_id  TEXT NOT NULL UNIQUE,
    option_a_topic_id   INTEGER NOT NULL REFERENCES thread_topics (id),
    option_a_angle      TEXT NOT NULL,
    option_b_topic_id   INTEGER NOT NULL REFERENCES thread_topics (id),
    option_b_angle      TEXT NOT NULL,
    option_c_topic_id   INTEGER NOT NULL REFERENCES thread_topics (id),
    option_c_angle      TEXT NOT NULL,
    status              TEXT NOT NULL DEFAULT 'PENDING',
    resolved_topic_id   INTEGER,
    resolved_angle      TEXT,
    created_at          TEXT NOT NULL,
    resolved_at         TEXT
);
CREATE INDEX IF NOT EXISTS idx_thread_selections_status ON thread_selections (status);

-- UNIQUE(topic_id, angle) = garantie dure qu'un couple (sujet, angle) n'est jamais régénéré
-- (anti-répétition niveau 1, voir TODO.md T15).
CREATE TABLE IF NOT EXISTS threads (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    selection_id   INTEGER REFERENCES thread_selections (id),
    topic_id       INTEGER NOT NULL REFERENCES thread_topics (id),
    angle          TEXT NOT NULL,
    hook_label     TEXT,
    tweets         TEXT NOT NULL DEFAULT '[]',
    status         TEXT NOT NULL DEFAULT 'DRAFT',
    tokens_in      INTEGER NOT NULL DEFAULT 0,
    tokens_out     INTEGER NOT NULL DEFAULT 0,
    prompt_version TEXT,
    created_at     TEXT NOT NULL,
    sent_at        TEXT,
    error          TEXT,
    UNIQUE (topic_id, angle)
);
CREATE INDEX IF NOT EXISTS idx_threads_status ON threads (status);
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


# --- T15 : backlog de Topics, sélection Discord (réactions), threads générés. ---

_ALL_THREAD_ANGLES = list(ThreadAngle)


def insert_topic_ideas(conn: sqlite3.Connection, ideas: list[ThreadTopicIdea]) -> list[int]:
    """Insère un lot de sujets issus d'un seul appel d'idéation (voir
    `analyzer.GeminiAnalyzer.ideate_thread_topics`). Retourne les ids insérés, dans l'ordre."""
    now = dt.datetime.now(dt.UTC).isoformat()
    ids: list[int] = []
    for idea in ideas:
        cur = conn.execute(
            """
            INSERT INTO thread_topics (group_name, theme, title, premise, created_at, source)
            VALUES (?, ?, ?, ?, ?, 'ai_ideation')
            """,
            (idea.group_name, idea.theme.value, idea.title, idea.premise, now),
        )
        ids.append(int(cur.lastrowid))
    conn.commit()
    return ids


def backlog_topic_count(conn: sqlite3.Connection) -> int:
    """Nombre de topics ayant encore au moins un angle non consommé — sert de seuil de
    réapprovisionnement (voir `thread_pipeline.run_thread_replenish`)."""
    row = conn.execute(
        """
        SELECT COUNT(*) AS n FROM thread_topics t
        WHERE (SELECT COUNT(*) FROM threads th WHERE th.topic_id = t.id) < ?
        """,
        (len(_ALL_THREAD_ANGLES),),
    ).fetchone()
    return int(row["n"])


def available_angles(conn: sqlite3.Connection, topic_id: int) -> list[ThreadAngle]:
    """Angles pas encore utilisés pour ce topic — l'IA n'a jamais généré ce couple."""
    used = {
        row["angle"]
        for row in conn.execute(
            "SELECT angle FROM threads WHERE topic_id = ?", (topic_id,)
        ).fetchall()
    }
    return [angle for angle in _ALL_THREAD_ANGLES if angle.value not in used]


def recent_group_theme_pairs(conn: sqlite3.Connection, *, days: int = 14) -> set[tuple[str, str]]:
    """(group_name, theme) déjà traités dans les `days` derniers jours — anti-répétition niveau
    2 (voir TODO.md T15) : le vrai garde-fou puisque les Topics sont écrits librement par l'IA,
    donc pas garantis textuellement uniques d'un lot d'idéation à l'autre."""
    since = (dt.datetime.now(dt.UTC) - dt.timedelta(days=days)).isoformat()
    rows = conn.execute(
        """
        SELECT t.group_name AS group_name, t.theme AS theme
        FROM threads th JOIN thread_topics t ON t.id = th.topic_id
        WHERE th.created_at >= ?
        """,
        (since,),
    ).fetchall()
    return {(row["group_name"], row["theme"]) for row in rows}


def recent_hook_labels(conn: sqlite3.Connection, *, limit: int = 5) -> list[str]:
    """Styles de hook des N derniers threads — injecté en note système pour éviter de répéter
    la même accroche d'un thread à l'autre (voir `_HOOK_TEMPLATES` dans analyzer.py)."""
    rows = conn.execute(
        """
        SELECT hook_label FROM threads
        WHERE hook_label IS NOT NULL ORDER BY created_at DESC LIMIT ?
        """,
        (limit,),
    ).fetchall()
    return [row["hook_label"] for row in rows]


def select_picker_candidates(
    conn: sqlite3.Connection, *, count: int = 3, recent_window_days: int = 14
) -> list[tuple[ThreadTopicRecord, ThreadAngle]]:
    """Choisit `count` couples (topic, angle) jamais consommés ensemble, diversifiés par
    (group_name, theme) sur la fenêtre récente. Priorité aux topics les moins récemment proposés
    (`last_offered_at` NULL ou ancien d'abord — NULL trie en premier en ASC sous SQLite)."""
    excluded_pairs = recent_group_theme_pairs(conn, days=recent_window_days)
    rows = conn.execute(
        "SELECT * FROM thread_topics ORDER BY last_offered_at ASC, created_at ASC"
    ).fetchall()

    picked: list[tuple[ThreadTopicRecord, ThreadAngle]] = []
    used_pairs_this_batch: set[tuple[str, str]] = set()
    for row in rows:
        if len(picked) >= count:
            break
        topic = _row_to_topic(row)
        pair = (topic.group_name, topic.theme.value)
        if pair in excluded_pairs or pair in used_pairs_this_batch:
            continue
        angles = available_angles(conn, topic.id)
        if not angles:
            continue
        picked.append((topic, angles[0]))
        used_pairs_this_batch.add(pair)
    return picked


def touch_offered_topics(conn: sqlite3.Connection, topic_ids: list[int]) -> None:
    """Marque les topics comme proposés aujourd'hui, choisis ou non — désature `last_offered_at`
    pour que `select_picker_candidates` ne les re-propose pas en priorité demain."""
    now = dt.datetime.now(dt.UTC).isoformat()
    conn.executemany(
        "UPDATE thread_topics SET last_offered_at = ? WHERE id = ?",
        [(now, topic_id) for topic_id in topic_ids],
    )
    conn.commit()


def get_topic(conn: sqlite3.Connection, topic_id: int) -> ThreadTopicRecord:
    row = conn.execute("SELECT * FROM thread_topics WHERE id = ?", (topic_id,)).fetchone()
    return _row_to_topic(row)


def insert_selection(
    conn: sqlite3.Connection,
    *,
    discord_message_id: str,
    option_a: tuple[int, ThreadAngle],
    option_b: tuple[int, ThreadAngle],
    option_c: tuple[int, ThreadAngle],
) -> int:
    now = dt.datetime.now(dt.UTC).isoformat()
    cur = conn.execute(
        """
        INSERT INTO thread_selections
            (discord_message_id, option_a_topic_id, option_a_angle,
             option_b_topic_id, option_b_angle, option_c_topic_id, option_c_angle,
             status, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, 'PENDING', ?)
        """,
        (
            discord_message_id,
            option_a[0],
            option_a[1].value,
            option_b[0],
            option_b[1].value,
            option_c[0],
            option_c[1].value,
            now,
        ),
    )
    conn.commit()
    return int(cur.lastrowid)


def pending_selections(conn: sqlite3.Connection) -> list[ThreadSelectionRecord]:
    rows = conn.execute(
        "SELECT * FROM thread_selections WHERE status = 'PENDING' ORDER BY created_at ASC"
    ).fetchall()
    return [_row_to_selection(row) for row in rows]


def expire_stale_selections(conn: sqlite3.Connection, *, ttl_hours: float) -> int:
    """Bascule en EXPIRED les sélections PENDING trop vieilles (humain n'a pas réagi à temps) —
    libère les topics concernés pour une future proposition, sans jamais les marquer consommés
    (seule l'insertion réussie dans `threads` consomme un couple topic/angle)."""
    cutoff = (dt.datetime.now(dt.UTC) - dt.timedelta(hours=ttl_hours)).isoformat()
    cur = conn.execute(
        "UPDATE thread_selections SET status = 'EXPIRED' "
        "WHERE status = 'PENDING' AND created_at < ?",
        (cutoff,),
    )
    conn.commit()
    return cur.rowcount


def resolve_selection(
    conn: sqlite3.Connection, selection_id: int, *, topic_id: int, angle: ThreadAngle
) -> None:
    now = dt.datetime.now(dt.UTC).isoformat()
    conn.execute(
        """
        UPDATE thread_selections
        SET status = 'RESOLVED', resolved_topic_id = ?, resolved_angle = ?, resolved_at = ?
        WHERE id = ?
        """,
        (topic_id, angle.value, now, selection_id),
    )
    conn.commit()


def insert_thread(
    conn: sqlite3.Connection,
    *,
    selection_id: int | None,
    topic_id: int,
    angle: ThreadAngle,
    hook_label: str | None,
    writing: ThreadWritingResult,
    tokens_in: int,
    tokens_out: int,
    prompt_version: str,
) -> int:
    """Enregistre le thread généré, statut DRAFT (en attente d'envoi Discord). La contrainte
    `UNIQUE(topic_id, angle)` lève `sqlite3.IntegrityError` si ce couple existe déjà — ne devrait
    jamais arriver si l'appelant a bien vérifié `available_angles` avant de générer."""
    now = dt.datetime.now(dt.UTC).isoformat()
    cur = conn.execute(
        """
        INSERT INTO threads
            (selection_id, topic_id, angle, hook_label, tweets, status,
             tokens_in, tokens_out, prompt_version, created_at)
        VALUES (?, ?, ?, ?, ?, 'DRAFT', ?, ?, ?, ?)
        """,
        (
            selection_id,
            topic_id,
            angle.value,
            hook_label,
            json.dumps(writing.tweets, ensure_ascii=False),
            tokens_in,
            tokens_out,
            prompt_version,
            now,
        ),
    )
    conn.commit()
    return int(cur.lastrowid)


def mark_thread_sent(conn: sqlite3.Connection, thread_id: int) -> None:
    now = dt.datetime.now(dt.UTC).isoformat()
    conn.execute("UPDATE threads SET status = 'SENT', sent_at = ? WHERE id = ?", (now, thread_id))
    conn.commit()


def mark_thread_failed(conn: sqlite3.Connection, thread_id: int, error: str) -> None:
    conn.execute("UPDATE threads SET status = 'FAILED', error = ? WHERE id = ?", (error, thread_id))
    conn.commit()


def pending_threads(conn: sqlite3.Connection) -> list[ThreadRecord]:
    rows = conn.execute(
        "SELECT * FROM threads WHERE status = 'DRAFT' ORDER BY created_at ASC"
    ).fetchall()
    return [_row_to_thread(row) for row in rows]


def _row_to_topic(row: sqlite3.Row) -> ThreadTopicRecord:
    return ThreadTopicRecord(
        id=row["id"],
        group_name=row["group_name"],
        theme=ThreadTheme(row["theme"]),
        title=row["title"],
        premise=row["premise"],
        created_at=dt.datetime.fromisoformat(row["created_at"]),
        last_offered_at=dt.datetime.fromisoformat(row["last_offered_at"])
        if row["last_offered_at"]
        else None,
        source=row["source"],
    )


def _row_to_selection(row: sqlite3.Row) -> ThreadSelectionRecord:
    return ThreadSelectionRecord(
        id=row["id"],
        discord_message_id=row["discord_message_id"],
        option_a_topic_id=row["option_a_topic_id"],
        option_a_angle=ThreadAngle(row["option_a_angle"]),
        option_b_topic_id=row["option_b_topic_id"],
        option_b_angle=ThreadAngle(row["option_b_angle"]),
        option_c_topic_id=row["option_c_topic_id"],
        option_c_angle=ThreadAngle(row["option_c_angle"]),
        status=SelectionStatus(row["status"]),
        resolved_topic_id=row["resolved_topic_id"],
        resolved_angle=ThreadAngle(row["resolved_angle"]) if row["resolved_angle"] else None,
        created_at=dt.datetime.fromisoformat(row["created_at"]),
        resolved_at=dt.datetime.fromisoformat(row["resolved_at"]) if row["resolved_at"] else None,
    )


def _row_to_thread(row: sqlite3.Row) -> ThreadRecord:
    return ThreadRecord(
        id=row["id"],
        selection_id=row["selection_id"],
        topic_id=row["topic_id"],
        angle=ThreadAngle(row["angle"]),
        hook_label=row["hook_label"],
        tweets=json.loads(row["tweets"]) if row["tweets"] else [],
        status=ThreadStatus(row["status"]),
        tokens_in=row["tokens_in"],
        tokens_out=row["tokens_out"],
        prompt_version=row["prompt_version"],
        created_at=dt.datetime.fromisoformat(row["created_at"]),
        sent_at=dt.datetime.fromisoformat(row["sent_at"]) if row["sent_at"] else None,
        error=row["error"],
    )
