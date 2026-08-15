"""Configuration et secrets. Une seule source de vérité, indifférente à l'origine des
variables d'environnement (.env en local, GitHub Actions Secrets en CI)."""

from __future__ import annotations

from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # --- Secrets obligatoires : aucune valeur par défaut, échec explicite si absents. ---
    gemini_api_key: str
    discord_webhook_route_a: str  # #actus-videos — haute valeur
    discord_webhook_route_b: str  # #drafts-twitter — volume quotidien

    # --- Paramètres avec valeur par défaut raisonnable, ajustables sans toucher au code. ---
    # gemini-3.5-flash-lite : 15 RPM / 500 RPD gratuits (quota réel du compte, confirmé par
    # l'utilisateur — plus serré que les 1500 RPD documentés publiquement). Le choix définitif
    # pour la production sera tranché par T5bis.
    gemini_model: str = "gemini-3.5-flash-lite"
    # Modèle de secours : quota (RPM/RPD, ~identique au principal) totalement indépendant,
    # même clé API. N'intervient qu'après un 429 sur gemini_model — voir analyzer.py
    # `_generate_with_fallback`. gemini-3.1-flash-lite est déjà validé en conditions réelles
    # (voir T5), donc un choix de secours "connu et fiable" plutôt qu'une inconnue.
    gemini_fallback_model: str = "gemini-3.1-flash-lite"
    # 2e modèle de secours — n'intervient qu'après un 429 sur gemini_fallback_model, avant de
    # basculer sur gemini_api_key_2 (voir T5quinquies). Quota RPM/RPD indépendant des deux
    # modèles précédents.
    gemini_second_fallback_model: str = "gemini-2.5-flash-lite"
    # 2e clé API (compte Google distinct) — quota totalement indépendant de gemini_api_key.
    # Optionnelle : absente, un seul compte est utilisé (comportement inchangé). Présente, la
    # chaîne des 3 modèles ci-dessus est retentée sur cette clé une fois la première épuisée
    # (429 sur les trois) — voir analyzer.py `_generate_with_fallback`.
    gemini_api_key_2: str | None = None
    # Salon "#info-a-verifier" — filet de dernier recours pour tout ce qui est classé
    # BRUIT_INUTILE (voir T13). Optionnel : absent, ces articles restent simplement filtrés
    # comme avant (comportement inchangé). Aucun appel Gemini supplémentaire — réutilise la
    # classification déjà produite par classify().
    discord_webhook_info_a_verifier: str | None = None
    # Salon dédié "#concert" — Concert/Événement France (voir Route.CONCERT dans models.py).
    # Optionnel : absent, ces articles retombent sur discord_webhook_route_a (comportement
    # identique à avant l'introduction de cette route dédiée) — voir notifier._webhook_url_for.
    # Présent, ils partent uniquement vers ce salon, plus jamais vers #actus-videos.
    discord_webhook_concert: str | None = None
    # Espacement minimum entre deux appels Gemini réels (voir analyzer.py `_throttle`).
    # 15 RPM -> 4s/appel au maximum ; 4.5s laisse ~11% de marge. Évite de reproduire la
    # rafale qui avait déclenché un 429 en test avec --limit élevé et aucune pause.
    gemini_min_seconds_between_calls: float = 4.5
    db_path: Path = Path("data/kpop.db")
    sources_path: Path = Path("config/sources.yaml")
    artist_tiers_path: Path = Path("config/artist_tiers.yaml")

    # Filet de sécurité France — voir context.md §4. Modifiable sans toucher au code.
    # "Paris"/"France" couvrent déjà la quasi-totalité des cas (un article mentionne presque
    # toujours la ville ou le pays). Les salles nommées explicitement ci-dessous ne servent
    # que le cas marginal où un article ne cite QUE le nom de la salle, sans jamais écrire
    # "Paris" ni "France" — notamment pour les petites salles où passent des groupes/
    # solistes moins médiatisés (moins susceptibles d'avoir un article qui prend la peine de
    # préciser la ville). Ajout 10/08/2026, suite à un signalement sur la couverture des
    # petits groupes/solistes de passage à Paris.
    france_keywords: list[str] = [
        "Paris",
        "France",
        "Accor Arena",
        "Stade de France",
        "Zénith",
        "Adidas Arena",
        "Trianon",
        "La Cigale",
        "Olympia",
        "Élysée Montmartre",
        "Bataclan",
    ]

    # Filet de sécurité record/palier viral — voir T13. Déclenché seulement si un de ces
    # mots-clés ET un artiste de config/artist_tiers.yaml sont tous les deux présents (la
    # combinaison des deux limite les faux positifs — un mot-clé seul serait trop générique).
    viral_milestone_keywords: list[str] = [
        "billion views",
        "million views",
        "billion streams",
        "million streams",
        "million copies",
        "million albums",
        "record high",
        "all-time high",
    ]

    request_timeout_seconds: float = 15.0

    # --- T15 : threads Twitter quotidiens — tous optionnels, absents = fonctionnalité inactive,
    # comportement du reste du pipeline strictement inchangé (même principe que
    # discord_webhook_social ci-dessous). ---
    # Bot Discord (Developer Portal), permissions View Channel / Read Message History /
    # Add Reactions uniquement — l'envoi des messages reste 100 % webhook (voir notifier.py),
    # ce token ne sert qu'à poser/lire des réactions (discord_reactions.py).
    discord_bot_token: str | None = None
    # Id numérique du salon où pointe discord_webhook_thread — nécessaire séparément du webhook
    # car les endpoints REST de réactions sont scopés par channel_id, pas par webhook.
    discord_thread_channel_id: str | None = None
    # Webhook dédié au picker + à la diffusion du thread final (même salon).
    discord_webhook_thread: str | None = None
    # Seuil de réapprovisionnement du backlog de Topics (voir thread_pipeline.run_thread_replenish).
    thread_topic_backlog_min: int = 15
    # Taille du lot généré par appel d'idéation quand le backlog descend sous le seuil.
    thread_ideation_batch_size: int = 12
    # Concepts viraux curés (T15bis, idéation hybride) — croisés en code avec les groupes de
    # artist_tiers_path pour former les Topics candidats, voir thread_pipeline._candidate_pairs.
    thread_concepts_path: Path = Path("config/thread_concepts.yaml")
    # Modèle dédié à l'idéation/rédaction des threads (T15ter) — chaîne distincte de
    # gemini_model/*_fallback_model (réservée au pipeline articles à fort volume, ~225
    # appels/jour). Les threads ne coûtent que 1-2 appels/jour : largement la marge pour un
    # modèle de meilleure qualité rédactionnelle (flash plutôt que flash-lite) sans impact sur
    # le quota qui compte vraiment.
    # Les 3 noms ci-dessous ont été vérifiés par un vrai appel API (voir incident du 28/07/2026,
    # TODO.md T15ter) : `gemini-3.1-flash` et `gemini-2.5-flash` (choisis initialement par
    # analogie avec les variantes -lite existantes) n'existent pas/plus pour ce compte — 404
    # immédiat, sans jamais déclencher le repli. `gemini-3.1-flash-lite` en dernier secours est
    # le même modèle déjà éprouvé comme fallback du pipeline articles (T5ter).
    thread_gemini_model: str = "gemini-3.5-flash"
    thread_gemini_fallback_model: str = "gemini-3.6-flash"
    thread_gemini_second_fallback_model: str = "gemini-3.1-flash-lite"
    # Délai avant qu'une sélection PENDING (personne n'a réagi) soit marquée EXPIRED.
    thread_selection_ttl_hours: float = 24.0
    # Bibliothèque interne d'images (T16) — 1 dossier par groupe + _generic pour les sujets
    # transverses, voir config/media_library/README.md.
    media_library_path: Path = Path("config/media_library")

    # --- Visuel social 9:16 (RSS image + titre/points clés -> PNG), pipeline séparé
    # (social_pipeline.py) du cycle articles principal — voir TODO.md. Optionnel : webhook
    # absent, fonctionnalité inactive (y compris le 3e appel Gemini SocialVisualContent dans
    # run_cycle, voir pipeline.py), comportement du reste du pipeline strictement inchangé. ---
    discord_webhook_social: str | None = None
    social_visual_template_path: Path = Path("templates/social_post.html")
    # Format publication Instagram (4:5, feed) — même news, même image source, gabarit distinct
    # (pas de zone de sécurité TikTok/Reels, proportions différentes). Envoyé automatiquement en
    # plus du format TikTok/Reels dès que discord_webhook_social est configuré, vers le même
    # salon — pas un webhook séparé.
    social_visual_instagram_template_path: Path = Path("templates/social_post_instagram.html")
    # Borne le temps d'un run (lancement Chromium + téléchargements d'images RSS).
    social_visual_batch_limit: int = 20


_settings: Settings | None = None


def get_settings() -> Settings:
    """Charge la configuration une seule fois (échoue immédiatement si un secret manque)."""
    global _settings
    if _settings is None:
        _settings = Settings()  # type: ignore[call-arg]  # valeurs lues depuis l'environnement
    return _settings
