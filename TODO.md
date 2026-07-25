# Feuille de route — Pipeline de curation K-pop

> Statut : **socle + cœur métier fonctionnels, testés (54 tests) et validés en conditions
> réelles (vrais appels Gemini, vrais messages Discord reçus et vérifiés sur mobile).**
> Le déploiement autonome (T10) est fait et validé : le pipeline tourne désormais seul sur
> GitHub Actions, toutes les 15 min, sans intervention manuelle. Chaque tâche est autonome,
> testable, et livrable dans l'ordre indiqué.
>
> **Architecture retenue (v3)** : hébergement 100 % gratuit (GitHub Actions, dépôt public),
> fournisseur LLM **Gemini Flash** (`gemini-3.5-flash-lite` en phase de test), routage à **deux salons Discord**
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
| **Fournisseur LLM** | **Google Gemini** (`gemini-3.5-flash-lite`, configurable), tier gratuit — derrière une interface interchangeable | Choisi pour la phase de test : 15 RPM / **500 RPD** gratuits (GA depuis le 21/07/2026 ; RPD réel du compte, plus serré que les 1500 documentés publiquement). Volume calibré (~150 art./jour ≈ 225 appels/jour en prod, voir point 4) confortablement sous ce plafond. SDK `google-genai`, sortie contrainte nativement via `response_schema` (Pydantic). Choix définitif pour la production à confirmer par T5bis |
| **Modèle de secours (quota)** | **`gemini-3.1-flash-lite`**, implémenté (T5ter) — bascule automatique sur 429 | Quota RPM/RPD indépendant du modèle principal, même clé/SDK/prompt : zéro risque qualité non validée, contrairement à un vrai fournisseur tiers. Déjà validé en conditions réelles (précédent modèle principal) — secours "connu", pas une inconnue |
| **Fournisseur LLM alternatif (non retenu comme fallback)** | Groq documenté, jamais implémenté | Écarté comme fallback automatique tant que T5bis n'a pas validé sa qualité éditoriale — resterait une option de bascule manuelle si Gemini devenait indisponible |
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
│   ├── sources.yaml             # flux RSS actifs (Soompi + Yonhap Culture)
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
- `git init`, commit local, **dépôt GitHub public créé et poussé** (`alihamdani1/kpop_`).
- **Fait quand** : `python -m kpop_bot --help` s'exécute sans erreur. ✔️ Vérifié.

### ✅ T2 — Configuration et secrets — FAIT
- `settings.py` opérationnel, échoue explicitement si un secret manque (vérifié).
- `config/sources.yaml` : **Soompi + Yonhap Culture + Koreaboo + Billboard K-Pop actifs ;
  allkpop présent mais désactivé** (flux RSS retiré par le site, confirmé par des 404 sur 4
  URLs candidates — aucun correctif code applicable ; Google News/RSS tiers écartés, voir
  discussion). Koreaboo remplace allkpop et comble un vrai vide : Soompi/Yonhap Culture ne
  couvraient quasiment aucun contenu scandale/gossip (vérifié sur 20 titres de chaque, zéro
  `SCANDALE_DRAMA`). Billboard K-Pop ajouté ensuite — angle scandale/industrie sérieux
  (procès, litiges), complémentaire du ton tabloïd de Koreaboo plutôt que redondant. Korea
  Herald, KpopStarz, Korea Times, Billboard K-Town testés et écartés (flux introuvables,
  volume trop faible, ou chevauchement éditeur).
- `config/artist_tiers.yaml` : table de départ en place.
- `.env` réel créé par l'utilisateur avec les vraies clés — testé en conditions réelles.

### ✅ T3 — Couche de persistance (SQLite versionnée) — FAIT
- Schéma complet en place (`route`, `tweet_draft`, `video_summary`, `france_override` inclus),
  tests unitaires au vert.
- `data/kpop.db` est suivie par git et **recommise automatiquement par le workflow GitHub
  Actions** à chaque cycle où elle change (confirmé — commit `github-actions[bot]` observé
  sur `origin/main`).

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
- **✅ FAIT — validé en conditions réelles** : appels Gemini réels effectués (alors sur
  `gemini-3.1-flash-lite`, modèle principal à l'époque du test — voir T5ter pour le changement
  de modèle depuis), classifications cohérentes, résumés en français, brouillons de tweet
  générés. Le 429 rencontré en test était un plafond RPM normal, correctement géré (cycle
  arrêté proprement, repris ensuite).

### ✅ T5ter — Modèle de secours Gemini (fallback quota) — FAIT
- **Décision** : un second modèle **Gemini** (pas Groq) — même clé API, même SDK, même format
  de prompt, quota RPM/RPD totalement indépendant du modèle principal. Choisi plutôt qu'un
  fournisseur tiers pour ne pas introduire une qualité éditoriale non validée (Groq n'a jamais
  été testé sur notre tâche — c'est justement l'objet de T5bis, toujours pas fait).
- `analyzer.py` : `_generate_with_fallback()` essaie `gemini_model` (principal), et
  **uniquement sur 429** retente une fois avec `gemini_fallback_model`. Toute autre erreur, ou
  un second 429 sur le modèle de secours, remonte normalement — le filet de sécurité existant
  (article repris au cycle suivant) reste la dernière protection.
- `classify()` et `write()` en bénéficient tous les deux.
- Modèle de secours retenu : `gemini-3.1-flash-lite` — c'était le modèle principal jusqu'au
  changement décrit ci-dessus, donc déjà validé en conditions réelles (voir T5). Un secours
  "connu et fiable" plutôt qu'une inconnue.
- **Mise à jour** : modèle principal passé à `gemini-3.5-flash-lite` (GA le 21/07/2026, même
  palier gratuit 15 RPM / 500 RPD réel), `gemini-3.1-flash-lite` conservé comme secours pour
  la raison ci-dessus. Changement de config pure (`settings.py`), aucun impact sur le
  mécanisme de bascule lui-même.
- Nouveau réglage `gemini_fallback_model` dans `settings.py` / `.env.example` — ajustable sans
  toucher au code, comme `gemini_model`.
- Bascule journalisée en `WARNING`, visible dans les logs GitHub Actions (T8).
- **Extension — throttling RPM** : `_throttle()` espace chaque appel réel d'au moins
  `gemini_min_seconds_between_calls` (4.5s par défaut, ~11% de marge sous 15 RPM). Nécessaire
  pour pouvoir relever `--limit` sans reproduire le 429 observé en rafale — voir T7.
  Vérifié en conditions réelles (appels espacés de 3.8-5.3s, zéro 429).
- **Fait quand** : test simulé confirmant la bascule (429 sur le principal → réponse correcte
  via le secours) + le cas où les deux échouent continue de lever proprement. ✔️ Vérifié
  (mocks — pas encore observé en conditions réelles, la situation ne s'est pas représentée).

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
- CLI réellement construite : `run` (cycle complet, `--dry-run`, `--limit`, défaut **30**
  depuis le durcissement RPM — voir T5ter) **et**
  `resend` (non prévu au plan initial — renvoie les articles déjà `SENT` sans appeler Gemini,
  ajouté pour permettre de tester le rendu Discord sans reconsommer de quota).
- **Simplification assumée par rapport au plan initial** : pas de sous-commandes séparées
  `fetch` / `analyze` / `send` — `run` fait tout d'un coup. À reconsidérer seulement si un besoin
  concret de les isoler apparaît.
- **Fait quand** : `python -m kpop_bot run --dry-run --limit 5` déroule le cycle complet sans
  rien envoyer. ✔️ Vérifié, ainsi qu'un cycle réel complet (Gemini + Discord).

### 🟡 T8 — Journalisation et observabilité — PARTIEL
- Logs horodatés, niveau configurable, résumé de fin de cycle (collectés/nouveaux/classifiés/
  Route A/Route B/filtrés/échoués/tokens) : **fait et vérifié en réel**, consultable dans
  l'onglet **Actions** du dépôt GitHub depuis que T10 tourne.
- **Pas encore fait** : commande `stats` (volumétrie/répartition sur 7 jours) — jamais construite.

### ✅ T9 — Tests — FAIT, maintenu à jour à chaque changement
- Unitaires + intégration `respx` (RSS, Gemini mocké, webhooks) — **54 tests, tous au vert**,
  aucun test n'appelle une API réelle. Mis à jour à chaque évolution (format 4 messages,
  filtre mots-clés France, User-Agent navigateur, `EmptyFeedError`, commande `resend`).

### ✅ T10 — Déploiement (workflow GitHub Actions) — FAIT, validé en conditions réelles
- Dépôt GitHub **public** créé (`alihamdani1/kpop_`), premier push effectué (branche `main`).
- `.github/workflows/pipeline.yml` : `schedule` (cron **15 min**) + `workflow_dispatch` ;
  `concurrency` (`pipeline-run`, `cancel-in-progress: false`) anti-chevauchement ; checkout,
  setup Python 3.12 (cache pip), secrets, exécution du pipeline, commit + push automatique de
  `data/kpop.db` si modifiée (`github-actions[bot]`, commit seulement si diff non vide).
- Les 3 secrets déclarés dans **GitHub → Settings → Secrets and variables → Actions** :
  `GEMINI_API_KEY`, `DISCORD_WEBHOOK_ROUTE_A`, `DISCORD_WEBHOOK_ROUTE_B`.
- **✅ FAIT — validé par un déclenchement manuel (`workflow_dispatch`) réel** : cycle complet
  exécuté sur le runner GitHub (collecte 108 articles, 5 classifiés dans la limite de test,
  1 article envoyé sur Route A avec les 4 messages Discord attendus, reçus et vérifiés sur
  mobile), `data/kpop.db` recommise et repoussée sur `main` (`013b1c2..cda8c35`), zéro erreur
  d'analyse ou d'envoi. Le pipeline tourne maintenant en autonome, plus besoin de le lancer
  depuis un terminal.

### ⏳ T11 — Réglage et durcissement — DÉBLOQUÉ (T10 fait), PAS COMMENCÉ
- Ajustement du prompt selon les erreurs observées en conditions réelles (catégorie, viralité,
  qualité des brouillons de tweet).
- Surveillance mensuelle des quotas gratuits Gemini (principal + secours).
- **Calibration du score de viralité** : si la rédaction note quels tweets publiés ont
  réellement bien marché, ces données permettent de recouper l'heuristique avec la réalité.
- ✅ **Fait plus tôt que prévu** : `--limit` relevé à 30 (ex-5) début vie du projet, dès qu'un
  vrai backlog (94 articles) a rendu le problème concret — voir T5ter pour le throttle qui
  rend ça sûr. Réévaluer encore à la hausse seulement si un nouveau goulot apparaît.
- Purge/archivage des articles anciens si la base versionnée grossit trop.

### 📝 T12 — Digest hebdomadaire — PLANIFIÉ, PAS ENCORE IMPLÉMENTÉ

Salon Discord dédié résumant les temps forts de la semaine — plus de valeur éditoriale qu'un
simple comptage (remplace l'idée initiale de commande `stats` de T8 sur ce terrain-là ; `stats`
reste utile pour du pur diagnostic technique, les deux ne sont pas mutuellement exclusifs).

**Principe retenu** : un seul appel IA par semaine (coût négligeable — 1 vs ~2100 appels/semaine
en régime normal), mais cet appel ne rédige QUE l'intro narrative. La liste des 10 articles
(titres, liens) est générée par le code à partir de la base, jamais par le modèle — pour ne
jamais risquer qu'il invente ou déforme une URL.

**Découpage** :

| # | Sous-tâche | Fichier | Contenu |
|---|---|---|---|
| T12.1 | Sélection des articles | `storage.py` | Nouvelle fonction `top_articles_since(conn, since, limit=10)` : articles `SENT` des 7 derniers jours, toutes routes confondues (A et B — une semaine calme peut avoir de bons articles B), triés par `virality` DESC puis `importance` DESC |
| T12.2 | Synthèse IA | `analyzer.py` | Nouveau schéma Pydantic `WeeklyDigestIntro` (un seul champ `intro: str`, 3-4 phrases) ; nouvelle méthode sur `GeminiAnalyzer`, bénéficie automatiquement du fallback (T5ter) |
| T12.3 | Construction et envoi | `notifier.py` | Nouvel embed : intro IA en description + liste numérotée des 10 articles (titre + lien, générée par le code) en fields ou texte |
| T12.4 | Orchestration + CLI | `pipeline.py`, `__main__.py` | `run_weekly_digest(settings)` ; nouvelle sous-commande `python -m kpop_bot weekly-digest` |
| T12.5 | Déploiement | `.github/workflows/` | Nouveau workflow séparé (pas de logique conditionnelle dans le cron 15 min existant), cron hebdomadaire |
| T12.6 | Tests | `tests/` | Sélection/tri (cas limites : moins de 10 articles disponibles, semaine sans aucun envoi), construction de l'embed, orchestration mockée |

**Décisions à confirmer avant de coder** (proposées par défaut, à corriger si besoin) :

| Sujet | Proposition par défaut |
|---|---|
| Nom du salon / webhook | `#recap-hebdo` → nouveau secret `DISCORD_WEBHOOK_WEEKLY` (optionnel dans `Settings`, pour ne pas casser `run` si non configuré) |
| Jour/heure d'exécution | Lundi 9h (heure UTC) |
| Style de contenu | Intro IA + liste fiable en code (validé à l'oral, retenu ci-dessus) |
| Portée de la sélection | Route A + Route B confondues (pas seulement Route A) |

**Fait quand** : un lundi, `#recap-hebdo` reçoit un message avec une intro cohérente sur la
semaine écoulée et 10 liens corrects vers les articles les plus importants envoyés.

---

## Ordre d'exécution — où on en est

```
✅T1 → ✅T2 → ✅T3 → ✅T4 → ✅T5 → ✅T5ter → ✅T6 → ✅T7 → 🟡T8 → ✅T9 → ✅T10 → T11
                                                                                ▲
                                                                      on est ici — objectif initial atteint
       └──────────────── socle + métier : fait ────────────────┘   (T4bis / T5bis toujours reportées)
```

**Restant concrètement** :
- **Objectif initial atteint** : le pipeline tourne 100 % en autonome sur GitHub Actions
  (cron 15 min), sans intervention manuelle, avec un modèle de secours en cas de quota atteint.
- **T11** — peut démarrer dès que quelques jours de production réelle auront donné assez de
  signal (erreurs de catégorie/viralité observées, quotas, qualité des brouillons de tweet).
- **T8** partiel — commande `stats` non construite (mineur, pas bloquant).
- **T12** — digest hebdomadaire, planifié ci-dessous, pas encore implémenté.
- **T4bis** (dédup sémantique) et **T5bis** (évaluation comparative Gemini/Groq) — volontairement
  repoussées, elles affinent un système qui marche déjà plutôt que de bloquer sa mise en route.
  T5bis nécessite en plus un travail d'annotation manuelle de ta part.

---

## Décisions prises

### Points 1 à 4 (sources, cadence, filet France, volume)

| # | Sujet | Décision |
|---|---|---|
| 1 | Sources RSS | **Soompi + Yonhap Culture + Koreaboo + Billboard K-Pop** actifs. **allkpop désactivé** — flux RSS mort (404 sur 4 URLs candidates), remplacé par Koreaboo (comble le vide gossip/scandale). Billboard K-Pop ajouté pour un angle scandale/industrie plus sérieux, complémentaire de Koreaboo. Korea Herald/KpopStarz/Korea Times/Billboard K-Town testés et écartés |
| 2 | Cadence du cron | **Fixée à 15 min** (GitHub Actions) |
| 3 | Filet mots-clés France | **Règle dure, hardcodée**, avant l'appel IA + réécriture forcée après coup. Mots-clés : `Paris`, `France`, `Accor Arena`, `Stade de France`, `Zénith` |
| 4 | Volume attendu | **~150 articles/jour en production** (≈225 appels Gemini/jour). `--limit` relevé de 5 à **30** une fois le backlog réel (94 articles) constaté — voir T5ter |

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

### Nouvelle décision — résilience quota et digest hebdomadaire

| Sujet | Décision |
|---|---|
| Fallback sur quota dépassé | **Second modèle Gemini**, pas Groq — quota indépendant, zéro risque qualité non validée. Implémenté (T5ter). Principal : `gemini-3.5-flash-lite` ; secours : `gemini-3.1-flash-lite` |
| Digest hebdomadaire | Remplace/complète l'idée de commande `stats` (T8) par quelque chose de plus utile éditorialement — top 10 de la semaine, intro IA + liste fiable en code. Planifié (T12), pas encore codé |

Plus aucun point structurant n'est ouvert sur l'architecture initiale. Les seuls ajustements
restants (liste de sources étendue au-delà des 3 retenues, contenu précis d'`artist_tiers.yaml`,
mapping exact des badges, détails de configuration de T12) sont des paramètres modifiables sans
toucher au code, ou des sous-tâches déjà découpées ci-dessus.
