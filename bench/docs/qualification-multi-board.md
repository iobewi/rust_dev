# Recette matérielle multicarte — IOBEWI Device Lab (#40)

**Statut : préparée, NON EXÉCUTÉE.** Aucune preuve historique M2 n'est
réinterprétée comme qualification de cette architecture.

## Conditions d'entrée

- Revue indépendante des PR serveur #41, navigateur #42 et interface #43,
  validation de leurs SHA exacts et CI verte. Ne pas fusionner automatiquement.
- Consentement opérateur **explicite** avant toute intervention sur les deux
  ESP32-S3 : choisir ou ouvrir un port peut provoquer un reset.
- Le serveur déjà en service ne doit pas être redémarré. Vérifier `GET /health`
  puis client `identity()` / `devices()`, sans réservation ni commande.
- Conserver la configuration privée et les preuves dans leur emplacement
  persistant privé ; ne pas versionner jetons, MAC/identités USB, traces brutes
  ou dumps. Ne pas changer les critères de qualification v3.
- Deux cartes ESP32-S3 distinctes, avec deux IDs logiques déjà autorisés par
  le serveur ; identifier leurs ports séparément par confirmation opérateur.
  La simple égalité VID/PID ne prouve pas leur identité physique.
- Aucune lecture flash, écriture, effacement, reset, activation BOOT ni
  programmation sans consentement spécifique à l'action. Ne pas inférer un
  consentement matériel de l'autorisation de développer ce logiciel.

## Séquence proposée

| Étape | Action | Preuve / critère attendu | Effet matériel |
|---|---|---|---|
| M40-0 | Relever SHA de l'UI, du serveur et de l'agent, epoch et générations avant connexion | État initial et périmètre documentés | Aucun |
| M40-1 | Dans **une seule fenêtre**, choisir ID A puis explicitement port A avec « Ajouter une carte » | A visible « Connectée », agent A enregistré sous ID A | Ouverture USB ; reset éventuel à autoriser |
| M40-2 | Ajouter ID B et port B dans la même fenêtre | Deux entrées visibles, deux agents indépendants et deux ports ouverts | Ouverture USB ; reset éventuel à autoriser |
| M40-3 | Sélection A→B→A par **nom**, sans commande | Aucune fermeture/réouverture, reset, génération nouvelle ou perte de capture induite par la sélection | Aucun attendu |
| M40-4 | Observer simultanément les deux consoles et leurs événements spontanés | Aucun octet A dans B ni B dans A, horodatages et cursors distincts | Observation |
| M40-5 | Déconnecter A par son **statut** | A reste visible « Déconnectée », B reste connectée et continue à capturer | Fermeture USB A |
| M40-6 | Reconnecter A sur son port exact avec identité revérifiée | Agent A et génération renouvelés ; B inchangée, aucun rejeu | Ouverture USB ; reset éventuel à autoriser |
| M40-7 | Débrancher physiquement A avec accord opérateur | A visible « USB perdu/Reconnexion en attente », B stable | Débranchement physique |
| M40-8 | Rebrancher A ; vérifier l'identité et la reconnexion de **son port** | Deux agents opérationnels, B non interrompue, aucun rejeu de commande | Rebranchement USB |
| M40-9 | Demander annulation de reconnexion A, puis vérifier « Déconnectée » | A ne se reconnecte pas spontanément, B inchangée | Aucun attendu |
| M40-10 | Autoriser puis refuser explicitement un ID non configuré ; tester un port déjà revendiqué | Aucun deuxième propriétaire du même port ou ID ; refus visible | Aucun port supplémentaire ne doit être ouvert |
| M40-11 | Avec consentement spécifique, lancer sur A une opération **sans écriture** dont l'exécution est suffisamment longue pour sélectionner B | L'opération conserve ID/génération A et son résultat reste dans l'historique A ; B pilotable indépendamment | Dépend de la commande autorisée |
| M40-12 | Examiner révocations/grants, réservations, quarantaine et reprise après interruption selon scénario approuvé | Aucune autorité transférée entre A et B, aucun résultat indéterminé déclaré réussi | À autoriser au cas par cas |

**Ne pas exécuter M40-11/M40-12** sans préciser la commande, les conditions
de retour console, les effets possibles, le consentement et l'état de
restauration. Une commande de lecture flash ou d'identification peut
nécessiter une transition de mode / reset ; ce ne sont pas des lectures
passives garanties.

## Arrêt, récupération et verdict

Interrompre la campagne au premier ciblage erroné, état incertain,
réénumération imprévue, mélange d'UART ou risque de flash. Ne jamais
rejouer automatiquement la mutation ou masquer une quarantaine. Conserver
les preuves et procéder à la récupération locale explicitement autorisée.

Pour chaque étape, relever : numéro, SHA, heure, ID, génération avant/après,
état USB et agent, changements d'epoch, résultat attendu/réel, journaux
expurgés et décision PASS/FAIL/BLOCKED. Conserver les artefacts dans un
nouveau dossier de campagne privé et indépendant des anciennes preuves M2.

Le verdict **logiciel** peut être établi par CI ; le verdict **matériel
multicarte** reste `NOT RUN` jusqu'à la campagne avec les deux S3.
