"""Schémas Pydantic partagés : domaine métier, sorties IA, enregistrements stockés."""

from __future__ import annotations

import datetime as dt
from enum import StrEnum

from pydantic import BaseModel, Field, model_validator


class Category(StrEnum):
    SCANDALE_DRAMA = "SCANDALE_DRAMA"
    COMEBACK_SORTIE = "COMEBACK_SORTIE"
    CONCERT_EVENEMENT_FRANCE = "CONCERT_EVENEMENT_FRANCE"
    BRUIT_INUTILE = "BRUIT_INUTILE"


class Importance(StrEnum):
    MINEUR = "MINEUR"
    MODERE = "MODERE"
    MAJEUR = "MAJEUR"


class Virality(StrEnum):
    FAIBLE = "FAIBLE"
    MODERE = "MODERE"
    ELEVE = "ELEVE"
    VIRAL = "VIRAL"


# Identique en Discord et dans les logs — voir TODO.md § Routage Discord.
VIRALITY_BADGES: dict[Virality, str] = {
    Virality.VIRAL: "🔴",
    Virality.ELEVE: "🟠",
    Virality.MODERE: "🟡",
    Virality.FAIBLE: "⚪",
}


class Route(StrEnum):
    A = "A"  # #actus-videos — haute valeur
    B = "B"  # #drafts-twitter — volume quotidien
    IGNORED = "IGNORED"  # bruit inutile, jamais diffusé


def determine_route(category: Category, virality: Virality | None) -> Route:
    """Détermine la route de diffusion. Décision prise en code, jamais laissée à l'IA.

    - BRUIT_INUTILE -> ignoré, aucun envoi.
    - CONCERT_EVENEMENT_FRANCE ou viralité VIRAL/ÉLEVÉ -> Route A (#actus-videos).
    - Sinon (MODÉRÉ/FAIBLE, hors Concert France) -> Route B (#drafts-twitter).
    """
    if category == Category.BRUIT_INUTILE:
        return Route.IGNORED
    if category == Category.CONCERT_EVENEMENT_FRANCE or virality in (
        Virality.VIRAL,
        Virality.ELEVE,
    ):
        return Route.A
    return Route.B


class ClassificationResult(BaseModel):
    """Sortie du 1er appel IA (toujours exécuté, un seul par article)."""

    category: Category
    importance: Importance
    virality: Virality | None = Field(
        default=None,
        description="Null si category == BRUIT_INUTILE, sinon attendu.",
    )
    virality_reason: str | None = Field(default=None)
    artists: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def _enforce_virality_nullability(self) -> ClassificationResult:
        """Garde-fou de cohérence, indépendant de ce que l'IA a réellement renvoyé.

        - BRUIT_INUTILE : la viralité est toujours effacée (pas de jugement sur du contenu
          jamais diffusé), même si le modèle en a fourni une par erreur.
        - Catégorie retenue sans viralité fournie : plutôt que de faire échouer tout
          l'article pour un champ manquant, on retombe sur FAIBLE avec une raison explicite
          — l'article part quand même en Route B, ce qui est le choix le moins alarmant.
        """
        if self.category == Category.BRUIT_INUTILE:
            self.virality = None
            self.virality_reason = None
        elif self.virality is None:
            self.virality = Virality.FAIBLE
            self.virality_reason = "Non fourni par le modèle — repli par défaut."
        return self


class WritingResult(BaseModel):
    """Sortie du 2e appel IA, déclenché uniquement si route != IGNORED."""

    summary_fr: str = Field(description="2 phrases, ton journalistique, français.")
    tweet_draft: str = Field(
        max_length=280,
        description=(
            "Brouillon prêt à copier-coller. Ton journalistique neutre, 1-2 emojis max, "
            "2 hashtags pertinents, en français. Jamais publié automatiquement."
        ),
    )
    video_summary: str | None = Field(
        default=None,
        description="Résumé détaillé pour scripter une vidéo — uniquement pour la Route A.",
    )


class FetchedItem(BaseModel):
    """Un item RSS normalisé, avant tout passage par l'IA."""

    source: str
    title: str
    url: str
    published_at: dt.datetime
    raw_summary: str
    fingerprint: str


class ArticleStatus(StrEnum):
    NEW = "NEW"
    ANALYZED = "ANALYZED"
    SENT = "SENT"
    FILTERED = "FILTERED"
    FAILED = "FAILED"


class ArticleRecord(BaseModel):
    """Reflet typé d'une ligne de la table `articles` (lecture)."""

    id: int
    fingerprint: str
    source: str
    title: str
    url: str
    published_at: dt.datetime
    raw_summary: str
    status: ArticleStatus
    category: Category | None = None
    importance: Importance | None = None
    virality: Virality | None = None
    virality_reason: str | None = None
    route: Route | None = None
    france_override: bool = False
    summary_fr: str | None = None
    video_summary: str | None = None
    tweet_draft: str | None = None
    artists: list[str] = Field(default_factory=list)
    tokens_in: int = 0
    tokens_out: int = 0
    prompt_version: str | None = None
    created_at: dt.datetime
    sent_at: dt.datetime | None = None
    error: str | None = None
