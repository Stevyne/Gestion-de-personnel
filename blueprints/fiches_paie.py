"""Blueprint « Fiche de paie » : reçu de paiement imprimable.

URL publiques exposées par ce blueprint :

    /employes/<id>/fiche-de-paie                → composer le reçu
    /employes/<id>/fiche-de-paie/apercu         → reçu prêt à imprimer (HTML)
    /employes/<id>/fiche-de-paie/pdf            → reçu en PDF

Le document reprend la présentation d'un reçu papier : en-tête de
l'entreprise, titre et période, identité du bénéficiaire, montant en
toutes lettres puis en chiffres, mode de paiement, lieu et date, et deux
cadres de signature.

Tout est modifiable avant impression : les champs sont pré-remplis à
partir de la fiche de l'employé (salaires, fonction) mais restent
librement éditables, et chaque bloc peut être coché ou décoché pour
choisir ce qui figure sur le papier. Le document n'est pas un bulletin de
salaire détaillé : il ne calcule ni retenues ni cotisations, il atteste
du versement d'un montant.

Politique d'accès :
    * admin/RH : tous les employés.
    * manager  : les employés de son département.
    * employé  : sa propre fiche, en lecture et impression.
"""
import io
from datetime import date, datetime

from flask import (
    Blueprint, abort, make_response, render_template, request, session,
)
from reportlab.lib import colors
from reportlab.lib.enums import TA_CENTER
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import cm
from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

from services.montant_lettres import montant_en_lettres

# Blocs facultatifs du reçu. L'ordre est celui d'impression ; l'identifiant
# sert de nom de paramètre dans la querystring, il ne doit pas changer.
SECTIONS = (
    ("entete", "En-tête de l'entreprise"),
    ("titre", "Titre du document"),
    ("identite", "Noms & prénoms / fonction"),
    ("montant_lettres", "Montant en toutes lettres"),
    ("montant_chiffres", "Montant en chiffres"),
    ("periode", "Période"),
    ("paiement", "Mode de paiement"),
    ("date_lieu", "Lieu et date"),
    ("signatures", "Cadres de signature"),
)
SECTIONS_PAR_DEFAUT = tuple(cle for cle, _ in SECTIONS)

MODES_PAIEMENT = ("Espèces", "Virement bancaire", "Chèque", "Mobile Money")

MOIS = (
    "janvier", "février", "mars", "avril", "mai", "juin",
    "juillet", "août", "septembre", "octobre", "novembre", "décembre",
)

DEVISE = "Ariary"


def _periode_par_defaut():
    aujourdhui = date.today()
    return "Mois de %s %d" % (MOIS[aujourdhui.month - 1], aujourdhui.year)


def _lire_montant(valeur, defaut=0):
    """Accepte « 400 000 », « 400000.50 » ou « 400 000,50 »."""
    if valeur is None or str(valeur).strip() == "":
        return float(defaut or 0)
    texte = str(valeur).strip().replace(" ", "").replace(",", ".")
    try:
        return float(texte)
    except ValueError:
        return float(defaut or 0)


def _formater_montant(montant):
    """400000 -> « 400 000 ».

    Le séparateur est une espace insécable U+00A0, et non l'espace fine
    U+202F que recommande la typographie française : cette dernière est
    absente du jeu de caractères WinAnsi des polices standard, ReportLab la
    rendrait donc par un carré noir à l'impression.
    """
    try:
        valeur = float(montant)
    except (TypeError, ValueError):
        return "0"
    entier = int(round(abs(valeur)))
    texte = "{:,}".format(entier).replace(",", "\u00a0")
    return ("-" + texte) if valeur < 0 else texte


def _lire_date(valeur):
    """Retourne un objet date, ou None si le champ est vide ou invalide."""
    if not valeur:
        return None
    try:
        return datetime.strptime(str(valeur)[:10], "%Y-%m-%d").date()
    except ValueError:
        return None


def _bool(valeur, defaut=True):
    """Interprète une case à cocher transmise en querystring."""
    if valeur is None:
        return defaut
    return str(valeur).lower() in ("1", "on", "oui", "true", "yes")


def creer_blueprint_fiches_paie(deps):
    bp = Blueprint("fiches_paie", __name__)
    db_cursor = deps["db_cursor"]
    login_required = deps["login_required"]
    role_required = deps["role_required"]
    get_current_employee = deps["get_current_employee"]
    employee_in_scope = deps["employee_in_scope"]
    log_action = deps.get("log_action")
    nom_entreprise = deps.get("nom_entreprise") or "AVOTRA MG SA"
    lieu_par_defaut = deps.get("lieu_par_defaut") or "Antananarivo"

    # --------------------------------------------------------------- helpers

    def _charger_employe(employe_id):
        with db_cursor() as (conn, cur):
            cur.execute("SELECT * FROM employes WHERE id = %s", (employe_id,))
            employe = cur.fetchone()
            if employe is None:
                abort(404)
            if not employee_in_scope(cur, employe_id):
                abort(403)
            # Le cloisonnement par département ne suffit pas ici : un salaire
            # ne regarde pas un collègue. L'employé est donc limité à sa
            # propre fiche, même au sein de son département.
            if session.get("role") == "employe":
                moi = get_current_employee()
                if not moi or moi.get("id") != employe_id:
                    abort(403)
        return employe

    def _parametres(employe):
        """Normalise la querystring en valeurs de reçu, avec repli.

        Tout vient de la querystring : le reçu reste donc partageable par
        simple copie d'URL, et le bouton « retour » du navigateur ne perd
        pas la saisie.
        """
        args = request.args
        montant = _lire_montant(args.get("montant"), employe.get("salaire"))
        jour = _lire_date(args.get("date_paiement")) or date.today()
        return {
            "employe": employe,
            "entreprise": (args.get("entreprise") or nom_entreprise).strip(),
            "titre": (args.get("titre") or "FICHE DE PAIE").strip(),
            "periode": (args.get("periode") or _periode_par_defaut()).strip(),
            "montant": montant,
            "montant_lettres": montant_en_lettres(montant),
            "montant_formate": _formater_montant(montant),
            "mode_paiement": (args.get("mode_paiement") or MODES_PAIEMENT[0]).strip(),
            "lieu": (args.get("lieu") or lieu_par_defaut).strip(),
            "date_paiement": jour,
            "date_paiement_str": jour.strftime("%d/%m/%Y"),
            "signature_1": (args.get("signature_1") or "L'employé").strip(),
            "signature_2": (args.get("signature_2") or "DAF").strip(),
            "blocs": {cle: _bool(args.get("sec_" + cle)) for cle, _ in SECTIONS},
            "devise": DEVISE,
        }

    def _consigner(employe):
        if log_action:
            try:
                log_action(
                    session.get("user_id"), session.get("username"),
                    "fiche_paie", "employe", employe.get("id"),
                    "Génération d'une fiche de paie pour %s %s"
                    % (employe.get("prenom", ""), employe.get("nom", "")),
                )
            except Exception:
                # Le reçu doit s'imprimer même si la piste d'audit est
                # indisponible ; log_action journalise déjà son propre échec.
                pass

    # ----------------------------------------------------------------- routes

    @bp.route("/employes/<int:employe_id>/fiche-de-paie")
    @login_required
    @role_required("admin", "rh", "manager", "employe")
    def fiche_de_paie(employe_id):
        employe = _charger_employe(employe_id)
        valeurs = {
            "entreprise": nom_entreprise,
            "titre": "FICHE DE PAIE",
            "periode": _periode_par_defaut(),
            "montant": employe.get("salaire") or 0,
            "mode_paiement": MODES_PAIEMENT[0],
            "lieu": lieu_par_defaut,
            "date_paiement": date.today(),
            "signature_1": "L'employé",
            "signature_2": "DAF",
            "sections": {cle: True for cle, _ in SECTIONS},
        }
        return render_template(
            "fiche_paie_form.html",
            employe=employe,
            valeurs=valeurs,
            sections=SECTIONS,
            modes_paiement=MODES_PAIEMENT,
        )

    @bp.route("/employes/<int:employe_id>/fiche-de-paie/apercu")
    @login_required
    @role_required("admin", "rh", "manager", "employe")
    def apercu(employe_id):
        employe = _charger_employe(employe_id)
        contexte = _parametres(employe)
        _consigner(employe)
        return render_template(
            "fiche_paie_apercu.html",
            sections=SECTIONS,
            modes_paiement=MODES_PAIEMENT,
            **contexte,
        )

    @bp.route("/employes/<int:employe_id>/fiche-de-paie/pdf")
    @login_required
    @role_required("admin", "rh", "manager", "employe")
    def pdf(employe_id):
        employe = _charger_employe(employe_id)
        ctx = _parametres(employe)
        _consigner(employe)
        return _pdf(ctx, employe)

    return bp


# ==================== GÉNÉRATION PDF ====================
def _pdf(ctx, employe):
    """Construit le reçu en PDF (ReportLab), dans l'ordre des sections."""
    buffer = io.BytesIO()
    doc = SimpleDocTemplate(
        buffer, pagesize=A4,
        rightMargin=2 * cm, leftMargin=2 * cm,
        topMargin=1.6 * cm, bottomMargin=1.6 * cm,
        title="%s — %s %s" % (ctx["titre"], employe.get("prenom", ""), employe.get("nom", "")),
    )
    styles = getSampleStyleSheet()
    centre = ParagraphStyle("centre", parent=styles["Normal"], alignment=TA_CENTER)
    normal = ParagraphStyle("normal", parent=styles["Normal"], fontSize=10.5, leading=15)
    gris = ParagraphStyle("gris", parent=styles["Normal"], fontSize=8.5,
                          textColor=colors.HexColor("#6b7280"))

    elements = []
    sec = ctx["blocs"]

    if sec.get("entete"):
        elements.append(Paragraph(
            "<b><font size=14>%s</font></b>" % _echapper(ctx["entreprise"]), centre))
        elements.append(Spacer(1, 4))

    if sec.get("titre") or sec.get("periode"):
        if sec.get("titre"):
            elements.append(Paragraph(
                "<b><font size=13>%s</font></b>" % _echapper(ctx["titre"].upper()), centre))
        if sec.get("periode"):
            elements.append(Paragraph(
                "<font size=11>%s</font>" % _echapper(ctx["periode"]), centre))
        elements.append(Spacer(1, 10))

    if sec.get("identite"):
        nom_complet = "%s %s" % (employe.get("prenom", ""), employe.get("nom", ""))
        elements.append(Paragraph(
            "<b>Noms &amp; Prénoms :</b> %s" % _echapper(nom_complet.strip()), normal))
        elements.append(Paragraph(
            "<b>Fonction :</b> %s" % _echapper(employe.get("poste") or "—"), normal))
        elements.append(Spacer(1, 6))

    if sec.get("montant_lettres"):
        elements.append(Paragraph(
            "Je soussigné(e) reconnais avoir reçu la somme de :", normal))
        elements.append(Paragraph(
            "<b><i>%s</i></b>" % _echapper(ctx["montant_lettres"]), normal))
        elements.append(Spacer(1, 6))

    if sec.get("montant_chiffres"):
        elements.append(Paragraph(
            "<b>Montant :</b> %s %s" % (ctx["montant_formate"], DEVISE), normal))

    if sec.get("paiement"):
        elements.append(Paragraph(
            "<b>Mode de paiement :</b> %s" % _echapper(ctx["mode_paiement"]), normal))
        elements.append(Spacer(1, 6))

    if sec.get("date_lieu"):
        elements.append(Paragraph(
            "<b>%s, le %s</b>" % (_echapper(ctx["lieu"]), ctx["date_paiement_str"]), normal))
        elements.append(Spacer(1, 20))

    if sec.get("signatures"):
        t = Table(
            [[Paragraph("<b>%s</b>" % _echapper(ctx["signature_1"]), centre),
              Paragraph("<b>%s</b>" % _echapper(ctx["signature_2"]), centre)],
             ["", ""],
             [Paragraph("Signature", gris), Paragraph("Signature", gris)]],
            colWidths=[7.5 * cm, 7.5 * cm], rowHeights=[0.6 * cm, 2.2 * cm, 0.5 * cm],
        )
        t.setStyle(TableStyle([
            ("ALIGN", (0, 0), (-1, -1), "CENTER"),
            ("VALIGN", (0, 0), (-1, -1), "TOP"),
            ("LINEBELOW", (0, 1), (0, 1), 0.6, colors.HexColor("#9ca3af")),
            ("LINEBELOW", (1, 1), (1, 1), 0.6, colors.HexColor("#9ca3af")),
            ("TOPPADDING", (0, 0), (-1, -1), 2),
        ]))
        elements.append(t)

    doc.build(elements)
    buffer.seek(0)

    nom_fichier = "fiche-paie-%s-%s-%s.pdf" % (
        (employe.get("nom") or "employe").lower().replace(" ", "-"),
        (employe.get("prenom") or "").lower().replace(" ", "-"),
        ctx["date_paiement"].strftime("%Y%m"),
    )
    reponse = make_response(buffer.getvalue())
    reponse.headers["Content-Type"] = "application/pdf"
    reponse.headers["Content-Disposition"] = "inline; filename=%s" % nom_fichier
    return reponse


def _echapper(texte):
    """ReportLab analyse le balisage : le texte saisi doit être neutralisé."""
    return (str(texte).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"))
