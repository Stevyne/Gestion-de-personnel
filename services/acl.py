"""Contrôle d'accès centralisé par rôle.

Les décorateurs ``role_required`` protègent les routes une par une. Cette ACL
ajoute un filet central : plutôt que d'espérer qu'aucun décorateur n'a été
oublié, on part du principe qu'un rôle restreint n'accède qu'à ce qui est
explicitement listé ici.

Deux rôles sont concernés :

* **manager** — pilote le temps et les workflows RH de son département
  (présences, absences, congés, permissions) et consulte la liste des
  employés **sans jamais pouvoir la modifier** ;
* **employe** — accède à son espace : tableau de bord, congés, permissions,
  messagerie, notifications, historique, calendrier et le matériel qui lui
  est attribué.

``admin`` et ``rh`` conservent un accès global et ne sont pas filtrés.
Le rôle ``technicien`` n'est volontairement pas restreint ici : son périmètre
n'a pas encore été arbitré, il reste donc inchangé.

Pour accorder un nouvel écran à un rôle, il suffit d'ajouter son endpoint
dans l'ensemble correspondant — c'est le seul endroit à modifier.
"""

# Endpoints accessibles à tout utilisateur authentifié, quel que soit son rôle.
# Sans eux, une session valide ne pourrait même plus se déconnecter.
COMMUN = frozenset({
    'static', 'health_live', 'health_ready',
    'auth.login', 'auth.logout', 'auth.avatar_image',
    'auth.mon_profil', 'auth.mon_profil_infos', 'auth.mon_profil_mot_de_passe',
    'auth.mon_profil_photo', 'auth.mon_profil_photo_supprimer',
    'notifications.notifications', 'notifications.mark_notifications_read',
    'messagerie.messagerie_inbox', 'messagerie.messagerie_nouveau',
    'messagerie.messagerie_voir', 'messagerie.messagerie_repondre',
    'messagerie.messagerie_quitter', 'messagerie.messagerie_non_lus',
    'messagerie.messagerie_piece_jointe',
    'dashboard.dashboard',
    'recherche.recherche_page', 'recherche.api_recherche',
    'self_service',
})

# --- EMPLOYÉ : son espace, rien de plus -----------------------------------
EMPLOYE = COMMUN | frozenset({
    # Congés et permissions : dépôt et suivi de ses propres demandes.
    'conges.conges', 'conges.add_conge', 'conges.annuler_conge',
    'conges.update_conge',
    'permissions', 'add_permission', 'annuler_permission',
    'update_permission', 'delete_permission',
    'self_service_conges', 'self_service_presences', 'self_service_materiels',
    # Absences : uniquement le circuit de justification le concernant.
    'absence_justifications.self_service_absences',
    'absence_justifications.formulaire_justificatif',
    'absence_justifications.deposer_justificatif',
    'absence_justifications.telecharger_justificatif',
    # Consultation.
    'calendrier_conges', 'presences.historique',
    'export_presences_pdf', 'export_presences_excel',
    'export_conges_pdf', 'export_conges_excel',
    # Objectifs : ce module ne pose aucun role_required, il applique ses
    # propres contrôles enregistrement par enregistrement — le propriétaire
    # pour la progression et la soumission, le manager ou les RH pour la
    # validation, 403 pour tout le reste (voir _verifier_ecriture). L'ACL
    # lui ouvre donc la porte telle quelle : c'est lui qui trie, et le faire
    # ici en double reviendrait à masquer un bouton que le module sait
    # parfaitement refuser.
    'objectifs.objectifs_liste', 'objectifs.objectif_nouveau',
    'objectifs.objectif_detail', 'objectifs.objectif_modifier',
    'objectifs.objectif_demarrer', 'objectifs.objectif_progression',
    'objectifs.objectif_soumettre', 'objectifs.objectif_annuler',
    'objectifs.objectif_valider', 'objectifs.objectif_non_atteint',
    'objectifs.objectif_reactiver',
    # Compétences de la fiche employé : lecture seule de son propre profil.
    # Le catalogue, lui, reste réservé à admin/RH.
    'competences.employe_competences',
    # Fiche de paie : composée ici, mais le cloisonnement fin est fait dans
    # la route — un employé n'atteint que sa propre fiche, un manager celles
    # de son département. L'ACL ne sait pas filtrer par enregistrement.
    'fiches_paie.fiche_de_paie', 'fiches_paie.apercu', 'fiches_paie.pdf',
})

# --- MANAGER : temps et workflows RH, employés en lecture seule -------------
MANAGER = EMPLOYE | frozenset({
    'presences.presences', 'presences.add_presence', 'presences.clock_in',
    'presences.clock_out', 'presences.delete_presence',
    'absences.absences', 'absences.add_absence', 'absences.delete_absence',
    'absences.synchroniser_absences',
    'conges.avis_conge', 'conges.delete_conge',
    'avis_permission',
    # Employés : lecture seule. Ni add_employee, ni add_employee_alt,
    # ni edit_employee, ni delete_employee.
    'index', 'view_employee',
    # Niveaux de compétences des employés de son département, en lecture et
    # en écriture — mais pas le catalogue, qui reste à admin/RH. Les routes
    # vérifient elles-mêmes l'appartenance au département (403 sinon).
    'competences.employe_competence_ajouter',
    'competences.employe_competence_modifier',
    'competences.employe_competence_supprimer',
})

# Rôles soumis au filtre central. admin et rh ont un accès global.
ROLES_FILTRES = {
    'manager': MANAGER,
    'employe': EMPLOYE,
}

# Rôles qui contournent l'ACL (accès global assumé).
ROLES_GLOBAUX = frozenset({'admin', 'rh'})


def endpoint_autorise(role, endpoint):
    """Vrai si ``role`` peut accéder à ``endpoint``.

    Un endpoint inconnu (None) laisse passer : mieux vaut rendre une 404
    standard que bloquer brutalement une route non résolue.
    """
    if endpoint is None:
        return True
    if role in ROLES_GLOBAUX:
        return True
    autorises = ROLES_FILTRES.get(role)
    if autorises is None:
        return True
    return endpoint in autorises
