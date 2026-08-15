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
    SocialVisualContent,
    ThreadAngle,
    ThreadConcept,
    ThreadTopicRecord,
    ThreadWritingResult,
    TopicIdeationResult,
    Virality,
    WritingResult,
)

logger = logging.getLogger(__name__)

PROMPT_VERSION = "v5"  # v5 : 3e appel SocialVisualContent (titre + points clés pour le visuel
# social 9:16, TOUTES routes retenues) remplace le post Instagram — celui-ci n'est plus utilisé
# (voir TODO.md). v4 avait ajouté `headline_fr` directement au 2e appel (rédaction) ; retiré ici,
# déplacé vers ce nouvel appel dédié. v3 : contexte de page scrapée (T18) + post Instagram
# remplace le script TikTok de T14. v2 : distinction record vérifiable / classement racoleur
# (T13).

_T = TypeVar("_T", bound=BaseModel)


class AnalysisError(Exception):
    """Réponse invalide ou erreur API — l'appelant marque l'article FAILED et continue."""


class QuotaExceededError(AnalysisError):
    """429 (quota) ou 5xx (erreur serveur transitoire — surcharge, indisponibilité momentanée) —
    signal distinct : déclenche le repli sur le modèle/la clé suivante, et si toute la chaîne est
    épuisée, l'appelant arrête le cycle plutôt que de marquer FAILED en boucle. Élargi aux 5xx
    suite à un incident réel (T15ter, 28/07/2026) : un 503 sur le modèle principal remontait
    directement en `AnalysisError` sans jamais essayer les modèles de secours, alors que c'est
    exactement le genre de panne transitoire qu'une chaîne de repli est censée absorber."""


# Codes HTTP serveur transitoires (surcharge, indisponibilité momentanée, timeout amont) — voir
# QuotaExceededError. Distinct des erreurs client (400, 404...) qui ne se résoudront jamais en
# retentant sur un autre modèle/une autre clé.
_RETRYABLE_SERVER_ERROR_CODES = (500, 502, 503, 504)


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


def _page_context_block(page_text: str | None) -> str:
    """Bloc optionnel injecté en fin de prompt (T18) — vide si aucun texte de page n'a été
    scrapé pour cet article (voir scraper.py, best-effort partout)."""
    return _PAGE_CONTEXT_BLOCK.format(page_text=page_text) if page_text else ""


def _known_artists_text(artists: list[str]) -> str:
    """Texte injecté dans les prompts de rédaction pour forcer un nom précis plutôt qu'une
    formulation aussi vague qu'un titre racoleur (voir TODO.md T18)."""
    if artists:
        return ", ".join(artists)
    return "non identifié précisément — reste général plutôt que d'inventer un nom"


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


_PAGE_CONTEXT_BLOCK = """
## Contexte additionnel (extrait de la page article, en plus du titre/extrait ci-dessus)
{page_text}

Utilise ce contexte en priorité pour identifier précisément les artistes/groupes cités, \
notamment quand le titre reste volontairement vague à leur sujet (titre racoleur) — le nom \
exact apparaît souvent seulement dans le corps de l'article, jamais dans le titre.
"""

_CLASSIFICATION_SYSTEM_PROMPT = """\
Tu es l'assistant éditorial d'un média francophone spécialisé K-pop. Pour l'article \
anglophone fourni, classe-le selon quatre axes, en te basant sur le titre, l'extrait, et le \
contexte additionnel de page fourni ci-dessous s'il y en a un.

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
Liste les noms d'artistes/groupes explicitement cités dans l'article. Si le titre est vague à \
leur sujet mais qu'un nom apparaît dans l'extrait ou le contexte additionnel, cite-le quand \
même — c'est justement ce que la rédaction a besoin de savoir.
{france_note}{milestone_note}{page_context_block}
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
été classé : catégorie {category}, importance {importance}, viralité {virality}. \
Artiste(s)/groupe(s) déjà identifié(s) à l'étape de classification : {known_artists}. Rédige \
en français, dans le style d'un média d'actualité :

- `summary_fr` : un résumé de 2 phrases, ton journalistique, directement exploitable pour un \
tri rapide.
- `tweet_draft` : un brouillon de tweet PRÊT À PUBLIER (jamais posté automatiquement — un \
humain le relit toujours avant). 260 caractères maximum (un tag visuel sera ajouté séparément \
devant, ne l'inclus pas). Ton journalistique neutre. 1 à 2 emojis maximum. Exactement 2 \
hashtags pertinents (ex. #KPop et le nom du groupe). En français. IMPORTANT : nomme \
précisément l'artiste/groupe concerné (voir liste ci-dessus) — ne reste jamais aussi vague \
qu'un titre racoleur qui ne le citerait pas. {engagement_hook}
{video_instruction}{page_context_block}
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

# Prompt dédié — remplace le post Instagram de T18 (lui-même un remplacement du script TikTok
# de T14), séparé de _WRITING_SYSTEM_PROMPT : demander au même appel un tweet court et contraint
# ET un titre + points clés pour un format visuel différent dilue la qualité des deux sur un
# modèle lite. Déclenché pour TOUTE route retenue (A, B, CONCERT), pas seulement Route
# A/CONCERT comme les fonctionnalités précédentes — voir pipeline.py. Reprend les mêmes bonnes
# pratiques déjà validées pour le thread Twitter (T15ter) et le post Instagram : mots bannis,
# few-shot, auto-vérification — pas de schéma JSON recopié en dur, `response_schema=
# SocialVisualContent` contraint déjà la sortie côté API et prime toujours en cas de divergence.
_SOCIAL_VISUAL_SYSTEM_PROMPT = """\
Tu es responsable éditorial(e) d'un compte TikTok/Instagram francophone d'actu K-pop, en charge \
du texte affiché sur un visuel vertical type « card d'actu » (image en haut, texte en bas — pas \
un tweet, pas un post à lire en entier). L'article suivant a déjà été classé : catégorie \
{category}, importance {importance}, viralité {virality}. Artiste(s)/groupe(s) déjà \
identifié(s) à l'étape de classification : {known_artists}.

RÈGLE ABSOLUE : toute information (chiffre, nom, événement, citation) doit provenir \
strictement du titre, de l'extrait, ou du contexte additionnel de page fourni ci-dessous s'il \
y en a un. Si une information manque, reste plus général plutôt que d'inventer. Nomme \
précisément l'artiste/groupe identifié ci-dessus — ne reste jamais aussi vague qu'un titre \
racoleur qui ne le citerait pas.

## Style — bannis le « langage IA »
Interdiction d'utiliser : « en effet », « cependant », « néanmoins », « il est important de \
noter », « plongeons dans », « crucial », « véritable », « incontournable », « d'ailleurs », \
« il convient de », « au cœur de », « décryptage ». Écris comme un rédacteur qui poste à chaud \
: phrases courtes, direct.

## Structure attendue
1. `headline_fr` : une accroche courte et percutante, 10 à 15 mots MAXIMUM — le fait le plus \
marquant de l'article, formulation choc plutôt qu'une phrase grammaticale complète, jamais de \
point final.
2. `key_points_fr` : 2 à 3 points clés COURTS (une phrase chacun, ~15-20 mots), qui \
COMPLÈTENT l'accroche SANS JAMAIS LA RÉPÉTER — chaque point doit apporter une information \
distincte (contexte, réaction, conséquence, chiffre) que l'accroche ne dit pas déjà. Si \
l'article n'offre vraiment que 2 informations distinctes, n'en donne que 2 : mieux vaut 2 \
points solides qu'un 3e redondant.

AVANT DE RÉPONDRE : relis l'accroche et les points clés ensemble — vérifie qu'aucun des deux \
ne répète l'autre mot pour mot ou en substance. Corrige si besoin.

Exemple de ton attendu (sujet fictif, à ne jamais réutiliser tel quel — sert uniquement à \
calibrer le style, pas à copier la structure des phrases) :
{{
  "headline_fr": "Le groupe confirme un comeback surprise pour le mois prochain",
  "key_points_fr": [
    "Un premier extrait a été dévoilé ce matin, sans aucune fuite avant l'annonce officielle.",
    "L'agence promet un concept radicalement différent des sorties précédentes."
  ]
}}
{page_context_block}
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
Tu es scénariste Twitter/X pour un média francophone spécialisé K-pop, avec la plume d'un \
passionné qui poste sur son propre compte — pas d'un rédacteur institutionnel. Rédige, en \
français, un thread complet sur le sujet suivant :

Groupe/portée : {group_name}
Thème : {theme}
Titre : {title}
Promesse : {premise}

Angle imposé : {angle_label}
{angle_instruction}

## Style — bannis le « langage IA »
Interdiction d'utiliser : « en effet », « cependant », « néanmoins », « il est important de \
noter », « plongeons dans », « crucial », « véritable », « incontournable », « d'ailleurs », \
« il convient de », « au cœur de », « décryptage ». Écris comme un passionné qui tweete à chaud \
: phrases courtes, direct, oralité maîtrisée (contractions, ponctuation dynamique — tirets, \
deux-points).

## Structure attendue
1. Tweet 1 (hook) : DOIT contenir deux éléments indissociables — (a) une accroche qui stoppe le \
scroll (chiffre marquant, affirmation contrariante, ou question — voir le style imposé \
ci-dessous) et (b) la promesse exacte de ce que le thread va montrer/apprendre. Interdit de \
démarrer par le seul nom du groupe sans accroche. JAMAIS de méta-commentaire du type « un \
thread 🧵 » ou « petit thread sur... ».
2. Tweets intermédiaires (3 à 6) : un point par tweet. Chaque tweet se termine sur une « boucle \
ouverte » qui appelle le suivant (ex. « Mais le plus surprenant est arrivé après. », « Et c'est \
là que tout a basculé : ») — jamais une simple liste de faits juxtaposés.
3. Dernier tweet : clôture orientée partage — une question qui divise ou invite à réagir, \
jamais un appel explicite au retweet (pénalisé par l'algorithme X aujourd'hui).

## Mise en forme
- Aère avec des sauts de ligne les tweets qui portent plusieurs idées — inutile de forcer une \
structure sur un tweet déjà court et percutant (en particulier le hook et la clôture).
- Ponctuation dynamique bienvenue (tirets —, deux-points :) pour rythmer la lecture.
- 5 à 8 tweets au total. Chaque tweet ≤260 caractères (la numérotation "n/total" sera ajoutée \
séparément, ne l'inclus pas).
- Aucun hashtag dans les tweets du corps. Au plus 1-2 hashtags, uniquement sur le tout dernier \
tweet.
- 1 emoji maximum par tweet, pour le repérage visuel — jamais de décoration.
- Prudence factuelle : reste sur des faits largement connus/publics ; si tu n'es pas sûr·e d'un \
chiffre ou d'une citation précise, formule plus généralement plutôt que d'inventer.

## Styles de hook à éviter (déjà utilisés récemment, pour ne pas être répétitif)
{recent_hooks}

## Exemple de ton attendu (sujet fictif, à ne jamais réutiliser tel quel — sert uniquement à \
calibrer le style, pas à copier la structure des phrases)
{{
  "premise_respectee": true,
  "tweets": [
    "Un groupe qui vend 2 millions d'albums peut s'effondrer en un seul été. Voici comment ça \
s'est joué.",
    "2019 : le groupe cartonne, tournée mondiale complète. Personne n'imagine ce qui arrive.",
    "L'agence annonce une pause \\"pour repos\\". Sauf que la pause dure 3 ans.",
    "Et c'est là que tout bascule : deux membres quittent le label, en silence.",
    "Résultat aujourd'hui ? Le groupe existe encore sur le papier — mais plus personne ne parie \
sur un retour ensemble.",
    "Toi, tu penses qu'ils remonteront un jour sur scène tous les cinq ?"
  ]
}}

## Avant de répondre — vérifie honnêtement (le champ `premise_respectee` doit refléter une \
vraie vérification, pas une réponse automatique)
- Le thread tient-il réellement la promesse énoncée dans `Promesse : {premise}` ci-dessus — pas \
seulement celle du tweet 1 ?
- Le ton est-il celui d'un passionné qui tweete, sans aucun mot banni ci-dessus ?
- Aucun hashtag ne s'est glissé avant le tout dernier tweet ?

Réponds uniquement selon le schéma JSON fourni.
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
        self,
        item: ArticleRecord,
        *,
        france_flag: bool,
        milestone_flag: bool = False,
        page_text: str | None = None,
    ) -> tuple[ClassificationResult, int, int]:
        system_prompt = _CLASSIFICATION_SYSTEM_PROMPT.format(
            artist_tiers=self._artist_tiers_text,
            france_note=_FRANCE_NOTE if france_flag else "",
            milestone_note=_MILESTONE_NOTE if milestone_flag else "",
            page_context_block=_page_context_block(page_text),
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
        self,
        item: ArticleRecord,
        classification: ClassificationResult,
        route: Route,
        *,
        page_text: str | None = None,
    ) -> tuple[WritingResult, int, int]:
        system_prompt = _WRITING_SYSTEM_PROMPT.format(
            category=classification.category.value,
            importance=classification.importance.value,
            virality=classification.virality.value if classification.virality else "N/A",
            known_artists=_known_artists_text(classification.artists),
            engagement_hook=_ENGAGEMENT_HOOKS.get(classification.category, ""),
            video_instruction=_VIDEO_INSTRUCTION if route == Route.A else "",
            page_context_block=_page_context_block(page_text),
        )
        user_content = f"Titre : {item.title}\nExtrait : {item.raw_summary}\nLien : {item.url}"
        return self._generate_with_fallback(system_prompt, user_content, WritingResult)

    def write_social_visual(
        self,
        item: ArticleRecord,
        classification: ClassificationResult,
        *,
        page_text: str | None = None,
    ) -> tuple[SocialVisualContent, int, int]:
        """3e appel, dédié — remplace le post Instagram (T18). Contrairement à celui-ci, appelé
        pour TOUTE route retenue (A, B, CONCERT), pas seulement Route A/CONCERT : le visuel
        social 9:16 couvre tous les articles envoyés (voir pipeline.py, social_pipeline.py)."""
        system_prompt = _SOCIAL_VISUAL_SYSTEM_PROMPT.format(
            category=classification.category.value,
            importance=classification.importance.value,
            virality=classification.virality.value if classification.virality else "N/A",
            known_artists=_known_artists_text(classification.artists),
            page_context_block=_page_context_block(page_text),
        )
        user_content = f"Titre : {item.title}\nExtrait : {item.raw_summary}\nLien : {item.url}"
        return self._generate_with_fallback(system_prompt, user_content, SocialVisualContent)

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
        429 ou en erreur serveur transitoire (5xx, voir `QuotaExceededError`), repart au début
        de la chaîne de modèles sur la clé API suivante (2e compte Google, quota totalement
        indépendant — voir T5quinquies). Si la toute dernière combinaison clé/modèle échoue
        aussi, l'erreur remonte normalement : le filet de sécurité existant (article repris au
        cycle suivant, voir pipeline.py) reste la dernière protection."""
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
                    "Échec (quota ou erreur serveur transitoire) sur %s (clé n°%d) — bascule "
                    "sur %s (clé n°%d).",
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
            if exc.code == 429 or exc.code in _RETRYABLE_SERVER_ERROR_CODES:
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
