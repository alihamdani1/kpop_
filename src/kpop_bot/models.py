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
    CONCERT = "CONCERT"  # #concert — Concert/Événement France, salon dédié
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
    - CONCERT_EVENEMENT_FRANCE -> Route CONCERT (#concert), quelle que soit la viralité —
      salon dédié pour être averti en premier des dates de billetterie, séparé du volume
      général de #actus-videos.
    - Sinon, viralité VIRAL/ÉLEVÉ -> Route A (#actus-videos).
    - Sinon (MODÉRÉ/FAIBLE) -> Route B (#drafts-twitter).
    """
    if category == Category.BRUIT_INUTILE:
        return Route.IGNORED
    if category == Category.CONCERT_EVENEMENT_FRANCE:
        return Route.CONCERT
    if virality in (Virality.VIRAL, Virality.ELEVE):
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


class ThreadTheme(StrEnum):
    """Thème éditorial d'un sujet de thread (T15) — distinct de `Category` (classification
    d'articles) : un thread n'est pas rattaché à un article précis, voir `ThreadTopicIdea`."""

    RIVALITE_COMPARAISON = "RIVALITE_COMPARAISON"
    RETROSPECTIVE_CARRIERE = "RETROSPECTIVE_CARRIERE"
    ANALYSE_COMEBACK = "ANALYSE_COMEBACK"
    RECAP_SCANDALE = "RECAP_SCANDALE"
    CONNEXION_FRANCE = "CONNEXION_FRANCE"
    MYTHE_VS_REALITE = "MYTHE_VS_REALITE"
    RECORD_ANECDOTE = "RECORD_ANECDOTE"
    COULISSES_INDUSTRIE = "COULISSES_INDUSTRIE"
    CULTURE_FANS = "CULTURE_FANS"


class ThreadAngle(StrEnum):
    """Traitement narratif appliqué à un Topic — le vrai levier de variété quand un même Topic
    est réutilisé (voir TODO.md T15, anti-répétition niveau 1 : `UNIQUE(topic_id, angle)`)."""

    CONTRARIEN = "CONTRARIEN"
    GUIDE_PRATIQUE = "GUIDE_PRATIQUE"
    CAS_ETUDE = "CAS_ETUDE"
    STORYTELLING = "STORYTELLING"


class SelectionStatus(StrEnum):
    PENDING = "PENDING"
    RESOLVED = "RESOLVED"
    EXPIRED = "EXPIRED"


class ThreadStatus(StrEnum):
    DRAFT = "DRAFT"
    SENT = "SENT"
    FAILED = "FAILED"


class ThreadConcept(BaseModel):
    """Un concept viral curé (`config/thread_concepts.yaml`, T15bis) — matière première fixe,
    jamais inventée par l'IA. Croisé en code avec un groupe (`config/artist_tiers.yaml`) pour
    former un Topic candidat ; l'IA ne rédige ensuite qu'un titre/premise sur-mesure pour la
    paire déjà choisie (voir `ThreadTopicWriting`, `thread_pipeline._candidate_pairs`)."""

    id: str
    theme: ThreadTheme
    label: str
    brief: str = Field(
        description="Matière première générale du concept, injectée dans le prompt de rédaction."
    )
    weight: float = Field(
        default=1.0,
        description="Poids de priorité (T15quater) — un concept à poids 2.0 repasse deux fois "
        "plus souvent qu'un concept à poids 1.0 dans la rotation déterministe de "
        "`thread_pipeline._candidate_pairs`. Défaut 1.0 pour la rétrocompatibilité avec les "
        "concepts existants qui ne précisent pas ce champ.",
    )


class ThreadTopicIdea(BaseModel):
    """Un sujet de thread prêt à être inséré en base (T15) — `group_name`/`theme`/`concept_id`
    sont fixés en code (croisement déterministe, voir `thread_pipeline._candidate_pairs`),
    `title`/`premise` sont rédigés par l'IA à partir du `brief` du concept (voir
    `ThreadTopicWriting`). Contrairement à la version initiale de T15, l'IA ne choisit plus ni le
    groupe ni le thème — seule la rédaction du titre/premise lui revient, ce qui élimine le
    risque d'un sujet halluciné ou hors-sol (voir TODO.md T15bis)."""

    group_name: str = Field(
        description="Groupe/artiste concerné, ou portée générale (ex. 'industrie K-pop') si le "
        "sujet n'est pas rattaché à un act précis."
    )
    theme: ThreadTheme
    title: str = Field(description="Titre court du sujet, pour l'affichage dans l'embed picker.")
    premise: str = Field(
        description="La promesse/l'angle de fond du sujet en 1-2 phrases — ce que le thread "
        "devra développer, quel que soit l'angle narratif choisi ensuite."
    )
    concept_id: str | None = Field(
        default=None,
        description="Id du concept curé à l'origine de ce topic (voir ThreadConcept). None pour "
        "les topics historiques générés par l'ancienne idéation libre, avant T15bis.",
    )


class ThreadTopicWriting(BaseModel):
    """Sortie du 1er appel IA de T15bis (idéation) pour UNE paire (groupe, concept) déjà choisie
    par le code — l'IA ne renvoie jamais le groupe ni le concept eux-mêmes, uniquement le
    contenu rédactionnel, référencé par `pair_index` pour un rattachement sûr côté code (voir
    `thread_pipeline.run_thread_replenish`, qui valide que les index reçus correspondent
    exactement aux paires soumises avant tout usage)."""

    pair_index: int = Field(description="Position (0-indexée) de la paire dans la liste soumise.")
    title: str = Field(
        description="Titre court, spécifique au groupe cité — pas un texte générique."
    )
    premise: str = Field(description="Promesse de fond en 1-2 phrases, spécifique à ce groupe.")


class TopicIdeationResult(BaseModel):
    """Sortie de l'appel d'idéation (T15bis) — un lot de rédactions en un seul appel Gemini, pour
    réapprovisionner le backlog par lot plutôt qu'un appel par sujet (même principe de coût
    maîtrisé que le digest hebdomadaire T12)."""

    topics: list[ThreadTopicWriting]


class ThreadWritingResult(BaseModel):
    """Sortie du 2e appel IA de T15 (rédaction du thread complet), déclenchée uniquement après
    la réaction humaine de sélection — jamais générée avant qu'un humain ait choisi parmi les
    3 options proposées."""

    premise_respectee: bool = Field(
        description="Vérification honnête, à faire AVANT de rédiger les tweets : le thread "
        "va-t-il réellement tenir la promesse du `premise` fourni — pas seulement celle du "
        "tweet 1 ? Champ placé avant `tweets` (T15ter) pour forcer cette vérification avant la "
        "rédaction plutôt qu'une justification a posteriori. Seule vérification demandée à "
        "l'IA qui n'est pas mécaniquement vérifiable par du code (sémantique) — la longueur des "
        "tweets et l'absence de hashtag dans le corps sont, elles, imposées par des validators "
        "stricts ci-dessous, jamais par une simple auto-déclaration."
    )
    tweets: list[str] = Field(
        description="Le thread complet, dans l'ordre de publication. Chaque tweet ≤260 "
        "caractères (la numérotation 'n/total' est ajoutée en code, jamais par l'IA — même "
        "logique que TWEET_TAG_LABELS, pour ne jamais risquer une incohérence de comptage)."
    )

    @model_validator(mode="after")
    def _enforce_thread_shape(self) -> ThreadWritingResult:
        """Un thread trop court manque d'impact, un thread trop long fait décrocher — 5 à 8
        tweets est la fourchette cible (voir TODO.md T15, stratégie de contenu viral)."""
        if not self.premise_respectee:
            raise ValueError(
                "L'IA indique elle-même que le thread ne respecte pas le premise fourni — "
                "rejeté plutôt que diffusé (voir TODO.md T15ter), repris au cycle suivant."
            )
        if not 5 <= len(self.tweets) <= 8:
            raise ValueError(f"Un thread doit contenir 5 à 8 tweets, reçu {len(self.tweets)}.")
        for index, tweet in enumerate(self.tweets, start=1):
            if len(tweet) > 260:
                raise ValueError(
                    f"Tweet {index}/{len(self.tweets)} dépasse 260 caractères ({len(tweet)})."
                )
        body_tweets = self.tweets[:-1]
        if any("#" in tweet for tweet in body_tweets):
            raise ValueError(
                "Un hashtag a été détecté dans un tweet du corps (avant le dernier) — interdit, "
                "voir TODO.md T15ter (validator strict, pas une auto-déclaration du modèle)."
            )
        return self


class ThreadTopicRecord(BaseModel):
    """Reflet typé d'une ligne de la table `thread_topics` (T15)."""

    id: int
    group_name: str
    theme: ThreadTheme
    title: str
    premise: str
    created_at: dt.datetime
    last_offered_at: dt.datetime | None = None
    source: str
    concept_id: str | None = None


class ThreadSelectionRecord(BaseModel):
    """Reflet typé d'une ligne de la table `thread_selections` (T15) — l'état du picker
    quotidien (3 options proposées, résolu ou non par une réaction humaine)."""

    id: int
    discord_message_id: str
    option_a_topic_id: int
    option_a_angle: ThreadAngle
    option_b_topic_id: int
    option_b_angle: ThreadAngle
    option_c_topic_id: int
    option_c_angle: ThreadAngle
    status: SelectionStatus
    resolved_topic_id: int | None = None
    resolved_angle: ThreadAngle | None = None
    created_at: dt.datetime
    resolved_at: dt.datetime | None = None


class ThreadRecord(BaseModel):
    """Reflet typé d'une ligne de la table `threads` (T15) — sert aussi de registre de
    consommation via la contrainte `UNIQUE(topic_id, angle)` côté SQL."""

    id: int
    selection_id: int | None = None
    topic_id: int
    angle: ThreadAngle
    hook_label: str | None = None
    tweets: list[str] = Field(default_factory=list)
    status: ThreadStatus
    tokens_in: int = 0
    tokens_out: int = 0
    prompt_version: str | None = None
    created_at: dt.datetime
    sent_at: dt.datetime | None = None
    error: str | None = None


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
