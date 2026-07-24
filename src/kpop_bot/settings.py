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
    # gemini-3.5-flash-lite : 15 RPM / 1500 RPD gratuits, GA. Le choix définitif pour la
    # production sera tranché par T5bis.
    gemini_model: str = "gemini-3.5-flash-lite"
    # Modèle de secours : quota (RPM/RPD) totalement indépendant du modèle principal, même
    # clé API. N'intervient qu'après un 429 sur gemini_model — voir analyzer.py
    # `_generate_with_fallback`. gemini-3.1-flash-lite est déjà validé en conditions réelles
    # (voir T5), donc un choix de secours "connu et fiable" plutôt qu'une inconnue.
    gemini_fallback_model: str = "gemini-3.1-flash-lite"
    db_path: Path = Path("data/kpop.db")
    sources_path: Path = Path("config/sources.yaml")
    artist_tiers_path: Path = Path("config/artist_tiers.yaml")

    # Filet de sécurité France — voir context.md §4. Modifiable sans toucher au code.
    france_keywords: list[str] = [
        "Paris",
        "France",
        "Accor Arena",
        "Stade de France",
        "Zénith",
    ]

    request_timeout_seconds: float = 15.0


_settings: Settings | None = None


def get_settings() -> Settings:
    """Charge la configuration une seule fois (échoue immédiatement si un secret manque)."""
    global _settings
    if _settings is None:
        _settings = Settings()  # type: ignore[call-arg]  # valeurs lues depuis l'environnement
    return _settings
