# Feuille de route — Pipeline de curation K-pop

> Statut : **socle + cœur métier fonctionnels, testés (54 tests) et validés en conditions
> réelles (vrais appels Gemini, vrais messages Discord reçus et vérifiés sur mobile).**
> Il reste principalement le déploiement autonome (T10) pour que le pipeline tourne sans
> intervention manuelle. Chaque tâche est autonome, testable, et livrable dans l'ordre indiqué.
>
> **Architecture retenue (v3)** : hébergement 100 % gratuit (GitHub Actions, dépôt public),
> fournisseur LLM **Gemini Flash** (`gemini-3.1-flash-lite` en phase de test), routage à **deux salons Discord**
> (`#actus-videos` / `#drafts-twitter`) piloté par le score de viralité, brouillon de tweet
> généré pour tout article retenu. Aucune intégration directe à l'API X — publication 100 %
> humaine (« human-in-the-loop »).

---

## Choix techniques recommandés

| Domaine | Choix | Justification |
|---|---|---|
| **Langage** | Python 3.12 | Écosystème RSS/HTTP/IA le plus mature, aucune compilation |
| **Gestion des dépendances** | `venv` + `pyproject.toml` | Standard, zéro dépendance système |
| **Lecture RSS** | `feedparser` | Gère RSS 1.0/2.0, Atom, dates malformées, encodages exotiques |
| **Client HTTP** | `httpx` | Timeouts explicites, retries, API moderne |
| **Base de données** | **SQLite**, fichier versionné dans le dépôt | Un seul fichier, zéro serveur. Les runners GitHub Actions sont jetables : la base doit être **recommise dans le dépôt** à la fin de chaque exécution pour survivre au cycle suivant (voir T3) |
| **Hébergement / ordonnancement** | **GitHub Actions**, cron fixé à **15 min**, dépôt public | Minutes illimitées et gratuites sur dépôt public. Aucune machine à faire tourner. Limite assumée : timing approximatif |
| **Fournisseur LLM** | **Google Gemini** (`gemini-3.1-flash-lite`, configurable), tier gratuit — derrière une interface interchangeable | Choisi pour la phase de test : 15 RPM / 1500 RPD gratuits, plus de marge que `gemini-3.6-flash` (~10 RPM) qui avait déclenché un 429 dès 6-7 appels rapprochés. Volume calibré (~150 art./jour en prod, voir point 4) très sous le plafond gratuit dans les deux cas. SDK `google-genai`, sortie contrainte nativement via `response_schema` (Pydantic). Choix définitif pour la production à confirmer par T5bis |
| **Fournisseur LLM de secours** | Groq, tier gratuit (14 400 req/jour) | Bascule documentée si les quotas Gemini deviennent trop justes ou si T5bis montre un net avantage qualité |
| **Format de sortie IA** | Sortie contrainte par **schéma JSON** (`response_schema`) + validation Pydantic | Le modèle est empêché de produire une catégorie hors énumération, un JSON mal formé, ou un tweet trop long (`max_length=280` validé côté code) |
| **Validation / config** | `pydantic` v2 + `pydantic-settings` | Un seul outil pour le schéma IA, la config et les variables d'environnement |
| **Fichier de sources** | `YAML` (`PyYAML`) | Lisible et éditable par un non-développeur de la rédaction |
| **Diffusion** | **2 webhooks Discord** (Route A, Route B) + *embeds* (`httpx`) | Le routage se fait par valeur éditoriale/sociale, pas par catégorie — voir T6 |
| **Secrets** | GitHub Actions Secrets (prod) + `.env` local (dev) | `pydantic-settings` lit les variables d'environnement quelle que soit leur origine |
| **Journalisation** | `logging` stdlib | Conservée automatiquement dans l'historique GitHub Actions |
| **Tests** | `pytest` + `respx` (mock HTTP) | Le pipeline doit être testable **sans** appeler l'API ni Discord |
| **Qualité** | `ruff` (lint + format) | Un seul outil, ultra-rapide |

**Arborescence cible**

```
kpop_news_bot/
├── context.md
├── TODO.md
├── pyproject.toml
├── .env.example                 # modèle, versionné (pour le dev local)
├── .env                         # secrets réels en local, JAMAIS versionné
├── .github/
│   └── workflows/
│       └── pipeline.yml         # cron 15 min + déclenchement manuel (workflow_dispatch)
├── config/
│   ├── sources.yaml             # flux RSS actifs (dev : Soompi seul pour l'instant)
│   └── artist_tiers.yaml        # table Tier 1/2/3… injectée dans le prompt système
├── src/kpop_bot/
│   ├── __main__.py              # point d'entrée / CLI
│   ├── settings.py              # config + secrets
│   ├── models.py                # schémas Pydantic (dont la sortie IA en 2 temps)
│   ├── storage.py               # SQLite : schéma, dédup, statuts
│   ├── fetcher.py               # collecte RSS
│   ├── dedup.py                 # déduplication sémantique (embeddings) — T4bis
│   ├── analyzer.py              # filtre France + interface LLM + implémentation Gemini
│   ├── notifier.py              # embeds + envoi webhook Discord (Route A / Route B)
│   └── pipeline.py              # orchestration collecte → analyse → routage → diffusion
├── tests/
└── data/
    └── kpop.db                  # SQLite — VERSIONNÉ (contenu public, aucun secret dedans)
```

---

## Routage Discord — logique de référence

Il n'y a **pas** de salon par catégorie. Deux routes seulement, déterminées **en code** (pas
par l'IA elle-même) à partir de `category` et `virality` :

```python
def determine_route(category: Category, virality: Virality | None) -> Route:
    if category == Category.BRUIT_INUTILE:
        return Route.IGNORED  # aucun envoi, aucun 2e appel IA
    if category == Category.CONCERT_EVENEMENT_FRANCE or virality in (
        Virality.VIRAL,
        Virality.ELEVE,
    ):
        return Route.A  # #actus-videos
    return Route.B  # #drafts-twitter (MODERE / FAIBLE)
```

| | **Route A — `#actus-videos`** | **Route B — `#drafts-twitter`** |
|---|---|---|
| Déclencheurs | `virality ∈ {VIRAL, ÉLEVÉ}` **ou** `category = CONCERT_EVENEMENT_FRANCE` | `virality ∈ {MODÉRÉ, FAIBLE}`, hors Concert France |
| Contenu embed | Titre, badge score, **résumé détaillé** (pour vidéo), brouillon de tweet | Titre, badge score, brouillon de tweet — rien d'autre |

**Filet de sécurité France (règle dure)** : mots-clés `Paris`, `France`, `Accor Arena`,
`Stade de France`, `Zénith` — recherche insensible à la casse sur titre + extrait, **avant**
l'appel IA. En cas de match : le prompt de classification est informé (pour que l'importance et
la viralité restent cohérentes), **et** la catégorie est réécrite en dur à
`CONCERT_EVENEMENT_FRANCE` juste après la réponse du modèle, quoi qu'il ait répondu. Aucun
chemin de code ne permet à l'IA de contourner cette règle.

**Badges de viralité** (identiques en Discord et dans les logs) :

| Niveau | Badge |
|---|---|
| `VIRAL` | 🔴 |
| `ÉLEVÉ` | 🟠 |
| `MODÉRÉ` | 🟡 |
| `FAIBLE` | ⚪ |

---

## Tâches

### ✅ T1 — Initialisation du projet — FAIT
- Arborescence, `pyproject.toml`, `.gitignore`, venv, dépendances installées, `ruff` configuré.
- `git init` + commit local fait. **Le push vers un dépôt GitHub public n'a pas encore été
  fait** — reporté à T10, comme prévu.
- **Fait quand** : `python -m kpop_bot --help` s'exécute sans erreur. ✔️ Vérifié.

### ✅ T2 — Configuration et secrets — FAIT
- `settings.py` opérationnel, échoue explicitement si un secret manque (vérifié).
- `config/sources.yaml` : **Soompi + Yonhap Culture actifs ; allkpop présent mais désactivé**
  (flux RSS retiré par le site, confirmé par des 404 sur 4 URLs candidates — aucun correctif
  code applicable ; voir discussion Google News/RSS tiers, écartée pour l'instant).
- `config/artist_tiers.yaml` : table de départ en place.
- `.env` réel créé par l'utilisateur avec les vraies clés — testé en conditions réelles.

### ✅ T3 — Couche de persistance (SQLite versionnée) — FAIT (localement)
- Schéma complet en place (`route`, `tweet_draft`, `video_summary`, `france_override` inclus),
  tests unitaires au vert.
- **`data/kpop.db` existe déjà (données réelles) mais n'est pas encore suivi par git** — le
  mécanisme de recommit automatique dans le dépôt reste à construire, dans T10.

### ✅ T4 — Collecte RSS — FAIT, durci au-delà du plan initial
- Testé en conditions réelles sur Soompi (60 items) et Yonhap Culture (48 items, après
  correctif User-Agent).
- Ajout non prévu au plan initial : en-têtes de navigateur standards (contournement des 403
  naïfs) et détection explicite des flux vides/non-XML (`EmptyFeedError`) — pour ne jamais
  activer silencieusement une source qui ne remonte rien.

### T4bis — Déduplication sémantique *(toujours reportée, non prioritaire)*
- Bi-encodeur léger multilingue (ex. `intfloat/multilingual-e5-small`), similarité cosinus sur
  une fenêtre glissante de 48 h, pour détecter la même actu reprise par plusieurs sources.
- **Fait quand** : deux articles reformulant la même dépêche ne génèrent qu'un seul message Discord.

### T5 — Analyse IA (cœur du système)

Deux appels Gemini par article retenu, un seul pour le bruit filtré.

**Appel 1 — Classification** (`ClassificationResult`, toujours exécuté) :

| Champ | Type | Rôle |
|---|---|---|
| `category` | Enum (4) | SCANDALE_DRAMA / COMEBACK_SORTIE / CONCERT_EVENEMENT_FRANCE / BRUIT_INUTILE |
| `importance` | Enum (3) | MINEUR / MODERE / MAJEUR |
| `virality` | Enum (4), **nullable** | FAIBLE / MODERE / ELEVE / VIRAL — `null` si `BRUIT_INUTILE` |
| `virality_reason` | str, **nullable** | Justification courte, même condition de nullité |
| `artists` | liste de str | Entités citées |

Le filtre mots-clés France (voir « Routage Discord » ci-dessus) tourne avant cet appel et sa
conclusion est réappliquée en dur après coup, indépendamment de la réponse du modèle.

**Détermination de route** : fonction pure en code (`determine_route`), pas de jugement IA.

**Appel 2 — Rédaction** (`WritingResult`, uniquement si route ≠ IGNORED) :

| Champ | Type | Rôle |
|---|---|---|
| `summary_fr` | str, 2 phrases | Toujours généré (Route A et B) |
| `tweet_draft` | str, **≤ 280 caractères** | Toujours généré. Ton journalistique neutre, 1-2 emojis max, 2 hashtags pertinents, en français. Validé par Pydantic (`max_length=280`) — un dépassement déclenche le chemin d'erreur existant (`FAILED`, repris au cycle suivant), pas une troncature silencieuse |
| `video_summary` | str, **nullable** | Résumé détaillé (≈ 4-6 phrases), pensé pour scripter une vidéo. Demandé **uniquement si Route A** — économise des tokens de sortie sur la Route B, qui ne l'affiche pas |

> **Choix d'implémentation assumé** : le résumé « détaillé pour la vidéo » (Route A) est un
> champ distinct du résumé 2-phrases (`summary_fr`, toujours généré), plutôt qu'une réutilisation
> du même texte. Raison : un script vidéo a besoin de plus de matière qu'une phrase d'accroche
> Discord. Ajustable facilement si ce n'est pas l'intention — un seul champ à retirer/fusionner.

- `analyzer.py` expose une interface indépendante du fournisseur ; implémentation concrète avec
  le SDK `google-genai` (`response_schema` = modèle Pydantic).
- Le prompt système de l'appel 1 intègre le contenu de `config/artist_tiers.yaml`.
- Gestion d'erreurs typée : `429` (quota) → backoff et report au cycle suivant ; erreurs
  serveur → retry ; réponse invalide (schéma, tweet trop long) → article en `FAILED`, pipeline
  poursuivi.
- Enregistrement des tokens consommés par article et par appel.
- **✅ FAIT — validé en conditions réelles** : appels Gemini réels effectués (`gemini-3.1-flash-lite`),
  classifications cohérentes, résumés en français, brouillons de tweet générés. Le 429 rencontré
  en test était un plafond RPM normal, correctement géré (cycle arrêté proprement, repris ensuite).

### ⏳ T5bis — Évaluation du modèle avant mise en production — PAS COMMENCÉ
- Annoter à la main ~60 articles réels : catégorie/importance attendues, jugement humain
  « j'aurais tweeté ça / non » pour la viralité, et une relecture critique des brouillons de
  tweet générés (ton, longueur, pertinence des hashtags).
- Comparer Gemini Flash vs Groq sur : justesse de catégorie, taux de rattrapage du bruit,
  qualité du français, cohérence viralité/jugement humain, qualité des brouillons de tweet.
- **Nécessite ton travail d'annotation** — c'est un jugement éditorial, pas quelque chose que je
  peux faire seul.
- **Fait quand** : le choix de fournisseur est confirmé par les chiffres.

### ✅ T6 — Routage et diffusion Discord — FAIT, format revu par rapport au plan initial
- `BRUIT_INUTILE` → statut `FILTERED`, **aucun envoi**, aucun 2e appel IA.
- Sinon : `determine_route()` décide Route A ou B, puis **4 messages Discord distincts et
  numérotés** par article (évolution du plan initial, suite aux retours mobile) :
  1. En-tête `# INFO {n}` (n = position dans le cycle d'envoi)
  2. Embed d'info (titre, badge de score, résumé détaillé si Route A)
  3. En-tête `# Brouillon Tweet`
  4. Message texte brut avec le tweet — seul, pour rester copiable d'un appui long sur mobile
- Titres en syntaxe Markdown Discord (`#`), pas en gras — rendu identique et fonctionnel sur
  mobile et desktop, tant que c'est un message texte et pas un champ d'embed.
- Respect du rate-limit Discord (`429` + `Retry-After`), pause entre envois.
- Passage en `SENT` **uniquement** après réponse HTTP positive → aucun doublon en cas de crash.
- **✅ FAIT** : testé en réel, reçu et vérifié visuellement sur mobile par l'utilisateur —
  copie du tweet et titres agrandis confirmés fonctionnels.

### ✅ T7 — Orchestration du pipeline — FAIT (périmètre réduit, assumé)
- `pipeline.py` : collecte → classification des `NEW` → routage → rédaction (si retenu) →
  diffusion → reprise des `FAILED`. (Dédup sémantique T4bis toujours reportée.)
- CLI réellement construite : `run` (cycle complet, `--dry-run`, `--limit`, défaut 5) **et**
  `resend` (non prévu au plan initial — renvoie les articles déjà `SENT` sans appeler Gemini,
  ajouté pour permettre de tester le rendu Discord sans reconsommer de quota).
- **Simplification assumée par rapport au plan initial** : pas de sous-commandes séparées
  `fetch` / `analyze` / `send` — `run` fait tout d'un coup. À reconsidérer seulement si un besoin
  concret de les isoler apparaît.
- **Fait quand** : `python -m kpop_bot run --dry-run --limit 5` déroule le cycle complet sans
  rien envoyer. ✔️ Vérifié, ainsi qu'un cycle réel complet (Gemini + Discord).

### 🟡 T8 — Journalisation et observabilité — PARTIEL
- Logs horodatés, niveau configurable, résumé de fin de cycle (collectés/nouveaux/classifiés/
  Route A/Route B/filtrés/échoués/tokens) : **fait et vérifié en réel**.
- **Pas encore fait** : commande `stats` (volumétrie/répartition sur 7 jours) — jamais construite.
- « Consultable dans l'onglet Actions de GitHub » ne s'applique pas encore : on tourne en local
  pour l'instant, pas sur GitHub Actions (voir T10).

### ✅ T9 — Tests — FAIT, maintenu à jour à chaque changement
- Unitaires + intégration `respx` (RSS, Gemini mocké, webhooks) — **54 tests, tous au vert**,
  aucun test n'appelle une API réelle. Mis à jour à chaque évolution (format 4 messages,
  filtre mots-clés France, User-Agent navigateur, `EmptyFeedError`, commande `resend`).

### ❌ T10 — Déploiement (workflow GitHub Actions) — RIEN DE FAIT, PROCHAINE ÉTAPE
- Création du dépôt GitHub **public**, premier push (aucun dépôt distant n'existe encore).
- `.github/workflows/pipeline.yml` : `schedule` (cron **15 min**) + `workflow_dispatch` ;
  `concurrency` anti-chevauchement ; checkout, setup Python (cache dépendances), secrets,
  exécution du pipeline, commit automatique de `data/kpop.db` si modifiée.
- Secrets à déclarer dans **GitHub → Settings → Secrets and variables → Actions** :
  `GEMINI_API_KEY`, `DISCORD_WEBHOOK_ROUTE_A`, `DISCORD_WEBHOOK_ROUTE_B` (mêmes valeurs que
  ton `.env` local).
- **C'est la seule tâche qui bloque encore l'objectif initial** : tant qu'elle n'est pas faite,
  le pipeline ne tourne que quand on le lance nous-mêmes depuis le terminal — pas 24h/24 sans
  intervention.
- **Fait quand** : le workflow tourne seul toutes les 15 min, committe son état, un
  déclenchement manuel fonctionne pour les tests.

### ⏸️ T11 — Réglage et durcissement (après premières journées de production) — BLOQUÉ PAR T10
- Ajustement du prompt selon les erreurs observées en conditions réelles (catégorie, viralité,
  qualité des brouillons de tweet).
- Surveillance mensuelle des quotas gratuits Gemini/Groq.
- **Calibration du score de viralité** : si la rédaction note quels tweets publiés ont
  réellement bien marché, ces données permettent de recouper l'heuristique avec la réalité.
- Retrait progressif de `--limit 5` une fois le volume de production (~150/jour) validé.
- Purge/archivage des articles anciens si la base versionnée grossit trop.

---

## Ordre d'exécution — où on en est

```
✅T1 → ✅T2 → ✅T3 → ✅T4 → ✅T5 → ✅T6 → ✅T7 → 🟡T8 → ✅T9 → ❌T10 → ⏸️T11
                                                              ▲
                                                    on est ici — prochaine étape
       └──────────────── socle + métier : fait ────────────────┘   (T4bis / T5bis toujours reportées)
```

**Restant concrètement** :
- **T10** (déploiement GitHub Actions) — la seule tâche qui empêche encore le fonctionnement
  100 % autonome, l'objectif initial du projet. C'est la prochaine étape naturelle.
- **T8** partiel — commande `stats` non construite (mineur, pas bloquant).
- **T4bis** (dédup sémantique) et **T5bis** (évaluation comparative Gemini/Groq) — volontairement
  repoussées, elles affinent un système qui marche déjà plutôt que de bloquer sa mise en route.
  T5bis nécessite en plus un travail d'annotation manuelle de ta part.
- **T11** — ne peut pas commencer avant que T10 tourne en production depuis quelques jours.

---

## Décisions prises

### Points 1 à 4 (sources, cadence, filet France, volume)

| # | Sujet | Décision |
|---|---|---|
| 1 | Sources RSS | **Soompi + Yonhap Culture** actifs. **allkpop désactivé** — son flux RSS n'existe plus (404 confirmé sur 4 URLs candidates, refonte du site), pas de correctif applicable côté code. À remplacer par une autre source si le volume Route B doit être renforcé |
| 2 | Cadence du cron | **Fixée à 15 min** (GitHub Actions) |
| 3 | Filet mots-clés France | **Règle dure, hardcodée**, avant l'appel IA + réécriture forcée après coup. Mots-clés : `Paris`, `France`, `Accor Arena`, `Stade de France`, `Zénith` |
| 4 | Volume attendu | **~150 articles/jour en production** ; `--limit 5` pour les tests actuels |

### Points 5 à 10 (score de viralité — rappel)

| # | Sujet | Décision |
|---|---|---|
| 5 | Échelle | Énumération à 4 niveaux : Faible / Modéré / Élevé / Viral |
| 6 | Contexte additionnel | Table statique Tier 1/2/3… (`config/artist_tiers.yaml`) injectée dans le prompt |
| 7 | Portée | Calculé uniquement pour les catégories retenues ; `null` pour `BRUIT_INUTILE` |
| 8 | Exploitation en aval | **Révisé** : plus un simple badge isolé — pilote désormais le routage complet Route A / Route B (voir section dédiée) |
| 9 | Seuil de déclenchement | Résolu par la logique `determine_route()` |
| 10 | Garde-fou de confiance | `virality_reason` suffit, pas de mention explicite supplémentaire |

### Nouvelle décision — architecture de diffusion sociale

| Sujet | Décision |
|---|---|
| Intégration API X/Twitter | **Annulée.** Aucune publication automatique. Le robot rédige un brouillon (`tweet_draft`), la rédaction relit et publie elle-même — voir §4 de `context.md` |
| Salons Discord | **2 routes, pas de salon par catégorie** : `#actus-videos` (Route A) et `#drafts-twitter` (Route B) |

Plus aucun point structurant n'est ouvert. Les seuls ajustements restants (liste de sources
étendue au-delà des 3 retenues, contenu précis d'`artist_tiers.yaml`, mapping exact des badges)
sont des paramètres de configuration modifiables sans toucher au code.
