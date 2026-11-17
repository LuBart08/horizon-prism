# Plan du fork Prism Launcher pour Project Horizon

Ce document décrit comment transformer le spike M0 en fork maintenable.
Le spike M0 est **validé sur cette machine avec les vrais binaires Horizon** :
le flux *Prism → horizon-prepare → horizon-client* fonctionne sans fork.

## 1. Architecture retenue

**Un type d'instance dédié + un sidecar Rust. Pas de réimplémentation C++ du
moteur Horizon.**

```
Prism (C++/Qt)
├── HorizonInstance : MinecraftInstance
│     ├── horizon.json          (modèle d'instance : version, canal, pins, options)
│     ├── horizon/              (état géré : resources/, home/, root/)
│     └── minecraft/            (game dir : saves, screenshots, options du jeu)
└── sidecar Rust « horizon-tools »
      ├── horizon-tools prepare   (enveloppe horizon-prepare, --progress json)
      ├── horizon-tools check     (diagnostic pour « Repair »)
      └── horizon-tools launch    (enveloppe horizon-client, session sur stdin)
```

Pourquoi un sidecar :

- le contrat Horizon est en Rust ; le réimplémenter en C++ crée une dette de
  compatibilité à chaque version du `launch_api` ;
- les codes de sortie, la progression JSON et les rapports de crash restent
  dans **un** endroit ;
- Prism n'a qu'à parler un petit protocole stable (argv + JSON sur stdout).

Pourquoi `MinecraftInstance` comme base :

- on hérite du game dir, des sauvegardes, des captures, de la gestion Java
  (utile au préparateur), du compte Microsoft et de l'UI de lancement ;
- on évite de dupliquer la gestion des ressources/icônes/groupes.

**Découverte clé du M0** : `horizon-prepare --prism-dir <root|instance>` lit
déjà le JSON de version réécrit par Prism, ses bibliothèques Maven, ses assets
et ses composants, et se sert du client jar comme côté serveur. Le fork n'a
donc **rien** à extraire lui-même de Prism : il passe un chemin. Le launcher
officiel fait la même chose (`settings.json → minecraft_dir` pointe sur Prism,
avec `assets`/`libraries` symlinkés).

### Modèle d'instance

`horizon.json`, **pas** `mmc-pack.json` pour le lancement :

```json
{
  "formatVersion": 1,
  "channel": "stable",
  "minecraft": "26.3",
  "clientVersion": "nightly-2026-11-13-9f42064",
  "preparerVersion": "nightly-2026-11-13-9f42064",
  "pins": { "launch_api": "0.1.0", "output_version": 4, "native_hooks_api": 415 },
  "options": {
    "renderDistance": 12,
    "vsync": true,
    "fullscreen": false
  },
  "lastPrepared": "2026-11-13T09:20:00Z"
}
```

`clientVersion` et `preparerVersion` doivent rester **identiques** :
`output_version` a changé entre deux nightlies (3 → 4), et des ressources
préparées par l'un ne sont pas « current » pour l'autre.

`mmc-pack.json` peut rester présent (Prism l'exige pour certaines vues), mais
la **source de vérité** du lancement est `horizon.json`.

### Type d'instance

`instance.cfg` : `InstanceType=Horizon`.

`InstanceList::loadInstance` refuse tout type inconnu et journalise
« Unknown instance type » ; le fork doit enregistrer le type avant le
chargement.

## 2. Jalons

### M1 — squelette du fork ✅ (compilé et testé)

Objectif : une instance `Horizon` apparaît, s'ouvre, et « Play » échoue
proprement avec un message explicite.

Implémenté sur la branche `horizon/m1` :

- `launcher/InstanceList.cpp` : accepte `InstanceType=Horizon`, instancie
  `HorizonInstance` (et non `MinecraftInstance`).
- `launcher/minecraft/HorizonInstance.{h,cpp}` : sous-classe de
  `MinecraftInstance`, charge/écrit `horizon.json`, restaure
  `InstanceType=Horizon` après `MinecraftInstance::loadSpecificSettings`
  (qui le force à `OneSix`), expose `clientVersion()`, `channel()`,
  `resourcesDir()`, `horizonHomeDir()`, les pins (`launch_api`,
  `output_version`, `native_hooks_api`) et `isPrepared()`.
- `launcher/minecraft/launch/HorizonNotPrepared.{h,cpp}` : étape de
  lancement qui échoue avec « pas encore préparé » (M4) ou « lancement pas
  encore câblé » (M3), au lieu de lancer les étapes Java.
- `launcher/CMakeLists.txt` : nouveaux fichiers dans `MINECRAFT_SOURCES`.

Reste hors M1 : la création depuis la boîte de dialogue « New Instance »
(`InstanceFactory`) ; en attendant, une instance se crée à la main (voir
`fork/M1.md`).

Critère : une instance Horizon se charge, s'affiche avec « Horizon … not
prepared », et « Play » échoue avec un message clair sans rien télécharger.

### M2 — page de réglages

Objectif : choisir canal/version et quelques options sans éditer de JSON.

- `launcher/ui/pages/instance/HorizonSettingsPage.{h,cpp}` : canal
  (stable/beta/pinned), version Horizon, `launch_api` attendu (lecture seule),
  options de jeu (render distance, vsync, fullscreen).
- `launcher/ui/pages/instance/InstanceSettingsPage.cpp` : ajouter l'onglet.
- `launcher/instance/HorizonInstance.cpp` : validation (version connue,
  `launch_api` compatible avec le binaire installé).

Critère : les réglages persistent dans `horizon.json` et survivent à un
redémarrage.

### M3 — lancement ✅ (compilé et testé)

Objectif : « Play » lance Horizon avec la session Prism.

Implémenté sur la branche `horizon/m3` :

- `launcher/minecraft/launch/HorizonLaunchStep.{h,cpp}` :
  1. vérifie que les ressources sont préparées (M4 fera `--check`) ;
  2. construit la session depuis `AuthSession` : `name`, `uuid` sans tirets,
     `access_token`, `user_type=msa` ;
  3. lance directement `horizon-client` avec `--resources`, `--game-dir`,
     `--home`, largeur/hauteur et quick-play ;
  4. écrit la session sur **stdin** (`--session-stdin`), jamais dans `argv` ;
  5. lit `--launch-api` pour vérifier le pin et récupérer
     `crash_report_line` (pas de constante recopiée) ;
  6. traduit les codes de sortie (0/1/2/101) et suit le processus pour
     « Kill ».
- `launcher/minecraft/HorizonInstance.cpp` : `createLaunchTask()` ajoute
  `HorizonLaunchStep` après `HorizonNotPrepared`, qui ne échoue plus que si
  les ressources manquent ; résolution de `horizon-client` (`clientPath`,
  version épinglée, dernière version installée, `PATH`).

Écarts assumés par rapport au plan initial :

- appel direct de `horizon-client` au lieu de l'enveloppe `horizon-tools`
  (le sidecar arrive avec M4) ;
- `--player-dir` et `--fullscreen` pas encore utilisés ;
- l'authentification reste celle de Prism : un compte Microsoft valide est
  requis avant la tâche de lancement.

Critère : partie lancée avec un vrai compte ; « Kill » arrête le processus ;
un crash ouvre le rapport dans l'UI.

Validé avec le vrai client (nightly 2026-11-13) : ressources préparées en
98 s, lancement `--frames 2`, exit 0, première frame rendue, `smoke-report`
sans erreur. Détails : [`fork/M3.md`](../fork/M3.md).

### M4 — préparation et réparation ✅ (implémenté)

Objectif : premier lancement sans ligne de commande.

Implémenté sur la branche `horizon/m4` :

- `launcher/minecraft/launch/HorizonPrepareStep.{h,cpp}` : `--check`, puis
  `horizon-prepare --prepare --output <instance>/horizon/resources
  --prism-dir <Prism root ou instance> --java-home <Java 25> --progress json`
  si nécessaire, puis re-`--check` ;
- les gaps optionnels (sons, packs, panorama, polices, langues) ne bloquent
  **pas** le lancement : ils sont signalés comme extras manquants ;
- Java 25 : Prism `JavaPath` s'il correspond, sinon un runtime géré par Prism
  (`<data>/java/*`), sinon `/usr/libexec/java_home -v 25` sur macOS ; aucun
  téléchargement de runtime ;
- pas de jar serveur : `--prism-dir` fait lire à `horizon-prepare` le client
  jar et les assets de Prism.

Écarts assumés par rapport au plan initial :

- pas de bouton « Prepare / Repair » dans la page de réglages : la
  préparation est déclenchée par « Play » ;
- pas de `--complete` automatique (les extras restent optionnels).

Critère : installation neuve → « Play » → préparation → partie, sans terminal.

Détails et résultats : [`fork/M4.md`](../fork/M4.md).

### M5 — contenu Horizon

Objectif : gérer les `.hmod` (contenu) et `.hnmod` (natif) sans casser le
modèle Prism.

- `launcher/minecraft/mod/` : un `HorizonMod` léger (id, version, type
  `content|native`, source) ; **ne pas** réutiliser `Mod` Java.
- `launcher/modplatform/ModIndex.h` : un index Horizon (ou un index
  « local » tant qu'il n'y a pas de registre public).
- `launcher/ui/pages/modplatform/` : page « Horizon Content » (liste,
  activer/désactiver, mettre à jour).
- Les `.hnmod` natifs doivent être **construits** pour la plateforme : le
  fork doit refuser proprement un `.hnmod` incompatible plutôt que de laisser
  Horizon planter.

Critère : ajouter/retirer un `.hmod`, relancer, voir le contenu actif.

### M6 — packaging

Objectif : un produit distribuable.

- Renommer (binaire, bundle id, dossier de données) pour éviter tout conflit
  avec Prism.
- Repointer l'updater (`launcher/updater/`) vers les releases du fork.
- Nouvelles clés de signature macOS/Windows.
- Licence **GPL-3.0** conservée, sources publiées, avis de fork visible.
- CI : build macOS/Windows/Linux + tests du sidecar.

## 3. Carte des fichiers Prism à toucher

| Fichier | Rôle dans le fork |
|---|---|
| `launcher/InstanceList.cpp` | accepter `InstanceType=Horizon` |
| `launcher/minecraft/HorizonInstance.{h,cpp}` | modèle d'instance Horizon |
| `launcher/minecraft/MinecraftInstance.cpp` | points d'extension game dir / ressources |
| `launcher/BaseInstance.cpp` | type, icône, nom |
| `launcher/launch/LaunchTask.cpp` | remplacer les étapes Java par Horizon |
| `launcher/minecraft/launch/*` | `HorizonPrepareStep`, `HorizonLaunchStep` |
| `launcher/java/JavaUtils.cpp` | localiser JDK 25 pour le préparateur |
| `launcher/minecraft/auth/AuthSession.h` | mapping session → JSON Horizon |
| `launcher/modplatform/ModIndex.h` | index de contenu Horizon |
| `launcher/ui/pages/instance/InstanceSettingsPage.cpp` | onglet Horizon |
| `launcher/ui/MainWindow.cpp` | actions « Prepare », « Repair » |
| `launcher/updater/*` | updater du fork |
| `launcher/CMakeLists.txt` | nouveaux fichiers |

## 4. Contrat Horizon à respecter

Observé sur `nightly-2026-11-13-9f42064` (et `nightly-2026-11-08-fdc87ae`) :

- `horizon-client --launch-api` → JSON : `launch_api` (`0.1.0`),
  `minecraft` (`26.3`), `horizon`, `crash_report_line`, `options`, `content`
  (`mod.api` `0.2`/`0.3`, `native_mod.hooks` `415`, `resourcepack.format`,
  `datapack.format`), `exit_codes`.
- `horizon-prepare --launch-api` → JSON : `launch_api`, `minecraft`,
  `horizon`, `java_major` (`25`), `schema` (`7`), `output_version` (`4`),
  `options`.
- Options client : `--resources`, `--game-dir`, `--home`, `--player-dir`,
  `--width`, `--height`, `--fullscreen`, `--session-file`,
  `--session-stdin`, `--quickPlaySingleplayer`, `--quickPlayMultiplayer`.
- Options préparateur : `--prepare`, `--server-jar`, `--minecraft-dir`,
  `--prism-dir`, `--version-json`, `--client-jar`, `--libraries-dir`,
  `--assets-dir`, `--java-home`, `--root`, `--output`, `--check`,
  `--complete`, `--progress json`.
- `--prism-dir` accepte une racine Prism **ou** une instance ; il lit le JSON
  de version réécrit, les libs Maven, les assets et les composants, et se sert
  du client jar comme côté serveur : **pas de jar serveur à télécharger**.
- `--root` = dossier Horizon contenant `research-lock.json` (scratch des
  exporters dans `<root>/.local`) ; `--output` = ressources
  (défaut `<root>/.local/horizon-client/<version>`).
- `--check` : exit 0 si complet et à jour ; exit 1 avec un gap par ligne
  (`… missing: horizon-prepare <sous-commande>`). Les gaps `packs`,
  `panorama`, `fonts`, `languages`, `sounds` sont **optionnels**.
- Codes de sortie : `0` quitté, `1` erreur, `2` ligne de commande refusée,
  `101` panique.
- Crash : le marqueur est annoncé dans `crash_report_line`
  (`Horizon: crash report saved to `) ; ne pas le recopier en dur.
- Session : `{"name","uuid","access_token","user_type":"msa","xuid"}`,
  uuid sans tirets, **stdin uniquement** (`--session-file` existe mais écrit
  un secret sur disque : à éviter).
- `--frames N` et `--smoke-report PATH` sont des options de test **non
  annoncées** par `--launch-api` : les réserver aux tests explicites.
- `NATIVE_HOOKS_API=415` : un `.hnmod` construit pour une autre valeur doit
  être refusé avant lancement.

## 5. Risques

| Risque | Impact | Mitigation |
|---|---|---|
| `launch_api` casse entre versions Horizon | lancement cassé | pin dans `horizon.json`, `--launch-api` vérifié avant lancement |
| `output_version` change entre nightlies (3 → 4 constaté) | ressources « périmées », re-préparation | épingler client **et** préparateur ensemble, vérifier `--check` au lancement |
| `native_mod.hooks` change (411 → 415 constaté) | `.hnmod` incompatible | refuser avant lancement avec la valeur attendue |
| Prism change son modèle d'instance | fork difficile à rebaser | isoler le code Horizon dans peu de fichiers, pas de patch dispersé |
| Pas de registre public de contenu | M5 incertain | commencer par du contenu local, index branchable ensuite |
| `.hnmod` par plateforme | confusion joueur | refus explicite + message clair |
| GPL et marque Prism | juridique | fork renommé, sources publiées, avis visible |
| Compte Microsoft obligatoire | pas de test CI du lancement réel | tests avec stubs en CI, smoke test `--frames` documenté |

## 6. Ce que le spike a validé sur cette machine (à réutiliser tel quel)

- `horizon-prepare --prism-dir <PrismLauncher> --prepare --output DIR` :
  **134–168 s, 221 Mo**, exit 0, puis `--check` OK (sans `--complete`) ;
- `--check` réel : 0 sur des ressources complètes, 1 avec les cinq gaps
  optionnels correctement classés ;
- lancement réel `horizon-client` avec session sur stdin : exit 0,
  `--frames 2`, `smoke-report` (`frames: 2`, `error: null`) ;
- la découverte automatique des binaires installés
  (`horizon-launcher/settings.json → selected`) ;
- la construction des arguments avec filtrage par `--launch-api` ;
- la normalisation de l'uuid et le passage de session par stdin ;
- le mapping des codes de sortie, le timeout, et l'extraction du chemin de
  crash via `crash_report_line` ;
- la progression JSON du préparateur et le classement des gaps optionnels ;
- le repli sur les chemins explicites (`--version-json`, `--client-jar`,
  `--libraries-dir`, `--assets-dir`, `--server-jar`) pour les préparateurs
  sans `--prism-dir`, avec téléchargement SHA-1 vérifié du jar serveur.
