# Matrice de qualification du banc : ce qui est couvert, et ce qui ne l'est pas

**Statut : préparation.** Ce document décrit la couverture des scénarios Q1, Q2a–Q2d, Q3 et C3R-0–C3R-3 et les limites de ce
qu'ils démontrent. Il ne rapporte aucun résultat nouveau. Les verdicts réels se lisent dans le rapport du comparateur
(`bench/tools/qualification_compare.py`), produit à partir des dossiers de preuves privés.

## 1. Niveaux de qualification

On ne confond pas ces six niveaux. Un scénario réussi une fois établit au plus le premier.

| Niveau | Ce qu'il établit | Ce qui le démontre | État |
|---|---|---|---|
| **Fonctionnel nominal** | Le comportement attendu se produit dans les conditions normales | Un PASS vérifié par scénario et par carte/transport | Observé sur les passages réalisés (S3/UART externe, C3/USB Serial/JTAG) |
| **Répétabilité** | Le même résultat, mêmes critères, sur plusieurs exécutions | ≥ 3 PASS vérifiés, mêmes critères épinglés, même recette et même serveur, comportement identique | **Non démontrée** : plan dans `repeatability-plan.md` |
| **Résistance aux interruptions et aux pannes** | Aucun état incertain n'est transformé en PASS ; la récupération fonctionne | Injection de fautes contrôlée avec procédure de récupération | **Non qualifiée** |
| **Endurance** | Absence de dérive sur la durée et le nombre d'opérations | Campagne longue, statistiques | **Non qualifiée** |
| **Firmware consommateur** | Un firmware applicatif démarre et se comporte correctement | Recettes du dépôt consommateur ; marqueur applicatif | **Hors banc** (`boot_verified` vaut toujours `false`, par conception) |
| **Certification externe** | Conformité à un référentiel externe | Organisme ou audit tiers | **Non engagée** |

## 2. Exigences et scénarios

Le catalogue de référence est `bench/tools/qualification_criteria.json` (épinglable par SHA-256 avant une campagne).

| Exigence | Énoncé | Scénarios |
|---|---|---|
| REQ-01 | Cycle destructif complet par `Campaign`/`Orchestrator` : sauvegardes vérifiées, programmation, relecture indépendante, restauration, comparaison finale, clôture propre | Q1 |
| REQ-02 | Un grant expiré naturellement ne permet plus d'armer ; le propriétaire peut encore désarmer ; aucun grant n'est recréé | Q2a |
| REQ-03 | Une révocation locale se distingue d'une expiration et a les mêmes conséquences | Q2b |
| REQ-04 | Propriété : une nouvelle réservation ou une autre identité ne peut pas désarmer, avec un grant **valide** | Q2c, Q2d |
| REQ-05 | Redémarrage coordonné au repos : arrêt avant démarrage, nouvel epoch, même journal, reconnexion sans rejeu, nouvelle fenêtre de preuve | Q3 |
| REQ-06 | Identification, lectures répétées et table de partitions d'un C3, transport déclaré et inféré tous deux consignés | C3R-0, C3R-1, C3R-2 |
| REQ-07 | Un reset explicite et l'observation de la bannière ROM dans la même fenêtre de preuve | C3R-3 |

## 3. Couverture par scénario

| Scénario | Effets matériels | Ce qu'il démontre | Ce qu'il ne démontre pas |
|---|---|---|---|
| **Q1** | Lectures, écriture et effacement de la seule région approuvée, restauration | Cycle complet sans contourner les contrats ; référence restaurée octet pour octet | Résistance à une coupure en cours d'écriture ; endurance ; validité d'un firmware |
| **Q2a** | Armement d'un déclencheur `mark` (aucun accès flash) | Expiration naturelle : un seul événement de grant, `403 grant_required`, désarmement par le propriétaire | Qu'un déclencheur ne peut plus se produire (aucun stimulus contrôlé) |
| **Q2b** | Idem | Révocation locale distinguée de l'expiration (événement à zéro avec > 10 s restantes) | Idem |
| **Q2c** | Idem | `403 trigger_not_owned` pour une nouvelle réservation du même propriétaire, avec grant valide | Le comportement d'un déclencheur encore armé (la libération du bail l'annule) |
| **Q2d** | Idem, avec une seconde identité (`claude-b`, `read` + `control`) | `403 trigger_not_owned` pour une autre identité, avec grant valide | Idem ; l'isolation entre plus de deux identités |
| **Q3** | Redémarrage du serveur par `SIGHUP`, au repos | Ordre arrêt → démarrage prouvé par le journal du lanceur, nouvel epoch, mêmes base et verrou, reconnexion, fenêtres de preuve | Une coupure pendant une écriture ; un agent en blocage ; plusieurs redémarrages enchaînés |
| **C3R-0** | `identify` (l'entrée en chargeur peut redémarrer la carte) | Puce et capacité mesurées égales à la déclaration ; retour console prouvé **côté transport** | Que l'application ait redémarré correctement |
| **C3R-1** | Lectures (chargeur) | Deux lectures identiques d'une région déclarée immuable | Que la région soit libre ou réservée |
| **C3R-2** | Lectures (chargeur) | Une seule table valide, décodeur brut égal à la vue de l'API | La validité des images applicatives ou de la politique de démarrage |
| **C3R-3** | Un reset explicite | Bannière ROM observée après le reset, sans rupture de continuité | Un démarrage applicatif correct ; le comportement après réénumération USB (INDETERMINATE tant qu'un critère n'est pas écrit) |

## 4. Pièges d'interprétation (à relire avant d'écrire un bilan)

- **Stabilité d'une région ≠ identification d'une carte.** Le SHA-256 `7d2c7ac4888bfd75…` est celui de **8 Kio d'octets `0xFF`** :
  n'importe quelle région de 8 Kio effacée le donne. Une égalité avec cette référence prouve que la région est restée vierge, jamais
  que la carte est « la même ». L'identité mesurée (puce, capacité, identifiants USB) n'est qu'un indice.
- **Des octets `0xFF` ne prouvent pas qu'une région est libre** : elle peut être réservée et simplement pas encore écrite.
- **Transport** : la déclaration de l'opérateur et l'inférence de l'agent (identifiants USB) sont deux déclarations, pas une mesure du câblage.
- **`boot_verified: false`** est une constante du moteur : le banc ne vérifie pas le démarrage applicatif. Ce n'est pas un indicateur d'échec.
- **Expiration naturelle ≠ révocation** : seule la chronologie des événements de grant les distingue (Q2a/Q2b).
- **INDETERMINATE n'est pas un échec du banc** : c'est une issue inconnue, jamais rejouée automatiquement, jamais transformée en succès.
- **Un PASS simulé n'est pas un PASS matériel** ; une seule exécution n'est pas une preuve d'endurance.

## 5. Ce que vérifie le comparateur

Il lit les dossiers de preuves **sans jamais les modifier** (il refuse d'écrire dans une racine de preuves) et :

- contrôle la **complétude** (fichiers requis, fichiers propres au scénario) ;
- recalcule les **SHA-256** : recette, fichiers de support, sources du client, module de qualification, manifeste (forme canonique),
  relectures et sauvegardes (blobs contre leurs sidecars) ;
- contrôle la **provenance** (SHA Git sur 40 caractères, arbre propre, `--qual-allow-dirty` non utilisé) et les **versions** (Python, pytest,
  client, serveur, agent) ;
- contrôle la **cohérence** (PASS sans incident ni erreur de nettoyage, `verdicts.jsonl` égal à `report.json`, carte au repos en fin de
  run, génération et epoch inchangés — ou changés pour Q3 —, commandes dans le contrat du scénario) ;
- applique les **critères propres à chaque scénario** (chronologie des grants, codes de refus, grant valide au moment du refus, identités
  distinctes, fenêtres de preuve, ordre arrêt/démarrage, bannière ROM, etc.) ;
- vérifie la **traçabilité des gestes opérateur** : les demandes enregistrées (`operator-requests.jsonl`) doivent correspondre aux grants
  et aux révocations réellement observés dans les événements ;
- calcule une **empreinte de comportement** par run (commandes, refus, motif de grants, fenêtres, soumissions) pour comparer les runs entre eux
  et les cartes entre elles en ignorant les valeurs propres à la carte.

Règles qui ne se relâchent jamais :

1. une preuve **absente ou non vérifiable ne produit jamais un PASS** : un PASS enregistré non vérifiable est rapporté `INDETERMINATE` avec ses raisons ;
2. un PASS **contredit** par ses preuves (empreinte différente, état final sale, mauvais code de refus…) est rapporté `INDETERMINATE` et marqué `CONTRADICTED` ;
3. les verdicts **négatifs ou non concluants historiques** (FAIL, BLOCKED, INDETERMINATE, SKIP) sont conservés tels qu'enregistrés, listés dans la matrice, jamais réinterprétés ni comptés comme PASS ;
4. la répétabilité ne compte que les passages `candidate` ; les campagnes `historic` restent distinctes.

## 6. Non couvert

Résistance aux interruptions et aux pannes (coupure USB ou secteur, perte du navigateur ou du serveur pendant une écriture), endurance, firmware
consommateur et OTA, certification externe. L'injection de fautes pendant une écriture flash exigera une procédure de récupération préalable,
des sauvegardes vérifiées et une autorisation distincte.
