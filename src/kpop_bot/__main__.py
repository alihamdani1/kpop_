"""Point d'entrée CLI :
`python -m kpop_bot run [--dry-run] [--limit N]`
`python -m kpop_bot resend [--limit N]`
"""

from __future__ import annotations

import argparse
import logging
import sys

from kpop_bot.pipeline import resend_sent, run_cycle
from kpop_bot.settings import get_settings
from kpop_bot.social_pipeline import run_social_visuals
from kpop_bot.thread_pipeline import run_thread_replenish, run_thread_resolve, run_thread_select

# 30 : écoule un backlog de ~94 articles en ~4 cycles plutôt que ~19 (ex-limite de 5), tout
# en restant très sous le budget de 500 RPD (voir settings.py). L'espacement entre appels
# (gemini_min_seconds_between_calls) protège le RPM même à cette limite plus haute.
_DEFAULT_LIMIT = 30


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="kpop_bot", description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    run_parser = subparsers.add_parser("run", help="Exécute un cycle complet du pipeline.")
    run_parser.add_argument(
        "--dry-run",
        action="store_true",
        help="N'envoie rien sur Discord ; journalise ce qui aurait été envoyé.",
    )
    run_parser.add_argument(
        "--limit",
        type=int,
        default=_DEFAULT_LIMIT,
        help=f"Nombre maximum d'articles analysés par cycle (défaut : {_DEFAULT_LIMIT}).",
    )
    run_parser.add_argument(
        "--verbose", action="store_true", help="Active les logs de niveau DEBUG."
    )

    resend_parser = subparsers.add_parser(
        "resend",
        help="Renvoie sur Discord les articles déjà SENT — aucun appel Gemini, aucun coût.",
    )
    resend_parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Nombre maximum d'articles à renvoyer (défaut : tous).",
    )
    resend_parser.add_argument(
        "--verbose", action="store_true", help="Active les logs de niveau DEBUG."
    )

    replenish_parser = subparsers.add_parser(
        "thread-replenish",
        help="Réapprovisionne le backlog de Topics de threads (T15) si besoin — 1 appel Gemini.",
    )
    replenish_parser.add_argument(
        "--dry-run",
        action="store_true",
        help="N'appelle pas Gemini ; journalise si un réapprovisionnement serait déclenché.",
    )
    replenish_parser.add_argument(
        "--verbose", action="store_true", help="Active les logs de niveau DEBUG."
    )

    select_parser = subparsers.add_parser(
        "thread-select",
        help="Propose 3 (Topic, Angle) via un embed Discord + réactions (T15) — aucun appel "
        "Gemini.",
    )
    select_parser.add_argument(
        "--dry-run",
        action="store_true",
        help="N'envoie rien sur Discord ; journalise les options qui auraient été proposées.",
    )
    select_parser.add_argument(
        "--verbose", action="store_true", help="Active les logs de niveau DEBUG."
    )

    resolve_parser = subparsers.add_parser(
        "thread-resolve",
        help="Détecte les réactions sur le picker (T15), génère et diffuse le thread choisi.",
    )
    resolve_parser.add_argument(
        "--verbose", action="store_true", help="Active les logs de niveau DEBUG."
    )

    social_parser = subparsers.add_parser(
        "social-visuals",
        help="Génère et envoie le visuel 9:16 (image RSS + tweet) des articles SENT qui n'en "
        "ont pas encore reçu un, vers le salon Discord privé de prévisualisation.",
    )
    social_parser.add_argument(
        "--dry-run",
        action="store_true",
        help="N'utilise pas Chromium ni Discord ; journalise les articles qui seraient traités.",
    )
    social_parser.add_argument(
        "--verbose", action="store_true", help="Active les logs de niveau DEBUG."
    )

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    log = logging.getLogger(__name__)

    if args.command == "run":
        settings = get_settings()
        stats = run_cycle(settings, limit=args.limit, dry_run=args.dry_run)
        log.info("Cycle terminé — %s", stats.summary())
        if stats.errors:
            log.warning(
                "%d erreur(s) durant le cycle : %s", len(stats.errors), "; ".join(stats.errors)
            )
        return 0

    if args.command == "resend":
        settings = get_settings()
        count = resend_sent(settings, limit=args.limit)
        log.info("%d article(s) renvoyé(s) sur Discord.", count)
        return 0

    if args.command == "thread-replenish":
        settings = get_settings()
        inserted = run_thread_replenish(settings, dry_run=args.dry_run)
        log.info("Réapprovisionnement terminé — %d nouveau(x) topic(s).", inserted)
        return 0

    if args.command == "thread-select":
        settings = get_settings()
        posted = run_thread_select(settings, dry_run=args.dry_run)
        log.info("Sélection quotidienne — %s.", "publiée" if posted else "ignorée")
        return 0

    if args.command == "thread-resolve":
        settings = get_settings()
        stats = run_thread_resolve(settings)
        log.info(
            "Résolution terminée — résolus=%d en_attente=%d envoyés=%d échecs_envoi=%d "
            "échecs_génération=%d quota_atteint=%s",
            stats.resolved,
            stats.still_pending,
            stats.sent,
            stats.send_failed,
            stats.generation_failed,
            stats.quota_exceeded,
        )
        return 0

    if args.command == "social-visuals":
        settings = get_settings()
        stats = run_social_visuals(settings, dry_run=args.dry_run)
        log.info("Visuels sociaux — %s.", stats.summary())
        return 0

    parser.error(f"Commande inconnue : {args.command}")
    return 2


if __name__ == "__main__":
    sys.exit(main())
