"""
Résolution locale prénom -> genre ('H' / 'F'), multi-origine (FR / EN / AR).

Chaîne de résolution (dans shopify_gender) :
  1. dictionnaire embarqué ci-dessous (déterministe, testable)
  2. gender-guesser (large base ~48k noms) si installé  -> prod
  3. cache Claude (BigQuery)  -> prénoms déjà résolus par LLM
  4. Claude (si ANTHROPIC_API_KEY)  -> sinon 'Indéterminé'
"""

from __future__ import annotations
import unicodedata

# clé = prénom normalisé (minuscule, sans accent) -> 'H' ou 'F'
_F = ("marie anne sophie julie camille* laura sarah emma lea chloe manon clara ines louise alice "
      "juliette pauline elise emilie claire caroline celine nathalie isabelle sandrine stephanie "
      "valerie christine catherine sylvie martine françoise monique nicole veronique aurelie "
      "amelie charlotte oceane jade lisa eva mila rose anna elena maria lucia carla giulia "
      "jessica melanie audrey elodie coralie margaux justine morgane fanny lucie agathe apolline "
      "salome noemie manel yasmine yasmina samira nadia amina fatima fatiha khadija leila layla "
      "aicha meryem myriam sana sabrina karima naima farida houda imane malak lina nour hana "
      "emily olivia sophia isabella ava charlotte* mia amelia harper evelyn abigail ella scarlett "
      "grace chloe* victoria hannah lily zoe stella nora hazel ellie paisley elizabeth")
_H = ("jean pierre paul jacques michel andre philippe alain bernard rene daniel marcel henri "
      "louis georges roger claude* francois christian gerard robert maurice raymond guy joseph "
      "lucas hugo louis* nathan gabriel arthur ethan raphael jules adam noah tom theo enzo mael "
      "leo aaron liam sacha timeo mathis nolan clement maxime antoine quentin thomas alexandre "
      "julien nicolas sebastien david romain florian kevin anthony jerome vincent olivier "
      "guillaume damien fabien gregory jonathan mathieu benjamin cedric mohamed mohammed ahmed "
      "ali omar youssef yassine mehdi karim rachid hamza bilal samir nabil said hassan hussein "
      "khaled tarek walid amine ayoub ismael ibrahim abdel rayan imran anas bilel sofiane "
      "james john robert* michael william richard david* joseph* charles thomas* daniel* matthew "
      "anthony* mark donald steven andrew joshua kevin* brian george* edward ryan jacob nathan* "
      "adam* henry* nathaniel oscar jack harry oliver jacob* noah*")

# prénoms clairement mixtes/ambigus -> forcés 'Indéterminé' (le * marque l'ambiguïté à ignorer)
AMBIGUOUS = {"camille", "dominique", "claude", "sacha", "alix", "charlie", "morgan",
             "sasha", "andrea", "noa", "swann", "lou", "ange", "maxime"}


def _load(s: str, g: str) -> dict:
    out = {}
    for tok in s.split():
        name = tok.rstrip("*")
        if tok.endswith("*"):
            continue  # variante ambiguë déjà couverte ou à ignorer
        out.setdefault(name, g)
    return out


_DICT: dict = {}
_DICT.update(_load(_F, "F"))
_DICT.update(_load(_H, "H"))


def normalize(name: str) -> str:
    """minuscule, sans accent, 1er token (avant espace/tiret), lettres uniquement."""
    if not name:
        return ""
    n = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode()
    n = n.strip().lower()
    for sep in (" ", "-", "'", "."):
        if sep in n:
            n = n.split(sep)[0]
    return "".join(c for c in n if c.isalpha())


def local_gender(name: str) -> str | None:
    """'H' / 'F' via dico embarqué ; None si inconnu ou ambigu."""
    n = normalize(name)
    if not n or n in AMBIGUOUS:
        return None
    return _DICT.get(n)
