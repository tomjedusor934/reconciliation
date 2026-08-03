#!/usr/bin/env python3
"""Génère docs/statut-reconciliation-par-canal.pdf (deck 16:9, public non technique).

Usage: backend/.venv/bin/python scripts/generate_reco_channels_deck.py
Polices PDF de base (Helvetica) : texte limité à Latin-1 (pas de fleches
unicode ni d'apostrophes typographiques) - les fleches sont dessinees.
"""
import os

from reportlab.lib.colors import HexColor, white
from reportlab.pdfgen import canvas

# --- Charte (tailwind.config.js du front) ---------------------------------
INDIGO = HexColor("#2B2D42")
INDIGO_600 = HexColor("#242637")
INDIGO_300 = HexColor("#B8BACD")
INDIGO_100 = HexColor("#E8E9EF")
INDIGO_50 = HexColor("#F5F5F7")
MINT = HexColor("#00E49A")
MINT_700 = HexColor("#008254")
OCEAN = HexColor("#00C8B1")
OCEAN_700 = HexColor("#009E8D")
TURQ = HexColor("#00A7CB")
TURQ_700 = HexColor("#006985")
LEMON = HexColor("#F8F32B")
LEMON_700 = HexColor("#BAB100")
BLUSH = HexColor("#F7EBEC")
RED = HexColor("#D64550")
GREY = HexColor("#6B6E85")

W, H = 960, 540
MARGIN = 46
DECK_TITLE = "Reconciliation par canal - fonctionnement & statut"

F = "Helvetica"
FB = "Helvetica-Bold"
FO = "Helvetica-Oblique"


def wrap(c, text, font, size, max_w):
    words, lines, cur = text.split(), [], ""
    for w in words:
        trial = (cur + " " + w).strip()
        if c.stringWidth(trial, font, size) <= max_w:
            cur = trial
        else:
            if cur:
                lines.append(cur)
            cur = w
    if cur:
        lines.append(cur)
    return lines


def para(c, x, y, text, max_w, font=F, size=11, leading=15.5, color=INDIGO):
    """Ecrit un paragraphe, retourne le y sous la derniere ligne."""
    c.setFillColor(color)
    c.setFont(font, size)
    for line in wrap(c, text, font, size, max_w):
        c.drawString(x, y, line)
        y -= leading
    return y


def bullet(c, x, y, text, max_w, size=11, leading=15.5, color=INDIGO, dot=None):
    c.setFillColor(dot or OCEAN)
    c.circle(x + 2.6, y + size * 0.32, 2.2, stroke=0, fill=1)
    return para(c, x + 14, y, text, max_w - 14, size=size, leading=leading, color=color)


def chip(c, x, y, text, bg, fg=white, size=9.5, pad=8, h=18, font=FB):
    w = c.stringWidth(text, font, size) + 2 * pad
    c.setFillColor(bg)
    c.roundRect(x, y, w, h, h / 2, stroke=0, fill=1)
    c.setFillColor(fg)
    c.setFont(font, size)
    c.drawCentredString(x + w / 2, y + (h - size) / 2 + 1.2, text)
    return x + w


def arrow(c, x1, y1, x2, y2, color=INDIGO_300, width=1.8, head=6):
    c.setStrokeColor(color)
    c.setLineWidth(width)
    c.line(x1, y1, x2, y2)
    import math
    ang = math.atan2(y2 - y1, x2 - x1)
    for da in (2.65, -2.65):
        c.line(x2, y2, x2 + head * math.cos(ang + da), y2 + head * math.sin(ang + da))


def box(c, x, y, w, h, fill=white, stroke=INDIGO_100, r=9, line=1.4):
    c.setFillColor(fill)
    c.setStrokeColor(stroke)
    c.setLineWidth(line)
    c.roundRect(x, y, w, h, r, stroke=1, fill=1)


def header(c, kicker, title, accent, status=None, status_bg=None):
    c.setFillColor(accent)
    c.rect(0, H - 8, W, 8, stroke=0, fill=1)
    c.setFillColor(GREY)
    c.setFont(FB, 10)
    c.drawString(MARGIN, H - 42, kicker.upper())
    c.setFillColor(INDIGO)
    c.setFont(FB, 25)
    c.drawString(MARGIN, H - 72, title)
    if status:
        w = c.stringWidth(status, FB, 10) + 20
        chip(c, W - MARGIN - w, H - 74, status, status_bg or MINT_700, size=10, h=21)
    c.setStrokeColor(INDIGO_100)
    c.setLineWidth(1)
    c.line(MARGIN, H - 88, W - MARGIN, H - 88)


def footer(c, page):
    c.setFillColor(INDIGO_300)
    c.setFont(F, 8.5)
    c.drawString(MARGIN, 20, DECK_TITLE)
    c.drawRightString(W - MARGIN, 20, "Juillet 2026   ·   %d / 9" % page)


def source_convergence(c, x, y, w, src_a, src_b, key_label, color, result="Somme = 0  ->  rapproche"):
    """Schema : deux sources -> cle commune -> moteur."""
    bw, bh, kh = w * 0.44, 66, 34
    ya, yb = y + 96, y
    for by, (title, lines) in ((ya, src_a), (yb, src_b)):
        box(c, x, by, bw, bh, fill=INDIGO_50, stroke=INDIGO_100)
        c.setFillColor(INDIGO)
        c.setFont(FB, 10.5)
        c.drawString(x + 12, by + bh - 22, title)
        yy = by + bh - 38
        for ln in lines:
            yy = para(c, x + 12, yy, ln, bw - 24, size=9, leading=12, color=GREY)
    # Cle commune : pilule dimensionnee sur son texte, centree dans la zone droite
    kw = c.stringWidth(key_label, FB, 10) + 30
    cx = x + bw + (w - bw) / 2 + 4
    kx = min(cx - kw / 2, x + w - kw)
    kyc = (ya + yb + bh) / 2
    box(c, kx, kyc - kh / 2, kw, kh, fill=color, stroke=color, r=kh / 2)
    c.setFillColor(white)
    c.setFont(FB, 10)
    c.drawCentredString(kx + kw / 2, kyc - 3.5, key_label)
    arrow(c, x + bw + 3, ya + bh / 2, kx - 5, kyc + 8, color=color)
    arrow(c, x + bw + 3, yb + bh / 2, kx - 5, kyc - 8, color=color)
    # Resultat, centre dans la zone droite (avec retour a la ligne si besoin)
    c.setFont(FB, 9.5)
    c.setFillColor(INDIGO)
    ry = kyc - kh / 2 - 20
    zone_cx = x + bw + (w - bw) / 2 + 4
    for line in wrap(c, result, FB, 9.5, w - bw - 30):
        c.drawCentredString(zone_cx, ry, line)
        ry -= 13


# ===========================================================================
OUT = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                   "docs", "statut-reconciliation-par-canal.pdf")
c = canvas.Canvas(OUT, pagesize=(W, H))
c.setTitle("Reconciliation par canal - fonctionnement & statut")

# --- Slide 1 : titre --------------------------------------------------------
c.setFillColor(INDIGO)
c.rect(0, 0, W, H, stroke=0, fill=1)
c.setFillColor(MINT)
c.rect(0, H - 10, W, 10, stroke=0, fill=1)
c.setFillColor(HexColor("#3A3D57"))
for i, xx in enumerate(range(560, 960, 90)):
    c.circle(xx, 90 + (i % 2) * 46, 58, stroke=1, fill=0)
c.setFillColor(MINT)
c.setFont(FB, 12)
c.drawString(MARGIN, 372, "RECONCILIATION BANCAIRE")
c.setFillColor(white)
c.setFont(FB, 40)
c.drawString(MARGIN, 322, "Reconciliation par canal")
c.setFont(F, 19)
c.setFillColor(INDIGO_300)
c.drawString(MARGIN, 288, "Fonctionnement, cles de rapprochement et statut de chaque canal")
x = MARGIN
for label, col in (("ATM / MOSEL", OCEAN), ("Guichet Webripost", TURQ),
                   ("IP - releves MT940", MINT_700), ("Comptes Float", OCEAN_700),
                   ("Float OUTWARD - lots (nouveau)", TURQ_700)):
    x = chip(c, x, 226, label, col, size=10.5, h=24, pad=11) + 10
c.setFillColor(INDIGO_300)
c.setFont(F, 11)
c.drawString(MARGIN, 66, "Juillet 2026  ·  document de presentation - lecture non technique")
c.showPage()

# --- Slide 2 : principe commun ---------------------------------------------
header(c, "Vue d'ensemble", "Un meme principe pour tous les canaux", MINT)
y0 = H - 122
para(c, MARGIN, y0,
     "Reconcilier, c'est verifier que deux visions d'une meme operation racontent la meme histoire : "
     "ce que la comptabilite (Finacle) a enregistre d'un cote, et ce que la source du canal "
     "(fichiers d'automates, releves bancaires, exports guichet...) annonce de l'autre. "
     "Tous les canaux suivent la meme chaine en cinq etapes :",
     W - 2 * MARGIN, size=11.5, leading=16)

steps = [
    ("1. Collecte", "Fichiers deposes chaque jour ou lecture directe dans l'entrepot de donnees."),
    ("2. Normalisation", "Chaque ligne devient une ecriture standard : montant signe (debit -, credit +), date, compte."),
    ("3. Cle de rapprochement", "Chaque ecriture recoit une cle : la reference qui permet de retrouver ses contreparties."),
    ("4. Moteur de matching", "Les ecritures qui partagent la meme cle et dont la somme fait exactement zero sont rapprochees ensemble."),
    ("5. Emargement", "Les ecritures rapprochees sont archivees ; le reste demeure en attente et est retente chaque jour."),
]
bw, gap = 158, 17
x = MARGIN
yb = 236
for i, (t, d) in enumerate(steps):
    col = (MINT_700, OCEAN_700, TURQ_700, INDIGO, MINT_700)[i]
    box(c, x, yb, bw, 118, fill=white, stroke=INDIGO_100)
    c.setFillColor(col)
    c.rect(x, yb + 112, bw, 6, stroke=0, fill=1)
    c.setFont(FB, 11)
    c.drawString(x + 11, yb + 94, t)
    para(c, x + 11, yb + 76, d, bw - 22, size=9, leading=12, color=GREY)
    if i < 4:
        arrow(c, x + bw + 2, yb + 59, x + bw + gap - 3, yb + 59, color=INDIGO_300)
    x += bw + gap

box(c, MARGIN, 96, W - 2 * MARGIN, 104, fill=BLUSH, stroke=HexColor("#E8D4D7"))
c.setFillColor(INDIGO)
c.setFont(FB, 11.5)
c.drawString(MARGIN + 16, 172, "Trois regles d'or, valables partout")
yy = 152
yy = bullet(c, MARGIN + 16, yy, "Aucun doublon possible : chaque ecriture porte une empreinte unique ; retraiter un fichier ou relancer une extraction ne cree jamais de double.", W - 2 * MARGIN - 40, size=10, leading=13.5)
yy = bullet(c, MARGIN + 16, yy, "Rien ne se perd : une ecriture sans contrepartie reste \"en attente\" et repasse automatiquement dans le moteur a chaque cycle.", W - 2 * MARGIN - 40, size=10, leading=13.5)
yy = bullet(c, MARGIN + 16, yy, "Zero tolerance d'ecart : un groupe n'est rapproche que si sa somme fait exactement zero, centime pour centime, devise par devise.", W - 2 * MARGIN - 40, size=10, leading=13.5)
footer(c, 2)
c.showPage()

# --- Slide 3 : statuts ------------------------------------------------------
header(c, "Vue d'ensemble", "Le cycle de vie d'une ecriture et ses statuts", OCEAN)
y0 = H - 122
para(c, MARGIN, y0,
     "Chaque ecriture est toujours dans un - et un seul - des quatre statuts ci-dessous. "
     "C'est la lecture quotidienne de l'equipe : ce qui est vert est solde, ce qui est orange demande une contrepartie ou une action.",
     W - 2 * MARGIN, size=11.5, leading=16)

# schema de cycle
bw, bh = 168, 52
yline = 300
xs = [MARGIN, MARGIN + 226, MARGIN + 452, MARGIN + 678]
labels = [("Nouvelle ecriture", "issue de la collecte", INDIGO_50, INDIGO),
          ("EN ATTENTE", "cherche sa contrepartie", LEMON, INDIGO),
          ("RAPPROCHEE", "groupe soldant a zero", MINT, INDIGO),
          ("Emargement", "archivee, consultable", INDIGO_50, INDIGO)]
for (x, (t, d, bg, fg)) in zip(xs, labels):
    box(c, x, yline, bw, bh, fill=bg, stroke=INDIGO_100)
    c.setFillColor(fg)
    c.setFont(FB, 11.5)
    c.drawCentredString(x + bw / 2, yline + 30, t)
    c.setFont(F, 8.5)
    c.setFillColor(GREY)
    c.drawCentredString(x + bw / 2, yline + 14, d)
for i in range(3):
    arrow(c, xs[i] + bw + 3, yline + bh / 2, xs[i + 1] - 4, yline + bh / 2, color=INDIGO_300)
c.setFillColor(GREY)
c.setFont(FO, 8.5)
c.drawCentredString((xs[1] + bw + xs[2]) / 2, yline + bh + 8, "moteur quotidien")

# branches manuelles (actions operateur depuis "en attente")
bx = xs[1] + bw / 2
ybr = yline - 62
for bxx, t, col in ((300, "FORCEE (action manuelle justifiee)", TURQ),
                    (552, "EXCLUE (motif obligatoire)", GREY)):
    box(c, bxx, ybr, 226, 32, fill=white, stroke=col)
    c.setFillColor(col)
    c.setFont(FB, 9.5)
    c.drawCentredString(bxx + 113, ybr + 11.5, t)
    arrow(c, bx, yline - 2, bxx + 90, ybr + 34, color=col)
c.setFillColor(GREY)
c.setFont(FO, 8.5)
c.drawCentredString((300 + 552 + 226) / 2, ybr - 14,
                    "Une fois forcees ou exclues, ces ecritures rejoignent aussi l'emargement.")

defs = [
    ("En attente", LEMON, INDIGO, "L'ecriture n'a pas encore trouve son groupe soldant. Elle est representee au moteur a chaque cycle, sans limite de duree."),
    ("Rapprochee", MINT, INDIGO, "Le moteur a trouve un groupe de meme cle dont la somme fait zero. Automatique, tracable, horodate."),
    ("Forcee", TURQ, white, "Un operateur rapproche manuellement des ecritures (meme flux, meme devise, somme nulle) avec commentaire. Utilise pour les cas que la cle ne couvre pas."),
    ("Exclue", GREY, white, "Ecartee du rapprochement avec un motif obligatoire (doublon source, operation hors perimetre...). Reversible et auditee."),
]
x = MARGIN
cw = (W - 2 * MARGIN - 3 * 14) / 4
for t, col, fg, d in defs:
    box(c, x, 84, cw, 118, fill=white, stroke=INDIGO_100)
    chip(c, x + 10, 84 + 118 - 28, t, col, fg=fg, size=9.5)
    para(c, x + 10, 84 + 118 - 44, d, cw - 20, size=8.8, leading=11.8, color=GREY)
    x += cw + 14
footer(c, 3)
c.showPage()

# --- Slide 4 : ATM ----------------------------------------------------------
header(c, "Canal 1", "ATM - automates (fichiers MOSEL)", OCEAN, "ACTIF", MINT_700)
y0 = H - 118
colw = 470
yy = para(c, MARGIN, y0,
          "Retraits, depots et recharges de cartes effectues aux automates. On verifie que chaque operation "
          "physique a bien sa trace comptable.", colw, size=11.5, leading=16)
c.setFillColor(INDIGO)
c.setFont(FB, 12)
c.drawString(MARGIN, yy - 8, "Comment ca marche")
yy -= 28
yy = bullet(c, MARGIN, yy, "Les automates produisent chaque jour des fichiers MOSEL (format a positions fixes) : un enregistrement par operation, avec son type (retrait, depot, recharge, annulation).", colw)
yy = bullet(c, MARGIN, yy, "Selon le type d'evenement, l'ecriture est dirigee vers le bon compte comptable (un compte pour les retraits, un pour les depots).", colw)
yy = bullet(c, MARGIN, yy, "La cle de rapprochement est la reference de la transaction de l'automate ; en face, le mouvement Finacle du compte miroir porte la meme reference.", colw)
yy = bullet(c, MARGIN, yy, "Une operation annulee arrive avec le montant inverse : la paire s'annule d'elle-meme dans le moteur (somme nulle).", colw)
c.setFillColor(GREY)
yy = para(c, MARGIN, yy - 4, "A noter : les recharges de cartes sont collectees mais en attente d'un compte comptable dedie ; la bascule complete vers le rapprochement \"miroir Finacle\" est preparee et sera activee apres validation.", colw, font=FO, size=9.5, leading=13)
source_convergence(
    c, MARGIN + colw + 40, 218, W - 2 * MARGIN - colw - 40,
    ("Fichiers MOSEL", ["1 ligne = 1 operation", "retrait / depot / recharge"]),
    ("Mouvements Finacle", ["comptes miroirs ATM", "canal automate"]),
    "Ref. transaction ATM", OCEAN)
footer(c, 4)
c.showPage()

# --- Slide 5 : Webripost ----------------------------------------------------
header(c, "Canal 2", "Guichet - Webripost (Riposte / Thaler)", TURQ, "ACTIF", MINT_700)
y0 = H - 118
yy = para(c, MARGIN, y0,
          "Operations realisees au guichet : encaissement de cheques, depots et retraits d'especes. "
          "La source est l'export quotidien du systeme d'agence.", colw, size=11.5, leading=16)
c.setFillColor(INDIGO)
c.setFont(FB, 12)
c.drawString(MARGIN, yy - 8, "Comment ca marche")
yy -= 28
yy = bullet(c, MARGIN, yy, "Les exports guichet (CSV / Excel) arrivent en centimes et sans signe : le sens (debit ou credit) est deduit du type d'operation, puis le montant est signe comme partout ailleurs.", colw, dot=TURQ)
yy = bullet(c, MARGIN, yy, "La cle de rapprochement est la reference de l'operation guichet, presente a l'identique dans le mouvement Finacle correspondant.", colw, dot=TURQ)
yy = bullet(c, MARGIN, yy, "Les operations marquees \"douteuses\" (InDoubt) par le systeme d'agence sont ecartees du rapprochement tant qu'elles ne sont pas confirmees.", colw, dot=TURQ)
yy = bullet(c, MARGIN, yy, "Les extournes sont conservees a montant nul : elles restent visibles pour la tracabilite sans fausser les soldes.", colw, dot=TURQ)
c.setFillColor(GREY)
yy = para(c, MARGIN, yy - 4, "A noter : la configuration cible \"Cash in shop\" (couple export guichet + Finacle) est prete et sera activee apres validation metier.", colw, font=FO, size=9.5, leading=13)
source_convergence(
    c, MARGIN + colw + 40, 218, W - 2 * MARGIN - colw - 40,
    ("Exports guichet", ["cheques, depots, retraits", "CSV / Excel quotidiens"]),
    ("Mouvements Finacle", ["comptes guichet", "canal Webripost"]),
    "Reference d'operation", TURQ)
footer(c, 5)
c.showPage()

# --- Slide 6 : MT940 --------------------------------------------------------
header(c, "Canal 3", "Paiements instantanes - releves bancaires MT940", MINT_700, "ACTIF", MINT_700)
y0 = H - 118
yy = para(c, MARGIN, y0,
          "Les paiements instantanes transitent par un compte tenu chez la banque partenaire (BCEE). "
          "On rapproche ses releves bancaires officiels (format SWIFT MT940) des comptes miroirs Finacle.", colw, size=11.5, leading=16)
c.setFillColor(INDIGO)
c.setFont(FB, 12)
c.drawString(MARGIN, yy - 8, "Comment ca marche")
yy -= 28
yy = bullet(c, MARGIN, yy, "Chaque ligne du releve est lue et transformee en ecriture ; la reference client du releve est extraite ligne a ligne.", colw, dot=MINT_700)
yy = bullet(c, MARGIN, yy, "Particularite du canal : cette reference n'est pas directement la cle. Elle est traduite via l'entrepot de donnees des paiements en numero d'ordre du paiement - c'est lui qui sert de cle commune avec Finacle.", colw, dot=MINT_700)
yy = bullet(c, MARGIN, yy, "Si le paiement n'est pas encore present dans l'entrepot (delai de chargement), la traduction est simplement retentee les jours suivants : rien n'est reimporte, la cle est completee sur place.", colw, dot=MINT_700)
c.setFillColor(GREY)
yy = para(c, MARGIN, yy - 4, "A noter : le meme mecanisme est pret pour les comptes NOSTRO (correspondants bancaires) ; cette extension est configuree et sera activee apres validation.", colw, font=FO, size=9.5, leading=13)
source_convergence(
    c, MARGIN + colw + 40, 218, W - 2 * MARGIN - colw - 40,
    ("Releves BCEE (MT940)", ["document bancaire officiel", "1 ligne = 1 paiement"]),
    ("Mouvements Finacle", ["comptes miroirs IP", "numero d'ordre connu"]),
    "N. d'ordre du paiement", MINT_700,
    result="Cle traduite, puis somme = 0")
footer(c, 6)
c.showPage()

# --- Slide 7 : Float classique ----------------------------------------------
header(c, "Canal 4", "Comptes Float - rapprochement interne Finacle", OCEAN_700, "ACTIF", MINT_700)
y0 = H - 118
yy = para(c, MARGIN, y0,
          "Les comptes float sont des comptes de passage : l'argent y entre puis en ressort une fois le paiement "
          "regle. Ici, pas de source externe : on rapproche les mouvements Finacle entre eux (l'aller contre le retour). "
          "Un compte float sain revient toujours a zero, operation par operation.", W - 2 * MARGIN, size=11.5, leading=16)
c.setFillColor(INDIGO)
c.setFont(FB, 12)
c.drawString(MARGIN, yy - 10, "La cle depend du type de mouvement, lu dans son libelle")
ytab = yy - 30
rows = [
    ("Paiements unitaires", "SWIFT, BKRTP, SCRT1", "Numero d'ordre du paiement, porte par le mouvement lui-meme.", MINT_700),
    ("Remises groupees (aller)", "SCTXB, SDDXB, SDXBB", "Reference du lot d'emission (BLK...), commune a l'aller et a son reglement.", OCEAN_700),
    ("Retours de remises", "NCC / NCP", "Numero d'ordre extrait du libelle, puis retrouve dans l'entrepot des paiements pour remonter au message d'origine.", TURQ_700),
    ("Extournes", "annulations", "Reference de transaction recherchee dans l'entrepot ; ne complete la cle que si le circuit normal n'a rien donne.", GREY),
]
col1, col2, col3 = 190, 150, W - 2 * MARGIN - 190 - 150
rh = 46
for i, (t, code, d, col) in enumerate(rows):
    ry = ytab - i * (rh + 8) - rh
    box(c, MARGIN, ry, W - 2 * MARGIN, rh, fill=(INDIGO_50 if i % 2 else white), stroke=INDIGO_100)
    c.setFillColor(col)
    c.rect(MARGIN, ry, 5, rh, stroke=0, fill=1)
    c.setFillColor(INDIGO)
    c.setFont(FB, 10.5)
    c.drawString(MARGIN + 16, ry + rh - 20, t)
    c.setFillColor(col)
    c.setFont(FB, 9)
    c.drawString(MARGIN + 16, ry + 9, code)
    para(c, MARGIN + col1 + 20, ry + rh - 19, d, col3 + col2 - 30, size=9.8, leading=13, color=GREY)
c.setFillColor(GREY)
para(c, MARGIN, ytab - 4 * (rh + 8) - 16,
     "Canaux concernes : Float INWARD, Float IP entrant et sortant. Le Float OUTWARD utilisait ce schema jusqu'a l'arrivee du "
     "\"batch booking\" - voir page suivante.", W - 2 * MARGIN, font=FO, size=9.5, leading=13)
footer(c, 7)
c.showPage()

# --- Slide 8 : BB true ------------------------------------------------------
header(c, "Canal 5", "Float OUTWARD - rapprochement par lots", TURQ_700, "ACTIF - NOUVEAU", TURQ_700)
y0 = H - 116
colw2 = 430
yy = para(c, MARGIN, y0,
          "Le reglement des virements sortants est passe en \"batch booking\" : une remise de 100 paiements n'est "
          "plus reglee par un seul mouvement egal, mais par plusieurs agregats (NDGB) plus petits qui melangent "
          "des paiements de plusieurs remises. Une cle unique ne suffit plus.", colw2, size=11, leading=15)
c.setFillColor(INDIGO)
c.setFont(FB, 12)
c.drawString(MARGIN, yy - 8, "La reponse : les lots")
yy -= 28
yy = bullet(c, MARGIN, yy, "Chaque mouvement expose tous ses identifiants connus (message d'emission, message d'agregat, numeros d'ordre) grace a l'entrepot des paiements.", colw2, size=10.3, leading=14, dot=TURQ_700)
yy = bullet(c, MARGIN, yy, "Deux mouvements qui partagent un identifiant appartiennent au meme lot ; de proche en proche, remises, agregats, paiements unitaires et rejets se retrouvent dans le meme ensemble.", colw2, size=10.3, leading=14, dot=TURQ_700)
yy = bullet(c, MARGIN, yy, "Le lot entier est rapproche d'un bloc des que sa somme fait zero. Les lots se completent au fil des jours : un agregat qui arrive plus tard retrouve son lot.", colw2, size=10.3, leading=14, dot=TURQ_700)
yy = bullet(c, MARGIN, yy, "Nouvel ecran \"Lots\" : liste filtrable et graphe interactif montrant chaque mouvement, chaque identifiant partage et l'equilibre du lot.", colw2, size=10.3, leading=14, dot=TURQ_700)

# schema bipartite
gx = MARGIN + colw2 + 34
gw = W - MARGIN - gx
box(c, gx, 96, gw, 306, fill=INDIGO_50, stroke=INDIGO_100)
c.setFillColor(GREY)
c.setFont(FB, 8.5)
c.drawString(gx + 14, 384, "REMISES & UNITAIRES")
c.drawRightString(gx + gw - 14, 384, "AGREGATS DE REGLEMENT")
sp = [("SCTXB  -100k", 330), ("SDDXB   -90k", 258), ("SWIFT   -10k", 186)]
nd = [("NDGB  +60k", 312), ("NDGB  +130k", 234), ("NDGB  +10k", 162)]
keys = [("MSG-1", 322), ("MSG-2", 262), ("Ordre 123", 196)]
mbw, mbh = 108, 30
for t, yy2 in sp:
    box(c, gx + 14, yy2, mbw, mbh, fill=white, stroke=RED)
    c.setFillColor(RED)
    c.setFont(FB, 9)
    c.drawCentredString(gx + 14 + mbw / 2, yy2 + 10.5, t)
for t, yy2 in nd:
    box(c, gx + gw - 14 - mbw, yy2, mbw, mbh, fill=white, stroke=MINT_700)
    c.setFillColor(MINT_700)
    c.setFont(FB, 9)
    c.drawCentredString(gx + gw - 14 - mbw / 2, yy2 + 10.5, t)
kx = gx + gw / 2 - 40
for t, yy2 in keys:
    box(c, kx, yy2, 80, 22, fill=TURQ, stroke=TURQ, r=11)
    c.setFillColor(white)
    c.setFont(FB, 8)
    c.drawCentredString(kx + 40, yy2 + 7.5, t)
links_left = [(0, 0), (1, 1), (2, 2)]           # SP i -> key j
links_right = [(0, 0), (0, 1), (1, 1), (2, 2)]  # NDGB i -> key j
c.setLineWidth(1.4)
c.setStrokeColor(INDIGO_300)
for si, kj in links_left:
    c.line(gx + 14 + mbw, sp[si][1] + mbh / 2, kx, keys[kj][1] + 11)
for ni, kj in links_right:
    c.line(gx + gw - 14 - mbw, nd[ni][1] + mbh / 2, kx + 80, keys[kj][1] + 11)
box(c, gx + gw / 2 - 105, 108, 210, 30, fill=INDIGO, stroke=INDIGO, r=15)
c.setFillColor(MINT)
c.setFont(FB, 10)
c.drawCentredString(gx + gw / 2, 118, "Total du lot = 0  ->  rapproche")
footer(c, 8)
c.showPage()

# --- Slide 9 : synthese ------------------------------------------------------
header(c, "Synthese", "Panorama des canaux", MINT)
cols = [("Canal", 150), ("Ce que l'on compare", 265), ("Cle de rapprochement", 235), ("Statut", 218)]
xs2, xacc = [], MARGIN
for _, wdt in cols:
    xs2.append(xacc)
    xacc += wdt
ytop = H - 116
c.setFillColor(INDIGO)
c.rect(MARGIN, ytop - 30, W - 2 * MARGIN, 30, stroke=0, fill=1)
c.setFillColor(white)
c.setFont(FB, 10.5)
for (t, _), x in zip(cols, xs2):
    c.drawString(x + 12, ytop - 20, t)
rows9 = [
    ("ATM / MOSEL", "Fichiers des automates vs mouvements Finacle (comptes miroirs)", "Reference de la transaction de l'automate", "ACTIF", MINT_700, "bascule miroir preparee"),
    ("Guichet Webripost", "Exports guichet (cheques, depots, retraits) vs Finacle", "Reference de l'operation guichet", "ACTIF", MINT_700, "cible Cash in shop prete"),
    ("Paiements instantanes", "Releves bancaires BCEE (MT940) vs comptes miroirs Finacle", "Numero d'ordre, traduit via l'entrepot des paiements", "ACTIF", MINT_700, "extension NOSTRO prete"),
    ("Comptes Float", "Mouvements Finacle entre eux (aller vs reglement)", "Selon le type : numero d'ordre ou reference de lot BLK", "ACTIF", MINT_700, "inward + IP in / out"),
    ("Float OUTWARD - lots", "Remises et agregats NDGB regroupes en lots equilibres", "Identifiants partages -> lot ; somme du lot = 0", "ACTIF - NOUVEAU", TURQ_700, "vue Lots dediee"),
]
rh9 = 52
for i, (canal, comp, cle, st, stc, note) in enumerate(rows9):
    ry = ytop - 30 - (i + 1) * rh9
    box(c, MARGIN, ry, W - 2 * MARGIN, rh9, fill=(white if i % 2 else INDIGO_50), stroke=INDIGO_100, r=0, line=0.8)
    c.setFillColor(INDIGO)
    c.setFont(FB, 10)
    for line in wrap(c, canal, FB, 10, cols[0][1] - 22):
        c.drawString(xs2[0] + 12, ry + rh9 - 20 - (0 if line == canal else 12), line)
    para(c, xs2[1] + 12, ry + rh9 - 18, comp, cols[1][1] - 24, size=9, leading=12, color=GREY)
    para(c, xs2[2] + 12, ry + rh9 - 18, cle, cols[2][1] - 24, size=9, leading=12, color=GREY)
    chip(c, xs2[3] + 12, ry + rh9 - 30, st, stc, size=8.5, h=17)
    c.setFillColor(GREY)
    c.setFont(FO, 8)
    c.drawString(xs2[3] + 12, ry + 8, note)
c.setFillColor(GREY)
para(c, MARGIN, ytop - 30 - 5 * rh9 - 18,
     "Lecture : tous les canaux partagent le meme moteur (somme a zero par cle et par devise), les memes statuts et le meme "
     "emargement. Seules changent la source comparee et la maniere de construire la cle - c'est ce qui fait la specificite de chaque canal.",
     W - 2 * MARGIN, font=FO, size=9.5, leading=13)
footer(c, 9)
c.showPage()

c.save()
print("OK ->", OUT)
