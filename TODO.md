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
| **Modèles de secours (quota)** | **`gemini-3.1-flash-lite`** puis **`gemini-2.5-flash-lite`**, implémentés (T5ter / T5quinquies) — bascule automatique sur 429, dans cet ordre | Quota RPM/RPD indépendant du modèle principal, même clé/SDK/prompt : zéro risque qualité non validée, contrairement à un vrai fournisseur tiers |
| **2e clé API (montée en volume)** | Optionnelle (`gemini_api_key_2`), implémentée (T5quinquies) — retente toute la chaîne de modèles ci-dessus sur un 2e compte Google, uniquement après épuisement de la 1re clé | Double le pool de quotas gratuits disponible sans changer de fournisseur ni de format de prompt |
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
- `config/sources.yaml` : **Soompi + Yonhap Culture + Koreaboo + Billboard K-Pop + KpopStarz
  actifs ; allkpop présent mais désactivé** (flux RSS retiré par le site, confirmé par des
  404 sur 4 URLs candidates — aucun correctif code applicable ; Google News/RSS tiers
  écartés, voir discussion). Koreaboo remplace allkpop et comble un vrai vide : Soompi/Yonhap
  Culture ne couvraient quasiment aucun contenu scandale/gossip (vérifié sur 20 titres de
  chaque, zéro `SCANDALE_DRAMA`). Billboard K-Pop ajouté ensuite — angle scandale/industrie
  sérieux (procès, litiges), complémentaire du ton tabloïd de Koreaboo. **KpopStarz ajouté
  après un vrai manque constaté** : la sortie "WET" de J.Y. Park (23/07/2026) n'était couverte
  par aucune des 4 sources actives à l'époque — son vrai flux RSS (introuvable au premier
  essai) a été retrouvé en inspectant le HTML de la page d'accueil, contient bien la story
  manquée. Korea Herald, Korea Times, Korea JoongAng Daily, Billboard K-Town testés et
  écartés (flux introuvables/inexistants, volume trop faible, ou chevauchement éditeur).
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
| `tweet_draft` | str, **≤ 260 caractères côté IA** | Toujours généré. Ton journalistique neutre, 1-2 emojis max, 2 hashtags pertinents, en français, se termine par une question d'engagement. Validé par Pydantic (`max_length=260`) — un dépassement déclenche le chemin d'erreur existant (`FAILED`, repris au cycle suivant), pas une troncature silencieuse. Plafond abaissé de 280 à 260 pour laisser la place au tag préfixé en code juste après — voir T5quater |
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

### ✅ T5quater — Tag de catégorie + accroche d'engagement sur les tweets — FAIT
- **Demande** : préfixer chaque tweet d'un tag visuel (`[GOSSIP]`, `[RELEASE]`, `[FLASH]`,
  `[FRANCE]`) avec emoji, et terminer par une question qui donne envie de réagir (ex. « Notez le
  son sur 10 » pour une sortie).
- **Décision d'architecture** : le tag est **dérivé en code** (`determine_tweet_tag()` dans
  `models.py`) à partir de `category`/`importance`/`virality` déjà connus — jamais redemandé à
  l'IA. Deux raisons : (1) zéro coût/latence supplémentaire, (2) impossible que le tag contredise
  la catégorie déjà affichée dans l'embed, contrairement à une 2e classification IA indépendante
  sur la même donnée.
- Règles de `determine_tweet_tag()` (priorité dans cet ordre) :
  1. `MAJEUR` + viralité `VIRAL`/`ELEVE` → **FLASH** ⚡ (l'urgence prime sur le sujet)
  2. `SCANDALE_DRAMA` → **GOSSIP** 🍵
  3. `CONCERT_EVENEMENT_FRANCE` → **FRANCE** 🇫🇷
  4. `COMEBACK_SORTIE` → **RELEASE** 🎵
  5. Repli générique → **INFO** 📰 (non atteint aujourd'hui — `BRUIT_INUTILE` est toujours
     `IGNORED` avant d'arriver ici — mais garde-fou correct si une catégorie sans branche dédiée
     apparaît un jour ; distinct de RELEASE pour ne pas laisser croire qu'un contenu générique
     est spécifiquement une sortie musicale)
- Le préfixage (`f"{TWEET_TAG_LABELS[tag]}\n\n{tweet_draft}"`) a lieu dans `pipeline.py`,
  immédiatement après `gemini.write()` et avant l'enregistrement en base — donc la version
  taguée est ce qui est stockée, envoyée sur Discord, et renvoyée par `resend`.
- L'accroche de fin de tweet, elle, **reste générée par l'IA** (contrairement au tag) : son bon
  angle dépend du contenu précis de l'article, un gabarit figé sonnerait vite répétitif. Injectée
  via un nouveau paramètre `{engagement_hook}` dans le prompt de rédaction, avec une consigne
  différente par catégorie (`_ENGAGEMENT_HOOKS` dans `analyzer.py` — comeback : noter le son sur
  10 ou donner son avis ; scandale : avis neutre sans prendre parti ; concert France : compter
  parmi le public ou non).
- Garde-fou : le plafond IA passe de 280 à 260 caractères (le plus long tag, `🇫🇷 [FRANCE]`, fait
  ~13 caractères + 2 sauts de ligne) ; un log `WARNING` défensif signale tout dépassement de 280
  après préfixage, qui ne devrait jamais se produire en pratique.
- **Fait quand** : tests unitaires sur les 4 branches de `determine_tweet_tag()` + complétude de
  `TWEET_TAG_LABELS` (`test_models.py`), test sur l'injection de la bonne consigne d'engagement
  par catégorie dans le prompt (`test_analyzer.py`), test d'intégration bout en bout vérifiant
  que le tweet stocké/envoyé par `run_cycle()` porte bien le tag attendu (`test_pipeline.py`).
  ✔️ Vérifié par tests (mocks) — pas encore observé en conditions réelles (prochain cycle
  GitHub Actions).

### ✅ T5quinquies — 2e clé API + 3e modèle de secours (montée en volume) — FAIT
- **Demande** : augmenter le volume d'appels Gemini possible, en ajoutant (1) un 3e modèle dans
  la chaîne de secours et (2) une 2e clé API (2e compte Google), pour disposer d'un pool de
  quotas plus large sans changer de fournisseur.
- **Décision d'architecture** : `GeminiAnalyzer` ne prend plus une clé/un modèle principal/un
  modèle de secours séparément, mais `api_keys: list[str]` et `models: list[str]` — la même
  chaîne de modèles est tentée sur chaque clé, dans l'ordre (`_generate_with_fallback()` construit
  la liste de toutes les combinaisons clé×modèle et les essaie une à une, uniquement sur 429).
  Repli sur la 2e clé seulement après épuisement des **3** modèles sur la 1re — donc en dernier
  recours, pas en répartition de charge.
- Chaîne de modèles (identique sur les deux clés) : `gemini_model` (`gemini-3.5-flash-lite`) →
  `gemini_fallback_model` (`gemini-3.1-flash-lite`, T5ter) → **`gemini_second_fallback_model`**
  (nouveau, `gemini-2.5-flash-lite`).
- Nouveau réglage optionnel `gemini_api_key_2` dans `settings.py` / `.env` — absent par défaut,
  comportement mono-clé inchangé. Clé réelle du 2e compte de l'utilisateur ajoutée dans `.env`
  (jamais commité, voir `.gitignore`).
- Throttle (`_throttle()`) resté partagé entre toutes les clés/modèles, comme pour T5ter — le
  repli sur la 2e clé reste rare, un compteur séparé par clé n'apporterait rien.
- **Fait quand** : tests couvrant la bascule complète 1re clé (3 modèles épuisés) → 2e clé
  (reprise au modèle principal), et le cas où les deux clés épuisent toute la chaîne
  (`test_analyzer.py`). ✔️ Vérifié par tests (mocks) — pas encore observé en conditions réelles.

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

### ✅ T13 — Rattrapage des faux négatifs BRUIT_INUTILE (records/paliers viraux) — FAIT

**Origine** : l'article Soompi *"BLACKPINK's 'DDU-DU DDU-DU' Becomes 1st K-Pop Group MV Ever To
Hit 2.4 Billion Views"* n'a jamais été envoyé — classé `BRUIT_INUTILE` par l'IA (confirmé en
base : `status=FILTERED`, aucune erreur, donc **pas** un problème de quota). En creusant, 2 des
5 titres "record/million/billion" du corpus étaient de vrais faux négatifs (ce record BLACKPINK,
et "Gangnam Style" à 6 milliards de vues) — l'IA généralise trop le critère « classement
racoleur » à de vrais records chiffrés.

Trois filets complémentaires, chacun avec un rôle distinct :

| # | Mesure | Fichier | Garantie |
|---|---|---|---|
| 1 | Fix de prompt (wording `BRUIT_INUTILE`) | `analyzer.py` | Aucune — améliore le taux général, reste probabiliste (modèle *lite*, distinction intrinsèquement floue) |
| 2 | Filet déterministe mots-clés + artiste connu | `analyzer.py`, `settings.py` | Garantit ce motif précis (record chiffré + artiste répertorié), même si l'IA se trompe |
| 3 | Salon `#info-a-verifier` | `notifier.py`, `pipeline.py` | Garantit qu'aucun article n'est **définitivement** perdu, quoi que fassent 1 et 2 |

**1. Fix de prompt** — `_CLASSIFICATION_SYSTEM_PROMPT` distingue désormais explicitement un
« classement/liste subjectif ou putaclic » (reste `BRUIT_INUTILE`) d'un « record ou palier
VÉRIFIABLE ET CHIFFRÉ » lié à un artiste connu (ne l'est pas). `PROMPT_VERSION` passé à `v2`
pour distinguer les articles classés avant/après ce changement.

**2. Filet record/palier** (même principe que le filet France, mais correction en dernier
recours plutôt que règle dure — il n'y a pas de catégorie unique garantie ici) :
- `matches_viral_milestone(item, keywords, artist_tiers)` (`analyzer.py`) : vrai seulement si
  **un mot-clé de record ET un artiste de `artist_tiers.yaml`** sont tous deux présents — un
  mot-clé seul (ex. "record") serait bien trop générique (confirmé par un cas réel du corpus :
  un record de fréquentation de musée, sans rapport). Nouveau réglage `viral_milestone_keywords`
  dans `settings.py` (`billion views`, `million views`, `billion streams`, `million streams`,
  `million copies`, `million albums`, `record high`, `all-time high`).
- Si détecté : le prompt de classification reçoit une note système (`_MILESTONE_NOTE`, même
  mécanisme que `_FRANCE_NOTE`) précisant que ce N'EST PAS du bruit. **Si l'IA maintient quand
  même `BRUIT_INUTILE`** malgré la note, `classify()` force la catégorie à `COMEBACK_SORTIE`
  avec une viralité `ELEVE` et une raison explicite ("Filet record/palier : ...") — un repli
  raisonnable plutôt qu'un silence total, ajustable par un humain via Discord.
- N'écrase jamais un jugement IA déjà correct (catégorie ≠ `BRUIT_INUTILE`, ou viralité déjà
  fournie) — uniquement un filet de dernier recours.
- Priorité : le filet France reste prioritaire si les deux se déclenchent en même temps (un
  concert en France reste `CONCERT_EVENEMENT_FRANCE`, catégorie plus spécifique).

**3. Salon `#info-a-verifier`** — filet de dernier recours pour **tout** ce qui reste classé
`BRUIT_INUTILE` après les deux mesures ci-dessus, quelle qu'en soit la raison :
- **Aucun appel Gemini supplémentaire** : réutilise la classification déjà produite par le 1er
  appel IA (`classify()`), qui tourne déjà pour tous les articles, bruit inclus. Un seul message
  Discord allégé par article (titre original, source, catégorie/importance, lien) — pas le
  format 4-messages d'A/B, qui n'a de sens que pour du contenu à copier-coller.
- Nouveau statut `ArticleStatus.FILTERED_SENT` : un article `FILTERED` transmis à ce salon n'y
  est envoyé qu'une seule fois (sinon reproposé à chaque cycle de 15 min, indéfiniment).
- **Optionnel** (`DISCORD_WEBHOOK_INFO_A_VERIFIER` absent = comportement inchangé, ces articles
  restent simplement `FILTERED`) — pour ne jamais casser un déploiement existant qui n'aurait
  pas ce salon configuré.
- **Volume réel mesuré** : 95 articles `FILTERED` sur 133 traités (71 %) au moment de la
  demande — largement plus que ce qui part sur A+B. Message volontairement allégé (1 message,
  pas 4) pour rester gérable à ce volume.
- Webhook réel fourni par l'utilisateur, salon `#info-a-verifier`, ajouté dans `.env` (jamais
  commité) — **à ajouter aussi en secret GitHub Actions** (`DISCORD_WEBHOOK_INFO_A_VERIFIER`)
  pour être actif en production, comme les webhooks A/B.

**Fait quand** : tests sur `matches_viral_milestone` (mot-clé + artiste requis, aucun faux
positif sur un mot-clé seul), sur l'injection de la note système et la correction de dernier
recours dans `classify()` (et sa non-intervention si l'IA a déjà bien classé), sur
`build_review_message`/`notify_review`, et un test d'intégration bout en bout par mesure dans
`test_pipeline.py` (flag transmis, article envoyé + marqué `FILTERED_SENT`, comportement
inchangé si le webhook n'est pas configuré). ✔️ Vérifié par tests (mocks) — pas encore observé
en conditions réelles (prochain cycle GitHub Actions, une fois le secret ajouté).

### ✅ T14 — Script TikTok dédié (Route A) — FAIT

**Demande** : en plus du tweet (inchangé) et de `video_summary` (conservé tel quel), générer un
vrai script TikTok structuré (accroche + corps + idées de montage) pour les articles Route A,
envoyé sur un salon Discord dédié, en utilisant la 2e clé API Gemini en priorité.

**Architecture — 3e appel Gemini, dédié, Route A uniquement** :
- Schéma `TikTokScriptResult` (`models.py`), **révisé une fois** après relecture par
  l'utilisateur (voir « Itération du prompt » ci-dessous) — version finale à 6 champs : `hook`,
  `on_screen_texte` (texte overlay), `script_body`, `closing_hook`, `visual_ideas` (liste de
  3-5 suggestions de plans/montage), `caption_seo` (sous-objet `TikTokCaptionSeo` : `legende` +
  `hashtags`). **Aucun emoji autorisé dans aucun champ** (contrairement au tweet) — demande
  explicite de l'utilisateur.
- Prompt système dédié `_TIKTOK_SYSTEM_PROMPT` (`analyzer.py`), séparé de
  `_WRITING_SYSTEM_PROMPT` — demander au même appel un tweet court et contraint ET un script
  plus long avec des idées de montage dilue la qualité des deux sur un modèle lite ; un prompt
  à objectif unique est plus fiable. Structure hook→promesse tenue par le corps, budgets de
  mots/secondes par section (indicatifs, non validés par le code), étape d'auto-vérification
  avant réponse (relire et corriger toute invention ou emoji resté), exemple concret (few-shot)
  pour calibrer le ton. Pas de schéma JSON recopié en dur dans le texte du prompt : redondant
  avec `response_schema=TikTokScriptResult`, qui contraint déjà la sortie côté API et prime
  toujours en cas de divergence.
- Nouvelle méthode `GeminiAnalyzer.write_tiktok_script()`, réutilise `_generate_with_fallback()`
  → hérite automatiquement de toute la chaîne de secours (modèles + clés) sans code dupliqué.

**Itération du prompt (relecture utilisateur)** : l'utilisateur a proposé une version nettement
plus élaborée que le prompt initial (structure hook/promesse, texte à l'écran, auto-vérification,
légende SEO, exemple concret). Retour objectif donné avant implémentation : les ajouts de fond
sont de vraies améliorations (technique de copywriting reconnue), mais **le schéma JSON décrit
en toutes lettres dans le prompt ne servait à rien** tant que `TikTokScriptResult` (le schéma
réellement imposé à l'API) n'était pas mis à jour en conséquence — l'API applique toujours son
propre schéma, quoi que le texte du prompt demande en plus. Corrigé en modifiant le schéma
Pydantic pour qu'il corresponde exactement à ce que l'utilisateur voulait produire, et en
retirant le bloc JSON redondant du texte du prompt.

**Clé 2 en priorité** (demande explicite, pas le comportement par défaut de la chaîne de
secours) : plutôt que de modifier `_generate_with_fallback()` (logique déjà testée, on n'y
touche pas), `pipeline.py` instancie un **2e `GeminiAnalyzer` séparé**, dédié au script TikTok,
avec la liste de clés inversée (`_tiktok_api_keys()` : `[clé_2, clé_1]` au lieu de
`[clé_1, clé_2]`). La chaîne de secours existante fait le reste : clé 2 devient « essayée en
premier », clé 1 reste le repli si la clé 2 épuise toute sa chaîne de modèles. Aucune nouvelle
logique de retry écrite. Cette 2e instance n'est construite que si `discord_webhook_tiktok` est
configuré — sinon aucun coût, comportement strictement identique à avant T14.

**Échecs non bloquants** : contrairement à `classify()`/`write()`, un échec de génération ou
d'envoi du script TikTok (quota, erreur d'analyse, webhook en échec) ne fait ni échouer
l'article ni arrêter le cycle — c'est un bonus une fois le tweet/résumé vidéo déjà réussis, pas
un pré-requis de diffusion. Nouveaux compteurs dans `CycleStats` : `tiktok_generated`,
`tiktok_generation_failed`, `tiktok_sent`, `tiktok_send_failed`.

**Salon dédié** : nouveau webhook optionnel `discord_webhook_tiktok` (`settings.py`), fourni
par l'utilisateur et déjà en place dans `.env` local. Envoyé en plus de #actus-videos (Route A),
jamais à la place — `video_summary` reste inchangé et continue d'alimenter l'embed existant.
4 messages (même logique que `notify()`/`notify_review`) : en-tête `# INFO {n}` (compteur
séparé, propre à ce salon), embed contextuel (score + résumé détaillé + idées de montage — ces
dernières **uniquement** dans ce salon, pas dans #actus-videos pour ne pas l'encombrer), en-tête
`# Script TikTok`, puis un seul message brut regroupant accroche + texte à l'écran + corps +
chute + légende/hashtags — tout ce qui doit être copié pour tourner et publier, en un bloc.

**Migration de base** : `data/kpop.db` est réelle et versionnée avec des données en production
— `CREATE TABLE IF NOT EXISTS` n'aurait pas ajouté les nouvelles colonnes à la table existante.
Migration idempotente (`_migrate_schema()`, via `PRAGMA table_info` + `ALTER TABLE`) appelée à
chaque `init_db()` : couvre à la fois les bases déjà existantes et les bases neuves (créées
directement avec les colonnes par `_SCHEMA`, donc la migration y est un no-op). Étendue une 2e
fois (3 colonnes en plus) lors de la révision du schéma à 6 champs, sans casser les données déjà
migrées.

**Restant à faire (côté utilisateur)** : ajouter `DISCORD_WEBHOOK_TIKTOK` comme secret GitHub
Actions (déjà dans `.env` local, mais pas encore en production) — sans ça, le mécanisme reste
inactif sur le cron GitHub Actions.

**Fait quand** : tests sur le prompt dédié et son injection catégorie/importance/viralité
(`test_analyzer.py`), sur la migration de schéma (simulée sur une base « pré-T14 »,
`test_storage.py`), sur la construction des messages et l'ordre d'envoi (`test_notifier.py`),
et des tests d'intégration bout en bout dans `test_pipeline.py` : script généré seulement pour
la Route A, clé 2 en priorité (`_tiktok_api_keys`), échec non bloquant (article quand même
envoyé), comportement inchangé si le webhook n'est pas configuré (aucun appel Gemini
supplémentaire). ✔️ Vérifié par tests (mocks) — pas encore observé en conditions réelles.

### 📝 T15 — Threads Twitter quotidiens (backlog Topics + sélection Discord + génération Gemini) — PLANIFIÉ, PAS ENCORE IMPLÉMENTÉ

**Demande** : automatiser la création d'un thread Twitter par jour, avec un vrai flux de
sélection humaine avant génération (pas un simple envoi automatique) : le système propose 3
combinaisons (sujet, angle) jamais utilisées, la rédaction en choisit une par réaction Discord,
puis Gemini rédige le thread complet, diffusé pour relecture avant copier-coller manuel sur X
(même logique human-in-the-loop que `tweet_draft` — aucune intégration directe à l'API X, comme
pour le reste du projet).

**Différence de nature avec le reste du pipeline** : les Topics de thread sont **générés
librement par l'IA** (décision actée avec l'utilisateur), pas obligatoirement ancrés sur un
article déjà collecté. Conséquence assumée : contrairement au filet France ou au filet
record/palier (T13), **aucun filet déterministe n'est possible ici** — il n'y a pas de source
vérifiable à recouper. La relecture humaine avant publication reste donc la seule protection
contre une éventuelle invention factuelle, exactement comme pour `tweet_draft` aujourd'hui. Le
prompt de rédaction inclut malgré tout une consigne explicite de prudence (rester sur des faits
largement connus, préférer une formulation générale à un chiffre/une citation non sûrs).

**Interaction Discord — contrainte d'infrastructure** : de vrais boutons Discord (Components)
nécessitent soit un bot à connexion Gateway permanente, soit un endpoint HTTP public
(Interactions Endpoint) — les deux sont incompatibles avec le principe 100 % gratuit / sans
serveur du projet (tout tourne sur des crons GitHub Actions éphémères, voir T10). Solution
retenue : **réactions emoji + sondage périodique**, via un Bot Discord (token REST simple,
`httpx`, aucune connexion Gateway, aucune nouvelle dépendance) qui ne fait que lire/ajouter des
réactions — l'envoi des messages reste 100 % webhook, comme le reste du pipeline.

**Flux en 2 étapes** :

```
[thread_topics]  backlog alimenté par lot (job hebdomadaire, un seul appel Gemini d'idéation)
        │  quotidien : 3 (topic, angle) jamais consommés, diversifiés groupe/thème
        ▼
[thread-select] ── poste l'embed (webhook, ?wait=true pour récupérer le message_id)
        │           + seed des réactions 🇦🇧🇨 (bot token)
        │           enregistre thread_selections (PENDING)
        ▼  cron fréquent — nouvelle étape ajoutée au cron 15 min existant (pipeline.yml)
[thread-resolve] ── lit les réactions (bot token) sur les sélections PENDING
        │
        └─ réaction humaine détectée ──▶ Gemini write_thread(topic, angle, hook)
                                              │
                                              ▼  ThreadWritingResult.tweets: list[str]
                                    stocké dans `threads` (UNIQUE(topic_id, angle))
                                              │
                                              ▼
                        notifier.notify_thread() — 1 message Discord par tweet
                        (bloc de code ```, pour que "Copier le texte" marche tweet par tweet)
```

**Modèle de données — nouvelles tables** (ajoutées à `_SCHEMA`, `CREATE TABLE IF NOT EXISTS` —
pas de migration de colonnes nécessaire, ce sont des tables neuves) :

| Table | Rôle | Colonnes clés |
|---|---|---|
| `thread_topics` | Backlog de sujets (Groups/Themes = attributs, pas des tables séparées) | `id, group_name, theme, title, premise, created_at, last_offered_at, source` |
| `thread_selections` | État du picker quotidien | `id, discord_message_id, option_{a,b,c}_topic_id, option_{a,b,c}_angle, status, resolved_topic_id, resolved_angle, created_at, resolved_at` |
| `threads` | Thread généré, sert aussi de registre de consommation | `id, selection_id, topic_id, angle, hook_label, tweets (JSON), status, tokens_in, tokens_out, prompt_version, created_at, sent_at, error` — **`UNIQUE(topic_id, angle)`** = garantie dure qu'un couple (sujet, angle) n'est jamais régénéré |

**Nouveaux enums** (`models.py`, même style `StrEnum` que `Category`/`Importance`) :
- `ThreadTheme` : RIVALITE_COMPARAISON, RETROSPECTIVE_CARRIERE, ANALYSE_COMEBACK,
  RECAP_SCANDALE, CONNEXION_FRANCE, MYTHE_VS_REALITE, RECORD_ANECDOTE, COULISSES_INDUSTRIE,
  CULTURE_FANS — liste de départ, extensible.
- `ThreadAngle` : CONTRARIEN, GUIDE_PRATIQUE, CAS_ETUDE, STORYTELLING.
- `SelectionStatus` (PENDING/RESOLVED/EXPIRED), `ThreadStatus` (DRAFT/SENT/FAILED).

**Nouveaux schémas IA** : `ThreadTopicIdea` (group_name/theme/title/premise) +
`TopicIdeationResult` (lot complet, un seul appel Gemini par réapprovisionnement) ;
`ThreadWritingResult` (`tweets: list[str]`, validator imposant 5-8 tweets et `max_length=260`/tweet
— même marge que `WritingResult.tweet_draft`, pour laisser la place à un préfixe "n/total" ajouté
en code, jamais par l'IA, même logique que `TWEET_TAG_LABELS`).

**Registre de hooks viraux** : pas de nouvelle table — un dict Python dans `analyzer.py` (même
pattern que `_ENGAGEMENT_HOOKS`). Diversité pilotée en interrogeant `threads.hook_label` des N
derniers threads et en excluant ces styles du prompt suivant.

**Stratégie anti-répétition — 3 niveaux, du plus dur au plus doux** :
1. **Dur (contrainte SQL)** : `UNIQUE(topic_id, angle)` sur `threads`.
2. **Throttle groupe/thème (code)** : la sélection quotidienne exclut les topics dont le
   `(group_name, theme)` a été utilisé dans une fenêtre récente (proposition : 14 jours) — le
   vrai garde-fou puisque les Topics sont écrits librement par l'IA (donc pas garantis
   textuellement uniques d'un lot d'idéation à l'autre).
3. **Doux (prompt)** : l'idéation reçoit la liste des `(group_name, theme)` déjà utilisés
   récemment en note système ("évite ces combinaisons").

**Stratégie de contenu viral** (opérationnalisée dans le prompt dédié, reprend la technique déjà
validée pour le script TikTok en T14 : hook = promesse claire, corps doit la tenir, étape
d'auto-vérification avant réponse) :
- Tweet 1 : chiffre marquant / affirmation contrariante / question — jamais de méta-commentaire
  "un thread 🧵".
- Tweets intermédiaires : un point par tweet, auto-porteur mais connecté au précédent.
- Dernier tweet : clôture orientée partage (question qui divise, pas un appel au RT — pénalisé
  par l'algorithme X).
- **Angle = le vrai levier de variété** sur un même Topic réutilisable, chacun avec sa consigne
  dédiée dans un dict séparé (comme `_ENGAGEMENT_HOOKS`) : CONTRARIEN, GUIDE_PRATIQUE,
  CAS_ETUDE, STORYTELLING.
- Hashtags : **différent du tweet unique existant** (2/tweet) — aucun hashtag dans le corps,
  au plus 1-2 sur le tout dernier tweet (le spam par tweet nuit à la lisibilité d'un thread).
- Emoji : 1 maximum par tweet, repérage visuel, pas de décoration.

**Fichiers concernés** : `models.py` (enums + schémas + records typés), `analyzer.py`
(`ideate_thread_topics()`, `write_thread()`, nouveaux prompts dédiés — un par forme de sortie,
principe déjà appliqué en T14 —, dicts angle/hook), `storage.py` (schéma + CRUD dont la sélection
diversifiée groupe/thème), `discord_reactions.py` **(nouveau module)** — client REST minimal
(`seed_reactions`, `get_human_reaction`), séparé de `notifier.py` car authentification différente
(Bot token vs webhook) et responsabilité différente (lecture, pas seulement envoi), `notifier.py`
(embed picker + 1 message Discord par tweet, bloc de code), `thread_pipeline.py` **(nouveau
module)** — `run_thread_replenish/select/resolve(settings)`, séparé de `pipeline.py` pour ne
prendre aucun risque de régression sur `run_cycle` déjà en prod, `settings.py` (nouveaux champs
optionnels : `discord_bot_token`, `discord_thread_channel_id`, `discord_webhook_thread`,
`thread_topic_backlog_min` déf. 15, `thread_ideation_batch_size` déf. 12,
`thread_selection_ttl_hours` déf. 24 — absents = comportement `run` existant strictement
inchangé), `__main__.py` (sous-commandes `thread-replenish`/`thread-select`/`thread-resolve`),
`.github/workflows/pipeline.yml` (+1 étape `thread-resolve` sur le cron 15 min existant),
`.github/workflows/thread_select.yml` **(nouveau, cron quotidien — proposition 8h UTC)**,
`.github/workflows/thread_replenish.yml` **(nouveau, cron hebdomadaire — proposition dimanche
20h UTC)**.

**Nouveaux secrets** : `DISCORD_BOT_TOKEN` (Bot Discord Developer Portal, permissions *View
Channel*/*Read Message History*/*Add Reactions* — pas besoin de *Send Messages*, l'envoi reste
webhook), `DISCORD_THREAD_CHANNEL_ID`, `DISCORD_WEBHOOK_THREAD`.

**Ordre d'implémentation** : `models.py` → `storage.py` (avec tests sur la contrainte `UNIQUE` et
le tri de désaturation `last_offered_at`) → `analyzer.py` → `discord_reactions.py` (tests
`respx`) → `notifier.py` → `thread_pipeline.py` (tests d'intégration mockés) → `__main__.py` +
workflows.

**Fait quand** : `pytest` reste au vert (nouveaux tests + 54 existants) ; `thread-replenish
--dry-run` puis `thread-select --dry-run` déroulent sans rien envoyer et journalisent 3 candidats
diversifiés ; test réel une fois le Bot Discord créé et les secrets en place — réaction manuelle
sur Discord détectée par `thread-resolve`, thread reçu avec un message par tweet, bouton "Copier
le texte" fonctionnel sur mobile (même vérification que celle déjà faite pour `tweet_draft` en
T6).

---

## Ordre d'exécution — où on en est

```
✅T1 → ✅T2 → ✅T3 → ✅T4 → ✅T5 → ✅T5ter → ✅T5quater → ✅T5quinquies → ✅T6 → ✅T7 → 🟡T8 → ✅T9 → ✅T10 → ✅T13 → ✅T14 → T11
                                                                                ▲
                                                                      on est ici — objectif initial atteint
       └──────────────── socle + métier : fait ────────────────┘   (T4bis / T5bis toujours reportées)

📝 T15 (threads Twitter quotidiens) — planifié, indépendant du reste (nouveau pipeline parallèle,
   nouveaux crons dédiés), pas de dépendance bloquante sur T11/T12.
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
- **T13** : `DISCORD_WEBHOOK_INFO_A_VERIFIER` et `GEMINI_API_KEY_2` ajoutés comme secrets
  **GitHub Actions** — fait, confirmé par l'utilisateur.
- **T14** : salon Discord + webhook créés, prompt révisé (schéma à 6 champs, zéro emoji) — reste
  à ajouter `DISCORD_WEBHOOK_TIKTOK` comme secret GitHub Actions pour l'activer en production
  (`.env` local déjà à jour).
- **T15** — threads Twitter quotidiens, plan validé, pas encore codé. Nécessite en amont la
  création d'un Bot Discord (Developer Portal) et de son webhook dédié.

---

## Décisions prises

### Points 1 à 4 (sources, cadence, filet France, volume)

| # | Sujet | Décision |
|---|---|---|
| 1 | Sources RSS | **Soompi + Yonhap Culture + Koreaboo + Billboard K-Pop + KpopStarz** actifs. **allkpop désactivé** — flux RSS mort (404 sur 4 URLs candidates), remplacé par Koreaboo (comble le vide gossip/scandale). Billboard K-Pop ajouté pour un angle scandale/industrie sérieux. KpopStarz ajouté après un manque réel constaté (sortie "WET" de J.Y. Park manquée par les 4 sources précédentes) — flux trouvé en inspectant le HTML de la page (les URLs RSS devinées échouaient toutes) |
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

### Nouvelle décision — tag de catégorie et accroche d'engagement sur les tweets

| Sujet | Décision |
|---|---|
| Tag visuel (`[GOSSIP]`/`[RELEASE]`/`[FLASH]`/`[FRANCE]`) | **Dérivé en code**, pas redemandé à l'IA — voir T5quater pour le détail des règles et la justification (coût nul, cohérence garantie avec la catégorie déjà affichée) |
| Accroche de fin de tweet | **Reste générée par l'IA**, avec une consigne par catégorie injectée dans le prompt — contrairement au tag, l'angle dépend trop du contenu précis pour un gabarit figé |

### Nouvelle décision — rattrapage des faux négatifs BRUIT_INUTILE (records/paliers viraux)

| Sujet | Décision |
|---|---|
| Fix de prompt seul suffisant ? | **Non** — jugement probabiliste sur un modèle *lite*, sur une distinction intrinsèquement floue (record vérifiable vs classement putaclic). Améliore le taux général mais ne garantit rien seul — voir T13 |
| Garantie déterministe | Filet mots-clés (record) **+** artiste connu (`artist_tiers.yaml`) — mêmes principes que le filet France, mais correction de dernier recours plutôt que règle dure (pas de catégorie unique garantie) |
| Filet de dernier recours | Salon `#info-a-verifier` — tout ce qui reste `BRUIT_INUTILE` y est envoyé, à coût Gemini nul (réutilise `classify()`, déjà exécuté pour tous les articles) |

### Nouvelle décision — script TikTok (T14)

| Sujet | Décision |
|---|---|
| Un 3e appel séparé, ou fusionné avec `write()` ? | **Séparé**, prompt système dédié — mélanger un tweet court et contraint avec un script plus long dans le même appel dilue la qualité des deux sur un modèle lite |
| `video_summary` conservé ou remplacé ? | **Conservé tel quel** — le script TikTok est un contenu additionnel sur un nouveau salon, pas un remplacement |
| Priorité clé 2 | 2e instance `GeminiAnalyzer` avec la liste de clés inversée (`_tiktok_api_keys`), plutôt que de modifier `_generate_with_fallback()` — même mécanisme de secours existant, sans y toucher |
| Échec du script TikTok | Non bloquant — l'article est diffusé normalement même si son script échoue, c'est un bonus, pas un pré-requis |

### Nouvelle décision — threads Twitter quotidiens (T15)

| Sujet | Décision |
|---|---|
| Source des Topics | **Génération libre par l'IA**, pas ancrée sur un article du pipeline existant — variété éditoriale prioritaire sur la garantie factuelle stricte. Conséquence assumée : aucun filet déterministe possible ici, la relecture humaine reste la seule protection |
| Interaction Discord (choix parmi 3 options) | **Réactions emoji + sondage périodique** via un Bot Discord (token REST simple, pas de connexion Gateway) — de vrais boutons (Components) exigeraient un serveur HTTP public ou un bot permanent, en rupture avec le principe 100 % gratuit/sans serveur du projet |
| Diffusion du thread final | **Webhook simple**, comme le reste du pipeline — un message Discord par tweet (bloc de code), même logique que l'isolement du `tweet_draft` en T6 |
| Anti-répétition | 3 niveaux : contrainte SQL dure `UNIQUE(topic_id, angle)`, throttle groupe/thème en code (fenêtre glissante), note système côté prompt d'idéation |
| Modèle de données | 4 niveaux Groups > Themes > Topics > Angles, mais implémenté en 3 tables seulement (`thread_topics`, `thread_selections`, `threads`) — Groups/Themes sont des attributs, pas des tables séparées, pour éviter une abstraction inutile |

Plus aucun point structurant n'est ouvert sur l'architecture initiale. Les seuls ajustements
restants (liste de sources étendue au-delà des 3 retenues, contenu précis d'`artist_tiers.yaml`,
mapping exact des badges, détails de configuration de T12) sont des paramètres modifiables sans
toucher au code, ou des sous-tâches déjà découpées ci-dessus.
