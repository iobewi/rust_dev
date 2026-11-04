# Campagne de qualification v2 — lecture seule

Le [lot C3](qualification-c3.md) dispose d'un fichier dédié : sélectionnez explicitement
`test_c3.py` sur C3 ou `test_read_only.py` sur S3. Ne lancez pas le dossier entier
avec `--bench` sur une seule famille : les assertions de famille sont strictes.

Cette campagne renforce les recettes privées de la première qualification de la
PR #4. Les fichiers historiques et leurs verdicts ne sont pas modifiés. Un PASS
historique n'est pas une exécution de cette version. Les lots M3 et M7 de l'ancienne
campagne ne sont pas remplacés ici ; ce dossier cible M4/M5/M6/M8/M8b, avec M1 comme
contrôle avant/après. Il ne constitue pas une nouvelle qualification matérielle.

Les tests sont dans `bench/recipes/qualification_v2/test_read_only.py` ; le moteur
reste pytest avec le client API du banc. Aucun accès série direct, retry, écriture,
effacement, grant ou récupération automatique n'est ajouté. Ne pas employer xdist
pour une seule carte. Les opérations chargeur peuvent redémarrer le firmware.

## Préparation commune

Installer `./bench/python[pytest]`, puis connecter la carte dans la page. Prévoir un
token client read/control dans `BENCH_CLIENT_TOKEN`, jamais dans les arguments.
La carte doit être connectée, sans réservation, sans quarantaine et sans grant.
Pour M5 concurrence, fournir une deuxième **identité** indépendante read/control
par `BENCH_CLIENT_TOKEN_B`. Aucun droit program n'est nécessaire.

Utiliser un dossier privé hors Git, un rapport JUnit distinct par invocation et
une copie vérifiée de la sauvegarde. Créer le parent des rapports avant pytest et
protéger également les logs/JUnit avec `umask 077`. Les dumps sont des données
sensibles ; aucune preuve brute ne doit être publiée automatiquement.

Renseigner `BENCH_CLIENT_REVISION`, `BENCH_SERVER_REVISION`, `BENCH_PAGE_REVISION`
et `BENCH_TRANSPORT` si ces informations ont été vérifiées. Elles sont conservées
comme **déclarations opérateur**, pas comme observations garanties. Une valeur
absente est explicitement `not recorded`. `/health` ne fournit pas le SHA serveur,
et le client pytest ne peut pas vérifier la page réellement chargée ni le VID/PID
du port possédé par le navigateur. Les capacités observées sont enregistrées,
mais ne suffisent pas à prouver le transport. Ne pas déduire une qualification
UART d'un essai Serial/JTAG, ni l'inverse.

## Exécutions

Depuis la racine, adapter URL, carte et chemins privés :

```sh
umask 077
export BENCH_URL=http://127.0.0.1:8080
export BENCH_DEVICE=esp32-local
mkdir -p /qualification-runs/pr4-v2

# M1 et M5 : aucune intervention physique. M5 concurrence est SKIP sans token B.
python -m pytest bench/recipes/qualification_v2 -k 'm1 or m5' --bench \
  --bench-results /qualification-runs/pr4-v2/normal \
  --junitxml=/qualification-runs/pr4-v2/normal.xml -v

# M4 : sortie pytest en échec ATTENDUE, avec erreur de teardown absente.
python -m pytest bench/recipes/qualification_v2 -k m4 --bench \
  --bench-deliberate-failure \
  --bench-results /qualification-runs/pr4-v2/deliberate \
  --junitxml=/qualification-runs/pr4-v2/deliberate.xml -v
```

M4 utilise `pytest.fail` après une lecture stabilisée, indépendamment du contenu
flash. Vérifier JUnit `failures=1`, `errors=0`, l'état après nettoyage et rejouer M1.
L'enregistrement distingue l'échec du test de l'échec éventuel du teardown.

M6 exige le chemin privé et le SHA-256 vérifié de la **sauvegarde entière**. Cette
référence doit encore correspondre au contenu attendu de la carte. Remplacer les
deux paramètres ci-dessous par les vraies valeurs ; aucune valeur historique
n'est imposée dans la recette :

```sh
python -m pytest bench/recipes/qualification_v2 -k m6 --bench \
  --bench-reference /private/full-flash.bin \
  --bench-reference-sha256 FULL_REFERENCE_SHA256 \
  --bench-results /qualification-runs/pr4-v2/long-read \
  --junitxml=/qualification-runs/pr4-v2/long-read.xml -v
```

La référence est vérifiée avant la commande. Taille et digest du transfert sont
vérifiés par le client ; l'égalité des 10 Mio avec le préfixe de la référence est
ensuite une **assertion**, plus un `comparison.json` après succès. Toute différence
donne FAIL. Une référence absente donne SKIP ; une référence fournie invalide donne
FAIL. La réservation par défaut de 900 s couvre normalement le délai de lecture
de 600 s et le nettoyage. La progression visuelle, la veille et l'onglet masqué
restent des constats distincts, non prouvés par M6.

M8/M8b exigent une coordination préalable. Lancer **un seul essai à la fois**, une
fois Lionel prêt au geste. Le message `ready` indique le début de la fenêtre :

```sh
python -m pytest bench/recipes/qualification_v2 -k m8_interrupted --bench \
  --bench-interactive --bench-results /qualification-runs/pr4-v2/unplug \
  --junitxml=/qualification-runs/pr4-v2/unplug.xml -v -s

python -m pytest bench/recipes/qualification_v2 -k m8b --bench \
  --bench-interactive --bench-results /qualification-runs/pr4-v2/stale \
  --junitxml=/qualification-runs/pr4-v2/stale.xml -v -s
```

- M8 : débrancher pendant la lecture, puis rebrancher la même carte. Exige
  `OperationError`, `serial_disconnected`, phase `flash_read`, progression partielle
  valide et absence de résultat/dump partiel. Une autre exception fait échouer le
  test ; elle reste un fait à examiner, pas une déconnexion validée. Après nouvelle
  génération, l'ancienne instance doit refuser sans POST d'opération. Les événements
  doivent montrer déconnexion/reconnexion et aucune opération après reconnexion
  dans la fenêtre observée. Aucune deuxième commande n'est tentée avant ce changement.
- M8b : après `ready`, utiliser Déconnecter puis Connecter sur la même carte. Un
  rechargement de page n'est pas supposé suffisant. Exige `ContinuityError` et un
  compteur de soumissions inchangé. Timeout borné à 150 s pour le geste.

Sans geste réalisé à temps, le lot échoue ; sans `--bench-interactive`, il est SKIP.
Après chaque interruption, examiner l'état et lancer M1 dans une **nouvelle**
invocation. Une quarantaine ou une issue non stabilisée n'est jamais effacée pour
faire passer la campagne. Aucune écriture/coupure pendant programmation n'est prévue.

## Preuves automatiques

Chaque test exécuté reçoit un dossier distinct sous `--bench-results` contenant :

- recette et fichiers de support exacts, hashes de recette/client/plugin/support,
  versions Python/pytest/paquet et déclarations de provenance ;
- snapshots API avant/après, epoch, génération, capacités, réservation,
  quarantaine et durée du grant ; le mode console n'est pas exposé par cette API ;
- toutes les réservations créées par la campagne, y compris le second client M5,
  et sous-dossiers de dumps complets avec métadonnées adresse/taille/SHA-256 ;
- types de requêtes, compteur de POST d'opérations, diagnostics de phase/progression
  et états d'opération, sans corps de requêtes, arguments, tokens ou octets UART ;
- événements filtrés, bornes des lots, pertes serveur/agent et transitions de
  génération ; perte, changement d'epoch ou dépassement de la limite de capture
  invalident la preuve et font échouer le teardown ;
- `assertions.jsonl` : verdicts des phases setup/call/teardown, et `cleanup.json` :
  erreurs de nettoyage et compteurs. Le détail des assertions est fourni par pytest
  et JUnit ; il n'existe pas de trace de chaque assertion réussie séparément.

La capture d'événements ne conserve pas l'UART brut. Elle vérifie la fenêtre bornée
du test, pas l'absence éternelle de rejeu. Les journaux restent privés et contiennent
des identifiants techniques ; vérifier l'expurgation avant tout partage.

Le nettoyage tente **toutes** les fermetures, même si une échoue, puis conserve les
erreurs dans un groupe d'exceptions. pytest présente séparément l'échec initial et
les erreurs de teardown. Il ne force pas la libération d'une opération indéterminée.

## Contrôles logiciels des recettes

```sh
python -m pytest bench/python/tests bench/recipes -q
```

Sans `--bench`, aucune recette matérielle ne contacte le banc. Les nouveaux tests
injectent les faux positifs historiques : données différentes, erreur quelconque
au lieu d'une déconnexion, mauvais statut/code d'expiration et garde de génération
qui ne détecterait le changement qu'après soumission. Ils doivent être rejetés.
Les tests d'intégration exécutent M1, M4 et M5 dans des processus pytest séparés
contre le serveur API et deux identités simulées. Ce sont des preuves logicielles,
pas des essais sur la carte de Lionel. Les lots matériels v2 restent à rejouer.
