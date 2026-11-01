# W0 : réécriture contrôlée du secteur de preuve IAP1 de B″ (préparation)

**Statut : préparé, jamais exécuté.** Cette procédure n'a pas été jouée sur la carte. Elle suppose une décision explicite
d'exécution, séparée de celle qui a autorisé sa préparation.

Objectif : qualifier, sur matériel, l'**écriture** par le banc corrigé (transport, vérification MD5 du chargeur, verdicts),
avec le risque le plus faible possible : un seul secteur de 4 Kio, réécrit avec **exactement les octets déjà présents**.
Si tout se passe bien, la carte est inchangée octet pour octet ; la qualification porte sur le chemin d'écriture.

## Artefact et géométrie

| | |
|---|---|
| Fichier | `web/p91/m2-B2-proof.bin` (secteur IAP1 de B″, issu de `B2/app.iap1`, manifeste figé 17/17) |
| SHA-256 | `d1d5843caa241281ff82398041540cb2567e7a032a67525d468a0b604d35d7ad` (4096 octets) |
| Image liée | `web/p91/m2-B2-app.bin`, `1a36e280eabd4610677c0ae14fcfe01b1824c0655c0f887db08dc0d3c138a5f2`, 1 050 112 octets |
| Épingle de clé | `4b452a4c558aec346bf2f481177e99d0c8c02543495ff4c33229793c60b607ae` (publique) |
| Contrôle hôte | `iap1_pack verify` : *authentic*, taille signée 1 050 112 ; image A″ + cette preuve : `DigestMismatch` ; mauvaise épingle : `UnknownKey` |
| Secteur | `0x37f000`–`0x380000` : dernier secteur de `ota_1` (`0x1d0000`–`0x380000`) |
| Voisins | avant : `0x37e000`, vierge ; après : `0x380000`, premier secteur de `config`, vierge. `0x370000`–`0x37f000` entièrement vierge |
| Effacement | secteur de 4 Kio, aligné ; il termine un bloc de 64 Kio (`0x370000`–`0x380000`) : même un effacement par bloc ne peut atteindre ni l'image B″ (fin `0x2d0600`) ni `config` |
| Restauration | le même fichier (état avant = état voulu) ; sauvegardes : 54 relectures complètes de `ota_1` (`8c93ea6e…`) contenant ce secteur |

## Protocole

Toutes les commandes : `BENCH_PORT=8080 python3 bench/tools/benchctl.py …` (sans la variable, `benchctl` vise 8080 par défaut ; ici c'est le même port, mais
vérifiez `GET /api/info` : `capture` doit nommer le dossier attendu). Opérateur : page au premier plan, port tenu par cette seule page,
`reset auto` décoché, **aucun autre onglet connecté**.

0. **Hôte, sans carte** : `python3 bench/w0/check_w0.py prepare --server http://localhost:8080` doit afficher `RESULT: PASS`.
1. **État applicatif (sans commande)** : dans la capture, au dernier démarrage : `running ota_0 status Stable`, `OTA ready`, aucune session OTA ;
   `/health` 200 en TLS vérifié (méthode de `bench/README.md`, `BENCH_CA`, `BENCH_OTA_HOST`).
2. **S0, lecture seule** : `identify --stay`, puis `dump w0s0_boot 0x0 0xa000 w0s0_meta 0x10000 0x5000 w0s0_ota0 0x20000 0x1b0000 w0s0_ota1 0x1d0000 0x1b0000 w0s0_cfg 0x380000 0x80000 --stay`,
   puis `check_w0.py s0 --dumps <dossier dumps du serveur> --tag w0s0` → `PASS` (bootloader non signé, `otadata`/`ota_meta` = A″ sélectionnée sans transaction, A″, B″, `config`).
3. **Accès complet** : l'opérateur l'accorde **30 min** depuis la page, juste avant W0, et le révoque aussitôt après.
4. **W0, une seule fois** : `write p91/m2-B2-proof.bin@0x37f000 --stay --ttl 60` (ni `--erase`, ni `--md5` désactivé). Attendu : `completed`, `verification.md5 = verified`,
   `sha256_readback = not-run` (c'est l'étape 5 qui le fait). Le serveur et la page vérifient le SHA-256 du fichier avant toute écriture.
5. **Relecture indépendante, quel que soit le verdict de l'étape 4** : `dump w0s6_win 0x37e000 0x3000 w0s6_boot … w0s6_cfg …` (mêmes plages que S0, `--stay`),
   puis `check_w0.py after --dumps … --tag w0s6` : secteur = preuve (SHA-256 exact), `iap1_pack verify` sur la **relecture** = *authentic*, voisins vierges, cinq plages = références.
6. **Retour à A″** seulement si 5 est `PASS` : `console` (un redémarrage). Attendu : bootloader `UNSIGNED`, `plan slot=0 seq=1`, `running ota_0 status Stable`, `OTA ready`, `/health` 200 TLS vérifié.
7. Révocation de l'accès complet. Conservation des journaux (capture, `commands.jsonl`, relectures).

**Aucun second essai automatique.** Toute reprise est une nouvelle décision.

## Critères

**PASS** : étape 4 `completed` avec MD5 vérifié, **et** étape 5 `PASS` (SHA-256 indépendant égal), **et** étape 6 conforme. Les trois sont nécessaires : un `completed` seul ne suffit jamais.

**FAIL** : tout `failed` ; secteur relu ≠ preuve ; voisin ou plage de référence modifiés ; `iap1_pack verify` sur la relecture ≠ *authentic* ; retour à A″ non Stable ; `/health` ≠ 200 ou TLS non vérifié.

**Indéterminé** (`indeterminate`, page rechargée ou port perdu pendant l'écriture) :
1. **aucun redémarrage** : la page interdit `reset`, `console`, `tx`, `io0` et le retour console ; ne pas lever l'état tant que la relecture n'est pas faite ;
2. conserver tous les journaux, ne rien rejouer ;
3. relire seul `0x37e000`+`0x3000` (les lectures restent permises) et classer : *secteur = preuve* (l'écriture a eu lieu : verdict à consigner « indéterminé, état intact »), *vierge* (effacé, non programmé), *autre* (partiel) ;
4. vierge ou partiel : réécrire le **même fichier** est la restauration, mais c'est une écriture : décision nouvelle, jamais automatique ;
5. avec le bootloader non signé en place, A″ démarre sans consulter la preuve de B″ (constaté avant M2 : preuve de `ota_1` vierge, A démarrait Stable) ; B″ ne serait en revanche pas activable ni admissible sous `VerifiedBoot` tant que le secteur n'est pas restauré.

## Risques résiduels

* C'est la **première écriture** par le transport corrigé sur la carte (les lectures et l'identification ont réussi, y compris le changement de débit).
* `completed` ne prouve que transfert + MD5 du chargeur ; la preuve du contenu est la relecture SHA-256.
* Une coupure d'alimentation ou de USB entre l'effacement et la fin de la programmation laisse un secteur vierge ou partiel (fenêtre de quelques dizaines de ms). Récupérable, voir ci-dessus.
* La granularité réelle d'effacement du chargeur n'est pas observable par les voisins, qui sont vierges ; elle est sans conséquence ici (voir la géométrie).
* Le comportement d'onglet masqué n'a pas été exercé sur le matériel : rester au premier plan.
* Le navigateur et le relais de port (Docker) sont hors du dépôt ; vérifier `GET /api/info` avant toute commande.
