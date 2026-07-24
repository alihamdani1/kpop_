# Feuille de route — Pipeline de curation K-pop

> Statut : **toutes les décisions structurantes sont prises — développement en cours.**
> Chaque tâche est autonome, testable, et livrable dans l'ordre indiqué.
>
> **Architecture retenue (v3)** : hébergement 100 % gratuit (GitHub Actions, dépôt public),
> fournisseur LLM **Gemini Flash** (`gemini-3.6-flash`), routage à **deux salons Discord**
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
| **Fournisseur LLM** | **Google Gemini** (`gemini-3.6-flash`, configurable), tier gratuit — derrière une interface interchangeable | Volume calibré (~150 art./jour en prod, voir point 4) très sous le plafond gratuit. SDK `google-genai`, sortie contrainte nativement via `response_schema` (Pydantic) |
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

### T1 — Initialisation du projet
- Créer l'arborescence ci-dessus, `pyproject.toml`, `.gitignore` (`.env`, `__pycache__` — **pas** `data/`, la base est versionnée).
- Environnement virtuel + installation des dépendances.
- `git init` + premier commit local. Le push vers un dépôt GitHub public est fait au moment de
  la mise en production (T10), pas avant.
- Configurer `ruff`.
- **Fait quand** : `python -m kpop_bot --help` s'exécute sans erreur.

### T2 — Configuration et secrets
- `settings.py` : chargement des variables d'environnement — `GEMINI_API_KEY`,
  `DISCORD_WEBHOOK_ROUTE_A`, `DISCORD_WEBHOOK_ROUTE_B` — via `pydantic-settings`, indifférent
  à leur origine (`.env` local ou GitHub Secrets en CI).
- `config/sources.yaml` : liste des flux actifs. **Contenu actuel : Soompi, Yonhap
  Entertainment, allkpop** (point 1 tranché — voir plus bas).
- `config/artist_tiers.yaml` : table statique de « poids » par groupe/artiste (Tier 1, Tier 2,
  Tier 3…), injectée dans le prompt système de T5. Quelques entrées suffisent au démarrage —
  enrichissable sans toucher au code.
- `.env.example` documenté.
- **Fait quand** : la config se charge et échoue *explicitement* si une clé obligatoire manque,
  en local comme en CI.

### T3 — Couche de persistance (SQLite versionnée)
- Table `articles` : `id`, `fingerprint` (UNIQUE), `source`, `title`, `url`, `published_at`,
  `raw_summary`, `status`, `category`, `importance`, `virality`, `virality_reason`, `route`,
  `france_override` (bool), `summary_fr`, `video_summary`, `tweet_draft`, `artists`,
  `tokens_in`, `tokens_out`, `prompt_version`, `created_at`, `sent_at`, `error`.
  *(`raw_summary`, `prompt_version`, `france_override` conservés dès le départ : utile pour
  reconstituer un jeu d'évaluation et pour auditer le filet de sécurité plus tard.)*
- Création idempotente du schéma au démarrage ; index sur `fingerprint` et `status`.
- Fonctions : `exists(fingerprint)`, `insert_new(...)`, `mark(status, ...)`, `pending(status)`.
- **Particularité v2/v3** : `data/kpop.db` doit être **recommis dans le dépôt** à la fin de
  chaque run GitHub Actions (T10) — les runners sont jetables, sans ça la dédup repart de zéro.
- **Fait quand** : tests unitaires sur base temporaire — insertion, rejet du doublon, transitions
  de statut.

### T4 — Collecte RSS
- Lecture de tous les flux actifs (`config/sources.yaml`) avec `feedparser`, timeout par source.
- Normalisation : URL canonique (suppression des paramètres `utm_*`), date en UTC, extrait
  nettoyé du HTML.
- Empreinte = SHA-256 de l'URL canonique.
- Une source en échec est journalisée et n'interrompt pas les autres.
- **Fait quand** : la commande `fetch` insère uniquement les nouveautés ; deuxième exécution
  consécutive → 0 insertion.

### T4bis — Déduplication sémantique *(reportée après T5/T6 — non prioritaire pour l'instant)*
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
- **Fait quand** : sur un jeu de 10 articles réels, catégorie/importance/viralité cohérentes,
  résumés en français, brouillons de tweet sous 280 caractères et prêts à l'emploi, filet
  France vérifié sur un article de test contenant « Accor Arena ».

### T5bis — Évaluation du modèle avant mise en production
- Annoter à la main ~60 articles réels : catégorie/importance attendues, jugement humain
  « j'aurais tweeté ça / non » pour la viralité, et une relecture critique des brouillons de
  tweet générés (ton, longueur, pertinence des hashtags).
- Comparer Gemini Flash vs Groq sur : justesse de catégorie, taux de rattrapage du bruit,
  qualité du français, cohérence viralité/jugement humain, qualité des brouillons de tweet.
- **Fait quand** : le choix de fournisseur est confirmé par les chiffres.

### T6 — Routage et diffusion Discord
- `BRUIT_INUTILE` → statut `FILTERED`, **aucun envoi**, aucun 2e appel IA.
- Sinon : `determine_route()` décide Route A ou B, construction de l'embed correspondant
  (voir tableau « Routage Discord » ci-dessus), envoi au webhook de la route.
- Respect du rate-limit Discord (`429` + `Retry-After`), pause entre envois.
- Passage en `SENT` **uniquement** après réponse HTTP positive → aucun doublon en cas de crash.
- **Fait quand** : `#actus-videos` reçoit les articles VIRAL/ÉLEVÉ/Concert France avec résumé
  détaillé + tweet ; `#drafts-twitter` reçoit le reste (hors bruit) avec juste titre/score/tweet ;
  aucun article « bruit » n'apparaît nulle part.

### T7 — Orchestration du pipeline
- `pipeline.py` : collecte → (dédup sémantique, T4bis) → classification des `NEW` → routage →
  rédaction (si retenu) → diffusion → reprise des `FAILED`.
- CLI : `run` (cycle complet), `fetch`, `analyze`, `send`, `stats`, `--dry-run` (aucun envoi
  réel), `--limit N` (plafond d'articles par cycle — **5 en développement/test**, voir point 4).
- **Fait quand** : `python -m kpop_bot run --dry-run --limit 5` déroule le cycle complet sans
  rien envoyer, sur le flux de test Soompi.

### T8 — Journalisation et observabilité
- Logs horodatés, niveau configurable — consultables dans l'onglet **Actions** de GitHub.
- Résumé de fin de cycle : collectés / nouveaux / classifiés / Route A / Route B / filtrés /
  échoués + tokens consommés (par appel 1 et appel 2).
- Commande `stats` : volumétrie et répartition par catégorie, route et niveau de viralité sur
  7 jours.
- **Fait quand** : l'historique des runs GitHub Actions donne une lecture claire de chaque
  exécution.

### T9 — Tests
- Unitaires : normalisation d'URL, empreinte, filtre mots-clés France (matchs et non-matchs),
  `determine_route()` (toutes les combinaisons catégorie × viralité), validation `tweet_draft`
  (rejet si > 280 caractères), construction des deux embeds.
- Intégration avec `respx` : flux RSS simulé + réponses Gemini simulées (appel 1 et 2) +
  webhooks simulés → pipeline de bout en bout, **sans réseau**.
- **Fait quand** : `pytest` passe au vert, aucun test n'appelle une API réelle.

### T10 — Déploiement (workflow GitHub Actions)
- Création du dépôt GitHub **public**, premier push.
- `.github/workflows/pipeline.yml` : `schedule` (cron **15 min**) + `workflow_dispatch` ;
  `concurrency` anti-chevauchement ; checkout, setup Python (cache dépendances), secrets,
  exécution du pipeline, commit automatique de `data/kpop.db` si modifiée.
- Secrets déclarés dans **GitHub → Settings → Secrets and variables → Actions** :
  `GEMINI_API_KEY`, `DISCORD_WEBHOOK_ROUTE_A`, `DISCORD_WEBHOOK_ROUTE_B`.
- **Fait quand** : le workflow tourne seul toutes les 15 min, committe son état, un
  déclenchement manuel fonctionne pour les tests.

### T11 — Réglage et durcissement (après premières journées de production)
- Ajustement du prompt selon les erreurs observées en conditions réelles (catégorie, viralité,
  qualité des brouillons de tweet).
- Surveillance mensuelle des quotas gratuits Gemini/Groq.
- **Calibration du score de viralité** : si la rédaction note quels tweets publiés ont
  réellement bien marché, ces données permettent de recouper l'heuristique avec la réalité.
- Retrait progressif de `--limit 5` une fois le volume de production (~150/jour) validé.
- Purge/archivage des articles anciens si la base versionnée grossit trop.

---

## Ordre d'exécution

```
T1 → T2 → T3 → T4 → T5 → T6 → T7 → T8 → T9 → (T4bis) → T5bis → T10 → T11
       └────── socle ──────┘   └── métier (priorité actuelle) ──┘
```

T4bis (déduplication sémantique) et T5bis (évaluation comparative) sont volontairement
repoussées après un premier cycle fonctionnel bout en bout — elles affinent un système qui
marche déjà, plutôt que de bloquer sa mise en route.

---

## Décisions prises

### Points 1 à 4 (sources, cadence, filet France, volume)

| # | Sujet | Décision |
|---|---|---|
| 1 | Sources RSS | **Soompi + Yonhap Entertainment** (fiabilité) + **allkpop** (volume, alimente la Route B). En développement : Soompi seul dans `sources.yaml` |
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
