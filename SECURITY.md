# Sécurité

Simple API 3cx peut changer l’état des postes et des files d’attente d’un PBX. Une clé API ou un lien navigateur exposé doit être traité comme un accès actif.

## Versions prises en charge

Le projet n’a pas encore de release versionnée. Les correctifs sont publiés sur la branche <code>main</code> et validés sur l’installation de référence décrite dans le [README](README.md).

## Signaler un problème

Ne pas publier de vulnérabilité exploitable, clé API, identifiant SIP, URL de lien d’action ou donnée d’appel dans une issue publique. Utiliser le canal privé de signalement de vulnérabilités de GitHub s’il est disponible pour ce dépôt ; sinon, contacter le propriétaire du dépôt par un canal privé avant d’ouvrir une issue.

Indiquer la version 3CX, le système d’exploitation, le commit concerné, le comportement observé et la manière de le reproduire **sans fournir de secret actif**.

## En cas d’exposition d’un secret

- Révoquer la clé concernée avec <code>sudo simple-api-3cx key revoke &lt;nom&gt;</code>.
- Révoquer un lien permanent avec <code>sudo simple-api-3cx link revoke &lt;nom&gt;</code>.
- Retirer si nécessaire l’IP autorisée avec <code>sudo simple-api-3cx allow remove &lt;IP/CIDR&gt;</code>.
- Examiner les accès et régénérer les identifiants compromis avant de remettre l’intégration en service.
