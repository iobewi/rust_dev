# Sauvegarde complète de l'ESP32-P4 (G1, PC-BACKUP) et contrat de restauration

**Statut : recette écrite et qualifiée en SIMULATION uniquement. NON exécutée sur matériel.** Aucune lecture de flash réelle n'a été faite pour ce lot. Cible unique : ESP32-P4, 33 554 432 octets. Le C6 n'est pas couvert.

Fichiers : `recipes/qualification_campaign/test_pc_backup.py` (recette), `backup.py` (plan et vérification, fonctions pures), `bench/python/tests/test_pc_backup.py` (tests). Option de session : `backup.segment_size` (multiple de 4096, au plus 16 Mio, 4 Mio par défaut) et `backup.return_console` (vrai par défaut).

## Principe
- Chaque segment est une commande `flash.read` ordinaire : un artefact serveur de la taille du segment. `Orchestrator.backup` écrit la copie locale, la fsync, la relit et la compare **avant** de supprimer l'artefact serveur : un échec ne détruit jamais la seule copie.
- Le plan (`plan_segments`) couvre `[0, 33 554 432)` exactement une fois, dans l'ordre : 8 segments de 4 Mio par défaut.
- Un manifeste liste, par segment : index, adresse, taille, SHA-256, **identifiant de l'opération qui l'a produit**, nom de fichier. `verify_backup` relit tous les fichiers sur disque et refuse un segment manquant, dupliqué, décalé ou altéré, deux segments revendiquant la même opération, une somme de tailles différente de 33 554 432, et une image assemblée différente de la concaténation.
- L'image `full-flash.bin` et son descripteur `full-flash.json` (format de `Orchestrator.backup`) sont produits ; le SHA-256 global est recalculé depuis les fichiers relus.
- Verdicts : PASS seulement si les 33 554 432 octets ont été récupérés et vérifiés ; FAIL pour une lecture en échec définitif, une vérification ou un stockage plein ; INDETERMINATE pour une opération non réglée ou une rupture de continuité (epoch, génération, déconnexion) ; BLOCKED sans consentement, hors P4, hors 32 Mio, preuves non persistantes, espace insuffisant ou loader injoignable. Aucune lecture n'est rejouée.

## Limites d'artefacts (vérifiées sur le serveur simulé)
Stockage serveur : 64 Mio au total, 16 Mio par artefact. Les segments sont donc bornés à 16 Mio ; avec 4 Mio, jamais plus d'un artefact de sauvegarde n'est présent à la fois puisqu'il est supprimé après sa copie locale. Espace local exigé : environ trois fois la flash (copies de `Board.read_flash`, fichiers de segments, image assemblée) plus une marge de 256 Mio ; sinon BLOCKED.

## Point de vigilance : lectures enchaînées sur le P4
Au premier essai matériel, la deuxième lecture d'une même session de loader a échoué (voir `p4-flash-read-diagnostic.md`). La valeur par défaut `return_console=true` ouvre donc une session de loader neuve par segment : **c'est un contournement non vérifié sur matériel**, qui multiplie les entrées en loader (un redémarrage possible du P4 par segment, 8 par défaut). Avec `return_console=false`, la recette garde un seul loader mais s'expose au même échec.

## Tests simulés (simulateur déclarant `esp32p4`, 32 Mio, chaque secteur de 4 Kio contient son numéro)
Sauvegarde complète PASS avec image identique au contenu attendu, SHA global, 8 opérations distinctes, carte inchangée, aucun artefact serveur restant ; une session de loader (16 Mio ×2) ; lecture en échec à mi-parcours (segment 0 conservé, rien d'assemblé, pas de retry) ; agent disparu (INDETERMINATE, une seule lecture soumise) ; lecture bloquée (INDETERMINATE) ; changement de génération (INDETERMINATE) ; stockage plein (FAIL, artefacts d'autrui intacts) ; refus avant toute commande (consentement, C6, taille, preuves éphémères) ; vérification : segment manquant, dupliqué, décalé, taille, SHA-256, fichier altéré ou tronqué, image assemblée différente ; en processus : changement d'epoch, artefact de condensat différent, copie locale impossible (artefact serveur conservé), paramètre `return_console`. Compatibilité avec la primitive de comparaison `Orchestrator.compare`.

**Limites de la simulation** : elle ne prouve rien sur le CH343, le loader, esptool-js, la vitesse de lecture ni la durée réelle. Le simulateur a reçu deux ajustements (capacité et motif de contenu configurables ; un refus `503 capacity` à l'envoi devient le code `capacity`, comme dans `web/agent.js`) : l'ancien comportement laissait l'opération expirer en `indeterminate`, faute d'un code d'erreur texte.

## Contrat de restauration (préparé, NON implémenté, rien n'est exécuté)
Aucun mécanisme parallèle : la restauration réutilisera `Orchestrator.restore` (et `compare`), qui lit le descripteur `.json` d'une sauvegarde complète, valide sa cohérence, puis réécrit par segments de 4 Mio avec relecture indépendante. Les fichiers `seg-NNNN.bin` ont chacun leur descripteur : une restauration partielle par segments est possible sans nouvelle primitive.

Une restauration ne pourra commencer que si **tout** ce qui suit est établi :
1. **Source vérifiée** : `verify_backup` réussit à neuf sur le dossier (manifeste, fichiers, SHA-256 global) ; le SHA-256 global est cité dans le mandat de restauration.
2. **Cible** : même carte logique, puce `esp32p4`, flash 33 554 432 octets, mêmes génération et epoch que la session ; l'empreinte d'identité de la carte si elle a été enrôlée.
3. **Régions** : écriture limitée à des régions explicitement approuvées, alignées sur 4 Kio, présentes dans le `CampaignManifest` (`write_regions`).
4. **Consentements destructifs** : consentement de campagne et consentement par commande (`allow_destructive`), scope `program`, grant local de durée suffisante (2 × timeout + 90 s par appel), réservation explicite, option `--bench-allow-destructive`. Aucun de ces éléments n'est déduit d'un mandat de développement.
5. **Stockage** : racine de preuves persistante, sauvegarde source conservée en l'état.
6. **Entrée en loader** : fenêtre de loader établie ; l'échec de lecture enchaînée du P4 doit être levé ou contourné et vérifié avant d'écrire.
7. **Vérification indépendante** : relecture de chaque région écrite égale à la source, puis comparaison finale égale au SHA-256 global.

Conditions d'arrêt : tout écart de cible, d'epoch ou de génération ; toute opération indéterminée ; toute quarantaine ; tout SHA-256 différent ; toute perte de réservation ou de grant ; toute preuve impossible à conserver. Après un arrêt, **aucune reprise automatique** : l'opérateur décide, la source reste intacte. Un état final dont la comparaison n'est pas égale n'est jamais présenté comme restauré.
