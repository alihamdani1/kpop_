"""Bibliothèque interne d'images pour les threads (T16) — 1 dossier par groupe, `_generic` en
repli pour les sujets transverses. La rédaction possède les droits sur toutes les images, voir
`config/media_library/README.md` pour la convention d'alimentation.

Sélection volontairement non déterministe (contrairement au choix de Topic/Angle en T15bis/
quater) : l'enjeu ici est la variété visuelle d'un thread à l'autre, pas une garantie
d'anti-répétition stricte sur un sujet éditorial."""

from __future__ import annotations

import random
from pathlib import Path

_IMAGE_EXTENSIONS = (".jpg", ".jpeg", ".png", ".webp")

# Concepts (config/thread_concepts.yaml) rattachés à une sous-catégorie de `_generic/` — ne
# s'applique que si `group_name` n'a pas lui-même de dossier (portée générale, ex. "industrie
# K-pop") ; jamais utilisé quand un dossier de groupe réel existe (voir _resolve_pool).
_CONCEPT_TO_GENERIC_SUBCATEGORY: dict[str, str] = {
    "auto_composition": "industrie",
    "structure_agence": "industrie",
    "affaire_industrie": "industrie",
    "regles_contrats_idols": "industrie",
    "cout_realite_comeback": "industrie",
    "record_marquant": "charts_records",
    "anecdote_meconnue": "charts_records",
    "top_ventes_streams": "charts_records",
    "top_artistes_popularite": "charts_records",
    "tournee_france": "france",
    "fandom_francophone": "france",
    "connexion_france_mode": "france",
    "rituel_fandom": "fandom",
    "theorie_fan": "fandom",
}


def _images_in(folder: Path) -> list[Path]:
    if not folder.is_dir():
        return []
    return sorted(p for p in folder.iterdir() if p.suffix.lower() in _IMAGE_EXTENSIONS)


def _resolve_pool(
    media_library_path: Path, *, group_name: str, concept_id: str | None
) -> list[Path]:
    """Résout le dossier à utiliser, dans l'ordre : dossier du groupe -> sous-catégorie
    `_generic` (si le concept en a une) -> racine `_generic` à plat -> aucune image."""
    group_images = _images_in(media_library_path / group_name)
    if group_images:
        return group_images

    subcategory = _CONCEPT_TO_GENERIC_SUBCATEGORY.get(concept_id or "")
    if subcategory:
        subcategory_images = _images_in(media_library_path / "_generic" / subcategory)
        if subcategory_images:
            return subcategory_images

    return _images_in(media_library_path / "_generic")


def select_images_for_thread(
    media_library_path: Path, *, group_name: str, concept_id: str | None, count: int
) -> list[Path]:
    """Une image par tweet — pioche `count` images dans le dossier résolu, sans répétition tant
    que le pool le permet (cycle avec répétition si le pool est plus petit que `count`). Liste
    vide si aucune image n'est disponible — dégradation gracieuse, un thread ne doit jamais être
    bloqué faute de photo."""
    pool = _resolve_pool(media_library_path, group_name=group_name, concept_id=concept_id)
    if not pool or count <= 0:
        return []
    if count <= len(pool):
        return random.sample(pool, k=count)
    shuffled = pool.copy()
    random.shuffle(shuffled)
    return [shuffled[i % len(shuffled)] for i in range(count)]


def select_image_for_article(media_library_path: Path, *, artists: list[str]) -> Path | None:
    """Repli utilisé par social_pipeline.py quand l'image RSS de l'article est absente ou
    inexploitable — dossier du premier artiste cité qui a des photos, sinon `_generic` à plat.
    Contrairement à `_resolve_pool` (threads), pas de sous-catégorie `_generic` par concept : un
    article n'a pas de `concept_id`. None si aucune image disponible (dégradation gracieuse,
    aucun visuel n'est alors généré pour cet article)."""
    for artist in artists:
        pool = _images_in(media_library_path / artist)
        if pool:
            return random.choice(pool)
    generic_pool = _images_in(media_library_path / "_generic")
    return random.choice(generic_pool) if generic_pool else None


def select_extra_images(
    media_library_path: Path,
    *,
    group_name: str,
    concept_id: str | None,
    exclude: list[Path],
    count: int,
) -> list[Path]:
    """Images alternatives en plus de celles déjà attribuées aux tweets — pour offrir un choix
    de substitution avant publication manuelle sur X, si l'image auto-choisie sur un tweet
    précis ne convient pas. Exclut les images déjà utilisées ; retourne moins de `count` si le
    pool restant est plus petit (jamais d'échec faute de stock)."""
    pool = _resolve_pool(media_library_path, group_name=group_name, concept_id=concept_id)
    remaining = [image for image in pool if image not in exclude]
    if not remaining or count <= 0:
        return []
    return random.sample(remaining, k=min(count, len(remaining)))
