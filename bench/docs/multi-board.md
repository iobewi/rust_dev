# Device Lab — contextes multicarte (issue #40)

## État (v1.0.0)

Le lot A (PR #41) définit l'identité d'agent navigateur liée au couple (session autorisée, carte logique). Le lot B (PR #42) introduit `BoardContexts` (registre des ressources) et `BoardManager` (cycle de vie). Le lot C livre l'interface : la liste permanente **Cartes actives** (nom sélectionnable, statut-bouton de déconnexion/reconnexion, **Retirer**, **Ajouter une carte**) raccordée aux zones Identification, Commandes, consoles et opérations. Les anciennes sections « Carte locale » et « USB » sont supprimées. Le parcours utilisateur est décrit dans [interface](interface.md).

## Invariants

- Une seule entrée `BoardContext` par ID logique dans la fenêtre, et une
  seule entrée par **instance exacte** de `SerialPort` ; l'égalité VID/PID
  n'est jamais utilisée pour choisir ou remplacer un port.
- Chaque entrée possède indépendamment `engine`, `agent`,
  `reconnect`, état USB, identité physique observée et génération serveur.
- `selectedId` est uniquement une propriété de présentation. Chaque
  événement UART et chaque callback agent capture sa propre instance de
  contexte. Une sélection ne doit jamais ouvrir/fermer un port ou émettre
  une commande.
- La cible d'une opération est capturée sous la forme
  `{device,generation,context}` avant soumission et reste immuable pendant
  le traitement asynchrone. Les événements et résultats tardifs sont
  conservés sur cette même carte.
- `lost(port)` ne modifie que le propriétaire exact de ce port. En cas
  d'opération en cours, la carte passe en hold et aucune opération n'est
  rejouée. La reconnexion attend l'inactivité, reprend **le même port**
  déjà accordé et vérifie chip/flash_size/flash_id/mac.
- `disconnect(id)` est volontaire : refuse l'opération active, révoque
  les grants, ferme l'agent et le moteur de cette carte, et désactive sa
  reconnexion. L'entrée reste visible tant qu'elle n'est pas explicitement
  oubliée, sans effacer les preuves côté serveur.
- Les identifiants non autorisés, les doublons d'ID et de port sont refusés
  avant la création d'un moteur matériel.
- Le client navigateur multicarte active `scopedBrowserAgent` :
  `auth` WebSocket avec `device` et upload agent avec
  `X-Bench-Device`. Le chemin agent distant avec token et le navigateur
  historique restent inchangés.

## Frontières des PR

- **A — #41** : authentification serveur et associations autorisées,
  `local_control`, artefacts, expiration des sessions, compatibilité API.
- **B — #42** : `BoardContexts`, `BoardManager`, contrat `Agent`
  opt-in, simulations Node et Playwright avec deux ports et deux WebSockets
  dans une seule page ; isolation progressive des vues par device dans
  `ui.js`.
- **C** : liste persistante `Cartes actives`, gestionnaire raccordé à toute la zone de travail, transitions de sélection/déconnexion qualifiées dans Playwright.

## Qualification

Les tests unitaires injectent deux objets ports distincts et deux moteurs/agents simulés. Les tests Playwright utilisent un vrai serveur HTTP/WebSocket et une seule page Chromium, mais **aucun Web Serial matériel** : ports et moteurs sont simulés. Les preuves matérielles à deux ESP32-S3 (recettes M40, dont M40-11c/d et M40-12a) et leurs limites sont dans [qualification-multi-board](qualification-multi-board.md) et [qualification-multi-board-complement](qualification-multi-board-complement.md). Un PASS simulé n'est jamais un PASS matériel ; les critères v3 et les preuves M2 historiques ne changent pas.
