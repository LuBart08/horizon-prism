# horizon-prism

Un fork de [Prism Launcher](https://prismlauncher.org) pour lancer
**Project Horizon** — Minecraft Java 26.3 réécrit en Rust — à la place de
Minecraft vanilla.

Ce dépôt contient pour l'instant le **spike M0** : un script autonome qui
prouve le chemin d'intégration *sans* forker Prism. Il a été validé sur cette
machine avec les **vrais binaires Horizon**.

```
instance Prism  ──►  horizon-prepare  ──►  horizon-client
(meta, jar,            --prism-dir          (session Prism
 libraries, assets)     ressources            sur stdin)
```

## État

| Jalon | Contenu | État |
|---|---|---|
| M0 | spike hors fork, flux complet, vrais binaires | ✅ validé |
| M1 | squelette du fork : type d'instance `Horizon` | ✅ compilé et testé |
| M2 | page de réglages Horizon (canal, version, options) | à faire |
| M3 | étape de lancement `HorizonLaunchStep`, session sur stdin | ✅ compilé et testé |
| M4 | préparation/repair dans l'UI, progression | à faire |
| M5 | contenu Horizon (`.hmod`) via `horizon-prepare` | à faire |
| M6 | packaging, updater, clés de signature | à faire |

## M0 : ce qui est prouvé, pour de vrai

Validé avec `nightly-2026-11-08-fdc87ae` puis `nightly-2026-11-13-9f42064` :

1. **Préparation depuis Prism, sans re-téléchargement.**
   `horizon-prepare --prism-dir <PrismLauncher> --prepare --output DIR` lit
   directement le JSON de version réécrit par Prism, ses bibliothèques Maven,
   ses assets et ses composants. **Aucun jar serveur n'est nécessaire** : le
   client jar Prism sert de côté serveur.
   Mesure réelle : **134–168 s, 221 Mo**, `--check` OK derrière.
2. **Lancement réel avec la session Prism.** `horizon-client` reçoit
   `--resources`, `--game-dir`, `--home`, `--player-dir` et la session sur
   **stdin** (`--session-stdin`). Smoke test réel : `--frames 2` →
   exit 0, `smoke-report` JSON (`frames: 2`, `error: null`).
3. **Contrat lu, pas deviné.** Le spike interroge `--launch-api` des deux
   binaires et n'utilise que ce qui est annoncé (`crash_report_line`,
   `exit_codes`, `--prism-dir`, `--check`, `java_major`).
4. **Erreurs classées.** Instance non-26.3 → refus avant tout travail ;
   crash (1/101) → code 3 + chemin du rapport ; timeout → code 3.

### Découverte qui valide l'architecture

Le launcher Horizon officiel est **déjà configuré pour lire Prism** :

```json
// ~/Library/Application Support/horizon-launcher/settings.json
{ "minecraft_dir": ".../Library/Application Support/PrismLauncher" }
```

et il symlinke `assets/` et `libraries/` de Prism dans son
`linked-minecraft/`. Un fork de Prism qui prépare Horizon depuis ses propres
fichiers ne fait donc que suivre le chemin prévu par Horizon.

## Prérequis

- Prism Launcher avec une instance **Minecraft 26.3** lancée au moins une fois
  (jar, libs et assets téléchargés par Prism) ;
- un JDK **25** (Prism le fournit dans `java/`, sinon `JAVA_HOME` ou
  `/usr/libexec/java_home -v 25`) ;
- Horizon installé — sur macOS via `Horizon.app`, qui installe ses binaires
  versionnés dans
  `~/Library/Application Support/horizon-launcher/versions/<version>/`
  (`horizon-client`, `horizon-prepare`, `horizon-server`, `horizon-ctl`) ;
- un compte Microsoft propriétaire de Minecraft pour jouer : Horizon n'a pas
  de mode hors-ligne réel.

Le spike **détecte tout seul** le client et le préparateur installés (via
`settings.json → selected`, sinon la version la plus récente). Sinon :
`--client`, `--prepare`, `$HORIZON_CLIENT`, `$HORIZON_PREPARE`, ou le `PATH`.

## Utilisation

```sh
cd spike

# lister les instances Prism
python3 horizon_prism_spike.py --list-instances

# inspecter : version, fichiers meta, contrat du client installé
python3 horizon_prism_spike.py --instance Serveurs --stage inspect

# ne rien exécuter, juste voir les commandes
python3 horizon_prism_spike.py --instance Serveurs --dry-run

# préparation + lancement réels
python3 horizon_prism_spike.py --instance Serveurs \
    --session-json ~/session.json

# smoke test sans compte (2 frames, rapport JSON)
python3 horizon_prism_spike.py --instance Serveurs \
    --offline-session --frames 2 --smoke-report /tmp/smoke.json
```

`--no-complete` saute les extras optionnels (sons, packs, panorama, polices,
langues) : la préparation passe de ~600 Mo à ~221 Mo et le jeu démarre
silencieux. Par défaut le spike demande `--complete`.

### Session

JSON Horizon, exporté depuis Prism (compte Microsoft) :

```json
{
  "name": "Pseudo",
  "uuid": "069a79f444e94726a5befca90e38aaf5",
  "access_token": "…",
  "user_type": "msa"
}
```

`uuid` est normalisé sans tirets et en minuscules. Le token ne part **jamais**
dans `argv` (donc ni dans `ps` ni dans les logs) : uniquement sur stdin. Le
rapport ne contient que le nom, l'uuid et un booléen « a un token ».

`--offline-session` n'existe que pour les tests : le vrai client accepte une
session factice pour un smoke test, mais le multijoueur en ligne exige un vrai
compte.

### Stubs

`spike/stubs/` contient un faux `horizon-prepare` et un faux `horizon-client`
qui implémentent la forme du contrat (options, `--launch-api`, `--check`,
progression JSON, code 101 + ligne de crash, mode `--prism-dir` et mode
« ancien » avec chemins explicites). Ils servent aux tests reproductibles.

### Tests

```sh
cd spike
python3 -m unittest discover -s tests -v
```

15 tests : flux complet, mode `--prism-dir` et repli « ancien préparateur »,
gaps optionnels (sans et avec `--complete`), second run sans re-préparation,
mapping du crash, refus 26.2, session manquante, `--dry-run`, `--json` propre,
`--player-dir`, découverte des binaires installés, contrat réel des binaires,
`--check` réel sur des ressources préparées.

### Codes de sortie du spike

| Code | Sens |
|---|---|
| 0 | succès |
| 1 | une étape a échoué (préparation, E/S) |
| 2 | invocation invalide ou instance non supportée |
| 3 | crash (exit Horizon 1/101) ou lancement tué après timeout |

### Options utiles

`--stage inspect|prepare|launch|all`, `--resources`, `--game-dir`, `--home`,
`--player-dir`, `--java-home`, `--skip-prepare`, `--no-complete`, `--width`,
`--height`, `--fullscreen`, `--quick-play-world`, `--join`,
`--frames`, `--smoke-report`, `--prepare-timeout`, `--launch-timeout`,
`--json`, `--json-out`, `--verbose`.

## Ce que le spike ne fait pas

- aucun mod Java (pas de Fabric, pas de mixins) ;
- aucun build de mod natif ;
- aucune modification de la source de Prism ;
- pas encore de modèle d'instance Prism dédié.

## Contrat Horizon observé (nightly 2026-11-13)

| Élément | Valeur |
|---|---|
| `launch_api` | `0.1.0` |
| `minecraft` | `26.3` |
| `java_major` (préparateur) | `25` |
| `schema` / `output_version` | `7` / `4` (3 en 2026-11-08) |
| `native_mod.hooks` | `415` (411 en 2026-11-08) |
| `mod.api` | `0.2`, `0.3` |
| `resourcepack.format` | `97`, `1` |
| `datapack.format` | `121`, `0` |
| `crash_report_line` | `Horizon: crash report saved to ` |
| `exit_codes` | `0`, `1`, `2`, `101` |
| client | `--resources`, `--game-dir`, `--home`, `--player-dir`, `--width`, `--height`, `--fullscreen`, `--session-file`, `--session-stdin`, `--quickPlay*` |
| préparateur | `--prepare`, `--server-jar`, `--minecraft-dir`, `--prism-dir`, `--version-json`, `--client-jar`, `--libraries-dir`, `--assets-dir`, `--java-home`, `--root`, `--output`, `--check`, `--complete`, `--progress json` |

### Deux pièges réels découverts

1. **`output_version` change entre nightlies** (3 → 4). Les ressources
   préparées par une version ne sont pas « current » pour la suivante :
   `--check` le dit, mais il faut **épingler client et préparateur ensemble**
   dans le fork, sinon re-préparation à chaque mise à jour.
2. **`--frames` / `--smoke-report` ne sont pas annoncés par `--launch-api`**.
   Un launcher qui ne passe que les options annoncées les filtrerait ; le fork
   doit les réserver aux tests explicites.

## Suite

Le plan du fork (M1–M6), l'architecture retenue et la carte des fichiers Prism
à toucher sont dans [`docs/fork-plan.md`](docs/fork-plan.md).

## Fork Prism (M1, M3)

Le fork vit dans [`fork/`](fork/) :

- [`fork/M1.md`](fork/M1.md) : type d'instance, création d'une instance de
  test, outillage sans administrateur, problème AGL du SDK macOS 26 ;
- [`fork/M3.md`](fork/M3.md) : lancement réel de `horizon-client`, session
  sur stdin, résolution du binaire, test avec les vraies ressources ;
- [`fork/patches/`](fork/patches/) : les commits à appliquer sur
  `PrismLauncher/develop` ;
- [`fork/build-m1.sh`](fork/build-m1.sh) : configure + compile (Qt via `aqt`,
  dépendances via vcpkg) ;
- [`fork/make-test-instance.sh`](fork/make-test-instance.sh) : instance
  `HorizonTest` jetable ;
- un stub `AGL.framework` vide, généré par `build-m1.sh` dans `fork/stubs/`
  (non versionné), pour contourner la disparition d'AGL dans le SDK macOS 26.

Le clone complet de Prism (`fork/PrismLauncher/`) n'est **pas** versionné ici :
il a son propre dépôt et sera publié comme fork GitHub séparé.

