# Plan de répétabilité : trois passages par scénario et par carte

**Statut : plan, non exécuté.** Aucun accès matériel, aucun grant, aucune écriture flash, aucun redémarrage serveur n'est effectué par la
préparation. La fusion de ce plan et chaque passage restent soumis à une autorisation distincte de Lionel. Couverture et limites :
[qualification-matrix.md](qualification-matrix.md).

## 1. Principes

- **Les campagnes historiques restent distinctes** : leurs dossiers de preuves sont des racines `historic` du comparateur ; seules les racines
  `candidate` comptent pour la répétabilité. Rien n'est réécrit ni renommé.
- **Les critères sont écrits avant les runs** (`bench/tools/qualification_criteria.json`) et **épinglés** par leur SHA-256 (`--criteria-sha256`).
  Aucun critère n'est modifié pendant la campagne ; une modification crée une nouvelle version et une nouvelle campagne.
- **Un passage = un run de chaque scénario sur une carte**, dans un nouveau dossier de preuves, avec le même commit validé de la recette, le même
  serveur et la même session privée (hors dates). Tous les essais tentés comptent, y compris ceux qui échouent : on ne retient pas « 3 succès », on
  analyse les tentatives.
- **Exécution progressive** : un premier passage, analyse par le comparateur, décision de poursuivre, puis les suivants.
- **Aucune mutation automatique après incident** : pas de rejeu, pas de restauration improvisée, pas de suppression du verrou ou du journal.

## 2. Configuration exacte

| | ESP32-S3 | ESP32-C3 |
|---|---|---|
| Carte logique | `esp32-local` (une seule carte à la fois sur le banc) | `esp32-local` |
| Transport déclaré par l'opérateur | `external_uart` (pont série externe) | `usb_serial_jtag` (USB natif ; pas d'UART externe) |
| Inférence de l'agent | VID/PID du pont, `agent_reported` | `303a:1001`, `agent_reported` |
| Capacité | mesurée à l'enregistrement puis re-mesurée | idem |
| Table de partitions | `0x10000` | `0x8000` |
| Région d'essai (Q1) | `0x400000 + 0x2000`, **à approuver à nouveau** | `0x3FE000 + 0x2000`, **à approuver à nouveau** |
| Scénarios par passage | Q1, Q2a, Q2b, Q2c, Q2d, Q3 | C3R-0, C3R-1, C3R-2, C3R-3, Q1, Q2a, Q2b, Q2c, Q2d, Q3 |

Navigateur : une seule page, `http://127.0.0.1:8080`, port reconnecté après chaque changement de carte. Serveur : celui qui tourne, commit et sources
relevés dans `before.json` de chaque run ; **aucun changement de code serveur, page, client ou recette entre deux passages**. Le jeton de seconde identité
(`claude-b`) sert à Q2d.

## 3. Conditions initiales et finales (par run)

**Initiales** (sinon BLOCKED, rien n'est muté) : carte connectée du bon type, agent enregistré, génération courante, sans quarantaine, sans
réservation, grant à 0, aucune opération dans le journal, aucun artefact serveur, arbre `bench/` propre au commit validé, session privée valide
écrite par le script de session, approbations de la région et de Q3 données pour **cette** campagne.

**Finales** (vérifiées par le comparateur) : carte connectée et au repos, sans quarantaine ni réservation, grant à 0, aucune opération non résolue,
aucun artefact serveur, région restaurée et comparée égale à la sauvegarde (Q1), génération et epoch inchangés (sauf Q3 : tous deux changés).

## 4. Critères d'acceptation

Ceux de `qualification_criteria.json`, appliqués par `qualification_compare.py` : intégrité des preuves, provenance, cohérence, critères propres au
scénario, traçabilité des gestes. Un passage **réussit** quand chacun de ses runs est `PASS` **vérifié**. La **répétabilité d'un scénario** est
démontrée quand `repeat_min` (3) runs `candidate` sont `PASS` vérifiés avec la même empreinte de comportement et le même environnement (mêmes SHA de
recette, de support et de serveur).

## 5. Gestes opérateur et budget

Durées proposées par la page : 1 min, 5 min, 30 min, 2 h. Les demandes des recettes (« 90 s ») ne sont pas disponibles : on accorde 5 min.

| Scénario | Grants | Révocations | Autre |
|---|---|---|---|
| Q1 | 1 (30 min) | 1 | — |
| Q2a | 1 (1 min, puis **ne rien faire** jusqu'à l'expiration) | 0 | — |
| Q2b | 1 (5 min) | 1 (seulement sur demande « REVOKE … NOW ») | — |
| Q2c | 1 (5 min) | 1 (fin de test) | — |
| Q2d | 1 (5 min) | 1 (fin de test) | — |
| Q3 | 0 | 0 | 1 redémarrage du serveur (autorisation explicite) ; rechargement de la page seulement si la recette le demande |
| C3R-0, C3R-1, C3R-2 | 0 | 0 | entrée en chargeur possible (consentement `C3R-READ`) |
| C3R-3 | 0 | 0 | 1 reset voulu (consentement `C3R-RESET` et `--qual-allow-reset`) |

**Par passage et par carte** : 5 grants et 4 révocations, 1 redémarrage de serveur ; sur le C3, en plus, 1 reset. **Trois passages** : 15 grants,
12 révocations et 3 redémarrages par carte, donc 30 grants, 24 révocations et 6 redémarrages pour les deux cartes, plus 3 resets sur le C3. Compter
environ 15 minutes par passage et par carte, gestes compris.

**Traçabilité des gestes** : chaque recette consigne ses demandes dans `operator-requests.jsonl` ; le comparateur exige que leur nombre et leur nature
(grant / révocation / autre, d'après le verbe initial) correspondent aux événements de grant observés, et signale tout grant ou révocation non
demandé (par exemple un grant accordé deux fois). L'erreur de geste du premier Q2a historique (révocation et nouveau grant) serait détectée par ce
contrôle et resterait un FAIL de cause « opérateur ».

## 6. Conditions d'arrêt immédiat

On arrête la progression, on conserve les preuves et on rapporte l'état connu dès que l'un de ces cas se produit :

- un run `FAIL`, `INDETERMINATE` ou `BLOCKED` non expliqué par une précondition manifestement absente ;
- une opération non résolue, une soumission d'issue inconnue, une quarantaine ou un blocage de l'agent ;
- un changement inattendu d'epoch ou de génération (réénumération USB comprise) hors du scénario qui l'attend ;
- une restauration qui ne correspond pas à la sauvegarde, ou une région différente de la référence attendue ;
- un grant ou une réservation inattendus, un artefact serveur laissé, un fichier ou une preuve manquants ;
- une provenance non propre (arbre modifié, SHA différent du SHA épinglé, `--qual-allow-dirty`) ;
- un critère ou un outil dont le SHA-256 diffère de celui épinglé ;
- un geste opérateur incohérent avec ce qui était demandé.

## 7. Verdicts non concluants

Un run `INDETERMINATE` ou `BLOCKED` n'est jamais rejoué automatiquement, jamais effacé et jamais compté comme PASS. On inspecte, on classe la cause
(banc, client, firmware, recette, infrastructure, geste opérateur) avec ses preuves, puis on décide **explicitement** d'un nouveau passage. Une erreur
de recette ou d'infrastructure ne justifie pas de modifier une assertion pendant la campagne : toute correction crée une nouvelle version du code, une
nouvelle revue et une **nouvelle campagne**. Les verdicts historiques FAIL et INDETERMINATE restent listés dans la matrice, tels qu'enregistrés.

## 8. Comparaison inter-runs et inter-cartes

- **Inter-runs** (même carte et même scénario) : l'empreinte de comportement doit être identique (commandes dans l'ordre, refus et leur code,
  motif des grants, fenêtres, soumissions par client, changement ou non d'epoch et de génération, SHA du motif) ; l'environnement aussi (SHA de recette,
  de support, de serveur). Toute différence est rapportée comme dérive et empêche de conclure à la répétabilité.
- **Inter-cartes** : mêmes scénarios, comportements comparés en ignorant les valeurs propres à la carte (puce, capacité, SHA des lectures, région).
  Une différence de comportement entre S3 et C3 est rapportée, sans être interprétée automatiquement.
- **Durées** : consignées mais non comparées par le comparateur (elles dépendent du geste humain).

## 9. Commandes (modèles ; non exécutés par la préparation)

Épingler les critères avant la campagne, puis analyser après chaque passage (le dossier de sortie doit être hors des racines de preuves) :

```sh
sha256sum bench/tools/qualification_criteria.json        # à noter AVANT la campagne
python bench/tools/qualification_compare.py \
  --criteria bench/tools/qualification_criteria.json --criteria-sha256 <SHA épinglé> \
  --root historic-s3:historic=<racine S3 historique> --root historic-c3:historic=<racine C3 historique> \
  --root repeat-s3:candidate=<racine des passages S3> --root repeat-c3:candidate=<racine des passages C3> \
  --output <dossier privé de rapport> --strict
```

Les runs eux-mêmes utilisent les lanceurs privés existants (session par carte, recettes fusionnées, jeton lu dans le processus, jamais affiché).
Le rapport (`comparison.json`, `comparison.md`) est privé ; un exemple **simulé** du format est dans `docs/examples/qualification-comparison-example.md`.

## 10. Séquence proposée (chaque étape après une autorisation explicite)

1. Revue de Codex et fusion de l'outil et des documents.
2. Épingler le SHA des critères et noter les SHA (recette, serveur) de référence.
3. Carte S3 : vérification d'identité sans écriture, session sans région, puis **nouvelle approbation de la région et de Q3**.
4. **Passage 1 S3** → comparateur → décision. Puis passages 2 et 3 (une décision entre chaque).
5. Remplacement par le C3, reconnexion du port, vérification d'identité, **nouvelles approbations** (région, `C3R-READ`, `C3R-RESET`, Q3).
6. **Passage 1 C3** → comparateur → décision. Puis passages 2 et 3.
7. Rapport de répétabilité : matrice, dérives, comparaison inter-cartes, écarts et limites.

## 11. Hors périmètre de ce plan

Injection de fautes (coupures, arrêt du serveur pendant une écriture, opération interrompue) : plan, procédure de récupération et autorisation
distincts. Endurance au-delà de trois passages. Firmware consommateur et OTA. Certification externe.
