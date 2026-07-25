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


class TweetTag(StrEnum):
    FLASH = "FLASH"
    GOSSIP = "GOSSIP"
    FRANCE = "FRANCE"
    RELEASE = "RELEASE"
    INFO = "INFO"


# Préfixé au brouillon de tweet par le pipeline (voir pipeline.py) — jamais généré par l'IA,
# pour rester garanti cohérent avec category/importance/virality déjà décidés.
TWEET_TAG_LABELS: dict[TweetTag, str] = {
    TweetTag.FLASH: "⚡ [FLASH]",
    TweetTag.GOSSIP: "🍵 [GOSSIP]",
    TweetTag.FRANCE: "🇫🇷 [FRANCE]",
    TweetTag.RELEASE: "🎵 [RELEASE]",
    TweetTag.INFO: "📰 [INFO]",
}


def determine_tweet_tag(
    category: Category, importance: Importance, virality: Virality | None
) -> TweetTag:
    """Dérive le tag du tweet des champs déjà connus — aucun appel IA supplémentaire, aucun
    risque de contredire la catégorie déjà affichée dans l'embed.

    FLASH prime sur tout : une actu majeure et très virale mérite l'urgence avant son sujet,
    quel que soit ce sujet. INFO est un repli générique distinct de RELEASE (qui reste
    spécifique à COMEBACK_SORTIE) — non atteint en pratique aujourd'hui puisque BRUIT_INUTILE
    est toujours IGNORED avant d'arriver ici, mais garde-fou correct si une catégorie sans
    branche dédiée apparaît un jour.
    """
    if importance == Importance.MAJEUR and virality in (Virality.VIRAL, Virality.ELEVE):
        return TweetTag.FLASH
    if category == Category.SCANDALE_DRAMA:
        return TweetTag.GOSSIP
    if category == Category.CONCERT_EVENEMENT_FRANCE:
        return TweetTag.FRANCE
    if category == Category.COMEBACK_SORTIE:
        return TweetTag.RELEASE
    return TweetTag.INFO


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
        max_length=260,  # <260 pour laisser la place au tag préfixé ensuite (max ~13 car.)
        description=(
            "Brouillon prêt à copier-coller (hors tag, ajouté séparément). Ton journalistique "
            "neutre, 1-2 emojis max, 2 hashtags pertinents, en français. Se termine par une "
            "question courte pour créer de l'engagement. Jamais publié automatiquement."
        ),
    )
    video_summary: str | None = Field(
        default=None,
        description="Résumé détaillé pour scripter une vidéo — uniquement pour la Route A.",
    )


class TikTokCaptionSeo(BaseModel):
    """Légende + hashtags pour la publication TikTok elle-même (T14)."""

    legende: str = Field(
        description="Légende courte, mots-clés naturels de l'article, aucun emoji."
    )
    hashtags: list[str] = Field(
        description="3-5 hashtags pertinents pour la niche K-pop, mêlant hashtags larges et "
        "hashtags de niche."
    )


class TikTokScriptResult(BaseModel):
    """Sortie du 3e appel IA (T14), dédié — prompt système séparé du tweet pour éviter de
    diluer la qualité entre deux formats très différents sur un modèle lite. Déclenché
    uniquement pour la Route A (mêmes articles qui reçoivent déjà `video_summary`), et
    seulement si un salon TikTok dédié est configuré. Contrairement au tweet, aucun emoji
    n'est autorisé dans aucun champ."""

    hook: str = Field(
        description="Accroche percutante, 1-2 phrases (~15-20 mots), promesse claire et "
        "vérifiable, tenue par script_body. Aucun emoji."
    )
    on_screen_texte: str = Field(
        description="Texte overlay affiché dès les 2 premières secondes, 5-8 mots maximum, "
        "renforce le hook pour les spectateurs sans son. Aucun emoji."
    )
    script_body: str = Field(
        description="Corps du script, 4-6 phrases (~60-80 mots), ton oral/dynamique, plus de "
        "détail que le tweet. Ancré strictement dans les faits fournis, aucune invention, "
        "aucun emoji. Doit livrer explicitement la promesse du hook."
    )
    closing_hook: str = Field(
        description="Chute courte (~3-4s à l'oral), orientée déclencheur de partage (taguer "
        "quelqu'un, question qui divise) plutôt que simple invitation à commenter. Aucun emoji."
    )
    visual_ideas: list[str] = Field(
        description="3-5 suggestions courtes de plans/images pour le montage — propositions "
        "créatives, pas des faits, la seule partie où l'improvisation est acceptée."
    )
    caption_seo: TikTokCaptionSeo = Field(
        description="Légende et hashtags pour la publication TikTok elle-même."
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
    FILTERED_SENT = "FILTERED_SENT"  # bruit envoyé sur #info-a-verifier — voir T13
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
    tiktok_hook: str | None = None
    tiktok_on_screen_texte: str | None = None
    tiktok_script_body: str | None = None
    tiktok_closing_hook: str | None = None
    tiktok_visual_ideas: list[str] = Field(default_factory=list)
    tiktok_caption_legende: str | None = None
    tiktok_caption_hashtags: list[str] = Field(default_factory=list)
    artists: list[str] = Field(default_factory=list)
    tokens_in: int = 0
    tokens_out: int = 0
    prompt_version: str | None = None
    created_at: dt.datetime
    sent_at: dt.datetime | None = None
    error: str | None = None
