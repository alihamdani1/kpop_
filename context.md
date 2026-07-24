# Contexte du projet — Pipeline de curation d'actualités K-pop

## 1. En une phrase

Un robot qui surveille en continu les sites d'actualité K-pop anglophones, ne retient que
l'information réellement exploitable, la traduit et la résume en français, évalue son potentiel
de partage, puis dépose le tout — résumé, score, **brouillon de tweet prêt à l'emploi** — dans
le bon salon Discord de la rédaction, automatiquement, 24h/24.

## 2. Le problème à résoudre

La rédaction d'un média francophone spécialisé K-pop travaille aujourd'hui « à la main » :

- **Volume** : plusieurs sources anglophones publient plusieurs dizaines d'articles par jour.
  Les parcourir toutes, plusieurs fois par jour, occupe un temps de veille considérable.
- **Barrière de la langue** : les sources de référence (Soompi, allkpop, Yonhap…) publient en
  anglais, parfois en coréen. Chaque journaliste doit lire, comprendre, puis reformuler avant
  même de décider si le sujet mérite un article — ou un tweet.
- **Ratio signal/bruit** : la majorité des publications sont des brèves promotionnelles, des
  reprises de posts Instagram ou du contenu de remplissage. L'information à forte valeur
  (scandale, comeback confirmé, date de concert en France) est noyée dedans.
- **Réactivité** : sur un scandale ou une annonce de tournée, l'écart entre la publication
  anglophone et la reprise francophone se compte en heures. Celui qui publie en premier
  capte l'audience — sur le site comme sur les réseaux sociaux.

## 3. Objectifs métiers

| Objectif | Traduction concrète | Comment on le mesure |
|---|---|---|
| **Gagner du temps** | Supprimer la veille manuelle multi-onglets. Le journaliste ouvre Discord et voit une liste prête à exploiter. | Temps de veille quotidien avant / après |
| **Éliminer la barrière de la langue** | Chaque information arrive déjà en français, résumée de façon exploitable telle quelle. | Aucune lecture d'anglais requise pour trier |
| **Filtrer le bruit** | Le contenu sans valeur éditoriale n'atteint jamais Discord. | % d'articles écartés en catégorie « Bruit inutile » |
| **Prioriser** | Un niveau d'importance (mineur / modéré / majeur) permet de décider en un coup d'œil quoi traiter en premier. | Délai de réaction sur les sujets « majeur » |
| **Ne rien rater** | Aucun doublon, aucun oubli, même la nuit et le week-end. | Articles traités / articles publiés par les sources |
| **Accélérer la publication sur X/Twitter** | Un brouillon de tweet est rédigé pour chaque article retenu ; un humain le relit et le publie. Le robot ne poste jamais lui-même. | Délai entre publication source et tweet de la rédaction |
| **Prioriser l'effort social** | Séparer, via le score de viralité, ce qui mérite un traitement éditorial approfondi (vidéo) de ce qui relève du tweet rapide en volume. | Répartition des articles entre Route A et Route B |

**Ce que le système ne fait PAS** (périmètre volontairement exclu) : il ne rédige pas
d'article publiable, ne publie **jamais** rien automatiquement sur Discord ou sur X/Twitter, et
ne remplace aucune décision éditoriale. C'est un **outil d'aide à la décision** — un
« human-in-the-loop » assumé : le robot propose un brouillon, la rédaction relit, ajuste si
besoin, et publie elle-même. Il n'y a **aucune intégration directe à l'API X** dans ce projet.

**Sur le score de viralité, une précision importante** : le modèle ne dispose que du texte de
l'article, complété d'une table statique de « poids » par groupe/artiste (Tier 1, Tier 2…)
injectée dans le prompt système pour l'aider à pondérer. Il n'a en revanche accès ni au nombre
d'abonnés réel, ni aux tendances effectives sur les réseaux, ni à l'historique d'engagement de
la rédaction. Le score est donc une **estimation heuristique**, utile pour trier et prioriser
rapidement, pas une prédiction fiable au sens statistique tant qu'aucune boucle de calibration
sur des résultats réels n'a été mise en place.

Le score n'est **calculé que pour les articles retenus** (catégorie ≠ « Bruit inutile ») : un
article filtré n'a ni score de viralité ni brouillon de tweet, par cohérence — il n'est de toute
façon jamais diffusé. Il s'exprime sur une échelle à 4 niveaux (Faible / Modéré / Élevé / Viral).

## 4. Routage à deux salons — la logique de diffusion

Le score de viralité et la catégorie déterminent, pour chaque article retenu, l'un de deux
parcours. Il n'y a **pas** de salon par catégorie (scandale/comeback/concert) : le routage se
fait uniquement sur la valeur éditoriale et sociale de l'article.

| | **Route A — `#actus-videos`** (haute valeur) | **Route B — `#drafts-twitter`** (volume quotidien) |
|---|---|---|
| **Déclenchée par** | Score `VIRAL` ou `ÉLEVÉ`, **ou** catégorie `Concert/Événement France` (quel que soit le score) | Score `MODÉRÉ` ou `FAIBLE`, catégorie autre que Concert France |
| **Contenu de l'embed** | Titre, badge de score, **résumé détaillé** (pensé pour scripter une vidéo), brouillon de tweet | Titre, badge de score, brouillon de tweet — **prêt à copier-coller**, rien d'autre |
| **Usage rédaction** | Sujets à traiter en profondeur (vidéo, article) | Publication rapide en volume sur X |

Un article classé « Bruit inutile » n'emprunte aucune des deux routes : il est archivé sans
aucun jugement de viralité ni brouillon de tweet.

**Filet de sécurité « France » (règle dure, non négociable par l'IA)** : si le titre ou l'extrait
d'un article contient l'un des mots `Paris`, `France`, `Accor Arena`, `Stade de France`,
`Zénith` (recherche insensible à la casse), la catégorie est **forcée en code** à
`Concert/Événement France` — et donc l'article part sur la Route A — **indépendamment de ce que
le modèle IA aurait décidé**. Cette règle tourne avant l'appel IA (pour orienter son jugement
d'importance/viralité) et est réappliquée après coup comme filet déterministe : aucune erreur ou
hallucination du modèle ne peut faire passer un événement français à travers les mailles.

## 5. Flux de données global

```
   ┌─────────────────────────────────────────────────────────────────────┐
   │  SOURCES ANGLOPHONES  (flux RSS/Atom)                               │
   │  Soompi · allkpop · Yonhap Entertainment · … (liste configurable)  │
   └────────────────────────────────┬────────────────────────────────────┘
                                    │  ① COLLECTE
                                    ▼
   ┌─────────────────────────────────────────────────────────────────────┐
   │  ② DÉDUPLICATION                                                    │
   │  Empreinte stable (hash de l'URL canonique). Déjà en base → ignoré. │
   └────────────────────────────────┬────────────────────────────────────┘
                                    │  seulement les nouveautés
                                    ▼
   ┌─────────────────────────────────────────────────────────────────────┐
   │  ③ FILET MOTS-CLÉS FRANCE  (déterministe, avant l'IA)               │
   │  Match → note pour orienter le jugement IA, et réappliqué en force  │
   │  après la réponse, quoi qu'elle contienne.                          │
   └────────────────────────────────┬────────────────────────────────────┘
                                    ▼
   ┌─────────────────────────────────────────────────────────────────────┐
   │  ④ ANALYSE IA — appel 1 : CLASSIFICATION                            │
   │    • catégorie   : SCANDALE_DRAMA | COMEBACK_SORTIE                 │
   │                    | CONCERT_EVENEMENT_FRANCE | BRUIT_INUTILE       │
   │    • importance  : MINEUR | MODERE | MAJEUR                         │
   │    • viralité    : FAIBLE | MODERE | ELEVE | VIRAL (null si bruit)  │
   │    • artistes    : entités citées                                   │
   └────────────────────────────────┬────────────────────────────────────┘
                                    │
                    ┌───────────────┴───────────────┐
                    ▼                               ▼
          catégorie = BRUIT_INUTILE          catégorie exploitable
                    │                               │
                    ▼                               ▼
        ┌───────────────────────┐   ┌───────────────────────────────────┐
        │ ARCHIVÉ EN BASE       │   │ ⑤ DÉTERMINATION DE ROUTE (code)   │
        │ (jamais envoyé)       │   │ A si VIRAL/ÉLEVÉ/Concert France   │
        └───────────────────────┘   │ B sinon (MODÉRÉ/FAIBLE)           │
                                    └────────────────┬──────────────────┘
                                                     ▼
                            ┌────────────────────────────────────────────┐
                            │  ⑥ ANALYSE IA — appel 2 : RÉDACTION        │
                            │  • résumé_fr (2 phrases) — toujours        │
                            │  • brouillon_tweet (<280 car.) — toujours  │
                            │  • résumé_détaillé — seulement si Route A  │
                            └────────────────┬───────────────────────────┘
                                             ▼
                            ┌────────────────────────────────────────────┐
                            │  ⑦ DIFFUSION DISCORD                       │
                            │  Route A → #actus-videos                   │
                            │  Route B → #drafts-twitter                 │
                            └────────────────────────────────────────────┘
```

**Point clé de conception** : la déduplication (②) se produit **avant** tout appel IA. Un
article n'est donc analysé qu'une seule fois dans sa vie. C'est ce qui rend le coût
d'exploitation proportionnel au flux réel de nouveautés, et non à la fréquence d'exécution du
robot. Le découpage en deux appels IA (④ classification, ⑥ rédaction) évite en plus de rédiger
quoi que ce soit pour les articles finalement écartés comme « bruit ».

## 6. Cycle de vie d'un article

| Statut en base | Signification |
|---|---|
| `NEW` | Collecté, pas encore analysé |
| `ANALYZED` | Analyse IA réussie, en attente de diffusion |
| `SENT` | Diffusé sur Discord avec succès |
| `FILTERED` | Classé « bruit inutile », volontairement non diffusé |
| `FAILED` | Erreur (IA ou webhook) après épuisement des tentatives — repris au cycle suivant |

## 7. Contraintes et principes directeurs

- **Idempotence** : relancer le robot deux fois de suite ne produit jamais de doublon
  sur Discord. Le statut en base fait autorité.
- **Tolérance aux pannes** : une source RSS injoignable, une erreur d'API ou un webhook
  qui échoue ne doivent jamais interrompre le traitement des autres articles.
- **Sortie IA jamais « faite confiance à l'aveugle »** : la réponse du modèle est
  contrainte par un schéma JSON strict et validée avant tout usage. Une réponse
  non conforme = article marqué `FAILED`, jamais un message Discord malformé.
- **Aucune publication automatique** : le robot écrit des brouillons, jamais des messages
  publiés directement sur X/Twitter. La rédaction reste seule décisionnaire de ce qui part en
  ligne et sous quelle forme.
- **Coût maîtrisé et observable** : consommation de tokens journalisée à chaque appel.
- **Configuration ≠ code** : sources, webhooks, mots-clés et seuils vivent dans des
  fichiers de configuration. Ajouter une source ou ajuster la liste de mots-clés France ne
  demande aucune modification du code Python.
- **Aucun secret dans le dépôt** : clés d'API et URLs de webhooks passent exclusivement
  par variables d'environnement (`.env`, non versionné).

## 8. Glossaire

| Terme | Définition retenue dans ce projet |
|---|---|
| **Comeback** | Retour d'un artiste/groupe avec une nouvelle sortie musicale (single, EP, album) |
| **Drama / Scandale** | Controverse, litige contractuel, polémique, affaire judiciaire, départ de groupe |
| **Bruit inutile** | Reprise de post réseaux sociaux, publicité déguisée, classement racoleur, contenu sans fait nouveau vérifiable |
| **Item** | Une entrée d'un flux RSS = un article candidat |
| **Empreinte** | Hash SHA-256 de l'URL canonique, sert de clé de déduplication |
| **Score de viralité** | Estimation heuristique (produite par le premier appel IA) du potentiel de partage social d'un article. Basée sur le texte + une table statique de poids par artiste — pas sur des données d'engagement réelles |
| **Route A / Route B** | Les deux parcours de diffusion Discord, déterminés en code à partir de la catégorie et du score de viralité — voir §4 |
| **Brouillon de tweet** | Texte de moins de 280 caractères, ton journalistique neutre, généré par le second appel IA pour tout article retenu (hors bruit). Jamais publié automatiquement — destiné à être relu puis copié-collé par la rédaction |
| **Filet mots-clés France** | Règle déterministe, non contournable par l'IA, qui force la catégorie Concert/Événement France (et donc la Route A) dès qu'un mot-clé géographique français apparaît dans le titre ou l'extrait — voir §4 |
