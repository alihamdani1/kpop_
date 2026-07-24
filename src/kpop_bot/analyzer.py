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
    WritingResult,
)

logger = logging.getLogger(__name__)

PROMPT_VERSION = "v1"

_T = TypeVar("_T", bound=BaseModel)


class AnalysisError(Exception):
    """Réponse invalide ou erreur API — l'appelant marque l'article FAILED et continue."""


class QuotaExceededError(AnalysisError):
    """429 — signal distinct : l'appelant doit arrêter le cycle, pas marquer FAILED en boucle."""


def load_artist_tiers(path: Path) -> dict[str, list[str]]:
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    return {key: value for key, value in data.items() if key.startswith("tier_")}


def _format_artist_tiers(tiers: dict[str, list[str]]) -> str:
    lines = [f"- {name} : {', '.join(artists)}" for name, artists in sorted(tiers.items())]
    return "\n".join(lines) if lines else "(aucune donnée de référence disponible)"


def matches_france_keywords(item: ArticleRecord, keywords: list[str]) -> bool:
    """Filet de sécurité déterministe (context.md §4). Recherche insensible à la casse, sur
    mots entiers, dans le titre + l'extrait. Pure fonction, testable sans réseau."""
    text = f"{item.title} {item.raw_summary}"
    return any(re.search(rf"\b{re.escape(kw)}\b", text, re.IGNORECASE) for kw in keywords)


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
- BRUIT_INUTILE : reprise de post réseaux sociaux, publicité déguisée, classement racoleur, \
contenu sans fait nouveau vérifiable.

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
{france_note}
Réponds uniquement selon le schéma JSON fourni.
"""

_FRANCE_NOTE = """

## Note système
Cet article a été pré-identifié comme concernant potentiellement un lieu ou événement en \
France (détection automatique sur mots-clés). Si c'est bien le cas, la catégorie DOIT être \
CONCERT_EVENEMENT_FRANCE. Évalue quand même normalement l'importance et la viralité.
"""

_WRITING_SYSTEM_PROMPT = """\
Tu es l'assistant éditorial d'un média francophone spécialisé K-pop. L'article suivant a déjà \
été classé : catégorie {category}, importance {importance}, viralité {virality}. Rédige en \
français, dans le style d'un média d'actualité :

- `summary_fr` : un résumé de 2 phrases, ton journalistique, directement exploitable pour un \
tri rapide.
- `tweet_draft` : un brouillon de tweet PRÊT À PUBLIER (jamais posté automatiquement — un \
humain le relit toujours avant). Moins de 280 caractères. Ton journalistique neutre. 1 à 2 \
emojis maximum. Exactement 2 hashtags pertinents (ex. #KPop et le nom du groupe). En français.
{video_instruction}
Réponds uniquement selon le schéma JSON fourni.
"""

_VIDEO_INSTRUCTION = """
- `video_summary` : un résumé plus détaillé (4 à 6 phrases), pensé pour servir de base à un \
script vidéo — contexte, faits, ce qui rend le sujet notable.
"""


class GeminiAnalyzer:
    """Implémentation Gemini de l'analyse. Le reste du pipeline ne dépend que de `classify()`
    et `write()` — basculer vers un autre fournisseur (ex. Groq) n'impacte que cette classe."""

    def __init__(
        self,
        *,
        api_key: str,
        model: str,
        artist_tiers: dict[str, list[str]],
        fallback_model: str | None = None,
        min_seconds_between_calls: float = 4.5,
    ) -> None:
        self._client = genai.Client(api_key=api_key)
        self._model = model
        self._fallback_model = fallback_model
        self._artist_tiers_text = _format_artist_tiers(artist_tiers)
        # 15 RPM sur gemini-3.5-flash-lite / gemini-3.1-flash-lite -> 1 appel/4s max.
        # 4.5s laisse ~11% de marge. Espace CHAQUE appel réel (classify, write, et une
        # éventuelle tentative de secours) — voir `_throttle()`. 0 = désactivé (tests).
        self._min_interval = min_seconds_between_calls
        self._last_call_at: float | None = None

    def classify(
        self, item: ArticleRecord, *, france_flag: bool
    ) -> tuple[ClassificationResult, int, int]:
        system_prompt = _CLASSIFICATION_SYSTEM_PROMPT.format(
            artist_tiers=self._artist_tiers_text,
            france_note=_FRANCE_NOTE if france_flag else "",
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
        return result, tokens_in, tokens_out

    def write(
        self, item: ArticleRecord, classification: ClassificationResult, route: Route
    ) -> tuple[WritingResult, int, int]:
        system_prompt = _WRITING_SYSTEM_PROMPT.format(
            category=classification.category.value,
            importance=classification.importance.value,
            virality=classification.virality.value if classification.virality else "N/A",
            video_instruction=_VIDEO_INSTRUCTION if route == Route.A else "",
        )
        user_content = f"Titre : {item.title}\nExtrait : {item.raw_summary}\nLien : {item.url}"
        return self._generate_with_fallback(system_prompt, user_content, WritingResult)

    def _generate_with_fallback(
        self, system_prompt: str, user_content: str, schema: type[_T]
    ) -> tuple[_T, int, int]:
        """Essaie le modèle principal ; sur 429 uniquement, retente une fois sur le modèle de
        secours (quota indépendant — même clé API, même fournisseur, donc même format de
        prompt et même niveau de confiance qualité). Si le secours échoue aussi, l'erreur
        remonte normalement : le filet de sécurité existant (article repris au cycle suivant,
        voir pipeline.py) reste la dernière protection."""
        try:
            return self._generate(self._model, system_prompt, user_content, schema)
        except QuotaExceededError:
            if not self._fallback_model:
                raise
            logger.warning(
                "Quota atteint sur %s — bascule sur le modèle de secours %s.",
                self._model,
                self._fallback_model,
            )
            return self._generate(self._fallback_model, system_prompt, user_content, schema)

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
        self, model: str, system_prompt: str, user_content: str, schema: type[_T]
    ) -> tuple[_T, int, int]:
        self._throttle()
        try:
            response = self._client.models.generate_content(
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
    "matches_france_keywords",
]
