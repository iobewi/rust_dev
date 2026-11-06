# Generic bench development and qualification

Discuss work in French. Keep the browser as the exclusive USB owner, the Python
server as REST/operation broker. Version generic bench qualification recipes in
this repository; version firmware/project recipes in their consumer repositories. Do not put
firmware assertions or OTA business logic in the bench. No physical writes without
explicit hardware consent; never infer one from permission to change source code.

## Début de chaque nouvelle session — banc IOBEWI

- Lis [la procédure de session](bench/docs/session-startup.md) avant d'utiliser le
  banc. Lance Claude depuis la racine de ce dépôt (`/workspace` dans le
  devcontainer). `CLAUDE.md` importe ce fichier ; `/memory` permet de vérifier
  que les consignes sont chargées. Ne duplique pas ces règles dans une mémoire
  personnelle qui pourrait devenir périmée.
- Relève la branche, le SHA et les modifications locales. Vérifie Python et le
  chemin réel du module `iobewi_bench` : ne teste pas par erreur un autre clone
  installé en mode éditable. Dans le devcontainer, les dépendances sont déjà
  installées ; ne reconstruis pas l'image à chaque session.
- Commence par `GET /health` sur `BENCH_URL` (défaut
  `http://127.0.0.1:8080`). Réutilise un serveur sain ; ne le redémarre pas pour
  commencer une conversation. Un serveur dégradé, un port occupé par un autre
  service ou une instance distante inaccessible doivent être diagnostiqués,
  pas remplacés par une seconde instance.
- Si le serveur local attendu est arrêté et qu'aucun ancien processus n'est
  encore actif, utilise uniquement `python bench/tools/run_server.py`, depuis
  la racine, avec le fichier privé `BENCH_TOKENS_FILE` existant et le même
  `BENCH_STATE_DIR` persistant. La procédure décrit le premier provisionnement,
  le lancement détaché et l'attente bornée de disponibilité.
- Consulte ensuite `identity()` et `devices()` avec le client Python officiel,
  sans réserver de carte. Vérifie les scopes, la carte logique, sa puce, sa
  capacité, sa génération, sa quarantaine, sa réservation et son grant.
  L'absence de carte ne doit pas empêcher le travail logiciel indépendant.
- Le navigateur est **sur le PC de l'opérateur**, à `http://localhost:8080`
  (ou au port local réellement transféré par VS Code). Lui seul sélectionne et
  possède le port USB via Web Serial. Aucun Chromium dans le devcontainer,
  aucun transfert USB vers Docker et aucun `espflash`, esptool ou accès
  `/dev/tty*` pour contourner le banc. Le Python dans Docker ne parle qu'à l'API.
- Demande la sélection du port au navigateur seulement si la tâche matérielle
  l'exige et qu'aucun agent correspondant n'est déjà prêt. Les grants et la
  récupération restent des gestes locaux de l'opérateur. Une sélection de port
  peut avoir un effet sur le matériel ; elle n'est pas un prérequis à une simple
  lecture d'état serveur. Aucun identify/reset/console/write/erase automatique.
- Annonce brièvement l'état observé et les seuls prérequis manquants. Un
  démarrage réussi du serveur n'est pas une preuve de disponibilité matérielle.
  La nouvelle session ne reprend jamais une ancienne campagne implicitement.

## Utilisation et fin de session

- Utilise `BenchClient`, le plugin pytest, `CampaignManifest`, `Campaign` et
  `Orchestrator` ; versionne les recettes génériques du banc dans ce dépôt et
  les recettes métier dans leur dépôt consommateur. Les dossiers privés servent
  aux données et preuves d'exécution, pas à l'unique exemplaire des tests. Lis `bench/docs/api.md`, `bench/docs/pytest.md` et
  `bench/docs/reliability.md` pour le scénario concerné. Ne réimplémente pas
  l'API ni les verdicts de projet dans le serveur.
- `--bench` autorise l'accès du test au banc ; ce n'est pas un consentement
  d'effacement ou de programmation. Ceux-ci nécessitent les consentements
  explicites de campagne et de commande, les scopes, les secteurs approuvés,
  la sauvegarde vérifiée et un grant local suffisant. Les autres commandes
  matérielles doivent aussi rester dans le périmètre demandé par l'utilisateur.
- En cas d'interruption, aucun rejeu ni nouvelle tentative implicite de mutation.
  Conserve les issues `indeterminate`, ferme la fenêtre de preuve et procède à
  une récupération locale explicite. Désarmer son déclencheur après expiration
  du grant ne réaccorde aucun droit ; les contrôles de réservation restent actifs.
- Termine la campagne par restauration/vérification quand nécessaire, libération
  des réservations et révocation locale du grant avant `Campaign.close()`.
  Signale immédiatement les opérations encore incertaines et les limites du
  nettoyage ; ne les masque pas par un redémarrage serveur.
- Laisse le serveur sain disponible entre sessions, sauf demande d'arrêt ou
  scénario de test autorisé. Pour un arrêt/redémarrage demandé, envoie SIGTERM
  ou SIGHUP au **lanceur identifié**, puis attends sa fin ou le changement
  d'epoch attendu. Ne tue pas de processus par recherche large de nom.
- Ne supprime jamais le verrou ou la base SQLite pour débloquer le banc. Ne
  régénère pas les tokens d'une instance existante. N'affiche ni tokens,
  fichiers de secrets, dump flash ni journaux UART bruts dans un bilan.
- Conserve un bilan privé : révisions, epoch/génération, preuves, opérations
  pendantes et état final. À la session suivante, vérifie de nouveau l'état réel.

## Méthode obligatoire — rédiger, relire, puis qualifier

Lis [la méthode de qualification](bench/docs/qualification-workflow.md).
Pour toute nouvelle recette matérielle ou modification de scénario :

1. Rédige le contrat (objectif, assertions, effets, prérequis, arrêt, restauration)
   et le code paramétré dans une branche et une PR du dépôt approprié.
   Les recettes maintenues ne doivent pas exister uniquement sous /root ou dans
   un dossier privé hors Git. Les contrôles logiciels sans matériel restent permis.
2. Fais relire la PR et son SHA par l'autre assistant : Claude rédige les tests,
   Codex réalise la revue indépendante, puis Claude corrige les remarques.
   Ne prétends pas qu'une revue a eu lieu sans retour effectif du réviseur.
   Lionel arbitre et valide le périmètre de campagne ; ne lui redemande pas les
   décisions déjà explicites et encore applicables.
3. Ne lance aucun nouvel essai matériel avant la revue du commit retenu et
   l'accord de Lionel pour la campagne. Une PR ouverte, une CI verte ou un
   opt-in pytest ne remplacent ni cette validation ni les autorisations matérielles.
   Une récupération/restauration déjà autorisée reste prioritaire.
4. Exécute le SHA validé sans modification des sources pertinentes, relève les
   versions réelles du serveur/client/agent/recette et conserve un nouveau dossier
   de preuves pour chaque run. Aucun correctif d'assertion en cours d'exécution.
   Toute modification substantielle repasse en revue avant le prochain run.
5. Examine les preuves et limites avant de proposer la fusion. Ne fusionne pas
   et ne clôt pas d'issue sans instruction explicite de Lionel.

Versionne les modèles de manifestes et les instructions de rejeu ; garde secrets,
consentements privés, sauvegardes et traces brutes hors Git sur un stockage
persistant vérifié. Ne suppose pas que /root/bench-evidence ou /root/bench-recipes
survivent à une recréation du conteneur. Préserve les recettes locales historiques
et leurs preuves avant intégration ; un PASS local ne devient pas rétroactivement
le PASS d'un nouveau commit.

## Software checks

Use Python 3.12+ and Node 22+. In `bench`:

```sh
python -m pip install -r requirements.txt -e './python[pytest]'
python -m pytest python/tests recipes -q
npm ci --ignore-scripts
npm test
```

Les tests navigateur suivants sont exécutés par la CI logicielle, hors du
devcontainer courant. Ne les installe pas au démarrage d'une session :

```sh
npx playwright install --with-deps chromium
npm run test:browser
```

These are software simulations. General CI must never require a physical board.
Hardware pytest recipes require an explicit opt-in; generic bench qualification
recipes belong here and project-specific recipes belong in the consumer.
Do not merge changes without explicit user validation.

## Qualification evidence

- Preserve the first recipe versions and all historical evidence. Use a new run
  directory/version for a corrected recipe; never rewrite an old result.
- Verify the board's reference state before every batch. Record backup/restore
  policy, actual flash regions and supported transport in a validated manifest.
- Never silently correct assertions during a running test. Classify bench,
  firmware, recipe and operator failures separately, with the relevant evidence.
- Never retry hardware work or a lost operation submission automatically. Unknown
  outcomes stay indeterminate. Do not replace PendingOperation with an expected
  failure assertion just to pass a recipe.
- Close proof windows explicitly at epoch/generation interruptions. Old Board and
  cursor handles do not become valid again after recovery.
- Require campaign and per-operation programming consent, all server scopes and a
  live local grant. Remote clients cannot create, renew or revoke that grant.
- Retain artifacts needed by active or uncertain work. Keep verified restoration
  backups privately before cleaning disposable server artifacts.
- Before a successful final verdict, verify restoration, local recovery, release
  of reservations and local revocation. A simulator PASS is never hardware PASS.
- Never commit tokens, USB serial numbers, private flash dumps or campaign logs.
- The durable server needs a persistent private BENCH_STATE_DIR. Do not publish or
  discard its recovery database. A disk failure requires inspection and restart.
