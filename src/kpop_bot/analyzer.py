"""Analyse IA (T5) : filet de sécurité mots-clés France, puis deux appels Gemini séquentiels
— classification (toujours) puis rédaction (uniquement si l'article est retenu)."""

from __future__ import annotations

import logging
import re
import time
from pathlib import Path
from typing import TypeVar

import yaml
from google import genai
from google.genai import errors, types
from pydantic import BaseModel, ValidationError

from kpop_bot.models import (
    ArticleRecord,
    Category,
    ClassificationResult,
    Route,
    ThreadAngle,
    ThreadConcept,
    ThreadTopicRecord,
    ThreadWritingResult,
    TikTokScriptResult,
    TopicIdeationResult,
    Virality,
    WritingResult,
)

logger = logging.getLogger(__name__)

PROMPT_VERSION = "v2"  # v2 : distinction record vérifiable / classement racoleur — voir T13

_T = TypeVar("_T", bound=BaseModel)


class AnalysisError(Exception):
    """Réponse invalide ou erreur API — l'appelant marque l'article FAILED et continue."""


class QuotaExceededError(AnalysisError):
    """429 — signal distinct : l'appelant doit arrêter le cycle, pas marquer FAILED en boucle."""


def load_artist_tiers(path: Path) -> dict[str, list[str]]:
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    return {key: value for key, value in data.items() if key.startswith("tier_")}


def load_thread_concepts(path: Path) -> list[ThreadConcept]:
    """Charge les concepts viraux curés (T15bis, `config/thread_concepts.yaml`) — matière
    première fixe du croisement déterministe groupe × concept, voir
    `thread_pipeline._candidate_pairs`."""
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    return [ThreadConcept(**item) for item in data.get("concepts", [])]


def _format_artist_tiers(tiers: dict[str, list[str]]) -> str:
    lines = [f"- {name} : {', '.join(artists)}" for name, artists in sorted(tiers.items())]
    return "\n".join(lines) if lines else "(aucune donnée de référence disponible)"


def _matches_any_keyword(text: str, keywords: list[str]) -> bool:
    """Recherche insensible à la casse, sur mots/phrases entiers (frontière `\\b`), gère les
    mots-clés multi-mots (ex. 'Stade de France', 'billion views') aussi bien qu'un seul mot."""
    return any(re.search(rf"\b{re.escape(kw)}\b", text, re.IGNORECASE) for kw in keywords)


def matches_france_keywords(item: ArticleRecord, keywords: list[str]) -> bool:
    """Filet de sécurité déterministe (context.md §4). Recherche insensible à la casse, sur
    mots entiers, dans le titre + l'extrait. Pure fonction, testable sans réseau."""
    text = f"{item.title} {item.raw_summary}"
    return _matches_any_keyword(text, keywords)


def matches_viral_milestone(
    item: ArticleRecord, keywords: list[str], artist_tiers: dict[str, list[str]]
) -> bool:
    """Filet de sécurité déterministe pour les records/paliers de streaming (T13) — ex.
    "BLACKPINK's MV becomes 1st K-pop group MV to hit 2.4 billion views". Exige un mot-clé
    de record ET un artiste déjà répertorié dans artist_tiers.yaml : un mot-clé seul (ex.
    'record') serait bien trop générique pour ne pas produire de faux positifs. Pure
    fonction, testable sans réseau."""
    text = f"{item.title} {item.raw_summary}"
    if not _matches_any_keyword(text, keywords):
        return False
    known_artists = [artist for artists in artist_tiers.values() for artist in artists]
    return _matches_any_keyword(text, known_artists)


_CLASSIFICATION_SYSTEM_PROMPT = """\
Tu es l'assistant éditorial d'un média francophone spécialisé K-pop. Pour l'article \
anglophone fourni, classe-le selon quatre axes, en te basant uniquement sur le titre et \
l'extrait donnés.

## Catégorie (choisis-en une seule)
- SCANDALE_DRAMA : controverse, litige contractuel, polémique, affaire judiciaire, départ de \
groupe.
- COMEBACK_SORTIE : retour d'un artiste/groupe avec une nouvelle sortie musicale (single, EP, \
album), teaser, tracklist, date de sortie.
- CONCERT_EVENEMENT_FRANCE : concert, showcase, fan-meeting ou tout événement ayant lieu en \
France ou clairement annoncé pour la France.
- BRUIT_INUTILE : reprise de post réseaux sociaux, publicité déguisée, classement/liste \
subjectif ou putaclic (ex. « Top 10 des idoles les plus riches »), contenu sans fait nouveau \
vérifiable. ATTENTION : un record ou palier VÉRIFIABLE ET CHIFFRÉ (vues, streams, ventes, \
certification) lié à un artiste connu N'EST PAS du bruit, même si le titre ressemble à un \
classement — classe-le normalement (souvent COMEBACK_SORTIE) et évalue sa viralité, qui est en \
général élevée pour ce genre de record.

## Importance
- MINEUR : anecdotique, n'affecte pas la couverture éditoriale.
- MODERE : mérite un traitement mais sans urgence.
- MAJEUR : à traiter en priorité (rupture, annonce confirmée à fort enjeu, événement daté).

## Viralité — uniquement si la catégorie N'EST PAS BRUIT_INUTILE (sinon omets ce champ)
Estime le potentiel de partage social, sur la seule base du texte et de la table de référence \
ci-dessous. C'est une estimation raisonnée, pas une prédiction garantie.
- FAIBLE : intérêt limité, probablement peu partagé.
- MODERE : intérêt correct pour les fans du groupe concerné.
- ELEVE : sujet qui devrait bien circuler au-delà du cœur de fanbase.
- VIRAL : fort potentiel de partage massif (scandale majeur, comeback d'un groupe Tier 1, \
annonce inattendue).
Justifie ton choix en une phrase courte dans `virality_reason`.

### Table de référence — poids des artistes/groupes
{artist_tiers}
Un artiste absent de cette liste n'a ni bonus ni malus explicite.

## Artistes
Liste les noms d'artistes/groupes explicitement cités dans l'article.
{france_note}{milestone_note}
Réponds uniquement selon le schéma JSON fourni.
"""

_FRANCE_NOTE = """

## Note système
Cet article a été pré-identifié comme concernant potentiellement un lieu ou événement en \
France (détection automatique sur mots-clés). Si c'est bien le cas, la catégorie DOIT être \
CONCERT_EVENEMENT_FRANCE. Évalue quand même normalement l'importance et la viralité.
"""

_MILESTONE_NOTE = """

## Note système
Cet article a été pré-identifié comme pouvant décrire un record ou palier vérifiable (vues, \
streams, ventes, certification) atteint par un artiste déjà répertorié dans la table de \
référence (détection automatique). Si c'est bien le cas, ce N'EST PAS un classement racoleur : \
la catégorie ne doit PAS être BRUIT_INUTILE — classe-le normalement (souvent COMEBACK_SORTIE) \
et évalue l'importance et la viralité en conséquence (généralement élevée pour ce type de record).
"""

_WRITING_SYSTEM_PROMPT = """\
Tu es l'assistant éditorial d'un média francophone spécialisé K-pop. L'article suivant a déjà \
été classé : catégorie {category}, importance {importance}, viralité {virality}. Rédige en \
français, dans le style d'un média d'actualité :

- `summary_fr` : un résumé de 2 phrases, ton journalistique, directement exploitable pour un \
tri rapide.
- `tweet_draft` : un brouillon de tweet PRÊT À PUBLIER (jamais posté automatiquement — un \
humain le relit toujours avant). 260 caractères maximum (un tag visuel sera ajouté séparément \
devant, ne l'inclus pas). Ton journalistique neutre. 1 à 2 emojis maximum. Exactement 2 \
hashtags pertinents (ex. #KPop et le nom du groupe). En français. {engagement_hook}
{video_instruction}
Réponds uniquement selon le schéma JSON fourni.
"""

_VIDEO_INSTRUCTION = """
- `video_summary` : un résumé plus détaillé (4 à 6 phrases), pensé pour servir de base à un \
script vidéo — contexte, faits, ce qui rend le sujet notable.
"""

# Consigne de fin de tweet, pour créer de l'engagement — varie selon la catégorie déjà connue.
# Volontairement laissé à l'IA (contrairement au tag) : le bon accroche dépend du contenu
# précis de l'article, un gabarit figé sonnerait vite répétitif.
_ENGAGEMENT_HOOKS: dict[Category, str] = {
    Category.COMEBACK_SORTIE: (
        "Termine par une question courte qui donne envie de réagir : par exemple une "
        "invitation à noter le son sur 10, ou à dire ce qu'on en pense."
    ),
    Category.SCANDALE_DRAMA: (
        "Termine par une question courte et neutre qui invite à donner son avis sur la "
        "situation, sans prendre parti ni sensationnaliser."
    ),
    Category.CONCERT_EVENEMENT_FRANCE: (
        "Termine par une question courte qui invite les lecteurs à dire s'ils comptent y assister."
    ),
}

# Prompt dédié (T14), séparé de _WRITING_SYSTEM_PROMPT : demander au même appel de réussir un
# tweet court et contraint ET un script plus long avec des idées de montage dilue la qualité
# des deux sur un modèle lite — un prompt à objectif unique est plus fiable.
#
# Pas de schéma JSON recopié en dur dans le texte : `response_schema=TikTokScriptResult`
# contraint déjà la sortie côté API (decoding contraint) — le redemander en prose serait
# redondant, et si jamais les deux divergeaient, c'est toujours response_schema qui gagne.
_TIKTOK_SYSTEM_PROMPT = """\
Tu es scénariste pour une chaîne TikTok francophone spécialisée K-pop. L'article suivant a \
déjà été classé : catégorie {category}, importance {importance}, viralité {virality}.

Ta mission : rédiger un script court, prêt à être tourné par un humain (jamais publié \
automatiquement), optimisé pour la rétention et le partage sur TikTok.

RÈGLE ABSOLUE : toute information (chiffre, nom, événement, citation) doit provenir \
strictement du titre et de l'extrait fournis. Si une information manque, reste plus bref \
plutôt que d'inventer.

RÈGLE ABSOLUE : aucun emoji, nulle part (hook, texte à l'écran, script, chute, légende) — \
contrairement au tweet, qui en autorise. Le ton doit rester percutant sans smiley.

STRUCTURE (le hook pose une promesse, le corps doit la tenir avant la fin — ne jamais \
promettre une information que le corps ne livre pas) :

1. `hook` (1-2 phrases, ~15-20 mots, ~5-6 secondes à l'oral) : capte l'attention en 2-3 \
secondes — question, chiffre marquant, ou affirmation qui surprend. Formule une promesse \
claire et vérifiable dans l'article.
2. `on_screen_texte` (5-8 mots maximum) : texte overlay affiché dès les 2 premières secondes, \
renforce visuellement le hook parlé (pour les spectateurs sans son).
3. `script_body` (4-6 phrases, ~60-80 mots, ~20-25 secondes à l'oral) : ton oral et \
dynamique, comme si on parlait à la caméra. Développe le sujet avec plus de détail que le \
tweet déjà rédigé. Doit explicitement livrer la promesse du hook. Si le sujet le permet, \
inclure une micro-relance vers le milieu du script (ex. « mais attends, c'est pas tout... ») \
pour retenir l'attention jusqu'au bout.
4. `closing_hook` (1 phrase courte, ~3-4 secondes à l'oral) : priorité au déclencheur de \
partage plutôt qu'au simple commentaire — ex. inviter à taguer quelqu'un de concerné, ou \
poser une question qui divise l'audience K-pop. Registre oral, cohérent avec la chute du \
tweet déjà rédigé.
5. `visual_ideas` (3 à 5 suggestions courtes) : plans/images pour le montage (archives, \
zoom, texte à l'écran secondaire...). Seule partie du script où l'improvisation créative \
est acceptée.
6. `caption_seo` : légende courte incluant les mots-clés naturels de l'article, et 3-5 \
hashtags pertinents pour la niche K-pop, mêlant hashtags larges et hashtags de niche.

DURÉE CIBLE TOTALE : environ 30-40 secondes à l'oral (hook + script_body + closing_hook).

AVANT DE RÉPONDRE : relis le hook, le script_body et le closing_hook, et vérifie qu'aucune \
phrase ne contient une information (nom, chiffre, événement) absente du titre et de \
l'extrait fournis, et qu'aucun emoji ne s'est glissé nulle part. Corrige si besoin.

Exemple de ton attendu (article fictif, à ne jamais réutiliser tel quel — sert uniquement à \
calibrer le ton, pas à copier la structure des phrases) :
{{
  "hook": "Un membre du groupe vient de battre un record que personne n'avait touché depuis \
10 ans.",
  "on_screen_texte": "RECORD BATTU",
  "script_body": "Alors ce qui vient de se passer, personne ne s'y attendait. Le titre solo \
qu'il vient de sortir a dépassé en 24h un chiffre que même les plus gros comebacks du groupe \
n'avaient jamais atteint. Mais attends, c'est pas fini : ce record tenait depuis plus de 10 \
ans dans l'industrie. Les fans parlent déjà d'un tournant dans sa carrière solo.",
  "closing_hook": "Tague la personne qui va halluciner en voyant ce chiffre.",
  "visual_ideas": [
    "Zoom sur le compteur de vues qui grimpe",
    "Archive du comeback précédent en comparaison",
    "Texte à l'écran avec le chiffre exact du record",
    "Réactions de fans en incrustation"
  ],
  "caption_seo": {{
    "legende": "Il vient de battre un record vieux de 10 ans",
    "hashtags": ["#kpop", "#kpopnews", "#comeback"]
  }}
}}

Réponds uniquement selon le schéma JSON fourni.
"""


# --- T15 : threads Twitter quotidiens — idéation de sujets + rédaction du thread. Prompts
# dédiés (un par forme de sortie, même principe qu'en T14). Depuis T15bis, l'idéation ne
# choisit plus librement le groupe/thème : ils viennent d'un croisement déterministe
# groupe × concept curé (voir thread_pipeline._candidate_pairs) — seule la rédaction du
# titre/premise reste confiée à l'IA. La relecture humaine avant copier-coller sur X reste la
# protection ultime avant publication, comme pour tout le reste du pipeline. ---


def _format_candidate_pairs(pairs: list[tuple[str, ThreadConcept]]) -> str:
    return "\n".join(
        f'- pair_index={index} : groupe "{group}", concept "{concept.label}" — '
        f"{concept.brief.strip()}"
        for index, (group, concept) in enumerate(pairs)
    )


# T15bis — idéation hybride : les paires (groupe, concept) sont choisies en amont, en code,
# par croisement déterministe (voir thread_pipeline._candidate_pairs). L'IA ne rédige plus que
# le titre/premise pour chaque paire déjà fixée — elle ne peut plus inventer ni le groupe ni le
# thème, ce qui élimine le risque d'un sujet halluciné ou hors-sol (voir TODO.md T15bis).
_THREAD_IDEATION_SYSTEM_PROMPT = """\
Tu es responsable éditorial(e) d'un média francophone spécialisé K-pop, en charge du contenu \
Twitter/X. On te fournit ci-dessous une liste de {count} paires (groupe, concept) déjà choisies \
— ta seule mission est de rédiger, pour CHAQUE paire, un titre court et une "premise" (1-2 \
phrases : la promesse de fond du sujet) qui exploitent spécifiquement ce groupe et ce concept. \
Tu ne dois JAMAIS changer le groupe ni le concept proposé, ni en inventer d'autres — uniquement \
écrire un contenu sur-mesure pour le lien entre les deux.

## Paires à traiter
{pairs}

## Consignes
- Réponds pour CHAQUE paire ci-dessus, en rappelant son `pair_index` pour un rattachement sûr.
- Le titre et la premise doivent être spécifiques au groupe cité — évite un texte générique qui \
s'appliquerait tel quel à n'importe quel groupe.
- Reste sur des faits largement connus et vérifiables — un humain relira toujours le thread \
final avant publication, mais ne pars pas d'une prémisse déjà fausse.

Réponds uniquement selon le schéma JSON fourni ({count} éléments, un par paire, chacun avec son \
`pair_index`).
"""

_THREAD_ANGLE_INSTRUCTIONS: dict[ThreadAngle, str] = {
    ThreadAngle.CONTRARIEN: (
        "Défends une prise de position à contre-courant de l'avis commun sur ce sujet — sans "
        "être gratuit ni provocateur pour provoquer : l'angle contrarien doit rester "
        "défendable et argumenté."
    ),
    ThreadAngle.GUIDE_PRATIQUE: (
        "Structure le thread comme un guide concret (« 3 choses à savoir sur... », « comment "
        "reconnaître... ») — chaque tweet intermédiaire livre un point actionnable ou "
        "vérifiable, pas une opinion vague."
    ),
    ThreadAngle.CAS_ETUDE: (
        "Choisis UN exemple précis lié au sujet et dissèque-le en profondeur (contexte, "
        "déroulé, conséquences) plutôt que de survoler plusieurs exemples."
    ),
    ThreadAngle.STORYTELLING: (
        "Raconte le sujet comme une histoire, dans l'ordre chronologique, avec un vrai twist "
        "ou une tension qui se dénoue vers la fin — pas une simple liste de faits."
    ),
}

# Registre de hooks viraux (T15) — pas de table dédiée : la diversité se pilote en excluant les
# labels déjà utilisés récemment (`storage.recent_hook_labels`), voir `_pick_hook_label`.
_HOOK_TEMPLATES: dict[str, str] = {
    "chiffre_marquant": "Ouvre sur un chiffre concret et surprenant lié au sujet.",
    "affirmation_contrariante": (
        "Ouvre sur une affirmation qui va à contre-courant de l'avis général."
    ),
    "question_qui_pique": "Ouvre sur une question directe que peu de gens se posent vraiment.",
    "angle_surprise": (
        "Ouvre en jouant sur la surprise (« Ce que tu ne savais probablement pas sur... ») "
        "sans tomber dans le putaclic."
    ),
    "petite_histoire": "Ouvre en plantant une scène précise, comme le début d'une anecdote.",
}

_THREAD_WRITING_SYSTEM_PROMPT = """\
Tu es scénariste Twitter/X pour un média francophone spécialisé K-pop. Rédige un thread complet \
sur le sujet suivant :

Groupe/portée : {group_name}
Thème : {theme}
Titre : {title}
Promesse : {premise}

Angle imposé : {angle_label}
{angle_instruction}

## Structure attendue
1. Tweet 1 (hook) : capte l'attention en une phrase — chiffre marquant, affirmation \
contrariante, ou question. JAMAIS de méta-commentaire du type « un thread 🧵 » ou « petit \
thread sur... ». Pose une promesse claire que la suite doit tenir.
2. Tweets intermédiaires (3 à 6) : un point par tweet, auto-porteur mais connecté au précédent \
(ex. « mais attends, ce n'est pas tout... »).
3. Dernier tweet : clôture orientée partage — une question qui divise ou invite à réagir, \
jamais un appel explicite au retweet (pénalisé par l'algorithme X aujourd'hui).

## Règles de forme
- 5 à 8 tweets au total. Chaque tweet ≤260 caractères (la numérotation "n/total" sera ajoutée \
séparément, ne l'inclus pas).
- Aucun hashtag dans les tweets du corps. Au plus 1-2 hashtags, uniquement sur le tout dernier \
tweet.
- 1 emoji maximum par tweet, pour le repérage visuel — jamais de décoration.
- Prudence factuelle : reste sur des faits largement connus/publics ; si tu n'es pas sûr·e d'un \
chiffre ou d'une citation précise, formule plus généralement plutôt que d'inventer.

## Styles de hook à éviter (déjà utilisés récemment, pour ne pas être répétitif)
{recent_hooks}

## Avant de répondre
Relis le tweet 1 : pose-t-il une vraie promesse ? Les tweets suivants la tiennent-ils \
explicitement avant la fin ? Corrige si besoin.

Réponds uniquement selon le schéma JSON fourni (liste ordonnée de tweets).
"""


class GeminiAnalyzer:
    """Implémentation Gemini de l'analyse. Le reste du pipeline ne dépend que de `classify()`
    et `write()` — basculer vers un autre fournisseur (ex. Groq) n'impacte que cette classe."""

    def __init__(
        self,
        *,
        api_keys: list[str],
        models: list[str],
        artist_tiers: dict[str, list[str]],
        min_seconds_between_calls: float = 4.5,
    ) -> None:
        if not api_keys:
            raise ValueError("api_keys ne peut pas être vide.")
        if not models:
            raise ValueError("models ne peut pas être vide.")
        self._clients = [genai.Client(api_key=key) for key in api_keys]
        self._models = models
        self._artist_tiers_text = _format_artist_tiers(artist_tiers)
        # 15 RPM sur gemini-3.5-flash-lite / gemini-3.1-flash-lite -> 1 appel/4s max.
        # 4.5s laisse ~11% de marge. Espace CHAQUE appel réel (classify, write, et une
        # éventuelle tentative de secours) — voir `_throttle()`. 0 = désactivé (tests).
        self._min_interval = min_seconds_between_calls
        self._last_call_at: float | None = None

    def classify(
        self, item: ArticleRecord, *, france_flag: bool, milestone_flag: bool = False
    ) -> tuple[ClassificationResult, int, int]:
        system_prompt = _CLASSIFICATION_SYSTEM_PROMPT.format(
            artist_tiers=self._artist_tiers_text,
            france_note=_FRANCE_NOTE if france_flag else "",
            milestone_note=_MILESTONE_NOTE if milestone_flag else "",
        )
        user_content = f"Source : {item.source}\nTitre : {item.title}\nExtrait : {item.raw_summary}"
        result, tokens_in, tokens_out = self._generate_with_fallback(
            system_prompt, user_content, ClassificationResult
        )

        if france_flag and result.category != Category.CONCERT_EVENEMENT_FRANCE:
            logger.info(
                "Filet France : catégorie forcée à CONCERT_EVENEMENT_FRANCE pour %s "
                "(l'IA avait répondu %s).",
                item.url,
                result.category,
            )
            # Reconstruction via le constructeur (et non model_copy) pour que le validator
            # de cohérence virality/catégorie se redéclenche sur la nouvelle catégorie.
            result = ClassificationResult(
                **{**result.model_dump(), "category": Category.CONCERT_EVENEMENT_FRANCE}
            )
        elif milestone_flag and result.category == Category.BRUIT_INUTILE:
            # Dernier recours : l'IA a maintenu BRUIT_INUTILE malgré la note système. On force
            # une catégorie et une viralité par défaut plutôt que de laisser filtrer un record
            # potentiellement très partageable — voir T13. Moins strict que le filet France
            # (pas de catégorie unique garantie), donc une correction, pas une règle dure.
            logger.info(
                "Filet record/palier : catégorie forcée à COMEBACK_SORTIE pour %s (l'IA avait "
                "maintenu BRUIT_INUTILE malgré la note système).",
                item.url,
            )
            result = ClassificationResult(
                **{
                    **result.model_dump(),
                    "category": Category.COMEBACK_SORTIE,
                    "virality": Virality.ELEVE,
                    "virality_reason": (
                        "Filet record/palier : mention d'un record chiffré (vues/streams/"
                        "ventes) associée à un artiste répertorié — viralité estimée par "
                        "défaut, à ajuster si besoin."
                    ),
                }
            )
        return result, tokens_in, tokens_out

    def write(
        self, item: ArticleRecord, classification: ClassificationResult, route: Route
    ) -> tuple[WritingResult, int, int]:
        system_prompt = _WRITING_SYSTEM_PROMPT.format(
            category=classification.category.value,
            importance=classification.importance.value,
            virality=classification.virality.value if classification.virality else "N/A",
            engagement_hook=_ENGAGEMENT_HOOKS.get(classification.category, ""),
            video_instruction=_VIDEO_INSTRUCTION if route == Route.A else "",
        )
        user_content = f"Titre : {item.title}\nExtrait : {item.raw_summary}\nLien : {item.url}"
        return self._generate_with_fallback(system_prompt, user_content, WritingResult)

    def write_tiktok_script(
        self, item: ArticleRecord, classification: ClassificationResult
    ) -> tuple[TikTokScriptResult, int, int]:
        """3e appel, dédié (T14) — Route A uniquement, appelé par le pipeline sur une instance
        séparée dont l'ordre des clés API peut différer de celle de classify()/write() (voir
        pipeline.py `_tiktok_api_keys`)."""
        system_prompt = _TIKTOK_SYSTEM_PROMPT.format(
            category=classification.category.value,
            importance=classification.importance.value,
            virality=classification.virality.value if classification.virality else "N/A",
        )
        user_content = f"Titre : {item.title}\nExtrait : {item.raw_summary}\nLien : {item.url}"
        return self._generate_with_fallback(system_prompt, user_content, TikTokScriptResult)

    def ideate_thread_topics(
        self, *, candidate_pairs: list[tuple[str, ThreadConcept]]
    ) -> tuple[TopicIdeationResult, int, int]:
        """1 seul appel pour réapprovisionner le backlog de Topics (T15bis) — même principe de
        coût maîtrisé que le digest hebdomadaire (T12) : un appel par lot, pas un par sujet. Les
        paires (groupe, concept) sont déjà choisies par le code avant cet appel (croisement
        déterministe, voir `thread_pipeline._candidate_pairs`) — l'IA ne rédige que le
        titre/premise, elle ne peut plus inventer ni le groupe ni le thème."""
        system_prompt = _THREAD_IDEATION_SYSTEM_PROMPT.format(
            count=len(candidate_pairs),
            pairs=_format_candidate_pairs(candidate_pairs),
        )
        user_content = (
            f"Rédige un titre et une premise pour chacune des {len(candidate_pairs)} paires "
            "ci-dessus."
        )
        return self._generate_with_fallback(system_prompt, user_content, TopicIdeationResult)

    def _pick_hook_label(self, recent_hook_labels: list[str]) -> str:
        """Exclut les styles déjà utilisés récemment (voir `storage.recent_hook_labels`) ; si
        tous les styles ont été vus récemment (registre restreint), retombe sur le premier
        plutôt que d'échouer — un léger risque de répétition vaut mieux qu'un cycle bloqué."""
        available = [label for label in _HOOK_TEMPLATES if label not in recent_hook_labels]
        return available[0] if available else next(iter(_HOOK_TEMPLATES))

    def write_thread(
        self, topic: ThreadTopicRecord, angle: ThreadAngle, *, recent_hook_labels: list[str]
    ) -> tuple[ThreadWritingResult, str, int, int]:
        """2e appel de T15, déclenché uniquement après la réaction humaine de sélection — jamais
        avant. Retourne aussi le `hook_label` choisi, pour que l'appelant l'enregistre sur la
        ligne `threads` et alimente la désaturation du prochain choix."""
        hook_label = self._pick_hook_label(recent_hook_labels)
        system_prompt = _THREAD_WRITING_SYSTEM_PROMPT.format(
            group_name=topic.group_name,
            theme=topic.theme.value,
            title=topic.title,
            premise=topic.premise,
            angle_label=angle.value,
            angle_instruction=_THREAD_ANGLE_INSTRUCTIONS[angle],
            recent_hooks=", ".join(recent_hook_labels) if recent_hook_labels else "(aucun)",
        )
        user_content = f"Style de hook imposé pour le tweet 1 : {_HOOK_TEMPLATES[hook_label]}"
        result, tokens_in, tokens_out = self._generate_with_fallback(
            system_prompt, user_content, ThreadWritingResult
        )
        return result, hook_label, tokens_in, tokens_out

    def _generate_with_fallback(
        self, system_prompt: str, user_content: str, schema: type[_T]
    ) -> tuple[_T, int, int]:
        """Essaie chaque modèle de la chaîne (principal puis secours, quota indépendant mais
        même clé API), dans l'ordre, sur la première clé. Uniquement si les trois échouent en
        429, repart au début de la chaîne de modèles sur la clé API suivante (2e compte Google,
        quota totalement indépendant — voir T5quinquies). Si la toute dernière combinaison
        clé/modèle échoue aussi, l'erreur remonte normalement : le filet de sécurité existant
        (article repris au cycle suivant, voir pipeline.py) reste la dernière protection."""
        attempts = [
            (key_index, model) for key_index in range(len(self._clients)) for model in self._models
        ]
        for attempt_index, (key_index, model) in enumerate(attempts):
            try:
                return self._generate(
                    self._clients[key_index], model, system_prompt, user_content, schema
                )
            except QuotaExceededError:
                if attempt_index == len(attempts) - 1:
                    raise
                next_key_index, next_model = attempts[attempt_index + 1]
                logger.warning(
                    "Quota atteint sur %s (clé n°%d) — bascule sur %s (clé n°%d).",
                    model,
                    key_index + 1,
                    next_model,
                    next_key_index + 1,
                )

    def _throttle(self) -> None:
        """Espace les appels réels pour rester sous la limite RPM. Un throttle partagé entre
        principal et secours est plus prudent qu'un compteur par modèle — coût négligeable
        vu la rareté du secours."""
        if self._last_call_at is not None:
            wait = self._min_interval - (time.monotonic() - self._last_call_at)
            if wait > 0:
                time.sleep(wait)
        self._last_call_at = time.monotonic()

    def _generate(
        self,
        client: genai.Client,
        model: str,
        system_prompt: str,
        user_content: str,
        schema: type[_T],
    ) -> tuple[_T, int, int]:
        self._throttle()
        try:
            response = client.models.generate_content(
                model=model,
                contents=user_content,
                config=types.GenerateContentConfig(
                    system_instruction=system_prompt,
                    response_mime_type="application/json",
                    response_schema=schema,
                ),
            )
        except errors.APIError as exc:
            if exc.code == 429:
                raise QuotaExceededError(str(exc)) from exc
            raise AnalysisError(f"Erreur API Gemini ({exc.code}) : {exc.message}") from exc

        try:
            parsed = schema.model_validate_json(response.text)
        except (ValidationError, ValueError) as exc:
            raise AnalysisError(f"Réponse Gemini invalide : {exc}") from exc

        usage = response.usage_metadata
        tokens_in = int(getattr(usage, "prompt_token_count", 0) or 0)
        tokens_out = int(getattr(usage, "candidates_token_count", 0) or 0)
        return parsed, tokens_in, tokens_out


__all__ = [
    "PROMPT_VERSION",
    "AnalysisError",
    "QuotaExceededError",
    "GeminiAnalyzer",
    "load_artist_tiers",
    "load_thread_concepts",
    "matches_france_keywords",
    "matches_viral_milestone",
]
